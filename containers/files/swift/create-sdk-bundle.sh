#!/bin/bash
set -e

if [ $# -lt 2 ]; then
    echo "Usage: $0 <path-to-xcode-developer-dir> <output-dir>"
    echo "Example: $0 /path/to/Xcode.app/Contents/Developer ./sdk-bundle"
    exit 1
fi

DEV_DIR="$1"
OUTPUT="$2/darwin.artifactbundle"

echo "Creating artifact bundle at $OUTPUT"
mkdir -p "$OUTPUT"

# Find SDKs
find_sdk() {
    local platform=$1
    local prefix=$2
    local sdk_dir="$DEV_DIR/Platforms/$platform.platform/Developer/SDKs"
    ls -1 "$sdk_dir" 2>/dev/null | grep "^$prefix[0-9]" | head -1 || echo ""
}

IOS_SDK=$(find_sdk "iPhoneOS" "iPhoneOS")
SIM_SDK=$(find_sdk "iPhoneSimulator" "iPhoneSimulator")
MAC_SDK=$(find_sdk "MacOSX" "MacOSX")
TVOS_SDK=$(find_sdk "AppleTVOS" "AppleTVOS")
TVSIM_SDK=$(find_sdk "AppleTVSimulator" "AppleTVSimulator")
XROS_SDK=$(find_sdk "XROS" "XROS")
XRSIM_SDK=$(find_sdk "XRSimulator" "XRSimulator")

echo "Found SDKs:"
[ -n "$IOS_SDK" ] && echo "  iOS: $IOS_SDK"
[ -n "$SIM_SDK" ] && echo "  iOS Sim: $SIM_SDK"
[ -n "$MAC_SDK" ] && echo "  macOS: $MAC_SDK"
[ -n "$TVOS_SDK" ] && echo "  tvOS: $TVOS_SDK"
[ -n "$TVSIM_SDK" ] && echo "  tvOS Sim: $TVSIM_SDK"
[ -n "$XROS_SDK" ] && echo "  visionOS: $XROS_SDK"
[ -n "$XRSIM_SDK" ] && echo "  visionOS Sim: $XRSIM_SDK"

# Symlink Developer directory
ln -sf "$DEV_DIR" "$OUTPUT/Developer"

# Generate info.json
cat > "$OUTPUT/info.json" <<EOF
{
    "schemaVersion": "1.0",
    "artifacts": {
        "darwin": {
            "type": "swiftSDK",
            "version": "0.0.1",
            "variants": [
                {
                    "path": ".",
                    "supportedTriples": ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"]
                }
            ]
        }
    }
}
EOF

# Generate toolset.json
cat > "$OUTPUT/toolset.json" <<EOF
{
    "schemaVersion": "1.0",
    "rootPath": "toolset/bin",
    "linker": {
        "path": "ld64.lld"
    },
    "swiftCompiler": {
        "extraCLIOptions": [
            "-Xfrontend", "-enable-cross-import-overlays",
            "-use-ld=lld"
        ]
    }
}
EOF

# Generate swift-sdk.json
cat > "$OUTPUT/swift-sdk.json" <<EOF
{
    "schemaVersion": "4.0",
    "targetTriples": {
EOF

first=true
add_triple() {
    local triple=$1 platform=$2 sdk=$3
    [ -z "$sdk" ] && return
    [ "$first" = false ] && echo "," >> "$OUTPUT/swift-sdk.json"
    first=false
    cat >> "$OUTPUT/swift-sdk.json" <<TRIPLE
        "$triple": {
            "sdkRootPath": "Developer/Platforms/$platform.platform/Developer/SDKs/$sdk",
            "includeSearchPaths": ["Developer/Platforms/$platform.platform/Developer/usr/lib"],
            "librarySearchPaths": ["Developer/Platforms/$platform.platform/Developer/usr/lib"],
            "swiftResourcesPath": "Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift",
            "swiftStaticResourcesPath": "Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift_static",
            "toolsetPaths": ["toolset.json"]
        }
TRIPLE
}

add_triple "arm64-apple-ios" "iPhoneOS" "$IOS_SDK"
add_triple "arm64-apple-ios-simulator" "iPhoneSimulator" "$SIM_SDK"
add_triple "x86_64-apple-ios-simulator" "iPhoneSimulator" "$SIM_SDK"
add_triple "arm64-apple-macosx" "MacOSX" "$MAC_SDK"
add_triple "x86_64-apple-macosx" "MacOSX" "$MAC_SDK"
add_triple "arm64-apple-tvos" "AppleTVOS" "$TVOS_SDK"
add_triple "arm64-apple-tvos-simulator" "AppleTVSimulator" "$TVSIM_SDK"
add_triple "x86_64-apple-tvos-simulator" "AppleTVSimulator" "$TVSIM_SDK"
add_triple "arm64-apple-xros" "XROS" "$XROS_SDK"
add_triple "arm64-apple-xros-simulator" "XRSimulator" "$XRSIM_SDK"

cat >> "$OUTPUT/swift-sdk.json" <<EOF

    }
}
EOF
