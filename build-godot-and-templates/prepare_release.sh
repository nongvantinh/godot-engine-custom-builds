#!/usr/bin/env bash

sign_windows() {
    can_sign_windows=0
    if [ ! -z "${SIGN_KEYSTORE}" ] && [ ! -z "${SIGN_PASSWORD}" ] && [[ $(type -P "osslsigncode") ]]; then
        can_sign_windows=1
    else
        echo "Disabling Windows binary signing as config.sh does not define the required data (SIGN_KEYSTORE, SIGN_PASSWORD), or osslsigncode can't be found in PATH."
    fi

    if [ $can_sign_windows == 0 ]; then
        return
    fi

    osslsigncode sign -pkcs12 ${SIGN_KEYSTORE} -pass "${SIGN_PASSWORD}" -n "${SIGN_NAME}" -i "${SIGN_URL}" -t http://timestamp.comodoca.com -in $1 -out $1-signed
    mv $1-signed $1
}

publish_nuget_packages() {
  echo "NUGET_API_KEY: ${NUGET_API_KEY}"

    can_publish_nuget=0
    if [ ! -z "${NUGET_SOURCE}" ] && [ ! -z "${NUGET_API_KEY}" ] && [[ $(type -P "dotnet") ]]; then
        can_publish_nuget=1
    else
        echo "Disabling NuGet package publishing as config.sh does not define the required data (NUGET_SOURCE, NUGET_API_KEY), or dotnet can't be found in PATH."
        return
    fi

    if dotnet nuget list source | grep -q "$NUGET_SOURCE"; then
        echo "Source '$NUGET_SOURCE' already exists. Skip adding this source"
    else
        echo "Adding $NUGET_SOURCE source..."
        dotnet nuget add source --name "$NUGET_SOURCE" "$NUGET_SOURCE_URL"
    fi

    for pkg in "$@"; do
        dotnet nuget push $pkg --source "${NUGET_SOURCE}" --api-key "${NUGET_API_KEY}" --skip-duplicate
    done
}

publish_maven_library() {
    can_publish_maven=0
    if [ ! -d "${basedir}/deps/keystore" ]; then
        echo "Disabling Android library publishing as ${basedir}/deps/keystore doesn't exist."
    else
        can_publish_maven=1
    fi

    if [ $can_publish_maven == 0 ]; then
        return
    fi
    # FIXME: Might be worth reworking the script to make it all sudo-safe and use appropriate users throughout.
    sudo sh build-android/upload-mavencentral.sh
}


cleanup_and_setup() {
    echo "Cleanup and setup"
    if [ $disable_cleanup -eq 0 ]; then
        echo "Cleaning old folders"

        rm -rf ${webdir}
        rm -rf ${relative_directory}
        rm -rf ${temporary_directory}

        echo "Setting up folders"
        mkdir -p ${webdir}
        mkdir -p ${relative_directory}
        mkdir -p ${reldir_mono}
        mkdir -p ${templatesdir}
        mkdir -p ${templatesdir_mono}

    fi
    echo "All folders have been setup"
}

