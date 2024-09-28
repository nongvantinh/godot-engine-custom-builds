#!/bin/bash

set -e
set -x  # Enable debugging

# Set the Godot version and image version (these can be passed as arguments if needed)
GODOT_VERSION="4.3.1"
IMG_VERSION="f40"
export NUM_CORES=$(nproc --ignore=1)
echo "number of cores will be used: ${NUM_CORES}"
basedir=$(pwd)
git_treeish="4.3"  # You can change this as needed or pass it as an argument
CONTAINER_VERSION="4.3"
build_classical=1
build_mono=0

# Set up necessary directories for dependencies and outputs
mkdir -p ${basedir}/deps
mkdir -p ${basedir}/out
mkdir -p ${basedir}/out/logs

# Function to download dependencies if missing
download_dependencies() {
  echo "Downloading dependencies"
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
prepare_godot_source() {
  echo "Prepare godot source"
  if [ ! -f "${basedir}/godot-${GODOT_VERSION}.tar.gz" ]; then
    echo "Cloning Godot repository..."
    git clone https://github.com/godotengine/godot.git || true
    pushd godot
    git checkout -b ${git_treeish} origin/${git_treeish} || git checkout ${git_treeish}
    git reset --hard
    git clean -fdx
    git pull origin ${git_treeish} || true
    
    # Validate version
    correct_version=$(python3 << EOF
import version;
if hasattr(version, "patch") and version.patch != 0:
  git_version = f"{version.major}.{version.minor}.{version.patch}"
else:
  git_version = f"{version.major}.{version.minor}"
print(git_version == "${GODOT_VERSION}")
EOF
    )
    
    if [[ "$correct_version" != "True" ]]; then
      echo "Version in version.py $correct_version doesn't match the passed ${GODOT_VERSION}."
      exit 1
    fi
    
    # Create tarball
    echo "Creating Godot tarball..."
    sh misc/scripts/make_tarball.sh -v ${GODOT_VERSION} -g ${git_treeish}
    popd
  fi
}

# Download dependencies if not present
download_dependencies

# Prepare Godot source tarball if not present
prepare_godot_source

WINDOWS_CONTAINER="ghcr.io/nongvantinh/godot-windows:${CONTAINER_VERSION}-${IMG_VERSION}"
LINUX_CONTAINER="ghcr.io/nongvantinh/godot-linux:${CONTAINER_VERSION}-${IMG_VERSION}"
WEB_CONTAINER="ghcr.io/nongvantinh/godot-web:${CONTAINER_VERSION}-${IMG_VERSION}"
# MACOS_CONTAINER="ghcr.io/nongvantinh/godot-osx:${CONTAINER_VERSION}-${IMG_VERSION}"
# ANDROID_CONTAINER="ghcr.io/nongvantinh/godot-android:${CONTAINER_VERSION}-${IMG_VERSION}"
# IOS_CONTAINER="ghcr.io/nongvantinh/godot-ios:${CONTAINER_VERSION}-${IMG_VERSION}"
# echo "Running Dockers"

docker_run="docker run --rm --env BUILD_NAME --env GODOT_VERSION_STATUS --env NUM_CORES --env CLASSICAL=${build_classical} --env MONO=${build_mono} -v ${basedir}/godot-${GODOT_VERSION}.tar.gz:/root/godot.tar.gz -v ${basedir}/mono-glue:/root/mono-glue -w /root/"

mkdir -p ${basedir}/mono-glue
${docker_run} -v ${basedir}/build-mono-glue:/root/build ${LINUX_CONTAINER} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/mono-glue

mkdir -p ${basedir}/out/windows
${docker_run} -v ${basedir}/build-windows:/root/build -v ${basedir}/out/windows:/root/out -v ${basedir}/deps/angle:/root/angle -v ${basedir}/deps/mesa:/root/mesa --env STEAM=${build_steam} ${WINDOWS_CONTAINER} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/windows

mkdir -p ${basedir}/out/linux
${docker_run} -v ${basedir}/build-linux:/root/build -v ${basedir}/out/linux:/root/out ${LINUX_CONTAINER} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/linux

mkdir -p ${basedir}/out/web
${docker_run} -v ${basedir}/build-web:/root/build -v ${basedir}/out/web:/root/out ${WEB_CONTAINER} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/web

# mkdir -p ${basedir}/out/macos
# ${docker_run} -v ${basedir}/build-macos:/root/build -v ${basedir}/out/macos:/root/out -v ${basedir}/deps/moltenvk:/root/moltenvk -v ${basedir}/deps/angle:/root/angle ${MACOS_CONTAINER} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/macos

# mkdir -p ${basedir}/out/android
# ${docker_run} -v ${basedir}/build-android:/root/build -v ${basedir}/out/android:/root/out -v ${basedir}/deps/keystore:/root/keystore ${ANDROID_CONTAINER} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/android

# mkdir -p ${basedir}/out/ios
# ${docker_run} -v ${basedir}/build-ios:/root/build -v ${basedir}/out/ios:/root/out ${IOS_CONTAINER} bash build/build.sh 2>&1 | tee ${basedir}/out/logs/ios

if [ ! -z "$SUDO_UID" ]; then
  chown -R "${SUDO_UID}:${SUDO_GID}" ${basedir}/git ${basedir}/out ${basedir}/mono-glue ${basedir}/godot*.tar.gz
fi

