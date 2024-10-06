#!/usr/bin/env bash

set -e

usage() {
    echo "Available commands:"
    echo "  docker                     Build Docker containers."
    echo "  godot                      Build Godot engines and templates."
    echo "  -h, --help                 Show this help message."
    echo
    echo "For more help on specific commands, use: $0 <command> -h or --help"
    echo
}

initialize() {
    chmod +x shared.sh
    source shared.sh
    
    local config_sh_path
    local file="config.sh"
    find_file_upwards --file "${file}" --path-ref config_sh_path
    if [[ $? -eq 0 ]]; then
        chmod +x "$config_sh_path"
        source "$config_sh_path"
    else
        echo "Failed to find $file"
    fi

    ensure_docker_available
}

main() {
    initialize

    if [[ $# -eq 0 ]]; then
        usage
        read -p "Please enter a command (docker or godot): " command
        set -- "$command"
    fi

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            docker)
                shift
                pushd "build-containers/" > /dev/null || exit 1
                if [[ -e "./main.sh" ]]; then
                    chmod +x main.sh
                    ./main.sh "$@"
                    break
                else
                    echo "Error: main.sh is not executable or does not exist."
                    exit 1
                fi
                popd > /dev/null || exit 1
                ;;
            godot)
                shift
                pushd "build-godot-and-templates/" > /dev/null || exit 1
                if [[ -e "./main.sh" ]]; then
                    chmod +x main.sh
                    ./main.sh "$@"
                    break
                else
                    echo "Error: main.sh is not executable or does not exist."
                    exit 1
                fi
                popd > /dev/null || exit 1
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
}

# Start the script
main "$@"
