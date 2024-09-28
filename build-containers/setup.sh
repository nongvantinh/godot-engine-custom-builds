#!/usr/bin/env bash

docker=$(command -v docker)

if [ -z "$docker" ]; then
  echo "Docker needs to be in PATH for this script to work."
  exit 1
else
  echo "Docker is installed at: $docker"
fi
