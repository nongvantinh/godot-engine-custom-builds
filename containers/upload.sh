#!/usr/bin/env bash

set -e

usage() {
    echo "Usage: $0 [OPTIONS...]"
    echo
    echo "Options:"
    echo "  -r, --registry <REGISTRY>      Specify the Docker registry."
    echo "  -u, --username <USERNAME>      Specify the username for the Docker registry."
    echo "  -t, --token <PAT_TOKEN>        Specify the Personal Access Token for Docker registry."
    echo "  -h, --help                     Show this help message."
    echo
}

cleanup_dangling_images() {
    echo "List of docker images"
    echo "$(docker images)"
    echo "Cleaning up dangling images..."

    dangling_images=$(docker images -f "dangling=true" -q)

    if [[ -n "${dangling_images}" ]]; then
        docker rmi ${dangling_images} || true
    else
        echo "No dangling images to remove."
    fi
}

tag_and_push_images() {
    echo "Tagging and pushing images..."
    docker images --format '{{.Repository}}:{{.Tag}}' | while read -r image; do
        if [[ ${image} == *"godot"* ]] && [[ ${image} != *"/"* ]]; then
            local image_name=$(echo "${image}" | awk -F ':' '{print $1}')
            local tag=$(echo "${image}" | awk -F ':' '{print $2}')
            local ghcr_name="${registry}/${username}/${image_name}"

            echo "Tagging ${image} as ${ghcr_name}:${tag}"
            docker tag "${image}" "${ghcr_name}:${tag}"

            echo "Pushing ${ghcr_name}:${tag} to ${registry}"
            docker push "${ghcr_name}:${tag}"
        fi
    done
}

main() {
    registry="${REGISTRY}"
    username="${USERNAME}"
    pat_token="${PAT_TOKEN}"

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            -r|--registry)
                registry="$2"
                shift 2
                ;;
            -u|--username)
                username="$2"
                shift 2
                ;;
            -t|--token)
                pat_token="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                echo "Invalid option: $1"
                usage
                exit 1
                ;;
        esac
    done

    login_to_github_container_registry  --registry "${registry}"     \
                                        --username "${username}"     \
                                        --pat_token "${pat_token}"
    cleanup_dangling_images
    tag_and_push_images

}

main "$@"