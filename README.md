
# Godot Engine Custom Builds

This project involves building a custom version of the Godot Game Engine that includes specific features you need, as well as fixes for bugs affecting your game that have not yet been merged into the upstream `master` branch.

## Preface

Before we begin, I want to share the motivation behind this project so you can easily understand the steps outlined in the instructions below for compiling your own version of the Godot Engine.

When I first started, I simply wanted to create a game of my own in the genres I enjoy. However, I gradually realized that the number of employees at the Godot Foundation is small, and they can only add essential features that everyone needs while modifying the codebase to make it easier to maintain. As contributors, we implement the proposed features we like and create pull requests for them to merge. Unfortunately, some features may not be merged by the code owners because they believe these can be accomplished using other software then importing modified assets into Godot. This is where I disagree; thus, I decided to maintain my own version of Godot. However, compiling it locally and manually publishing it to stores was too much work for me. I needed a way to automate the process of compiling my custom version of the Godot Engine and publishing the release on GitHub. This way, for every change I make to the engine, I would only have to wait around 1-2 hours for the Docker containers to spin up and publish the artifacts for me.


## Pre-requisites

First, ensure your package lists are up to date and install the required software:

```bash
sudo apt-get update
sudo apt install -y python-is-python3 openjdk-11-jdk dotnet-sdk-8.0 aspnetcore-runtime-8.0
```

### Generate Android Keystore

1. Navigate to the root of this project, then create the `data` directory.
2. Open a terminal and run the following command to generate your keystore:

   ```bash
   keytool -genkey -v -keystore godot-release.keystore -alias godot-release -keyalg RSA -keysize 2048 -dname "CN=Nông Văn Tình, OU=IT Software, O=Undeton, L=Cao Bang, ST=Cao Bang, C=VN" -validity 10000
   ```

3. After generating the keystore, update the following environment variables in `config.sh`:
   - `GODOT_ANDROID_SIGN_KEYSTORE`
   - `GODOT_ANDROID_KEYSTORE_ALIAS`
   - `GODOT_ANDROID_SIGN_PASSWORD`

   Ensure that `GODOT_ANDROID_SIGN_KEYSTORE` points to the generated keystore using an absolute path. For example, use:

   ```bash
   /home/my-account/Projects/godot-engine-custom-builds/data/godot-release.keystore
   ```

   **Note:** Avoid using `~/` in the path; specify the full path to ensure proper referencing.


First, please note that the GitHub Runner offered by GitHub has a limited amount of RAM, and the number of cores (4 cores for free subscriptions) is insufficient for the goals of this project. When I attempted to use GitHub's runner, it failed to compile custom Clang and encountered issues at the linking stage. Additionally, when using the container to compile the Godot Engine for Windows, it took 3-4 hours, which is too long. Eventually, if a running job takes too long, it will be terminated by GitHub. GitHub Actions workflows have a maximum runtime limit of 6 hours. If a workflow exceeds this time, it will be automatically terminated. This applies to both scheduled workflows and those triggered by events like pushes or pull requests. Therefore, you must have your own machine in other to folow the steps below.

The requirements for the machine that will be used are:

- **RAM**: At least 16 GB
- **Cores**: 8
- **Threads**: 16
- **OS**: Ubuntu 24.04

Here are the details of the machine I use:

```
System:
  Kernel: 6.8.0-45-generic arch: x86_64 bits: 64 compiler: gcc v: 13.2.0
  Desktop: GNOME v: 46.0 Distro: Ubuntu 24.04.1 LTS (Noble Numbat)

Machine:
  Type: Laptop System: ASUSTeK product: ASUS TUF Gaming F15 FX506HE_FX506HE
    v: 1.0 serial: <superuser required>
  Mobo: ASUSTeK model: FX506HE v: 1.0 serial: <superuser required>
    UEFI: American Megatrends LLC. v: FX506HE.313 date: 03/03/2023

Battery:
  ID-1: BAT1 charge: 44.4 Wh (100.0%) condition: 44.4/48.1 Wh (92.4%)
    volts: 12.5 min: 11.7 model: ASUS A32-K55 status: full

CPU:
  Info: 8-core model: 11th Gen Intel Core i7-11800H bits: 64 type: MT MCP
    arch: Tiger Lake rev: 1 cache: L1: 640 KiB L2: 10 MiB L3: 24 MiB
  Speed (MHz): avg: 3239 high: 4312 min/max: 800/4600 cores: 1: 4300 2: 2629
    3: 4300 4: 1631 5: 4100 6: 1838 7: 3998 8: 4299 9: 2862 10: 2071 11: 4246
    12: 799 13: 4312 14: 1849 15: 4300 16: 4300 bogomips: 73728
  Flags: avx avx2 ht lm nx pae sse sse2 sse3 sse4_1 sse4_2 ssse3 vmx

Graphics:
  Device-1: Intel TigerLake-H GT1 [UHD Graphics] vendor: ASUSTeK driver: i915
    v: kernel arch: Gen-12.1 bus-ID: 0000:00:02.0
  Device-2: NVIDIA GA107M [GeForce RTX 3050 Ti Mobile] vendor: ASUSTeK
    driver: nvidia v: 550.107.02 arch: Ampere bus-ID: 0000:01:00.0
  Device-3: Sonix USB2.0 HD UVC WebCam driver: uvcvideo type: USB
    bus-ID: 3-7:4
  Display: x11 server: X.Org v: 21.1.11 with: Xwayland v: 23.2.6 driver: X:
    loaded: modesetting,nvidia unloaded: fbdev,nouveau,vesa dri: iris gpu: i915
    resolution: 1920x1080~144Hz
  API: EGL v: 1.5 drivers: iris,nvidia,swrast platforms:
    active: gbm,x11,surfaceless,device inactive: wayland,device-1
  API: OpenGL v: 4.6.0 compat-v: 4.5 vendor: intel mesa v: 24.0.9-0ubuntu0.1
    glx-v: 1.4 direct-render: yes renderer: Mesa Intel UHD Graphics (TGL GT1)

Audio:
  Device-1: Intel Tiger Lake-H HD Audio vendor: ASUSTeK driver: snd_hda_intel
    v: kernel bus-ID: 0000:00:1f.3
  Device-2: NVIDIA vendor: ASUSTeK driver: snd_hda_intel v: kernel
    bus-ID: 0000:01:00.1
  API: ALSA v: k6.8.0-45-generic status: kernel-api
  Server-1: PipeWire v: 1.0.5 status: active

Network:
  Device-1: MEDIATEK MT7921 802.11ax PCI Express Wireless Network Adapter
    vendor: AzureWave driver: mt7921e v: kernel bus-ID: 0000:2d:00.0
  IF: wlp45s0 state: up mac: <filter>
  Device-2: Realtek RTL8111/8168/8211/8411 PCI Express Gigabit Ethernet
    vendor: ASUSTeK RTL8111/8168/8411 driver: r8169 v: kernel port: 3000
    bus-ID: 0000:2e:00.0
  IF: enp46s0 state: down mac: <filter>
  IF-ID-1: docker0 state: up speed: 10000 Mbps duplex: unknown mac: <filter>
  IF-ID-2: vethbf030b7 state: up speed: 10000 Mbps duplex: full
    mac: <filter>

Bluetooth:
  Device-1: IMC Networks Wireless_Device driver: btusb v: 0.8 type: USB
    bus-ID: 3-14:5
  Report: hciconfig ID: hci0 rfk-id: 0 state: up address: <filter> bt-v: 5.2
    lmp-v: 11

RAID:
  Hardware-1: Intel Volume Management Device NVMe RAID Controller driver: vmd
    v: 0.6 bus-ID: 0000:00:0e.0

Drives:
  Local Storage: total: 1.39 TiB used: 107.92 GiB (7.6%)
  ID-1: /dev/nvme0n1 vendor: Western Digital model: WD PC SN560
    SDDPNQE-1T00-1002 size: 953.87 GiB temp: 50.9 C
  ID-2: /dev/nvme1n1 vendor: Samsung model: SSD 970 EVO Plus 500GB
    size: 465.76 GiB temp: 69.8 C

Partition:
  ID-1: / size: 181.35 GiB used: 76.98 GiB (42.4%) fs: ext4
    dev: /dev/nvme1n1p1
  ID-2: /boot/efi size: 1.05 GiB used: 6.1 MiB (0.6%) fs: vfat
    dev: /dev/nvme1n1p2
  ID-3: /home size: 255.62 GiB used: 12.48 GiB (4.9%) fs: ext4
    dev: /dev/nvme1n1p3

Swap:
  ID-1: swap-1 type: partition size: 18.63 GiB used

: 1.42 GiB (7.6%)
    dev: /dev/nvme1n1p5

Sensors:
  System Temperatures: cpu: 56.0 C mobo: N/A
  Fan Speeds (rpm): cpu: 3800

Info:
  Memory: total: 16 GiB note: est. available: 15.36 GiB used: 4.4 GiB (28.6%)
  Processes: 407 Uptime: 22h 4m Init: systemd target: graphical (5)
  Packages: 1650 Compilers: N/A Shell: Bash v: 5.2.21 inxi: 3.3.34
```

## Getting Started

### 1. Install Ubuntu 24.04 OS

Download the Ubuntu 24.04 ISO file from the official Ubuntu website and flash it onto your USB drive, then install it on your machine. I have a laptop that I just bought after graduating from university, which has two SSDs: one with 1 TB of storage for my Windows OS, and another with 420 GB where I will install Ubuntu alongside Windows 11.

If you have the resources, you can buy a machine and install Ubuntu directly on it. If you don't have a powerful laptop, you can use a cloud service provider (such as Azure or AWS) for this purpose.

The most important requirement is that you must have a strong machine running Ubuntu 24.04, as I have only tested this project on that version. Additionally, a strong network connection is essential. I encountered various instances where the clone source in the bash script failed; the notorious source is `godot`. As a workaround, I cached this project, fetched the latest changes from `origin`, and then copied the cached project to the current build project to proceed. However, this does not guarantee success, as some steps in the container build still require cloning the source from other projects, which can fail if the network connection is slow.

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


### 2. Install Docker

Follow the instructions under **Install using the apt repository** to install Docker: [Docker Installation Guide](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository).

1. Authenticate with GitHub Container Registry

First, you need to authenticate with the registry. You can do this using a personal access token (PAT) that has the write:packages and read:packages scopes.
Using Docker CLI:

bash

echo $TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

Replace $TOKEN with your GitHub PAT.