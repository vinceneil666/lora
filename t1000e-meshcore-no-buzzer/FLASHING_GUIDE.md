# T1000-E Custom Firmware — Build & Flashing Guide

This firmware is a modified build of MeshCore with the **buzzer permanently disabled**.

> **Before you start:** Always attach the LoRa antenna to the T1000-E before connecting USB or powering the device. Powering the radio without an antenna can permanently damage the LR1110 radio chip.

---

## Prerequisites

You will need:
- A computer running **Windows, macOS, or Linux (x86/x64)**
- **Python 3** (download from [python.org](https://python.org))
- **PlatformIO CLI**
- **Nordic nrfutil**

Install PlatformIO and nrfutil:

```bash
pip install platformio nrfutil
```

Verify the install:
```bash
pio --version
nrfutil version
```

> **Note:** Building requires an x86/x64 machine. ARM-based Linux systems (e.g. Raspberry Pi) are not supported by the PlatformIO nRF52 toolchain.

---

## Step 1 — Get the Source Code

This firmware is based on [MeshCore](https://github.com/ripplebiz/MeshCore) v1.15.0 with the T1000-E buzzer disabled. You have two options:

### Option A — Use this repository directly (easiest)

Clone this repository and you already have everything you need — the modification is pre-applied:

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
cd YOUR_REPO_NAME
```

### Option B — Apply the change to a fresh MeshCore clone yourself

If you prefer to start from the official MeshCore source and apply the change manually:

**1. Clone MeshCore:**
```bash
git clone https://github.com/ripplebiz/MeshCore.git
cd MeshCore
```

**2. Open `variants/t1000-e/platformio.ini` and remove these lines from both the `[env:t1000e_companion_radio_usb]` and `[env:t1000e_companion_radio_ble]` sections:**

```ini
-D PIN_BUZZER=25
-D PIN_BUZZER_EN=37      ; P1/5 - required for T1000-E
```

And remove this line from `build_src_filter` in those same two sections:
```ini
+<helpers/ui/buzzer.cpp>
```

And remove this line from `lib_deps` in those same two sections:
```ini
end2endzone/NonBlockingRTTTL@^1.3.0
```

That's the only change needed. When `PIN_BUZZER` is undefined, all buzzer code is excluded at compile time via `#ifdef` guards — nothing else needs to be touched.

---

## Step 2 — Choose Your Variant

Pick the firmware variant that matches how you use the T1000-E:

| Environment | Use when |
|---|---|
| `t1000e_companion_radio_ble` | Pairing with the MeshCore app over Bluetooth (most common) |
| `t1000e_companion_radio_usb` | Connecting directly via USB serial |
| `t1000e_repeater` | Running as a standalone mesh repeater node |
| `t1000e_room_server` | Running as a room/channel server node |

The examples below use `t1000e_companion_radio_ble`. Replace it with your chosen variant if different.

---

## Step 3 — Build the Firmware

Open a terminal in the root of this folder (where `platformio.ini` is located) and run:

```bash
pio run -e t1000e_companion_radio_ble
```

On the **first run**, PlatformIO will download the nRF52 toolchain and all libraries (~500 MB). This takes several minutes. Subsequent builds are fast.

A successful build ends with output like:
```
RAM:   [====      ]  38.4% (used 125056 bytes from 327680 bytes)
Flash: [=======   ]  70.2% (used 460288 bytes from 655360 bytes)
=== [SUCCESS] ...
```

The built firmware files are placed in:
```
.pio/build/t1000e_companion_radio_ble/
  firmware.uf2   ← for drag-and-drop flashing
  firmware.zip   ← for manual nrfutil flashing
```

---

## Step 4 — Flash the Firmware

Choose one of the three methods below.

### Method 1 — PlatformIO one-command build + flash (Recommended)

Connect the T1000-E via USB, then run:

```bash
pio run -e t1000e_companion_radio_ble -t upload
```

PlatformIO builds and flashes in one step using nrfutil automatically.

---

### Method 2 — UF2 Drag-and-Drop (No extra tools needed)

After building (Step 2):

1. Put the T1000-E into bootloader mode:
   - Power off the device
   - Hold the activation button while connecting USB
   - The device appears as a USB drive named **`NRF52BOOT`**

2. Drag `.pio/build/t1000e_companion_radio_ble/firmware.uf2` onto the `NRF52BOOT` drive.

The device flashes and reboots automatically.

---

### Method 3 — Manual nrfutil DFU

Use this if Method 1 fails but the build succeeds.

1. Find your serial port:
   - Linux: `/dev/ttyACM0` or `/dev/ttyUSB0`
   - Mac: `/dev/cu.usbmodem*`
   - Windows: `COM3` (or similar — check Device Manager)

2. Put the T1000-E into bootloader mode (see Method 2, step 1).

3. Flash:
   ```bash
   nrfutil dfu usb-serial -pkg .pio/build/t1000e_companion_radio_ble/firmware.zip -p /dev/ttyACM0
   ```
   Replace `/dev/ttyACM0` with your actual port.

---

## What Changed in This Build

- Buzzer is **fully disabled** at compile time — no startup beep, no message beep, no shutdown beep
- The buzzer GPIO pins (P0.25 and P1.5) are left untouched by the firmware, so the hardware is unaffected
- All other T1000-E functionality is identical to the official MeshCore 1.15.0 release

---

## Troubleshooting

**Build fails on first run:**
PlatformIO downloads the nRF52 toolchain on first use. Run the command again if it times out mid-download.

**Device not detected:**
- Try a different USB cable (many cables are charge-only and carry no data)
- On Linux, add yourself to the dialout group: `sudo usermod -aG dialout $USER` then log out and back in

**Upload fails with "no DFU target found":**
The device is not in bootloader mode. Use Method 2 (UF2 drag-and-drop) instead.

**Device not appearing as USB drive (Method 2):**
Hold the activation button for a full 2 seconds while plugging in USB, then release.
