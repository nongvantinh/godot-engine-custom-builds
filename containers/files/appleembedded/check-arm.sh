#!/usr/bin/env bash

function check_toolchain
{
    local platform=$1
    local sdk_prefix=$2
    # if $3 is true, use the simulator SDK
    if [ "$3" == "true" ]; then
        SDK_DIR="/root/Xcode.app/Contents/Developer/Platforms/${sdk_prefix}Simulator.platform/Developer/SDKs/${sdk_prefix}Simulator.sdk"
        TARGET_OS="${platform}${APPLE_SDKV}-simulator"
        NAME="${platform} (Simulator)"
    else
        SDK_DIR="/root/Xcode.app/Contents/Developer/Platforms/${sdk_prefix}OS.platform/Developer/SDKs/${sdk_prefix}OS.sdk"
        TARGET_OS="${platform}${APPLE_SDKV}"
        NAME="${platform} (Device)"
    fi

    echo ""
    echo "*** checking ${NAME} toolchain ***"
    echo ""
    echo ""

    echo "int main(){return 0;}" | arm-apple-darwin11-clang -isysroot "$SDK_DIR" -mtargetos=${TARGET_OS} -xc -O2 -c -o test.o - || exit 1
    arm-apple-darwin11-ar rcs libtest.a test.o || exit 1
    rm test.o libtest.a
    echo "${NAME} toolchain OK"
}

check_toolchain "ios" "iPhone" false
check_toolchain "tvos" "AppleTV" false
check_toolchain "xros" "XR" false
# Check for simulator toolchains
check_toolchain "ios" "iPhone" true
check_toolchain "tvos" "AppleTV" true
check_toolchain "xros" "XR" true

