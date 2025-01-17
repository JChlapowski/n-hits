# Cell
import math
from operator import mod
from platform import python_implementation
import random
import numpy as np

import torch as t
from torch import optim
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl

from typing import Tuple
from functools import partial

from ..components.common import RepeatVector
from ...losses.utils import LossFunction

from torch.nn.init import _calculate_correct_fan

def siren_uniform_(tensor: t.Tensor, mode: str = 'fan_in', c: float = 6):
    r"""Fills the input `Tensor` with values according to the method
    described in ` Implicit Neural Representations with Periodic Activation
    Functions.` - Sitzmann, Martel et al. (2020), using a
    uniform distribution. The resulting tensor will have values sampled from
    :math:`\mathcal{U}(-\text{bound}, \text{bound})` where
    .. math::
        \text{bound} = \sqrt{\frac{6}{\text{fan\_mode}}}
    Also known as Siren initialization.
    Examples:
        >>> w = torch.empty(3, 5)
        >>> siren.init.siren_uniform_(w, mode='fan_in', c=6)
    :param tensor: an n-dimensional `torch.Tensor`
    :type tensor: torch.Tensor
    :param mode: either ``'fan_in'`` (default) or ``'fan_out'``. Choosing
        ``'fan_in'`` preserves the magnitude of the variance of the weights in
        the forward pass. Choosing ``'fan_out'`` preserves the magnitudes in
        the backwards pass.s
    :type mode: str, optional
    :param c: value used to compute the bound. defaults to 6
    :type c: float, optional
    """
    fan = _calculate_correct_fan(tensor, mode)
    std = 1 / math.sqrt(fan)
    bound = math.sqrt(c) * std  # Calculate uniform bounds from standard deviation
    with t.no_grad():
        return tensor.uniform_(-bound, bound)

class Sine(nn.Module):
    def __init__(self, w0: float = 1.0):
        """Sine activation function with w0 scaling support.
        Example:
            >>> w = torch.tensor([3.14, 1.57])
            >>> Sine(w0=1)(w)
            torch.Tensor([0, 1])
        :param w0: w0 in the activation step `act(x; w0) = sin(w0 * x)`.
            defaults to 1.0
        :type w0: float, optional
        """
        super(Sine, self).__init__()
        self.w0 = w0

    def forward(self, x: t.Tensor) -> t.Tensor:
        self._check_input(x)
        return t.sin(self.w0 * x)

    @staticmethod
    def _check_input(x):
        if not isinstance(x, t.Tensor):
            raise TypeError(
                'input to forward() must be torch.xTensor')

# Cell
class _StaticFeaturesEncoder(nn.Module):
    def __init__(self, in_features, out_features):
        super(_StaticFeaturesEncoder, self).__init__()
        layers = [nn.Dropout(p=0.5),
                  nn.Linear(in_features=in_features, out_features=out_features),
                  nn.ReLU()]
        self.encoder = nn.Sequential(*layers)

    def forward(self, x):
        x = self.encoder(x)
        return x

class _HiddenFeaturesLinearEncoder(nn.Module):
    def __init__(self, in_features, out_features, activ, batch_normalization, dropout_prob):
        super(_HiddenFeaturesLinearEncoder, self).__init__()
        
        layers = []

        if dropout_prob > 0:
            layers.append(nn.Dropout(dropout_prob))

        layers.append(nn.Linear(in_features=in_features, out_features=out_features))
        
        if batch_normalization:
            layers.append(nn.BatchNorm1d(num_features=out_features))
        
        if activ != None:

            layers.append(activ)

        
        self.encoder = nn.Sequential(*layers)

    def forward(self, x):

        if len(x.size()) == 3:

            x = x.squeeze(1)

        x = self.encoder(x)
        
        return x

class _HiddenFeaturesDownSampleEncoder(nn.Module):
    def __init__(self, kernel_size, stride, num_features, activ):
        super(_HiddenFeaturesDownSampleEncoder, self).__init__()
        
        self.ConvLayer = nn.Conv1d(1, 1, kernel_size=kernel_size, stride=stride)
        self.BatchNorm = nn.BatchNorm1d(num_features=num_features)
        self.activ = activ

    def forward(self, x):
        if len(x.size()) == 2:
            x = x.unsqueeze(1)

        x = self.ConvLayer(x)
        
        if self.activ != None:

            x = x.squeeze(1)
            x = self.BatchNorm(x)

            x = self.activ(x)
        
        return x

