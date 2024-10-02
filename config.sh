#!/usr/bin/env bash

# Registry where the containers will be staged and pushed.
export REGISTRY="ghcr.io"
export USERNAME="nongvantinh"
export PAT_TOKEN="40 random characters after github personal token has been generated"

export GIT_TREEISH="4.3"
export BASE_DISTRO="f40"
export CONTAINER_TYPE="all"
export XCODE_SDK=15.4
export OSX_SDK=14.5
export IOS_SDK=17.5

export BUILD_TYPE="all"
export GODOT_VERSION="4.3.1"
export FORCE_DOWNLOAD=false
export SKIP_DOWNLOAD=false
export SKIP_GIT_CHECKOUT=false

# Configuration file for user-specific details.
# This file is gitignore'd and will be sourced by build scripts.

# Note: For passwords or GPG keys, make sure that special characters such
# as $ won't be expanded, by using single quotes to enclose the string,
# or escaping with \$.

# Version string of the images to use in build.sh.
export IMAGE_VERSION='4.x-f36'

# Default build name used to distinguish between official and custom builds.
export BUILD_NAME='custom_build'

# Default number of parallel cores for each build.
export NUM_CORES=$(nproc --ignore=1)

# Set up your own signing keystore and relevant details below.
# If you do not fill all SIGN_* fields, signing will be skipped.

# Path to pkcs12 archive.
export SIGN_KEYSTORE=''

# Password for the private key.
export SIGN_PASSWORD=''

# Name and URL of the signed application.
# Use your own when making a thirdparty build.
export SIGN_NAME=''
export SIGN_URL=''

# Hostname or IP address of an OSX host (Needed for signing)
# eg 'user@10.1.0.10'
export OSX_HOST=''
# ID of the Apple certificate used to sign
export OSX_KEY_ID=''
# Bundle id for the signed app
export OSX_BUNDLE_ID=''
# Username/password for Apple's signing APIs (used for notarytool)
export APPLE_TEAM=''
export APPLE_ID=''
export APPLE_ID_PASSWORD=''

# NuGet source for publishing .NET packages
export NUGET_SOURCE='nuget.org'
# API key for publishing NuGet packages to nuget.org
export NUGET_API_KEY=''

# MavenCentral (sonatype) credentials
export OSSRH_GROUP_ID=''
export OSSRH_USERNAME=''
export OSSRH_PASSWORD=''
# Sonatype assigned ID used to upload the generated artifacts
export SONATYPE_STAGING_PROFILE_ID=''
# Used to sign the artifacts after they're built
# ID of the GPG key pair, the last eight characters of its fingerprint
export SIGNING_KEY_ID=''
# Passphrase of the key pair
export SIGNING_PASSWORD=''
# Base64 encoded private GPG key
export SIGNING_KEY=''

# Android signing configs
# Path to the Android keystore file used to sign the release build
export GODOT_ANDROID_SIGN_KEYSTORE=''
# Key alias used for signing the release build
export GODOT_ANDROID_KEYSTORE_ALIAS=''
# Password for the key used for signing the release build
export GODOT_ANDROID_SIGN_PASSWORD=''
