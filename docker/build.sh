#!/bin/bash

docker build . --rm -t  v2a-$(id -un)
