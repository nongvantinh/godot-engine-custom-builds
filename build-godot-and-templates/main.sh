#!/usr/bin/env bash

set -e

godot_usage() {
    echo "Usage: $0 godot [OPTIONS...]"
    echo
    echo "Available options for Godot:"
    echo "  -r, --registry <registry>                Specify the Docker registry."
    echo "  -u, --username <username>                Specify the username for the registry."
    echo "  -p, --password <password>                Specify the password for the registry."
    echo "  -gv, --godot_version <version>            Specify the Godot version (e.g. 3.1-alpha5) [mandatory]."
    echo "  -g, --git <treeish>                      Specify the git treeish (e.g. master)."
    echo "  -b, --build <type>                       Specify the build type (all|classical|mono, default: $BUILD_TYPE)."
    echo "  -f, --force                               Force redownload of all images."
    echo "  -s, --skip                                Skip downloading."
    echo "  -c, --skip-git-checkout                   Skip skip git checkout."
    echo "  -h, --help                                Show this help message."
    echo
}

godot_initialize() {
    local setup_sh_path
    local setup_file="setup.sh"
    find_file_upwards --file "$setup_file" --path-ref setup_sh_path
    if [[ $? -eq 0 ]]; then
        chmod +x "$setup_sh_path"
        source "$setup_sh_path"
    else
        echo "Failed to find $setup_file"
    fi
    
    local config_sh_path
    local config_file="config.sh"
    find_file_upwards --file "$config_file" --path-ref config_sh_path
    if [[ $? -eq 0 ]]; then
        chmod +x "$setup_sh_path"
        source "$setup_sh_path"
    else
        echo "Failed to find $config_file"
    fi
}

# Function to download dependencies if missing
godot_download_dependencies() {
    echo "Downloading dependencies"
    local basedir
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --basedir)
                basedir="$2"
                shift 2
                ;;
            *)
                echo "Invalid option: $1"
                return 1
                ;;
        esac
    done

    mkdir -p ${basedir}/deps
    
    # Download ANGLE libraries
    if [ ! -d "${basedir}/deps/angle" ]; then
      echo "Downloading ANGLE libraries..."
      mkdir -p ${basedir}/deps/angle
      pushd ${basedir}/deps/angle
      base_url=https://github.com/godotengine/godot-angle-static/releases/download/chromium%2F6601.2/godot-angle-static
      curl -L -o windows_arm64.zip $base_url-arm64-llvm-release.zip
      curl -L -o windows_x86_64.zip $base_url-x86_64-gcc-release.zip
      curl -L -o windows_x86_32.zip $base_url-x86_32-gcc-release.zip
      unzip -o windows_arm64.zip && rm -f windows_arm64.zip
      unzip -o windows_x86_64.zip && rm -f windows_x86_64.zip
      unzip -o windows_x86_32.zip && rm -f windows_x86_32.zip
      popd
    fi

    # Download Mesa libraries
    if [ ! -d "${basedir}/deps/mesa" ]; then
      echo "Downloading Mesa libraries..."
      mkdir -p ${basedir}/deps/mesa
      pushd ${basedir}/deps/mesa
      curl -L -o mesa_arm64.zip https://github.com/godotengine/godot-nir-static/releases/download/23.1.9-1/godot-nir-static-arm64-llvm-release.zip
      curl -L -o mesa_x86_64.zip https://github.com/godotengine/godot-nir-static/releases/download/23.1.9-1/godot-nir-static-x86_64-gcc-release.zip
      curl -L -o mesa_x86_32.zip https://github.com/godotengine/godot-nir-static/releases/download/23.1.9-1/godot-nir-static-x86_32-gcc-release.zip
      unzip -o mesa_arm64.zip && rm -f mesa_arm64.zip
      unzip -o mesa_x86_64.zip && rm -f mesa_x86_64.zip
      unzip -o mesa_x86_32.zip && rm -f mesa_x86_32.zip
      popd
    fi
}