make_tarball() {
    # Tarball

    if [ $disable_generate_tarball -eq 0 ]; then

        zcat godot-${binaries_version}.tar.gz | xz -c > ${relative_directory}/godot-${binaries_version}.tar.xz
        pushd ${relative_directory}
        sha256sum godot-${binaries_version}.tar.xz > godot-${binaries_version}.tar.xz.sha256
        popd

    fi

}
prepare_for_linux_classical() {
    ## Linux (Classical) ##

    echo "Preparing Linux Classical files..."

    # Define architectures and their paths
    declare -A architectures=(
        ["x86_64"]="out/linux/x86_64"
        ["x86_32"]="out/linux/x86_32"
        ["arm64"]="out/linux/arm64"
        ["arm32"]="out/linux/arm32"
    )

    # Process each architecture for the editor
    for arch in "${!architectures[@]}"; do
        binname="${godot_basename}_linux.${arch}"
        
        echo "Copying editor for ${arch}..."
        cp "${architectures[$arch]}/tools/godot.linuxbsd.editor.${arch}" "${binname}"
        zip -q -9 "${relative_directory}/${binname}.zip" "${binname}"
        rm "${binname}"
    done

    # Process templates
    echo "Copying templates..."
    for arch in "${!architectures[@]}"; do
        cp "${architectures[$arch]}/templates/godot.linuxbsd.template_release.${arch}" "${templatesdir}/linux_release.${arch}"
        cp "${architectures[$arch]}/templates/godot.linuxbsd.template_debug.${arch}" "${templatesdir}/linux_debug.${arch}"
    done

    echo "Linux Classical preparation complete."
}
prepare_for_windows_classical() {
    ## Windows (Classical) ##

    echo "Preparing Windows Classical files..."

    # Define architectures and their paths
    declare -A architectures=(
        ["x86_64"]="out/windows/x86_64"
        ["x86_32"]="out/windows/x86_32"
        ["arm64"]="out/windows/arm64"
    )

    # Process each architecture for the editor
    for arch in "${!architectures[@]}"; do
        binname="${godot_basename}_win${arch}.exe"
        wrpname="${godot_basename}_win${arch}_console.exe"

        echo "Copying and signing editor for ${arch}..."
        cp "${architectures[$arch]}/tools/godot.windows.editor.${arch}.exe" "${binname}"
        sign_windows "${binname}"
        cp "${architectures[$arch]}/tools/godot.windows.editor.${arch}.console.exe" "${wrpname}"
        sign_windows "${wrpname}"

        echo "Zipping ${binname} and ${wrpname}..."
        zip -q -9 "${relative_directory}/${binname}.zip" "${binname}" "${wrpname}"
        rm "${binname}" "${wrpname}"
    done

    # Process templates
    echo "Copying templates..."
    for arch in "${!architectures[@]}"; do
        cp "${architectures[$arch]}/templates/godot.windows.template_release.${arch}.exe" "${templatesdir}/windows_release_${arch}.exe"
        cp "${architectures[$arch]}/templates/godot.windows.template_debug.${arch}.exe" "${templatesdir}/windows_debug_${arch}.exe"
        cp "${architectures[$arch]}/templates/godot.windows.template_release.${arch}.console.exe" "${templatesdir}/windows_release_${arch}_console.exe"
        cp "${architectures[$arch]}/templates/godot.windows.template_debug.${arch}.console.exe" "${templatesdir}/windows_debug_${arch}_console.exe"
    done

    echo "Windows Classical preparation complete."
}

