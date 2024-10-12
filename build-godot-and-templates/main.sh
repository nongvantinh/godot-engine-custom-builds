#!/usr/bin/env bash

set -e

usage() {
    echo "Usage: $0 godot [OPTIONS...]"
    echo
    echo "Available options for Godot:"
    echo "  --registry <registry>                Specify the Docker registry."
    echo "  --username <username>                Specify the username for the registry."
    echo "  --godot-version <version>            Specify the Godot version (e.g. 3.1-alpha5) [mandatory]."
    echo "  --git <treeish>                      Specify the git treeish (e.g. master)."
    echo "  --build <type>                       Specify the build type (all|classical|mono, default: $BUILD_TYPE)."
    echo "  --force                               Force redownload of all images."
    echo "  --skip                                Skip downloading."
    echo "  --skip-git-checkout                   Skip skip git checkout."
    echo "  -h, --help                                Show this help message."
    echo
}

pull_images() {
    if [ $skip_download_containers -eq 1 ]; then
        return
    fi
    
    echo "Fetching images"

    login_to_github_container_registry  --registry "${registry}"     \
                                        --username "${username}"     \
                                        --pat_token "${pat_token}"

    echo "Fetching images from GitHub Container Registry..."

    windows_container="${registry}/${username}/godot-windows:${container_version}-${image_version}"
    linux_container="${registry}/${username}/godot-linux:${container_version}-${image_version}"
    web_container="${registry}/${username}/godot-web:${container_version}-${image_version}"
    macos_container="${registry}/${username}/godot-osx:${container_version}-${image_version}"
    android_container="${registry}/${username}/godot-android:${container_version}-${image_version}"
    ios_container="${registry}/${username}/godot-ios:${container_version}-${image_version}"
    
    local images=(
        "$windows_container"
        "$linux_container"
        "$web_container"
        "$macos_container"
        "$android_container"
        "$ios_container"
    )

    for image in $images; do
        echo "Pulling image: $image"
        docker pull "$image"
    done

    echo "Local images:"
    docker images | grep "$registry/$username"
}

download_moltenvk() {
    echo "Downloading MoltenVK...."
    
    if [ ! -d "${basedir}/deps/moltenvk" ]; then
        echo "Missing MoltenVK for macOS, downloading it."
        mkdir -p "${basedir}/deps/moltenvk"
        pushd "${basedir}/deps/moltenvk"
        curl -L -o moltenvk.tar https://github.com/godotengine/moltenvk-osxcross/releases/download/vulkan-sdk-1.3.283.0-2/MoltenVK-all.tar
        tar xf moltenvk.tar && rm -f moltenvk.tar
        mv MoltenVK/MoltenVK/include/ MoltenVK/
        mv MoltenVK/MoltenVK/static/MoltenVK.xcframework/ MoltenVK/
        popd
    fi
}

download_angle() {
    echo "Downloading ANGLE..."

    if [ ! -d "${basedir}/deps/angle" ]; then
        echo "Downloading ANGLE libraries..."
        mkdir -p "${basedir}/deps/angle"
        pushd "${basedir}/deps/angle"
        local base_url=https://github.com/godotengine/godot-angle-static/releases/download/chromium%2F6601.2/godot-angle-static
        curl -L -o windows_arm64.zip $base_url-arm64-llvm-release.zip
        curl -L -o windows_x86_64.zip $base_url-x86_64-gcc-release.zip
        curl -L -o windows_x86_32.zip $base_url-x86_32-gcc-release.zip
        curl -L -o macos_arm64.zip $base_url-arm64-macos-release.zip
        curl -L -o macos_x86_64.zip $base_url-x86_64-macos-release.zip
        unzip -o windows_arm64.zip && rm -f windows_arm64.zip
        unzip -o windows_x86_64.zip && rm -f windows_x86_64.zip
        unzip -o windows_x86_32.zip && rm -f windows_x86_32.zip
        unzip -o macos_arm64.zip && rm -f macos_arm64.zip
        unzip -o macos_x86_64.zip && rm -f macos_x86_64.zip
        popd
    fi
}

download_mesa() {
    echo "Downloading mesa..."

    if [ ! -d "${basedir}/deps/mesa" ]; then
      echo "Downloading Mesa libraries..."
      mkdir -p "${basedir}/deps/mesa"
      pushd "${basedir}/deps/mesa"
      curl -L -o mesa_arm64.zip https://github.com/godotengine/godot-nir-static/releases/download/23.1.9-1/godot-nir-static-arm64-llvm-release.zip
      curl -L -o mesa_x86_64.zip https://github.com/godotengine/godot-nir-static/releases/download/23.1.9-1/godot-nir-static-x86_64-gcc-release.zip
      curl -L -o mesa_x86_32.zip https://github.com/godotengine/godot-nir-static/releases/download/23.1.9-1/godot-nir-static-x86_32-gcc-release.zip
      unzip -o mesa_arm64.zip && rm -f mesa_arm64.zip
      unzip -o mesa_x86_64.zip && rm -f mesa_x86_64.zip
      unzip -o mesa_x86_32.zip && rm -f mesa_x86_32.zip
      popd
    fi
}

