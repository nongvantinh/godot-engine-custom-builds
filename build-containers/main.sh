#!/usr/bin/env bash

set -e

usage() {
    echo "Usage: $0 docker [OPTIONS...]"
    echo
    echo "Available options for Docker:"
    echo "  -r, --registry <registry>       Specify the Docker registry."
    echo "  -u, --username <username>       Specify the username for the registry."
    echo "  -p, --password <password>       Specify the password for the registry."
    echo "  -g, --godot <branch>            Specify the Godot branch (default: $GIT_BRANCH)."
    echo "  -d, --distro <distro>           Specify the base distribution for Docker images (default: $BASE_DISTRO)."
    echo "  -c, --container-type <type>     Specify the type of Docker image to build (default: $CONTAINER_TYPE)."
    echo "  -h, --help                      Show this help message."
    echo
}

select_container_type_usage() {
    echo "Usage: ${FUNCNAME[1]} [OPTIONS...]"
    echo "Available Docker container types:"
    echo "  linux                   Build a Linux container."
    echo "  windows                 Build a Windows container."
    echo "  web                     Build a container for web applications."
    echo "  android                 Build a container for Android applications."
    echo "  osx                     Build a macOS container."
    echo "  ios                     Build a container for iOS applications."
    echo "  all                     Build a container for all platforms."
    echo
}

select_container_type() {
    local -n container_types_ref
    declare -A seen  # Associative array to track seen container types
    declare -A arg_map=(
        [linux]="linux"
        [windows]="windows"
        [web]="web"
        [android]="android"
        [osx]="osx"
        [ios]="ios"
        [all]="all"
    )

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --container-types-ref)
                container_types_ref="$2"
                shift 2
                ;;
            linux|windows|web|android|osx|ios)
                seen["${arg_map[$1]}"]=1
                shift
                ;;
            all)
                for type in "${!arg_map[@]}"; do
                    if [[ "$type" != "all" ]]; then
                        seen["$type"]=1
                    fi
                done
                shift
                ;;
            *)
                echo "Invalid option: $1"
                return 1
                ;;
        esac
    done

    local additional_args=()
    if [ ${#seen[@]} -eq 0 ]; then
        select_container_type_usage
        read -p "Please enter the Docker container types (space-separated): " -a additional_args
    fi

    for arg in "${additional_args[@]}"; do
        if [[ ${arg_map[$arg]} ]]; then
            if [[ "$arg" == "all" ]]; then
                for type in "${!arg_map[@]}"; do
                    if [[ "$type" != "all" ]]; then
                        seen["$type"]=1
                    fi
                done
            else
                seen["${arg_map[$arg]}"]=1
            fi
        else
            echo "Invalid argument: '$arg' is not a valid argument for container type"
        fi
    done
    
    for type in "${!seen[@]}"; do
        container_types_ref+=("$type")
    done

    if [ ${#container_types_ref[@]} -eq 0 ]; then
        echo "No valid container types selected. Exiting."
        exit 1
    fi
}

confirm_settings() {
    echo "Docker image tag: ${image_version}"
    echo
    while true; do
        read -p "Is this correct? [y/n] " yn
        case $yn in
            [Yy]* ) break;;
            [Nn]* ) exit 1;;
            * ) echo "Please answer yes or no.";;
        esac
    done
}

build_docker() {
    local container_type="$1"

    docker build                                                                \
        --no-cache                                                              \
        --debug                                                                 \
        --build-arg IMAGE_VERSION=${image_version}                              \
        --build-arg REPO_URL="https://github.com/$USERNAME/$REPO_NAME"          \
        -t godot-"$container_type:${image_version}"                             \
        -f Dockerfile."$container_type" .                                       \
        2>&1 | tee logs/"$container_type".log
}