class _HiddenFeaturesUpsampleEncoder(nn.Module):
    def __init__(self, kernel_size, stride, num_features, activ):
        super(_HiddenFeaturesUpsampleEncoder, self).__init__()
        
        self.ConvLayer = nn.ConvTranspose1d(1, 1, kernel_size=kernel_size, stride=stride)
        self.BatchNorm = nn.BatchNorm1d(num_features=num_features)
        self.activ = activ

    def forward(self, x):
        if len(x.size()) == 2:
            x = x.unsqueeze(1)

        x = self.ConvLayer(x)
        
        if self.activ != None:

            x = x.squeeze(1)
            x = self.BatchNorm(x)

            x = self.activ(x)
        
        return x

class _sEncoder(nn.Module):
    def __init__(self, in_features, out_features, n_time_in):
        super(_sEncoder, self).__init__()
        layers = [nn.Dropout(p=0.5),
                  nn.Linear(in_features=in_features, out_features=out_features),
                  nn.ReLU()]
        self.encoder = nn.Sequential(*layers)
        self.repeat = RepeatVector(repeats=n_time_in)

    def forward(self, x):
        # Encode and repeat values to match time
        x = self.encoder(x)
        x = self.repeat(x) # [N,S_out] -> [N,S_out,T]
        return x

# Cell
class IdentityBasis(nn.Module):
    def __init__(self, backcast_size: int, forecast_size: int, interpolation_mode: str):
        super().__init__()
        assert (interpolation_mode in ['linear','nearest']) or ('cubic' in interpolation_mode)
        self.forecast_size = forecast_size
        self.backcast_size = backcast_size
        self.interpolation_mode = interpolation_mode

    def forward(self, theta: t.Tensor, insample_x_t: t.Tensor, outsample_x_t: t.Tensor) -> Tuple[t.Tensor, t.Tensor]:

        backcast = theta[:, :self.backcast_size]
        knots = theta[:, self.backcast_size:]

        if self.interpolation_mode=='nearest':
            knots = knots[:,None,:]
            forecast = F.interpolate(knots, size=self.forecast_size, mode=self.interpolation_mode)
            forecast = forecast[:,0,:]
        elif self.interpolation_mode=='linear':
            knots = knots[:,None,:]
            forecast = F.interpolate(knots, size=self.forecast_size, mode=self.interpolation_mode) #, align_corners=True)
            forecast = forecast[:,0,:]
        elif 'cubic' in self.interpolation_mode:
            batch_size = int(self.interpolation_mode.split('-')[-1])
            knots = knots[:,None,None,:]
            forecast = t.zeros((len(knots), self.forecast_size)).to(knots.device)
            n_batches = int(np.ceil(len(knots)/batch_size))
            for i in range(n_batches):
                forecast_i = F.interpolate(knots[i*batch_size:(i+1)*batch_size], size=self.forecast_size, mode='bicubic') #, align_corners=True)
                forecast[i*batch_size:(i+1)*batch_size] += forecast_i[:,0,0,:]

        return backcast, forecast

class StochasticPool1D(nn.Module):
    def __init__(self, kernel_size, stride):
        super(StochasticPool1D, self).__init__()
        self.kernel_size = kernel_size 
        self.stride = stride

    def gen_random(self, values):
        # print(values)
        # if t.sum(values) != 0:
        #     idx = t.multinomial(values, num_samples=1)
        # else:
        
        idx = t.randint(0, values.shape[0], size=(1,)).type(t.LongTensor)
        # print(idx)
        return values[idx]

    def forward(self, x):
        init_size = x.shape

        x = x.unfold(1, self.kernel_size, self.stride)

        x = x.contiguous().view(-1, self.kernel_size)
    
        idx = t.randint(0, x.shape[1], size=(x.shape[0],)).type(t.cuda.LongTensor)
        
        x = t.take(x, idx)

        x = x.contiguous().view(init_size[0], int(init_size[1]/self.kernel_size))

        return x
    

# Cell
def init_weights(module, initialization):
    if type(module) == t.nn.Linear:
        if initialization == 'orthogonal':
            t.nn.init.orthogonal_(module.weight)
        elif initialization == 'he_uniform':
            t.nn.init.kaiming_uniform_(module.weight)
        elif initialization == 'he_normal':
            t.nn.init.kaiming_normal_(module.weight)
        elif initialization == 'glorot_uniform':
            t.nn.init.xavier_uniform_(module.weight)
        elif initialization == 'glorot_normal':
            t.nn.init.xavier_normal_(module.weight)
        elif initialization == 'Sin':
            siren_uniform_(module.weight, mode='fan_in', c=6)
        elif initialization == 'lecun_normal':
            pass #t.nn.init.normal_(module.weight, 0.0, std=1/np.sqrt(module.weight.numel()))
        else:
            assert 1<0, f'Initialization {initialization} not found'
    elif type(module) == t.nn.Conv1d or type(module) == t.nn.ConvTranspose1d:
        t.nn.init.kaiming_uniform_(module.weight, nonlinearity='relu')