# Clone the Godot repository and create a tarball if needed
godot_prepare_godot_source() {
    local basedir
    local godot_version
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --basedir)
                basedir="$2"
                shift 2
                ;;
            --godot-version)
                godot_version="$2"
                shift 2
                ;;
            *)
                echo "Invalid option: $1"
                return 1
                ;;
        esac
    done

    echo "Prepare Godot source"
    
    if [ -f "${basedir}/godot-${godot_version}.tar.gz" ]; then
      echo "Tarball already exists. Skipping clone."
      return
    fi

    if [[ $skip_git_checkout == false ]]; then
      echo "Cloning Godot repository..."
      git clone https://github.com/godotengine/godot.git git || true
      pushd git
      git checkout -b "${git_treeish}" "origin/${git_treeish}" || git checkout "${git_treeish}"
      git reset --hard
      git clean -fdx
      git pull origin "${git_treeish}" || true
    
  # Validate version
      correct_version=$(python3 << EOF
    import version
    if hasattr(version, "patch") and version.patch != 0:
        git_version = f"{version.major}.{version.minor}.{version.patch}"
    else:
        git_version = f"{version.major}.{version.minor}"
    print(git_version == "${godot_version}")
EOF
    )

    if [[ "$correct_version" != "True" ]]; then
      echo "Version in version.py $correct_version doesn't match the passed ${godot_version}."
      exit 1
    fi

    
    # Create tarball
    echo "Creating Godot tarball..."
    sh misc/scripts/make_tarball.sh -v "${godot_version}" -g "${git_treeish}"
    
      popd
    fi
}

godot_build() {
    echo "Building Godot engine and its templates"

    local basedir
    local registry
    local username
    local contaner_version
    local img_version
    local godot_version
    local build_classical
    local build_mono
    local build_name="$BUILD_NAME"

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --basedir)
                basedir="$2"
                shift 2
                ;;
            --registry)
                registry="$2"
                shift 2
                ;;
            --username)
                username="$2"
                shift 2
                ;;
            --contaner-version)
                contaner_version="$2"
                shift 2
                ;;
            --image-version)
                img_version="$2"
                shift 2
                ;;
            --godot-version)
                godot_version="$2"
                shift 2
                ;;
            --build-classical)
                build_classical="$2"
                shift 2
                ;;
            --build-mono)
                build_mono="$2"
                shift 2
                ;;
            --build-name)
                build_name="$2"
                shift 2
                ;;
            -h|--help)
                godot_build_usage
                exit 0
                ;;
            *)
                echo "Invalid option: $1"
                godot_build_usage
                exit 1
                ;;
        esac
    done

    local windows_container="${registry}/${username}/godot-windows:${contaner_version}-${img_version}"
    local linux_container="${registry}/${username}/godot-linux:${contaner_version}-${img_version}"
    local web_container="${registry}/${username}/godot-web:${contaner_version}-${img_version}"
    local macos_container="${registry}/${username}/godot-osx:${contaner_version}-${img_version}"
    local android_container="${registry}/${username}/godot-android:${contaner_version}-${img_version}"
    local ios_container="${registry}/${username}/godot-ios:${contaner_version}-${img_version}"

    mkdir -p ${basedir}/out
    mkdir -p ${basedir}/out/logs

    docker_run="docker run --rm --env BUILD_NAME="$build_name" --env GODOT_VERSION_STATUS --env NUM_CORES --env CLASSICAL=${build_classical} --env MONO=${build_mono} -v ${basedir}/godot-${godot_version}.tar.gz:/root/godot.tar.gz -v ${basedir}/mono-glue:/root/mono-glue -w /root/"

    mkdir -p ${basedir}/mono-glue
    ${docker_run} -v ${basedir}/build-mono-glue:/root/build ${linux_container} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/mono-glue

    mkdir -p ${basedir}/out/windows
    ${docker_run} -v ${basedir}/build-windows:/root/build -v ${basedir}/out/windows:/root/out -v ${basedir}/deps/angle:/root/angle -v ${basedir}/deps/mesa:/root/mesa --env STEAM=${build_steam} ${windows_container} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/windows

    mkdir -p ${basedir}/out/linux
    ${docker_run} -v ${basedir}/build-linux:/root/build -v ${basedir}/out/linux:/root/out ${linux_container} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/linux

    mkdir -p ${basedir}/out/web
    ${docker_run} -v ${basedir}/build-web:/root/build -v ${basedir}/out/web:/root/out ${web_container} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/web

    mkdir -p ${basedir}/out/macos
    ${docker_run} -v ${basedir}/build-macos:/root/build -v ${basedir}/out/macos:/root/out -v ${basedir}/deps/moltenvk:/root/moltenvk -v ${basedir}/deps/angle:/root/angle ${macos_container} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/macos

    mkdir -p ${basedir}/out/android
    ${docker_run} -v ${basedir}/build-android:/root/build -v ${basedir}/out/android:/root/out -v ${basedir}/deps/keystore:/root/keystore ${android_container} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/android

    mkdir -p ${basedir}/out/ios
    ${docker_run} -v ${basedir}/build-ios:/root/build -v ${basedir}/out/ios:/root/out ${ios_container} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/ios

    if [ ! -z "$SUDO_UID" ]; then
      chown -R "${SUDO_UID}:${SUDO_GID}" ${basedir}/git ${basedir}/out ${basedir}/mono-glue ${basedir}/godot*.tar.gz
    fi
}

