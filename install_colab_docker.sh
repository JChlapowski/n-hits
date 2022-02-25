# First let's update all the packages to the latest ones with the following command
sudo apt update -qq

# Now we want to install some prerequisite packages which will let us use HTTPS over apt
sudo apt install apt-transport-https ca-certificates curl software-properties-common -qq

# After that we will add the GPG key for the official Docker repository to the system
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add -

# We will add the Docker repository to our APT sources
sudo add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu bionic stable"

# Next let's update the package database with our newly added Docker package repo
sudo apt update -qq

# Finally lets install docker with the below command
sudo apt install docker-ce

# Lets check that docker is running
docker

# Originally, we did the following: (but doesn't work in Colab...)
# sudo systemctl status docker

# The output should be similar to this snippet below
# ● docker.service - Docker Application Container Engine
#    Loaded: loaded (/lib/systemd/system/docker.service; enabled; vendor preset: enabled)
#    Active: active (running) since Tue 2019-01-01 19:22:114 UTC; 1min 25s ago
#      Docs: https://docs.docker.com
#  Main PID: 10096 (dockerd)
#     Tasks: 16
#    CGroup: /system.slice/docker.service
#            ├─10096 /usr/bin/dockerd -H fd://
#            └─10113 docker-containerd --config /var/run/docker/containerd/containerd.toml

# And now that everything is good, you should be able to do:

# docker run -it -p 8888:8888 gcr.io/tensorflow/tensorflow