# Cell
ACTIVATIONS = ['ReLU',
               'Softplus',
               'Tanh',
               'SELU',
               'LeakyReLU',
               'PReLU',
               'Sigmoid',
               'Sin']

class _NHITSBlock(nn.Module):
    """
    N-HiTS block which takes a basis function as an argument.
    """
    def __init__(self, n_time_in: int, n_time_out: int, n_x: int,
                 n_s: int, n_s_hidden: int, n_theta: int, n_theta_hidden: list,
                 n_pool_kernel_size: int, n_freq_downsample: int, pooling_mode: str, layer_mode: str, output_layer: str, basis: nn.Module,
                 n_layers: int,  batch_normalization: bool, dropout_prob: float, activation: str):
        """
        """
        super().__init__()

        assert (pooling_mode in ['max','conv', 'stochastic', 'none'])

        n_time_in_pooled = int(np.ceil(n_time_in/n_pool_kernel_size))

        if n_s == 0:
            n_s_hidden = 0
        n_theta_hidden = [n_time_in_pooled + (n_time_in+n_time_out)*n_x + n_s_hidden] + n_theta_hidden

        #print(n_theta_hidden)

        self.n_time_in = n_time_in
        self.n_time_out = n_time_out
        self.n_s = n_s
        self.n_s_hidden = n_s_hidden
        self.n_x = n_x
        self.n_pool_kernel_size = n_pool_kernel_size
        self.layer_mode = layer_mode
        self.pooling_mode = pooling_mode
    
        self.batch_normalization = batch_normalization
        self.dropout_prob = dropout_prob
        
        assert activation in ACTIVATIONS, f'{activation} is not in {ACTIVATIONS}'
        
        if activation != 'Sin':
            activ = getattr(nn, activation)()
        else:
            activ = Sine(30.0)



        if self.pooling_mode == 'max':

            self.pooling_layer = nn.MaxPool1d(kernel_size=self.n_pool_kernel_size,
                                              stride=self.n_pool_kernel_size)
            
            # kernel = n_freq_downsample
            # stride = kernel
            # self.pooling_layer = nn.MaxPool1d(kernel_size=kernel,
            #                                   stride=stride)
        # elif pooling_mode == 'conv':
        #     self.pooling_layer = nn.Conv1d(1, 1, kernel_size=self.n_pool_kernel_size, stride=self.n_pool_kernel_size)
            
        elif self.pooling_mode == 'stochastic':
            self.pooling_layer = StochasticPool1D(kernel_size=self.n_pool_kernel_size,
                                                  stride=self.n_pool_kernel_size)

        hidden_layers = []

        if self.pooling_mode == 'conv':

            hidden_layers.append(_HiddenFeaturesDownSampleEncoder(kernel_size=self.n_pool_kernel_size,
                                                                  stride=self.n_pool_kernel_size))

            # kernel = n_freq_downsample
            # stride = kernel
            # hidden_layers.append(_HiddenFeaturesDownSampleEncoder(kernel_size=kernel,
            #                                                       stride=stride,
            #                                                       num_features=n_theta_hidden[0],
            #                                                       activ=None))
        
        for i in range(n_layers):
            
            #downsample conv
            if layer_mode == 'conv' and n_theta_hidden[i] > n_theta_hidden[i+1]:
                                
                if math.floor(n_theta_hidden[i]/n_theta_hidden[i+1]) >= 2:
                    kernel = math.floor(n_theta_hidden[i]/n_theta_hidden[i+1])
                    stride = kernel

                    n_theta_hidden[i+1] = math.floor(((n_theta_hidden[i] - kernel)/stride) + 1)

                    hidden_layers.append(_HiddenFeaturesDownSampleEncoder(kernel_size=kernel, 
                                                                    stride=stride, 
                                                                    num_features=n_theta_hidden[i+1], 
                                                                    activ=activ))

                    # if self.batch_normalization:
                    #     hidden_layers.append(nn.BatchNorm1d(num_features=n_theta_hidden[i+1]))

                    # if self.dropout_prob>0:
                    #     hidden_layers.append(nn.Dropout(p=self.dropout_prob))
                else:
                    stride = 1
                    kernel = n_theta_hidden[i] - n_theta_hidden[i+1] + 1

                    hidden_layers.append(_HiddenFeaturesDownSampleEncoder(kernel_size=kernel, 
                                                                    stride=stride, 
                                                                    num_features=n_theta_hidden[i+1], 
                                                                    activ=activ))

            #upsample conv
            elif layer_mode == 'conv' and n_theta_hidden[i] < n_theta_hidden[i+1]:

                if math.floor(n_theta_hidden[i+1]/n_theta_hidden[i]) >= 2:
                    kernel = math.floor(n_theta_hidden[i+1]/n_theta_hidden[i])
                    stride = kernel

                    n_theta_hidden[i+1] = kernel * n_theta_hidden[i]

                    hidden_layers.append(_HiddenFeaturesUpsampleEncoder(kernel_size=kernel, 
                                                                    stride=stride, 
                                                                    num_features=n_theta_hidden[i+1], 
                                                                    activ=activ))

                    # if self.batch_normalization:
                    #     hidden_layers.append(nn.BatchNorm1d(num_features=n_theta_hidden[i+1]))

                    # if self.dropout_prob>0:
                    #     hidden_layers.append(nn.Dropout(p=self.dropout_prob))
                else:
                    stride = 1
                    kernel = n_theta_hidden[i+1] - n_theta_hidden[i] + 1

                    hidden_layers.append(_HiddenFeaturesUpsampleEncoder(kernel_size=kernel, 
                                                                    stride=stride, 
                                                                    num_features=n_theta_hidden[i+1], 
                                                                    activ=activ))

            elif layer_mode == 'linear':
                hidden_layers.append(_HiddenFeaturesLinearEncoder(in_features=n_theta_hidden[i], 
                                                                  out_features=n_theta_hidden[i+1], 
                                                                  activ=activ,
                                                                  batch_normalization=self.batch_normalization,
                                                                  dropout_prob=self.dropout_prob))

                # activ = Sine(1.0)

                # if self.batch_normalization:
                #     #print("Applying batch norm")
                #     hidden_layers.append(nn.BatchNorm1d(num_features=n_theta_hidden[i+1]))

                # if self.dropout_prob>0:
                #     hidden_layers.append(nn.Dropout(p=self.dropout_prob))

            
        if output_layer == 'linear':

            self.output_layer = [_HiddenFeaturesLinearEncoder(in_features=n_theta_hidden[-1], 
                                                              out_features=n_theta, 
                                                              activ=None,
                                                              batch_normalization=False,
                                                              dropout_prob=0)]
        
        elif output_layer == 'conv':

            if n_theta_hidden[-1] > n_theta:

                if math.floor(n_theta_hidden[-1]/n_theta) >= 2:

                    kernel = math.floor(n_theta_hidden[-1]/n_theta)
                    stride = kernel

                    n_theta_adjusted = math.floor(((n_theta_hidden[-1] - kernel)/stride) + 1)

                    if n_theta_adjusted != n_theta:

                        self.output_layer = [_HiddenFeaturesDownSampleEncoder(kernel_size=kernel, 
                                                                        stride=stride, 
                                                                        num_features=n_theta_adjusted, 
                                                                        activ=activ),
                                            _HiddenFeaturesLinearEncoder(in_features=n_theta_adjusted, 
                                                                         out_features=n_theta, 
                                                                         activ=None,
                                                                         batch_normalization=False,
                                                                         dropout_prob=0)]

                    else:
                        self.output_layer = [_HiddenFeaturesDownSampleEncoder(kernel_size=kernel, 
                                                                        stride=stride, 
                                                                        num_features=n_theta_adjusted, 
                                                                        activ=None)]

                else:

                    stride = 1
                    kernel = n_theta_hidden[-1] - n_theta + 1

                    self.output_layer = [_HiddenFeaturesDownSampleEncoder(kernel_size=kernel, 
                                                                    stride=stride, 
                                                                    num_features=n_theta, 
                                                                    activ=None)] 

            elif n_theta_hidden[-1] < n_theta:

                if math.floor(n_theta/n_theta_hidden[-1]) >= 2:

                    kernel = math.floor(n_theta/n_theta_hidden[-1])
                    stride = kernel

                    n_theta_adjusted = kernel * n_theta_hidden[-1]

                    if n_theta_adjusted != n_theta:

                        self.output_layer = [_HiddenFeaturesUpsampleEncoder(kernel_size=kernel, 
                                                                            stride=stride, 
                                                                            num_features=n_theta_adjusted, 
                                                                            activ=activ),
                                            _HiddenFeaturesLinearEncoder(in_features=n_theta_adjusted, 
                                                                         out_features=n_theta, 
                                                                         activ=None,
                                                                         batch_normalization=False,
                                                                         dropout_prob=0)]

                    else:
                        self.output_layer = [_HiddenFeaturesUpsampleEncoder(kernel_size=kernel, 
                                                                            stride=stride, 
                                                                            num_features=n_theta_adjusted, 
                                                                            activ=None)]

                else:

                    stride = 1
                    kernel = n_theta - n_theta_hidden[-1] + 1

                    self.output_layer = [_HiddenFeaturesUpsampleEncoder(kernel_size=kernel, 
                                                                    stride=stride, 
                                                                    num_features=n_theta, 
                                                                    activ=None)] 

            else:
                self.output_layer = [_HiddenFeaturesLinearEncoder(in_features=n_theta_hidden[-1], 
                                                                  out_features=n_theta, 
                                                                  activ=None,
                                                                  batch_normalization=False,
                                                                  dropout_prob=0)]

        elif output_layer == 'max':

            if n_theta_hidden[-1] > n_theta:

                if math.floor(n_theta_hidden[-1]/n_theta) >= 2:

                    kernel = math.floor(n_theta_hidden[-1]/n_theta)
                    stride = kernel

                    n_theta_adjusted = math.floor(((n_theta_hidden[-1] - kernel)/stride) + 1)

                    if n_theta_adjusted != n_theta:

                        self.output_layer = [nn.MaxPool1d(kernel_size=kernel, stride=stride),
                                            _HiddenFeaturesLinearEncoder(in_features=n_theta_adjusted, 
                                                                        out_features=n_theta, 
                                                                        activ=None,
                                                                        batch_normalization=False,
                                                                        dropout_prob=0)]

                    else:
                        self.output_layer = [nn.MaxPool1d(kernel_size=kernel, stride=stride)]

                else:

                    stride = 1
                    kernel = n_theta_hidden[-1] - n_theta + 1

                    self.output_layer = [nn.MaxPool1d(kernel_size=kernel, stride=stride)]
            
            else:
                self.output_layer = [_HiddenFeaturesLinearEncoder(in_features=n_theta_hidden[-1], 
                                                                        out_features=n_theta, 
                                                                        activ=None,
                                                                        batch_normalization=False,
                                                                        dropout_prob=0)]


        layers = hidden_layers + self.output_layer

        # n_s is computed with data, n_s_hidden is provided by user, if 0 no statics are used
        if (self.n_s > 0) and (self.n_s_hidden > 0):
            self.static_encoder = _StaticFeaturesEncoder(in_features=n_s, out_features=n_s_hidden)
        self.layers = nn.Sequential(*layers)
        self.basis = basis

    def forward(self, insample_y: t.Tensor, insample_x_t: t.Tensor,
                outsample_x_t: t.Tensor, x_s: t.Tensor) -> Tuple[t.Tensor, t.Tensor]:


        #print("Input size prior to pooling: " + str(insample_y.size()))
        if self.pooling_mode == 'max':
            insample_y = insample_y.unsqueeze(1)
            # Pooling layer to downsample input
            #print("Before applying conv pooling")
            #print(insample_y.shape)
            insample_y = self.pooling_layer(insample_y)
            insample_y = insample_y.squeeze(1)

        elif self.pooling_mode == 'stochastic':
            # print(insample_y.shape)
            insample_y = self.pooling_layer(insample_y)
            # print(insample_y.shape)

        #print("Input size after to pooling: " + str(insample_y.size()))
        #print("Post applying conv pooling")
        #print(insample_y.shape)

        batch_size = len(insample_y)
        if self.n_x > 0:
            insample_y = t.cat(( insample_y, insample_x_t.reshape(batch_size, -1) ), 1)
            insample_y = t.cat(( insample_y, outsample_x_t.reshape(batch_size, -1) ), 1)

        # Static exogenous
        if (self.n_s > 0) and (self.n_s_hidden > 0):
            x_s = self.static_encoder(x_s)
            insample_y = t.cat((insample_y, x_s), 1)

        # Compute local projection weights and projection
        #print("Post applying static encoding")
        #print(insample_y.shape)
        #print("Input size before forecast: " + str(insample_y.size()))
        theta = self.layers(insample_y)

        if len(theta.size()) == 3:
            theta = theta.squeeze(1)
        
        backcast, forecast = self.basis(theta, insample_x_t, outsample_x_t)

        return backcast, forecast