prepare_for_macos_classical() {
    ## macOS (Classical) ##

    echo "Preparing macOS Classical files..."

    # Editor
    binname="${godot_basename}_macos.universal"
    echo "Setting up Godot.app..."
    rm -rf Godot.app
    cp -r git/misc/dist/macos_tools.app Godot.app
    mkdir -p Godot.app/Contents/MacOS
    cp out/macos/tools/godot.macos.editor.universal Godot.app/Contents/MacOS/Godot
    chmod +x Godot.app/Contents/MacOS/Godot
    zip -q -9 -r "${relative_directory}/${binname}.zip" Godot.app
    rm -rf Godot.app
    sign_macos "${relative_directory}" "${binname}" 0

    # Templates
    echo "Setting up macos_template.app..."
    rm -rf macos_template.app
    cp -r git/misc/dist/macos_template.app .
    mkdir -p macos_template.app/Contents/MacOS

    for type in release debug; do
        cp out/macos/templates/godot.macos.template_${type}.universal macos_template.app/Contents/MacOS/godot_macos_${type}.universal
    done

    chmod +x macos_template.app/Contents/MacOS/godot_macos*
    zip -q -9 -r "${templatesdir}/macos.zip" macos_template.app
    rm -rf macos_template.app
    sign_macos_template "${templatesdir}" 0

    ## Web (Classical) ##
    echo "Preparing Web Classical files..."

    # Editor
    echo "Unzipping Web editor..."
    unzip out/web/tools/godot.web.editor.wasm32.zip -d "${webdir}/"
    brotli --keep --force --quality=11 "${webdir}"/*
    binname="${godot_basename}_web_editor.zip"
    cp out/web/tools/godot.web.editor.wasm32.zip "${relative_directory}/${binname}"

    # Templates
    echo "Copying Web templates..."
    for variant in release debug; do
        cp out/web/templates/godot.web.template_${variant}.wasm32.zip "${templatesdir}/web_${variant}.zip"
        cp out/web/templates/godot.web.template_${variant}.wasm32.nothreads.zip "${templatesdir}/web_nothreads_${variant}.zip"
        cp out/web/templates/godot.web.template_${variant}.wasm32.dlink.zip "${templatesdir}/web_dlink_${variant}.zip"
        cp out/web/templates/godot.web.template_${variant}.wasm32.nothreads.dlink.zip "${templatesdir}/web_dlink_nothreads_${variant}.zip"
    done
}

prepare_for_android_classical() {
    ## Android (Classical) ##

    echo "Preparing Android Classical files..."

    # Lib for direct download
    echo "Copying Android library..."
    cp out/android/templates/godot-lib.template_release.aar "${relative_directory}/godot-lib.${templates_version}.template_release.aar"

    # Editor
    echo "Copying Android editor files..."
    local editors=("android_editor.apk" "android_editor_horizonos.apk" "android_editor.aab")

    for editor in "${editors[@]}"; do
        local binname="${godot_basename}_${editor}"
        cp "out/android/tools/${editor}" "${relative_directory}/${binname}"
    done

    # Templates
    echo "Copying Android templates..."
    cp out/android/templates/*.apk "${templatesdir}/"
    cp out/android/templates/android_source.zip "${templatesdir}/"
}

prepare_for_ios_classical() {
    echo "Preparing iOS Classical files..."

    # Remove existing directory and copy new one
    rm -rf ios_xcode
    cp -r git/misc/dist/ios_xcode ios_xcode

    # Define source and destination paths
    declare -A ios_files=(
        [libgodot.ios.simulator.a]="libgodot.ios.release.xcframework/ios-arm64_x86_64-simulator/libgodot.a"
        [libgodot.ios.debug.simulator.a]="libgodot.ios.debug.xcframework/ios-arm64_x86_64-simulator/libgodot.a"
        [libgodot.ios.a]="libgodot.ios.release.xcframework/ios-arm64/libgodot.a"
        [libgodot.ios.debug.a]="libgodot.ios.debug.xcframework/ios-arm64/libgodot.a"
    )

    echo "Copying iOS libraries..."
    for src in "${!ios_files[@]}"; do
        cp "out/ios/templates/$src" "ios_xcode/${ios_files[$src]}"
    done

    # Copy MoltenVK and clean up
    echo "Copying MoltenVK framework..."
    cp -r deps/moltenvk/MoltenVK/MoltenVK.xcframework ios_xcode/
    rm -rf ios_xcode/MoltenVK.xcframework/{macos,tvos}*

    # Create zip archive
    echo "Creating zip archive..."
    (cd ios_xcode && zip -q -9 -r "${templatesdir}/ios.zip" *)
    
    # Cleanup
    rm -rf ios_xcode
}

post_preparation_for_classical() {
    echo "Finalizing preparation..."

    echo "${templates_version}" > "${templatesdir}/version.txt"
    
    echo "Creating TPZ archive..."
    pushd "${templatesdir}/.." > /dev/null
    zip -q -9 -r -D "${relative_directory}/${godot_basename}_export_templates.tpz" templates/*
    popd > /dev/null

    ## SHA-512 sums (Classical) ##
    echo "Generating SHA-512 sums..."
    pushd "${relative_directory}" > /dev/null
    sha512sum [Gg]* > SHA512-SUMS.txt
    mkdir -p "${basedir}/sha512sums/${binaries_version}"
    cp SHA512-SUMS.txt "${basedir}/sha512sums/${binaries_version}/"
    popd > /dev/null
}

prepare_for_linux_mono() {
    echo "Preparing for Linux Mono..."
    # Define the architectures and their corresponding paths
    declare -A architectures=(
        ["x86_64"]="out/linux/x86_64/tools-mono/godot.linuxbsd.editor.x86_64.mono"
        ["x86_32"]="out/linux/x86_32/tools-mono/godot.linuxbsd.editor.x86_32.mono"
        ["arm64"]="out/linux/arm64/tools-mono/godot.linuxbsd.editor.arm64.mono"
        ["arm32"]="out/linux/arm32/tools-mono/godot.linuxbsd.editor.arm32.mono"
    )

    # Prepare binaries for each architecture
    for arch in "${!architectures[@]}"; do
        binbasename="${godot_basename}_mono_linux"
        echo "Creating directory for ${binbasename}_${arch}..."
        
        mkdir -p "${binbasename}_${arch}"
        echo "Copying binary for ${arch}..."
        cp "${architectures[$arch]}" "${binbasename}_${arch}/${binbasename}.${arch}"

        echo "Copying GodotSharp for ${arch}..."
        cp -rp "out/linux/${arch}/tools-mono/GodotSharp" "${binbasename}_${arch}/"

        echo "Zipping ${binbasename}_${arch}..."
        zip -r -q -9 "${reldir_mono}/${binbasename}_${arch}.zip" "${binbasename}_${arch}"

        echo "Cleaning up temporary files for ${arch}..."
        rm -rf "${binbasename}_${arch}"

        echo "${binbasename}_${arch}: Done"
    done

    # Prepare templates
    echo "Copying templates..."
    declare -A templates=(
        ["x86_64"]="debug release"
        ["x86_32"]="debug release"
        ["arm64"]="debug release"
        ["arm32"]="debug release"
    )

    for arch in "${!templates[@]}"; do
        for template in ${templates[$arch]}; do
            src="out/linux/${arch}/templates-mono/godot.linuxbsd.template_${template}.${arch}.mono"
            dest="${templatesdir_mono}/linux_${template}.${arch}"
            echo "Copying ${template} template for ${arch}..."
            cp "$src" "$dest"
        done
    done

    echo "Templates copied successfully."
    
    echo "Preparation for Linux Mono: Complete."
}

prepare_for_windows_mono() {
    # Define architectures and their corresponding tools
    declare -A architectures=(
        ["x86_64"]="out/windows/x86_64"
        ["x86_32"]="out/windows/x86_32"
        ["arm64"]="out/windows/arm64"
    )

    # Process each architecture
    for arch in "${!architectures[@]}"; do
        binname="${godot_basename}_mono_win${arch}"
        wrpname="${binname}_console"
        
        echo "Preparing binaries for ${arch}..."

        mkdir -p "${binname}"
        cp "${architectures[$arch]}/tools-mono/godot.windows.editor.${arch}.mono.exe" "${binname}/${binname}.exe"
        sign_windows "${binname}/${binname}.exe"
        cp -rp "${architectures[$arch]}/tools-mono/GodotSharp" "${binname}/"
        cp "${architectures[$arch]}/tools-mono/godot.windows.editor.${arch}.mono.console.exe" "${binname}/${wrpname}.exe"
        sign_windows "${binname}/${wrpname}.exe"
        zip -r -q -9 "${reldir_mono}/${binname}.zip" "${binname}"
        rm -rf "${binname}"

        echo "Binaries for ${arch} prepared."
    done

    # Prepare templates
    echo "Copying templates..."
    for arch in "${!architectures[@]}"; do
        cp "${architectures[$arch]}/templates-mono/godot.windows.template_debug.${arch}.mono.exe" "${templatesdir_mono}/windows_debug_${arch}.exe"
        cp "${architectures[$arch]}/templates-mono/godot.windows.template_release.${arch}.mono.exe" "${templatesdir_mono}/windows_release_${arch}.exe"
        cp "${architectures[$arch]}/templates-mono/godot.windows.template_debug.${arch}.mono.console.exe" "${templatesdir_mono}/windows_debug_${arch}_console.exe"
        cp "${architectures[$arch]}/templates-mono/godot.windows.template_release.${arch}.mono.console.exe" "${templatesdir_mono}/windows_release_${arch}_console.exe"
    done

    echo "Templates copied successfully."
}

prepare_for_macos_mono() {
    ## macOS (Mono) ##

    # Editor
    binname="${godot_basename}_mono_macos.universal"
    rm -rf Godot_mono.app
    cp -r git/misc/dist/macos_tools.app Godot_mono.app
    mkdir -p Godot_mono.app/Contents/{MacOS,Resources}
    cp out/macos/tools-mono/godot.macos.editor.universal.mono Godot_mono.app/Contents/MacOS/Godot
    cp -rp out/macos/tools-mono/GodotSharp Godot_mono.app/Contents/Resources/GodotSharp
    chmod +x Godot_mono.app/Contents/MacOS/Godot
    zip -q -9 -r "${reldir_mono}/${binname}.zip" Godot_mono.app
    rm -rf Godot_mono.app
    sign_macos ${reldir_mono} ${binname} 1

    # Templates
    rm -rf macos_template.app
    cp -r git/misc/dist/macos_template.app .
    mkdir -p macos_template.app/Contents/{MacOS,Resources}
    cp out/macos/templates-mono/godot.macos.template_debug.universal.mono macos_template.app/Contents/MacOS/godot_macos_debug.universal
    cp out/macos/templates-mono/godot.macos.template_release.universal.mono macos_template.app/Contents/MacOS/godot_macos_release.universal
    chmod +x macos_template.app/Contents/MacOS/godot_macos*
    zip -q -9 -r "${templatesdir_mono}/macos.zip" macos_template.app
    rm -rf macos_template.app
    sign_macos_template ${templatesdir_mono} 1

}

prepare_for_android_mono() {
    ## Android (Mono) ##
    
    echo "Preparing Android Mono files..."

    # Copy lib for direct download
    cp out/android/templates-mono/godot-lib.template_release.aar "${reldir_mono}/godot-lib.${templates_version}.mono.template_release.aar"

    # Copy templates
    echo "Copying Android templates..."
    cp out/android/templates-mono/*.apk "${templatesdir_mono}/"
    cp out/android/templates-mono/android_source.zip "${templatesdir_mono}/"

    echo "Android Mono preparation complete."
}

prepare_for_ios_mono() {
    ## iOS (Mono) ##

    echo "Preparing iOS Mono files..."

    # Clean up and prepare iOS Xcode directory
    rm -rf ios_xcode
    cp -r git/misc/dist/ios_xcode ios_xcode

    # Define libraries to copy
    declare -A libs=(
        ["libgodot.ios.simulator.a"]="libgodot.ios.release.xcframework/ios-arm64_x86_64-simulator/libgodot.a"
        ["libgodot.ios.debug.simulator.a"]="libgodot.ios.debug.xcframework/ios-arm64_x86_64-simulator/libgodot.a"
        ["libgodot.ios.a"]="libgodot.ios.release.xcframework/ios-arm64/libgodot.a"
        ["libgodot.ios.debug.a"]="libgodot.ios.debug.xcframework/ios-arm64/libgodot.a"
    )

    # Copy libraries to the appropriate locations
    for lib in "${!libs[@]}"; do
        echo "Copying ${lib}..."
        cp "out/ios/templates-mono/${lib}" "ios_xcode/${libs[$lib]}"
    done

    # Copy MoltenVK framework
    echo "Copying MoltenVK framework..."
    cp -r deps/moltenvk/MoltenVK/MoltenVK.xcframework ios_xcode/
    rm -rf ios_xcode/MoltenVK.xcframework/{macos,tvos}*

    # Zip the ios_xcode directory
    cd ios_xcode || exit
    echo "Zipping iOS files..."
    zip -q -9 -r "${templatesdir_mono}/ios.zip" *
    cd .. || exit

    # Clean up
    rm -rf ios_xcode
    echo "iOS Mono preparation complete."
}

prepare_for_web_mono() {
    ## Web (Mono) ##

    echo "Preparing Web Mono files..."

    # Templates
    echo "Copying web templates..."
    cp out/web/templates-mono/godot.web.template_debug.wasm32.mono.zip "${templatesdir_mono}/web_debug.zip"
    cp out/web/templates-mono/godot.web.template_release.wasm32.mono.zip "${templatesdir_mono}/web_release.zip"

    echo "Web Mono preparation complete."
}

prepare_for_classical() {
    prepare_for_linux_classical
    prepare_for_windows_classical
    prepare_for_macos_classical
    prepare_for_android_classical
    prepare_for_ios_classical
    prepare_for_web_classical

    post_preparation_for_classical
}

prepare_for_mono() {
    prepare_for_linux_mono
    prepare_for_windows_mono
    prepare_for_macos_mono
    prepare_for_android_mono
    prepare_for_ios_mono
    prepare_for_web_mono

    post_preparation_for_mono
}

post_preparation_for_mono() {
    ## Templates TPZ (Mono) ##
    echo "Creating version file..."
    echo "${templates_version}.mono" > "${templatesdir_mono}/version.txt"

    echo "Zipping templates..."
    pushd "${templatesdir_mono}/.." || exit
    zip -q -9 -r -D "${reldir_mono}/${godot_basename}_mono_export_templates.tpz" templates/*
    popd || exit

    ## SHA-512 sums (Mono) ##
    echo "Generating SHA-512 sums..."
    pushd "${reldir_mono}" || exit
    sha512sum [Gg]* >> SHA512-SUMS.txt

    # Create directory for SHA-512 sums
    mkdir -p "${basedir}/sha512sums/${binaries_version}/mono"
    cp SHA512-SUMS.txt "${basedir}/sha512sums/${binaries_version}/mono/"

    popd || exit
}

publish_packages() {
    publish_nuget=1
    publish_maven=0

    # Check and publish NuGet packages
    if [ "${publish_nuget}" == "1" ]; then
        echo "Publishing NuGet packages..."
        publish_nuget_packages out/linux/x86_64/tools-mono/GodotSharp/Tools/nupkgs/*.nupkg
    fi

    # Check and publish Android library to MavenCentral
    if [ "${publish_maven}" == "1" ]; then
        echo "Publishing Android library to MavenCentral..."
        publish_maven_library
    fi
}

prepare() {
    # prepare_for_classical
    # prepare_for_mono
    publish_packages
}

main() {
    local godot_version="$1"
    local godot_version_status="$2"
    disable_cleanup=$3
    disable_generate_tarball=$4

    binaries_version="${godot_version}-$godot_version_status"
    templates_version="${godot_version}.$godot_version_status"

    basedir=$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd)
    webdir="${basedir}/web/${templates_version}"
    relative_directory="${basedir}/releases/${binaries_version}"
    reldir_mono="${relative_directory}/mono"
    temporary_directory="${basedir}/tmp"
    templatesdir="${temporary_directory}/templates"
    templatesdir_mono="${temporary_directory}/mono/templates"

    godot_basename="Godot_v${binaries_version}"

    echo "binaries_version: $binaries_version"
    echo "templates_version: $templates_version"
    echo "basedir: $basedir"
    echo "webdir: $webdir"
    echo "relative_directory: $relative_directory"
    echo "reldir_mono: $reldir_mono"
    echo "temporary_directory: $temporary_directory"
    echo "templatesdir: $templatesdir"
    echo "templatesdir_mono: $templatesdir_mono"
    echo "godot_basename: $godot_basename"

    cleanup_and_setup

    prepare

    echo "All editor binaries and templates prepared successfully for release"
}

main "$@"