godot_main() {
    local basedir=$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)
    godot_initialize "$basedir"

    local godot_version="${GODOT_VERSION}"
    local git_treeish="${GIT_TREEISH}"
    local build_type="${BUILD_TYPE}"
    local registry="${REGISTRY}"
    local username="${USERNAME}"
    local password="${PASSWORD}"
    local force_download="${FORCE_DOWNLOAD}"
    local skip_download="${SKIP_DOWNLOAD}"
    local skip_git_checkout="${SKIP_GIT_CHECKOUT}"

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
            -p|--password)
                password="$2"
                shift 2
                ;;
            -gv|--godot_version)
                godot_version="$2"
                shift 2
                ;;
            -g|--git)
                git_treeish="$2"
                shift 2
                ;;
            -b|--build)
                build_type="$2"
                shift 2
                ;;
            -f|--force)
                force_download="true"
                shift
                ;;
            -s|--skip)
                skip_download="true"
                shift
                ;;
            -c|--skip-git-checkout)
                skip_git_checkout=true
                shift
                ;;
            -h|--help)
                godot_usage
                exit 0
                ;;
            *)
                echo "Invalid option: $1"
                godot_usage
                exit 1
                ;;
        esac
    done

    if [ -z "$godot_version" ]; then
        echo "Error: Godot version (-v) is mandatory."
        godot_usage
        exit 1
    fi

    echo "Building Godot engine with the following parameters:"
    echo "Version: $godot_version"
    echo "Git treeish: ${git_treeish}"
    echo "Build type: $build_type"
    echo "Registry: ${registry:-Not specified}"
    echo "Username: ${username:-Not specified}"

    # Replace the following line with your Godot build command
    # godot -build ... --godot_version "$godot_version" --type "$build_type"
    echo "Godot build command executed with type $build_type and version $godot_version."

    echo "number of cores will be used: ${NUM_CORES}"
    build_classical=1
    build_mono=0

    godot_download_dependencies --basedir "$basedir"
    godot_prepare_godot_source  --basedir "$basedir"  --godot-version "$godot_version"
    godot_build --basedir "$basedir"                  \
                --registry "$registry"                \
                --username "$username"                \
                --contaner-version "$godot_branch"    \
                --image-version "$base_distro"        \
                --godot-version "$godot_version"      \
                --build-classical "$build_classical"  \
                --build-mono "$build_mono"            \
}

godot_main "$@"