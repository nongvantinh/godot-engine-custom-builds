#!/usr/bin/env bash

# Is this an organization account or just a normal user account
export IS_ORG_ACCOUNT=0

# Registry where the containers will be staged and pushed.
export REGISTRY="ghcr.io"
# The username(namespace) where the packages will be uploaded.
export USERNAME="nongvantinh"
# GitHub personal access token (classic)
export PAT_TOKEN="40 random characters after github personal token has been generated"
# The name of the repository where packages will be uploaded.
export REPO_NAME="godot-engine-custom-builds"

# The Git repository where the Godot source is located.
export GODOT_REPOSITORY="https://github.com/nongvantinh/godot.git"
# Name of the branch to check out after cloning the Godot source
export GIT_BRANCH="4.4.dev6"

export SKIP_DOWNLOAD_CONTAINERS=0
# Each version of Godot may require more or fewer dependencies.
# Therefore, we usually build containers for a specific version.
export CONTAINER_VERSION="4.4"
export VERSION_STATUS_PATCH="-1"
export BASE_VERSION=41
export BASE_DISTRO="f${BASE_VERSION}"
export CONTAINER_TYPE="all"
export XCODE_SDK=16.2
export OSX_SDK=15.2
export IOS_SDK=18.2

# all|classical|mono
export BUILD_TYPE="all"

# Configuration file for user-specific details.
# This file is gitignore'd and will be sourced by build scripts.

# Note: For passwords or GPG keys, make sure that special characters such
# as $ won't be expanded, by using single quotes to enclose the string,
# or escaping with \$.


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
export NUGET_SOURCE='github'
# API key for publishing NuGet packages to NUGET_SOURCE.
export NUGET_API_KEY=$PAT_TOKEN
export NUGET_SOURCE_URL="https://nuget.pkg.github.com/$USERNAME/index.json"

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
export GODOT_ANDROID_SIGN_KEYSTORE="/home/$SUDO_USER/Projects/godot-engine-custom-builds/data/godot-release.keystore"
# Key alias used for signing the release build
export GODOT_ANDROID_KEYSTORE_ALIAS='godot-release'
# Password for the key used for signing the release build
export GODOT_ANDROID_SIGN_PASSWORD='The-password-you-used-when-generating-keystore'