prepare_android_sign_keystore() {
    echo "Preparing android signing keystore..."

    if [ ! -d "${basedir}/deps/keystore" ]; then
        mkdir -p "${basedir}/deps/keystore"
        local config_sh_path
        local file="config.sh"
        find_file_upwards --file "${file}" --path-ref config_sh_path
        if [[ $? -eq 0 ]]; then
            echo "Copying $config_sh_path to ${basedir}/deps/keystore/"
            cp "$config_sh_path" "${basedir}/deps/keystore/"
        else
            echo "Failed to find $file"
        fi

        if [ ! -z "$GODOT_ANDROID_SIGN_KEYSTORE" ]; then
            cp "$GODOT_ANDROID_SIGN_KEYSTORE" "${basedir}/deps/keystore/"
            sed -i "${basedir}/deps/keystore/config.sh" -e "s@$GODOT_ANDROID_SIGN_KEYSTORE@/root/keystore/$GODOT_ANDROID_SIGN_KEYSTORE@"
        fi
    fi
}

prepare_godot_source() {
    echo "Preparing Godot source..."
    
    echo "Cloning Godot repository..."
    git clone "$godot_repository" "${basedir}/git" || true
    pushd "${basedir}/git"
    git checkout -b "${git_branch}" "origin/${git_branch}" || git checkout "${git_branch}"
    git reset --hard
    git clean -fdx
    git pull origin "${git_branch}" || true
    popd

    # Extract version information
    godot_version=$(python3 extract_version.py --get-version)
    godot_version_status=$(python3 extract_version.py --get-version-status)

    pushd "${basedir}/git"
    echo "Creating Godot tarball..."
    sh misc/scripts/make_tarball.sh -v "${godot_version}" -g "${git_branch}"

    popd
}

download_dependencies() {
    echo "Downloading dependencies..."

    mkdir -p ${basedir}/deps

    download_moltenvk
    download_angle
    download_mesa
    prepare_android_sign_keystore
    
    prepare_godot_source        
}

build() {
    echo "Building Godot engine and its templates"
    
    pull_images
    download_dependencies

    mkdir -p ${basedir}/out
    mkdir -p ${basedir}/out/logs

    local build_classical=0
    local build_mono=0

    if [[ "$build_type" == "all" ]]; then
        build_classical=1
        build_mono=1
    elif [[ "$build_type" == "classical" ]]; then
        build_classical=1
    elif [[ "$build_type" == "mono" ]]; then
        build_mono=1
    fi

    mkdir -p ${basedir}/mono-glue
    docker_run="docker run --rm --env BUILD_NAME="$build_name" --env GODOT_VERSION_STATUS="$godot_version_status" --env NUM_CORES="$num_cores" --env CLASSICAL=${build_classical} --env MONO=${build_mono} -v ${basedir}/godot-${godot_version}.tar.gz:/root/godot.tar.gz -v ${basedir}/mono-glue:/root/mono-glue -w /root/"

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

main() {
    basedir=$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)
    registry="${REGISTRY}"
    username="${USERNAME}"
    pat_token="${PAT_TOKEN}"
    godot_repository="$GODOT_REPOSITORY"
    container_version="${CONTAINER_VERSION}"
    image_version="${BASE_DISTRO}"
    git_branch="${GIT_BRANCH}"
    build_type="${BUILD_TYPE}"
    build_name="${BUILD_NAME}"
    skip_download_containers="${SKIP_DOWNLOAD_CONTAINERS}"
    num_cores="${NUM_CORES}"

    disable_cleanup=0
    disable_generate_tarball=0

    build=0
    release=0

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
            --pat-token)
                pat_token="$2"
                shift 2
                ;;
            --godot-repository)
                godot_repository="$2"
                shift 2
                ;;
            --container-version)
                container_version="$2"
                shift 2
                ;;
            --base-distro)
                image_version="$2"
                shift 2
                ;;
            --git-branch)
                git_branch="$2"
                shift 2
                ;;
            --build-type)
                build_type="$2"
                shift 2
                ;;
            --build-name)
                build_name="$2"
                shift 2
                ;;
            --skip-download-containers)
                skip_download_containers=1
                shift
                ;;
            --num-cores)
                num_cores="$2"
                shift
                ;;
            --disable-cleanup)
                disable_cleanup=1
                shift
                ;;
            --disable-generate-tarball)
                disable_generate_tarball=1
                shift
                ;;
            --build)
                build=1
                shift
                ;;
            --release)
                release=1
                shift
                ;;
            --clean-release)
                rm -rf ${basedir}/releases ${basedir}/tmp ${basedir}/web
                exit 0
                ;;
            --cleanup)
                rm -rf ${basedir}/godot*.tar.gz         \
                        ${basedir}/mono-glue            \
                        ${basedir}/out                  \
                        ${basedir}/releases             \
                        ${basedir}/tmp                  \
                        ${basedir}/web                  \
                        ${basedir}/git                  \
                        ${basedir}/sha512sums           \
                        ${basedir}/deps
                exit 0
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

    echo "Building Godot engine with the following parameters:"
    echo "Git branch: ${git_branch}"
    echo "Build type: $build_type"
    echo "Registry: ${registry:-Not specified}"
    echo "Username: ${username:-Not specified}"

    echo "Godot build command executed for ${build_type}."

    echo "number of cores will be used: ${num_cores}"

    if [ $build -eq 1 ]; then
        build
    fi

    if [ $release -eq 1 ]; then
        local release_sh_path
        local file="prepare_release.sh"
        find_file_upwards --file "${file}" --path-ref release_sh_path
        if [[ $? -eq 0 ]]; then
            chmod +x "$release_sh_path"
            $release_sh_path "$godot_version" "$godot_version_status" "$disable_cleanup" "$disable_generate_tarball"

        else
            echo "Failed to find $file"
        fi

    fi

}

main "$@"