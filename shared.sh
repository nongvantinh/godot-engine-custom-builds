#!/usr/bin/env bash

set -e

find_file_upwards_usage() {
    echo "Usage: find_file_upwards [OPTIONS]"
    echo
    echo "Search for a specified file from the current directory upwards."
    echo
    echo "Options:"
    echo "  -p, --path-ref <variable>  Reference variable to store the full path of the found file."
    echo "  -f, --file <filename>   The name of the file to search for."
    echo "  -h, --help              Display this help message."
    echo
    echo "Example:"
    echo "  find_file_upwards --path-ref result --file myfile.txt"
    echo
}

find_file_upwards() {
    local -n ref
    local file
    local dir=$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            -p|--path-ref)
                ref="$2"
                shift 2
                ;;
            -f|--file)
                file="$2"
                shift 2
                ;;
            -h|--help)
                find_file_upwards_usage
                exit 0
                ;;
            *)
                echo "Invalid option: $1"
                find_file_upwards_usage
                return 1
                ;;
        esac
    done

    if [[ -z "$file" ]]; then
        echo "File not specified."
        return 1
    fi

    while [[ "$dir" != "/" ]]; do
        if [[ -e "$dir/$file" ]]; then
            ref="$dir/$file"
            return 0
        fi
        dir="$(dirname "$dir")"
    done

    if [[ -e "/$file" ]]; then
        ref="/$file"
        return 0
    fi

    echo "File '$file' not found."
    return 1
}


ensure_docker_available() {
    docker=$(command -v docker)

    if [ -z "$docker" ]; then
      echo "Docker needs to be in PATH for this script to work."
      exit 1
    fi
}

login_to_github_container_registry() {
    echo "==========LOGIN TO GITHUB CONTAINER REGISTRY=========="

    local registry="${REGISTRY}"
    local username="${USERNAME}"
    local pat_token="${PAT_TOKEN}"

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
            --pat_token)
                pat_token="$2"
                shift 2
                ;;
            *)
                echo "Invalid option: $1"
                return 1
                ;;
        esac
    done

    if [[ -z "$registry" || -z "$username" || -z "$pat_token" ]]; then
        echo "Error: registry, username, and PAT token must be provided."
        return 1
    fi

    echo "Attempt to log in to the Docker registry..."

    if echo "$pat_token" | docker login "${registry}" -u "${username}" --password-stdin; then
        echo "Successfully logged in to ${registry} as ${username}."
    else
        echo "Error: Failed to log in to ${registry} as ${username}."
        return 1
    fi
}

# These are common functions, make them available in subshell as well.
export -f find_file_upwards
export -f ensure_docker_available
export -f login_to_github_container_registry
