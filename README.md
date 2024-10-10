# Godot Engine Custom Builds

This project allows you to build a custom version of the Godot Game Engine tailored to include specific features you need and fixes for bugs that haven't yet been merged into the upstream `master` branch.

## Background

To run this project, you must have Ubuntu 24.04 installed on your local machine.

Initially, I tried to set up this project using GitHub Actions. However, the GitHub machine used for the action had only 1GB of RAM and 4 threads, which severely impacted the build process. The limited RAM caused failures when compiling a custom Clang compiler, and the low number of threads made the build process much slower.

I then switched to a self-hosted GitHub runner, using my laptop with 16GB of RAM and 16 threads. This setup successfully built the custom Clang compiler but failed at the stage of building Godot binaries and templates. The issue was a 6-hour time limit imposed by GitHub, which kills any job that exceeds this duration.

As a result, I've decided to run this project locally and publish the compiled artifacts on GitHub Container Registry and GitHub Packages.

## Pre-requisites

Before you start, make sure your package lists are updated and install the necessary software:

```bash
sudo apt-get update
sudo apt install -y python-is-python3 openjdk-11-jdk      \
                    dotnet-sdk-8.0 aspnetcore-runtime-8.0 \
                    osslsigncode gh
```

Next, create a GitHub personal access token (classic) with the following permissions:

**scopes*:
  - **repo**

  - **workflow**

  - **write:packages**

  - **admin:org**
  - **admin:public_key**
  - **admin:repo_hook**
  - **admin:org_hook**

  - **gist**

  - **project**

Then update the `PAT_TOKEN` and `USERNAME` in the `config.sh`


Make sure to keep your token secure!


#### Install Docker

Follow the instructions under **Install using the apt repository** to install Docker: [Docker Installation Guide](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository).

#### Generate Android Keystore

1. Go to the root directory of this project and create a new folder called `data`.
2. Open a terminal and run this command to generate your keystore:

   ```bash
   keytool -genkey -v -keystore godot-release.keystore -alias godot-release -keyalg RSA -keysize 2048 -validity 10000
   ```

3. After the keystore is created, update the following variables in `config.sh`:
   - `GODOT_ANDROID_SIGN_KEYSTORE`
   - `GODOT_ANDROID_KEYSTORE_ALIAS`
   - `GODOT_ANDROID_SIGN_PASSWORD`

   Make sure `GODOT_ANDROID_SIGN_KEYSTORE` points to the generated keystore using an absolute path. For example:

   ```bash
   /home/my-account/Projects/godot-engine-custom-builds/data/godot-release.keystore
   ```

   **Note:** Avoid using `~/` in the path; always use the full path to ensure it works correctly. Since the script needs to run as `sudo` to work with Docker, it will be executed as the `root` user.

### Machine Requirements

Here are the specifications of the machine I used to run this project. If your machine has lower specs, it may fail at some stages:

- **RAM**: At least 16 GB
- **Cores**: 8
- **Threads**: 16
- **OS**: Ubuntu 24.04

Eventually, with the specification above, the web build will still fail at the linking stage due to insufficient RAM.

## Getting Started

### 1. Install Ubuntu 24.04 OS

Download the Ubuntu 24.04 ISO file from the official Ubuntu website and flash it onto your USB drive, then install it on your machine. I have a laptop that I just bought after graduating from university, which has two SSDs: one with 1 TB of storage for my Windows OS, and another with 420 GB where I will install Ubuntu alongside Windows 11.

If you have the money, you can buy a machine and install Ubuntu directly on it. If you don't have a powerful device, you can use a cloud service provider (such as Azure or AWS) for this purpose.

The most important requirement is that you must have a strong machine running Ubuntu 24.04, as I have only tested this project on that version. Additionally, a strong network connection is essential. I encountered various instances where the clone source in the bash script failed; the notorious source is `godot`.

Here is the example error:

```
Cloning into 'godot'...
remote: Enumerating objects: 732221, done.
remote: Counting objects: 100% (209/209), done.
remote: Compressing objects: 100% (139/139), done.
error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly: CANCEL (err 8)
error: 6591 bytes of body are still expected
fetch-pack: unexpected disconnect while reading sideband packet
fatal: early EOF
fatal: fetch-pack: invalid index-pack output
```


## Usage

### 1. Build Docker Containers

To avoid package conflicts, we isolate each build using Docker. First, we need to build the containers:

```bash
sudo ./main.sh docker --container-type all
# The above command is equivalent to this command:
sudo ./main.sh docker --container-type windows --container-type linux --container-type android --container-type ios --container-type osx --container-type web 
```

This command will build all Docker container types for all supported platforms. It will then automatically publish the compiled containers to the GitHub Container Registry using the information defined in `config.sh`.

### 2. Build Godot Engine and Templates

Once the containers are built and published, navigate to the root of this project and run:

```bash
sudo ./main.sh godot --build --release
```

This command will build the Godot Engine and its export templates for all supported platforms. It will package them and publish them to your GitHub releases, and for NuGet packages, it will push them to your account’s Packages.

### After Building

After completing the steps above, all containers and packages should be published to your GitHub account. You can download the binaries and export templates when developing on other devices. If you’re using the Mono version, you need to add the NuGet source to access the packages (since they are hosted on GitHub, not nuget.org).

#### Adding the NuGet Source

To configure the NuGet source for downloading the required packages, use the following command:

```bash
dotnet nuget add source --name "$NUGET_SOURCE" "$NUGET_SOURCE_URL"
```

For example:

```bash
dotnet nuget add source --name "github" "https://nuget.pkg.github.com/$USERNAME/index.json"
```

Replace **$USERNAME** with your actual GitHub username where you uploaded the packages.