ensure_valid_apple_sdks() {
    # Get the directory of the script, regardless of the current working directory
    local basedir=$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)
    local files_root="$basedir/files"

    local osx_sdk="${OSX_SDK}"
    local ios_sdk="${IOS_SDK}"
    local xcode_sdk="${XCODE_SDK}"

    if [ ! -e "${files_root}"/MacOSX${osx_sdk}.sdk.tar.xz ] || 
       [ ! -e "${files_root}"/iPhoneOS${ios_sdk}.sdk.tar.xz ] || 
       [ ! -e "${files_root}"/iPhoneSimulator${ios_sdk}.sdk.tar.xz ]; then

        if [ ! -r "${files_root}"/Xcode_${xcode_sdk}.xip ]; then
            echo
            echo "Error: 'files/Xcode_${xcode_sdk}.xip' is required for Apple platforms, but was not found or couldn't be read."
            echo "It can be downloaded from https://developer.apple.com/download/more/ with a valid apple ID."
            exit 1
        fi

        echo "Building OSX and iOS SDK packages. This will take a while"

        build_docker "xcode"
        docker run --rm \
            -v ${files_root}:/root/files \
            -e XCODE_SDKV=${xcode_sdk} \
            -e OSX_SDKV=${osx_sdk} \
            -e IOS_SDKV=${ios_sdk} \
            godot-xcode:${image_version} \
            2>&1 | tee logs/xcode_packer.log

        echo "SDK packages copied to '${files_root}'"
    fi
}

build_containers() {
    mkdir -p logs

    if ! is_container_built "fedora"; then
        echo "Building for fedora"
        docker build -t godot-fedora:${image_version} -f Dockerfile.base . 2>&1 | tee logs/base.log
    fi
    
    declare -A dependencies
    dependencies=(
        ["osx"]="xcode"
        ["ios"]="osx,xcode"
    )
    
    for container_type in "${container_types[@]}"; do
        if [[ "$container_type" == "osx" || "$container_type" == "ios" ]]; then
            ensure_valid_apple_sdks
        fi

        if [[ -n "${dependencies[$container_type]}" ]]; then
            IFS=',' read -ra deps <<< "${dependencies[$container_type]}"
            for dep in "${deps[@]}"; do
                if ! is_container_built "$dep"; then
                    echo "Building for ${dep}"
                    build_docker "$dep"
                fi
            done
            
        fi

        if ! is_container_built "$container_type"; then
            echo "Building for ${container_type}"
            build_docker "$container_type"
        fi

    done
}

is_container_built() {
    local container="$1"

    if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^godot-${container}:${image_version}$"; then
        return 0
    else
        return 1
    fi
}

main() {
    registry="${REGISTRY}"
    username="${USERNAME}"
    godot_branch="${GIT_BRANCH}"
    base_distro="${BASE_DISTRO}"
    arg_container_types=()

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --registry)
                registry="$2"
                shift 2
                ;;
            --username)
                username="$2"
                shift 2
                ;;
            --godot-branch)
                godot_branch="$2"
                shift 2
                ;;
            --base-distro)
                base_distro="$2"
                shift 2
                ;;
            --container-types)
                arg_container_types+=("$2")
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

    # If no container types were provided as arguments, prompt the user to select
    container_types=()
    select_container_type --container-types-ref container_types "${arg_container_types[@]}"

    image_version="$godot_branch-$base_distro"

    if [ ! -z "$PS1" ]; then
        confirm_settings --image-version  "${image_version}"
    fi

    echo "Building Docker container with the following parameters:"
    echo "Godot branch: ${godot_branch}"
    echo "Base distribution: ${base_distro}"
    echo "Image version: ${image_version}"
    echo "Registry: ${registry}"
    echo "Username: ${username}"
    echo "Docker build command executed for: ${container_types[*]}"

    build_containers

    local upload_sh_path
    local file="upload.sh"
    find_file_upwards --file "${file}" --path-ref upload_sh_path
    if [[ $? -eq 0 ]]; then
        chmod +x "${upload_sh_path}"
        "${upload_sh_path}"
    else
        echo "Failed to find ${file}"
    fi
}

main "$@"