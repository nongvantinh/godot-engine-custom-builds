#!/bin/bash

# Clean Up Dangling Images
echo "Cleaning up dangling images..."
docker rmi $(docker images -f "dangling=true" -q) || true  # Ignore errors if no dangling images

echo "Tagging and pushing images..."
docker images --format '{{.Repository}}:{{.Tag}}' | while read -r image; do
  if [[ $image == *"godot"* ]] && [[ $image != *"/"* ]]; then
    REPO_NAME=$(echo $image | awk -F ':' '{print $1}')
    TAG=$(echo $image | awk -F ':' '{print $2}')

    IMAGE_NAME="${REGISTRY_URL}/${GITHUB_REPOSITORY_OWNER}/$REPO_NAME"

    echo "Tagging $image as $IMAGE_NAME:$TAG"
    docker tag "$image" "$IMAGE_NAME:$TAG"

    echo "Pushing $IMAGE_NAME:$TAG to $REGISTRY_URL"
    docker push "$IMAGE_NAME:$TAG"
  fi
done
