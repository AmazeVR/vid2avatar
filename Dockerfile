ARG FROM_IMAGE_NAME=amazevr/22.10-pytorch:cuda11.3-py3.8-torch1.11.0-pytorch3d0.6.1-opencv-openimageio-kaolin0.13.0
FROM ${FROM_IMAGE_NAME}

ADD . /workspace/v2a
WORKDIR /workspace/v2a

RUN python -m pip install -U pip
RUN python -m pip install -e ./code
RUN python -m pip install -r requirement.txt
# RUN python -m pip uninstall opencv-python
# RUN python -m pip install opencv-python-headless

# ARG UNAME
# ARG UID
# ARG GID
# RUN groupadd -g $GID -o $UNAME
# RUN useradd -m -u $UID -g $GID -o -s /bin/bash $UNAME
# USER $UNAME