# Cell
class _NHITS(nn.Module):
    """
    N-HiTS Model.
    """
    def __init__(self,
                 n_time_in,
                 n_time_out,
                 n_s,
                 n_x,
                 n_s_hidden,
                 n_x_hidden,
                 stack_types: list,
                 n_blocks: list,
                 n_layers: list,
                 n_theta_hidden: list,
                 n_pool_kernel_size: list,
                 n_freq_downsample: list,
                 pooling_mode,
                 layer_mode,
                 output_layer,
                 interpolation_mode,
                 dropout_prob_theta,
                 activation,
                 initialization,
                 batch_normalization,
                 shared_weights):
        super().__init__()

        self.n_time_out = n_time_out

        blocks = self.create_stack(stack_types=stack_types,
                                   n_blocks=n_blocks,
                                   n_time_in=n_time_in,
                                   n_time_out=n_time_out,
                                   n_x=n_x,
                                   n_x_hidden=n_x_hidden,
                                   n_s=n_s,
                                   n_s_hidden=n_s_hidden,
                                   n_layers=n_layers,
                                   n_theta_hidden=n_theta_hidden,
                                   n_pool_kernel_size=n_pool_kernel_size,
                                   n_freq_downsample=n_freq_downsample,
                                   pooling_mode=pooling_mode,
                                   layer_mode=layer_mode,
                                   output_layer=output_layer,
                                   interpolation_mode=interpolation_mode,
                                   batch_normalization=batch_normalization,
                                   dropout_prob_theta=dropout_prob_theta,
                                   activation=activation,
                                   shared_weights=shared_weights,
                                   initialization=initialization)
        self.blocks = t.nn.ModuleList(blocks)

    def create_stack(self, stack_types, n_blocks,
                     n_time_in, n_time_out,
                     n_x, n_x_hidden, n_s, n_s_hidden,
                     n_layers, n_theta_hidden,
                     n_pool_kernel_size, n_freq_downsample, pooling_mode, 
                     layer_mode, 
                     output_layer,
                     interpolation_mode,
                     batch_normalization, dropout_prob_theta,
                     activation, shared_weights, initialization):

        block_list = []
        for i in range(len(stack_types)):
            #print(f'| --  Stack {stack_types[i]} (#{i})')
            for block_id in range(n_blocks[i]):

                # Batch norm only on first block
                if (len(block_list)==0) and (batch_normalization):
                    batch_normalization_block = True
                else:
                    batch_normalization_block = False

                # Shared weights
                if shared_weights and block_id>0:
                    nbeats_block = block_list[-1]
                else:
                    if stack_types[i] == 'identity':
                        n_theta = (n_time_in + max(n_time_out//n_freq_downsample[i], 1) )
                        basis = IdentityBasis(backcast_size=n_time_in,
                                              forecast_size=n_time_out,
                                              interpolation_mode=interpolation_mode)

                    else:
                        assert 1<0, f'Block type not found!'

                    nbeats_block = _NHITSBlock(n_time_in=n_time_in,
                                                   n_time_out=n_time_out,
                                                   n_x=n_x,
                                                   n_s=n_s,
                                                   n_s_hidden=n_s_hidden,
                                                   n_theta=n_theta,
                                                   n_theta_hidden=n_theta_hidden[i],
                                                   n_pool_kernel_size=n_pool_kernel_size[i],
                                                   n_freq_downsample=n_freq_downsample[i],
                                                   pooling_mode=pooling_mode,
                                                   layer_mode=layer_mode,
                                                   output_layer=output_layer,
                                                   basis=basis,
                                                   n_layers=n_layers[i],
                                                   batch_normalization=batch_normalization_block,
                                                   dropout_prob=dropout_prob_theta,
                                                   activation=activation)

                # Select type of evaluation and apply it to all layers of block
                init_function = partial(init_weights, initialization=initialization)
                nbeats_block.layers.apply(init_function)
                #print(f'     | -- {nbeats_block}')
                block_list.append(nbeats_block)
        return block_list

    def forward(self, S: t.Tensor, Y: t.Tensor, X: t.Tensor,
                insample_mask: t.Tensor, outsample_mask: t.Tensor,
                return_decomposition: bool=False):

        # insample
        insample_y    = Y[:, :-self.n_time_out]
        insample_x_t  = X[:, :, :-self.n_time_out]
        insample_mask = insample_mask[:, :-self.n_time_out]

        # outsample
        outsample_y   = Y[:, -self.n_time_out:]
        outsample_x_t = X[:, :, -self.n_time_out:]
        outsample_mask = outsample_mask[:, -self.n_time_out:]

        if return_decomposition:
            forecast, block_forecasts = self.forecast_decomposition(insample_y=insample_y,
                                                                    insample_x_t=insample_x_t,
                                                                    insample_mask=insample_mask,
                                                                    outsample_x_t=outsample_x_t,
                                                                    x_s=S)
            return outsample_y, forecast, block_forecasts, outsample_mask

        else:
            forecast = self.forecast(insample_y=insample_y,
                                     insample_x_t=insample_x_t,
                                     insample_mask=insample_mask,
                                     outsample_x_t=outsample_x_t,
                                     x_s=S)
            return outsample_y, forecast, outsample_mask

    def forecast(self, insample_y: t.Tensor, insample_x_t: t.Tensor, insample_mask: t.Tensor,
                 outsample_x_t: t.Tensor, x_s: t.Tensor):

        residuals = insample_y.flip(dims=(-1,))
        insample_x_t = insample_x_t.flip(dims=(-1,))
        insample_mask = insample_mask.flip(dims=(-1,))

        forecast = insample_y[:, -1:] # Level with Naive1
        for i, block in enumerate(self.blocks):
            backcast, block_forecast = block(insample_y=residuals, insample_x_t=insample_x_t,
                                             outsample_x_t=outsample_x_t, x_s=x_s)
            residuals = (residuals - backcast) * insample_mask
            forecast = forecast + block_forecast

        return forecast

    def forecast_decomposition(self, insample_y: t.Tensor, insample_x_t: t.Tensor, insample_mask: t.Tensor,
                               outsample_x_t: t.Tensor, x_s: t.Tensor):

        residuals = insample_y.flip(dims=(-1,))
        insample_x_t = insample_x_t.flip(dims=(-1,))
        insample_mask = insample_mask.flip(dims=(-1,))

        n_batch, n_channels, n_t = outsample_x_t.size(0), outsample_x_t.size(1), outsample_x_t.size(2)

        level = insample_y[:, -1:] # Level with Naive1
        block_forecasts = [ level.repeat(1, n_t) ]

        forecast = level
        for i, block in enumerate(self.blocks):
            backcast, block_forecast = block(insample_y=residuals, insample_x_t=insample_x_t,
                                             outsample_x_t=outsample_x_t, x_s=x_s)
            residuals = (residuals - backcast) * insample_mask
            forecast = forecast + block_forecast
            block_forecasts.append(block_forecast)

        # (n_batch, n_blocks, n_t)
        block_forecasts = t.stack(block_forecasts)
        block_forecasts = block_forecasts.permute(1,0,2)

        return forecast, block_forecasts

# Cell
class NHITS(pl.LightningModule):
    def __init__(self,
                 n_time_in,
                 n_time_out,
                 n_x,
                 n_x_hidden,
                 n_s,
                 n_s_hidden,
                 shared_weights,
                 activation,
                 initialization,
                 stack_types,
                 n_blocks,
                 n_layers,
                 n_theta_hidden,
                 n_pool_kernel_size,
                 n_freq_downsample,
                 pooling_mode,
                 layer_mode,
                 output_layer,
                 interpolation_mode,
                 batch_normalization,
                 dropout_prob_theta,
                 learning_rate,
                 lr_decay,
                 lr_decay_step_size,
                 weight_decay,
                 loss_train,
                 loss_hypar,
                 loss_valid,
                 frequency,
                 random_seed,
                 seasonality):
        super(NHITS, self).__init__()
        """
        N-HiTS model.

        Parameters
        ----------
        n_time_in: int
            Multiplier to get insample size.
            Insample size = n_time_in * output_size
        n_time_out: int
            Forecast horizon.
        shared_weights: bool
            If True, repeats first block.
        activation: str
            Activation function.
            An item from ['relu', 'softplus', 'tanh', 'selu', 'lrelu', 'prelu', 'sigmoid'].
        initialization: str
            Initialization function.
            An item from ['orthogonal', 'he_uniform', 'glorot_uniform', 'glorot_normal', 'lecun_normal'].
        stack_types: List[str]
            List of stack types.
            Subset from ['identity'].
        n_blocks: List[int]
            Number of blocks for each stack type.
            Note that len(n_blocks) = len(stack_types).
        n_layers: List[int]
            Number of layers for each stack type.
            Note that len(n_layers) = len(stack_types).
        n_theta_hidden: List[List[int]]
            Structure of hidden layers for each stack type.
            Each internal list should contain the number of units of each hidden layer.
            Note that len(n_theta_hidden) = len(stack_types).
        n_pool_kernel_size List[int]:
            Pooling size for input for each stack.
            Note that len(n_pool_kernel_size) = len(stack_types).
        n_freq_downsample List[int]:
            Downsample multiplier of output for each stack.
            Note that len(n_freq_downsample) = len(stack_types).
        batch_normalization: bool
            Whether perform batch normalization.
        dropout_prob_theta: float
            Float between (0, 1).
            Dropout for Nbeats basis.
        learning_rate: float
            Learning rate between (0, 1).
        lr_decay: float
            Decreasing multiplier for the learning rate.
        lr_decay_step_size: int
            Steps between each lerning rate decay.
        weight_decay: float
            L2 penalty for optimizer.
        loss_train: str
            Loss to optimize.
            An item from ['MAPE', 'MASE', 'SMAPE', 'MSE', 'MAE', 'PINBALL', 'PINBALL2'].
        loss_hypar:
            Hyperparameter for chosen loss.
        loss_valid:
            Validation loss.
            An item from ['MAPE', 'MASE', 'SMAPE', 'RMSE', 'MAE', 'PINBALL'].
        frequency: str
            Time series frequency.
        random_seed: int
            random_seed for pseudo random pytorch initializer and
            numpy random generator.
        seasonality: int
            Time series seasonality.
            Usually 7 for daily data, 12 for monthly data and 4 for weekly data.
        """

        if activation == 'SELU': initialization = 'lecun_normal'

        #------------------------ Model Attributes ------------------------#
        # Architecture parameters
        self.n_time_in = n_time_in
        self.n_time_out = n_time_out
        self.n_x = n_x
        self.n_x_hidden = n_x_hidden
        self.n_s = n_s
        self.n_s_hidden = n_s_hidden
        self.shared_weights = shared_weights
        self.activation = activation
        self.initialization = initialization
        self.stack_types = stack_types
        self.n_blocks = n_blocks
        self.n_layers = n_layers
        self.n_theta_hidden = n_theta_hidden
        self.n_pool_kernel_size = n_pool_kernel_size
        self.n_freq_downsample = n_freq_downsample
        self.pooling_mode = pooling_mode
        self.layer_mode = layer_mode
        self.interpolation_mode = interpolation_mode

        # Loss functions
        self.loss_train = loss_train
        self.loss_hypar = loss_hypar
        self.loss_valid = loss_valid
        self.loss_fn_train = LossFunction(loss_train,
                                          seasonality=self.loss_hypar)
        self.loss_fn_valid = LossFunction(loss_valid,
                                          seasonality=self.loss_hypar)

        # Regularization and optimization parameters
        self.batch_normalization = batch_normalization
        self.dropout_prob_theta = dropout_prob_theta
        self.learning_rate = learning_rate
        self.lr_decay = lr_decay
        self.weight_decay = weight_decay
        self.lr_decay_step_size = lr_decay_step_size
        self.random_seed = random_seed

        # Data parameters
        self.frequency = frequency
        self.seasonality = seasonality
        self.return_decomposition = False

        self.model = _NHITS(n_time_in=self.n_time_in,
                             n_time_out=self.n_time_out,
                             n_s=self.n_s,
                             n_x=self.n_x,
                             n_s_hidden=self.n_s_hidden,
                             n_x_hidden=self.n_x_hidden,
                             stack_types=self.stack_types,
                             n_blocks=self.n_blocks,
                             n_layers=self.n_layers,
                             n_theta_hidden=self.n_theta_hidden,
                             n_pool_kernel_size=self.n_pool_kernel_size,
                             n_freq_downsample=self.n_freq_downsample,
                             pooling_mode=self.pooling_mode,
                             layer_mode=layer_mode,
                             output_layer=output_layer,
                             interpolation_mode=self.interpolation_mode,
                             dropout_prob_theta=self.dropout_prob_theta,
                             activation=self.activation,
                             initialization=self.initialization,
                             batch_normalization=self.batch_normalization,
                             shared_weights=self.shared_weights)

    def training_step(self, batch, batch_idx):
        S = batch['S']
        Y = batch['Y']
        X = batch['X']
        sample_mask = batch['sample_mask']
        available_mask = batch['available_mask']

        outsample_y, forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                           insample_mask=available_mask,
                                                           outsample_mask=sample_mask,
                                                           return_decomposition=False)

        loss = self.loss_fn_train(y=outsample_y,
                                  y_hat=forecast,
                                  mask=outsample_mask,
                                  y_insample=Y)

        self.log('train_loss', loss, prog_bar=True, on_epoch=True)

        return loss

    def validation_step(self, batch, idx):
        S = batch['S']
        Y = batch['Y']
        X = batch['X']
        sample_mask = batch['sample_mask']
        available_mask = batch['available_mask']

        outsample_y, forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                           insample_mask=available_mask,
                                                           outsample_mask=sample_mask,
                                                           return_decomposition=False)

        loss = self.loss_fn_valid(y=outsample_y,
                                  y_hat=forecast,
                                  mask=outsample_mask,
                                  y_insample=Y)

        self.log('val_loss', loss, prog_bar=True)

        return loss

    def on_fit_start(self):
        t.manual_seed(self.random_seed)
        np.random.seed(self.random_seed)
        random.seed(self.random_seed)

    def forward(self, batch):
        S = batch['S']
        Y = batch['Y']
        X = batch['X']
        sample_mask = batch['sample_mask']
        available_mask = batch['available_mask']

        if self.return_decomposition:
            outsample_y, forecast, block_forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                                     insample_mask=available_mask,
                                                                     outsample_mask=sample_mask,
                                                                     return_decomposition=True)
            return outsample_y, forecast, block_forecast, outsample_mask

        outsample_y, forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                           insample_mask=available_mask,
                                                           outsample_mask=sample_mask,
                                                           return_decomposition=False)
        return outsample_y, forecast, outsample_mask

    def configure_optimizers(self):
        optimizer = optim.Adam(self.model.parameters(),
                               lr=self.learning_rate,
                               weight_decay=self.weight_decay)

        lr_scheduler = optim.lr_scheduler.StepLR(optimizer,
                                                 step_size=self.lr_decay_step_size,
                                                 gamma=self.lr_decay)

        return {'optimizer': optimizer, 'lr_scheduler': lr_scheduler}
