# Orbit

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Android-green.svg)]()
[![Security](https://img.shields.io/badge/Security-HMAC--SHA256%20%2B%20E2E-red.svg)]()
[![Desktop](https://img.shields.io/badge/KDE%20Plasma%206-Wayland%20Native-blue.svg)]()

**Orbit** (formerly PC Connect / PC Authenticator) is a complete, private, end-to-end encrypted **replacement for KDE Connect** and a **biometric lock-screen authenticator** for Linux (KDE Plasma 6 on Wayland / X11).

It combines bi-directional file sharing, Dolphin context menus, remote phone storage browsing, shared clipboard sync, media playback controls (MPRIS), Find My Phone, and remote PC lock with **passwordless phone fingerprint screen unlock** and **hardware USB cable detection**.

**Zero cloud dependencies. No third-party servers. No telemetry. 100% offline & local.**

---

## ✨ Features

### 📁 1. Seamless File Sharing (Bi-Directional)
- **PC to Phone:**
  - **Dolphin Right-Click Integration:** Right-click any file or folder in Dolphin and select **"Send to phone (Orbit)"**.
  - **CLI Transfer:** Run `orbit send <files...>` or `orbit-send <files...>`.
  - Native desktop notifications with transfer progress and completion alerts.
- **Phone to PC:**
  - **System Share Sheet Integration:** Tap "Share" on any photo, video, PDF, or document in any Android app (Google Photos, Gallery, Files, Chrome, WhatsApp) and select **"PC Connect"** $\rightarrow$ streams directly to `~/Downloads/`.
  - Interactive KDE notification on PC: click **"Open File"** or **"Open Downloads Folder"**.
  - Audible chime plays automatically on PC upon receiving files.

### 📱 2. Browse Phone Files Remotely
- **Native PyQt6 Phone Explorer:**
  - Launch from system tray or run `orbit browse`.
  - Quick bookmarks: 📱 Internal Storage, 📷 Photos/DCIM, 📥 Downloads, 📄 Documents, 🎵 Music, 🎬 Movies.
  - Download phone files to PC Downloads with one click or double-click.
  - Drag-and-drop upload from PC desktop straight into phone folders.
  - Create new folders and delete files directly on phone.
- **Modern Web File Explorer:**
  - Access in any web browser at `http://<laptop-ip>:1760/browse` with responsive dark-mode UI.

### 📋 3. Shared Clipboard Synchronization
- Copy text on Linux $\rightarrow$ tap **"Paste from PC"** on Android to receive.
- Copy text on Android $\rightarrow$ tap **"Copy to PC"** or share text to update the native KDE Plasma 6 clipboard (`org.kde.klipper`) instantly.
- One-click clipboard sync from PC tray or terminal: `orbit clip`.

### 🌐 4. Share Webpages Directly to PC Browser
- **Instant Browser Opening:** Share any link from Chrome, Firefox, Brave, YouTube, or social media on your phone $\rightarrow$ select **"PC Connect"** $\rightarrow$ opens automatically in your default desktop browser (Zen, Firefox, Chrome, etc.) and copies the URL to your PC clipboard!
- **On-Demand URL Launcher:** Tap **"Open Webpage"** directly in the phone app to type or paste any link to launch on your laptop immediately.

### 🖱️ 5. Remote Trackpad & Wireless Mouse Pointer
- **Kernel-Level `/dev/uinput` Virtual Driver:**
  - Works globally across all Wayland and XWayland windows, games, and lock screens with zero external dependencies.
  - Sub-millisecond latency via direct UDP stream (port `1762`) with automatic HTTP fallback.
- **Intuitive Gestures & Tactile Controls:**
  - ☝️ **1-Finger Drag:** Smooth cursor movement with acceleration.
  - ☝️ **1-Finger Tap:** Left Click with subtle haptic vibration.
  - ✌️ **2-Finger Scroll:** Smooth vertical and horizontal page scrolling.
  - ✌️ **2-Finger Tap:** Right Click context menu.
  - 👆 **Double-Tap & Drag:** Hold to select text, move windows, and drag-and-drop.
  - 🖱️ **Physical Buttons:** Dedicated Left, Right, and Middle click buttons (with hold-to-drag support).
  - ⌨️ **Quick Typing:** Send text or links directly from the trackpad screen.
  - ⚙️ **Customizable Sensitivity:** Adjust tracking speed on the fly (0.4x - 3.0x).

### 🎵 6. Remote Media Player Control (MPRIS)
- Control desktop media playback directly from your phone app:
  - Real-time track title, artist, and playback status for Spotify, VLC, Firefox, Zen Browser, Elisa, Chrome, etc.
  - Controls: ⏯️ Play/Pause, ⏮️ Previous, ⏭️ Next, 🔉 Volume Down, 🔊 Volume Up.
  - CLI control: `orbit media play|pause|next|prev|volup|voldown`.

### 🔔 7. Find My Phone & Ping PC
- **Find My Phone:** Click **"Find My Phone"** in the PC tray or run `orbit ring` $\rightarrow$ phone rings at maximum alarm volume and vibrates with an on-screen "Dismiss Alarm" button.
- **Ping PC:** Tap **"Ring Laptop"** on your phone $\rightarrow$ PC plays loud incoming call chime and displays an urgent desktop alert.

### 🔒 8. Passwordless Mobile Unlock & Biometrics
- **Passwordless Mobile Unlock:** Leave the password field blank and hit `Enter` on your lock screen $\rightarrow$ Phone lights up and scans fingerprint $\rightarrow$ **Laptop unlocks instantly!**
- **Strict 2FA Mode:** Type your Linux password first, followed by phone biometric approval.
- **Hardware USB Token Mode:** Plug your phone in via USB cable to unlock instantly with zero wireless network needed.
- **Remote Lock:** Tap **"Lock PC"** on phone to immediately lock the session via `loginctl lock-session`.

### 🔋 9. Native KDE System Tray & Phone Status
- Sits in your KDE Plasma panel with live connection status.
- Real-time phone battery level and charging state (e.g. `OPPO CPH2505 • 85% ⚡`).
- Quick access to all sharing, browsing, ringing, and locking features.

---

## 🚀 Quick Start (computer side)

```bash
git clone https://github.com/lunar-photon/Orbit.git
cd Orbit
./setup.sh --dry-run     # optional: show every step, change nothing
./setup.sh
```

Run it as your normal user; it asks for `sudo` only to install missing packages
and for the login integration (`/usr/local/bin/orbit-auth-check`, `/etc/orbit`,
`/etc/pam.d`). It is safe to run again (e.g. after `git pull`), and it migrates
an existing PC Connect / PC Authenticator install, keeping your phone pairing.

| Option | What it does |
| :--- | :--- |
| `--mode 2fa` | Login / lock screen: password, then approve on the phone (default for new installs) |
| `--mode passwordless` | Press Enter on an empty password and approve on the phone; typing the password still works |
| `--no-pam` | Don't touch login / lock screen at all |
| `--no-term-history` | Don't record Konsole tabs (they record their output so the phone can show full scrollback; set `NO_ORBIT_RECORDING=1` to skip per tab) |
| `--dry-run` | Print every action without changing anything |
| `--uninstall [--purge]` | Restore password-only login and remove Orbit (`--purge` also deletes `~/.config/orbit`) |

Password-only login without uninstalling: `linux/revert-to-password.sh`.

What goes where: the service is `orbit.service` (`systemctl --user status orbit`),
program files are in `~/.local/share/orbit/`, settings and pairings in
`~/.config/orbit/config.json`, and the commands `orbit`, `orbit-send` and
`orbit-tray` in `~/.local/bin/`.

---

## 📱 Android App Setup

### 1. Install the APK
- Download the prebuilt release: [Orbit.apk](Orbit.apk)
- Or download it directly over your local network in your phone browser:
  ```
  http://<laptop-ip>:1760/apk
  ```

### 2. Pair Your Phone (One-Time Setup)
1. Open the **Orbit** app on your phone.
2. Tap **"Auto-Scan Wi-Fi"** (or add your PC IP manually).
3. Tap **"Pair 🔑"** next to your discovered computer.
4. A **6-digit PIN** will pop up on your laptop screen as a desktop notification.
5. Enter the PIN in your phone app and tap **"Pair & Authorize"**.
6. Your phone is now cryptographically paired with 256-bit mutual encryption!

---

## 💻 CLI Commands Reference

`orbit` is available in your terminal (the old `pc-connect` name still works):

| Command | Description |
| :--- | :--- |
| `orbit status` | Check phone connection state, IP, and battery percentage |
| `orbit browse` | Open the native PyQt6 Phone File Explorer window |
| `orbit send <files...>` | Send one or more files to your phone |
| `orbit clip` | Push current PC clipboard to phone |
| `orbit clip "hello"` | Send custom text to phone clipboard |
| `orbit ring` | Ring phone loudly at max alarm volume (Find My Phone) |
| `orbit ping` | Play alert sound and notification on PC |
| `orbit media` | Show currently playing media track and artist |
| `orbit media [command]` | Control media: `play`, `pause`, `toggle`, `next`, `prev`, `volup`, `voldown` |
| `orbit lock` | Lock PC screen immediately |
| `orbit tray` | Launch the KDE Plasma system tray application |
| `orbit usb` | Enroll a USB cable unlock token |

---

## 🔌 Hardware USB Token Mode

Want your laptop to unlock instantly whenever your phone is plugged in via USB cable, even with Wi-Fi turned off?

1. Plug your phone into your laptop with a USB cable.
2. Run:
   ```bash
   orbit usb
   ```
3. Select your phone from the detected device list.
4. Done! Whenever your phone is plugged in, your lock screen will unlock immediately upon pressing `Enter`.

---

## 🛡️ Security Architecture

| Security Layer | Implementation |
| :--- | :--- |
| **Mutual Pairing** | Out-of-band 6-digit random PIN ceremony with 60-second expiry and max 3 attempts |
| **Bearer Tokens** | High-entropy 256-bit cryptographic tokens generated via `secrets.token_hex(32)` |
| **Request Signing** | Every unlock request requires HMAC-SHA256 signature generated with shared secret key |
| **Replay Defense** | Nonces + 45-second timestamp freshness window strictly enforced |
| **Biometric Auth** | Native Android `BiometricPrompt` supporting Fingerprint, Face, and Device Keyguard |
| **Local-Only** | Binds only to local interfaces (Wi-Fi, hotspot, USB reverse tethering). Zero external cloud requests |

---

## 🛠️ Building from Source

### Android APK (Fast, Standalone Toolchain)
```bash
cd android
./build-apk.sh
```
Builds and signs `Orbit.apk` in ~4 seconds without needing heavy Gradle dependencies.

---

## 📄 License

MIT License. Free and open source for personal and enterprise use.
