# LoRa MeshCore Scanner

A passive LoRa packet scanner and [MeshCore](https://github.com/meshcore-dev/MeshCore) protocol decoder for the **Heltec WiFi LoRa 32 V3**, with a live terminal UI and message decryption.

---

## Features

- **Passive LoRa scanner** — listens on a configurable frequency, captures all packets
- **Live terminal UI** — animated curses display with signal bars, RSSI/SNR graph, scrolling packet history
- **MeshCore protocol decoder** — decodes packet headers, hop paths, node advertisements, and frame types
- **Group message decryption** — decrypts public channel messages with the default MeshCore PSK, or a custom PSK
- **Direct message decryption** — decrypts private messages via ECDH using your node's private key and a key database built from captured ADVERT packets
- **Dual logging** — human-readable `.txt` log and structured `.jsonl` log per session
- **One-command installer** — installs dependencies, compiles firmware, flashes board, and sets up the `lora-scan` command

---

## Hardware

| Component | Details |
|-----------|---------|
| Board | Heltec WiFi LoRa 32 **V3** (ESP32-S3 + SX1262 + SSD1306 OLED) |
| Connection | USB (CP210x UART bridge — `/dev/ttyUSB*`) |

Other Heltec LoRa 32 variants (V2, V4) may work with minor pin adjustments in the firmware.

---

## Default Radio Parameters

| Parameter | Value |
|-----------|-------|
| Frequency | 869.618 MHz |
| Bandwidth | 62.5 kHz |
| Spreading Factor | 8 |
| Coding Rate | 4/8 |
| Sync Word | 0x12 (private LoRa) |

These match a common MeshCore network configuration. Edit the `#define` values at the top of `firmware/LoraScanner/LoraScanner.ino` to change them.

---

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/lora-scanner.git
cd lora-scanner
chmod +x install.sh
./install.sh
```

The installer will:
1. Install system packages (`python3-serial`, `python3-nacl`, `python3-cryptography`)
2. Download and configure `arduino-cli`
3. Install the Heltec ESP32 board core and required Arduino libraries
4. Compile and flash the firmware to a connected board
5. Install `lora-scan` as a system command

Then run:
```bash
lora-scan
```

Press `q` to exit.

---

## Manual Installation

### Dependencies

```bash
sudo apt install python3-serial python3-nacl python3-cryptography
```

### Firmware

Install [arduino-cli](https://arduino.github.io/arduino-cli/), then:

```bash
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
    https://resource.heltec.cn/download/package_heltec_esp32_index.json
arduino-cli core update-index
arduino-cli core install Heltec-esp32:esp32
arduino-cli lib install RadioLib "Adafruit SSD1306" "Adafruit GFX Library"
arduino-cli compile --fqbn Heltec-esp32:esp32:heltec_wifi_lora_32_V3 firmware/LoraScanner
arduino-cli upload  --fqbn Heltec-esp32:esp32:heltec_wifi_lora_32_V3 \
    -p /dev/ttyUSB0 firmware/LoraScanner
```

### Monitor

```bash
cd monitor
python3 lora_monitor.py /dev/ttyUSB0
```

---

## Terminal UI

```
  ██╗      ██████╗ ██████╗  █████╗     ███████╗ ██████╗ █████╗ ███╗  ██╗
  ...
  869.618 MHz  │  BW 62.5 kHz  │  SF 8  │  CR 4/8

  ⠙ ONLINE  ◉  O  o  │  Pkts:42  CRC-errs:0  │  log:lora_log.jsonl  │  q=quit

┌─ LAST PACKET ──────────────────────────────────────────────────────────┐
│  RSSI      -72.50 dBm  [████████████████░░░░░░░░░░░░░░]  MEDIUM       │
│  SNR        +8.25 dB   [███████████████████░░░░░░░░░░░]  GOOD         │
│  Freq Err   -312.0 Hz  (within tolerance)                              │
│  Time on Air  345.1 ms  Len: 28 bytes  @12:34:56.123                  │
│  MC Type:  GRP✓        Hops: 3                                         │
│  HEX:  15 06 D4 CD E0 4D ...                                           │
│  DATA: Hello from Node A                                               │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Decryption

### Group / Channel Messages

Group messages on the **public MeshCore channel** are decrypted automatically using the well-known default PSK (`8b3387e9c5cdea6ac9e5edbaa115cd72`). Decrypted text appears in the live UI and log file.

For custom channels:
```bash
python3 monitor/meshcore_decrypt.py group <packet-hex> --key <your-psk-hex>
```

### Direct Messages

Direct messages require your node's private key and the sender's public key.

**Step 1** — extract your private key from your MeshCore node (must be running original MeshCore firmware):
```bash
python3 monitor/meshcore_decrypt.py keys extract-key --port /dev/ttyUSB0
```

Or enter it manually (128 hex chars, from the MeshCore companion app):
```bash
python3 monitor/meshcore_decrypt.py keys set-key <128-hex-chars>
```

**Step 2** — harvest sender public keys from captured ADVERT packets:
```bash
python3 monitor/meshcore_decrypt.py keys harvest ~/lora_log.jsonl
```

**Step 3** — scan and decrypt everything in the log:
```bash
python3 monitor/meshcore_decrypt.py scan ~/lora_log.jsonl --harvest
```

**List known nodes:**
```bash
python3 monitor/meshcore_decrypt.py keys list
```

---

## Log Files

Two log files are written to your home directory during each session:

| File | Format | Contents |
|------|--------|----------|
| `~/lora_log.txt` | Human readable | Signal info, hex dump, MeshCore decode, decrypted message |
| `~/lora_log.jsonl` | JSON Lines | One JSON object per packet, machine-readable |

Example `lora_log.txt` entry:
```
────────────────────────────────────────────────────────────────────────
  Packet #   42    2026-05-01T23:28:20.187432

  Signal
    RSSI          -72.50 dBm   [████████████████░░░░░░░░░░░░░░░░░░░░] MEDIUM
    SNR            +8.25 dB
    Freq Error    -312.0 Hz    (within tolerance)
    Time on Air    345.09 ms

  Frame
    Length      28 bytes

  Payload
             0000:  15 06 D4 CD E0 4D 04 D9 0E B0 8C DC 35 6D DD A3  |.....M......5m..|
             0010:  B1 84 15 55 39 F4 35 AB                           |...U9.5.        |

  MeshCore Decode
    Header        0x15  v0  Route: FLOOD  Type: GRP_TXT
    Hops (6)      D4 → CD → E0 → 4D → 04 → D9
    Src Hash      0xB0
    Dst Hash      0x0E
    MAC           0xDC8C
    Ciphertext    16 bytes (encrypted)

  Decrypted Message
    Hello from Node A
```

---

## File Structure

```
lora-scanner/
├── install.sh                  # Automated installer
├── LICENSE
├── README.md
├── .gitignore
├── firmware/
│   └── LoraScanner/
│       └── LoraScanner.ino     # Arduino sketch for Heltec V3
└── monitor/
    ├── lora_monitor.py         # Curses terminal UI
    ├── meshcore.py             # MeshCore packet decoder
    ├── meshcore_decrypt.py     # AES-128-ECB decryptor + CLI
    └── meshcore_keys.py        # Key database manager
```

---

## MeshCore Crypto

All MeshCore message types use **AES-128-ECB** (no IV).

| Message Type | Key Source |
|--------------|-----------|
| Group (GRP_TXT/GRP_DATA) | 16-byte PSK |
| Direct (TXT_MSG/REQ/RESPONSE) | 16-byte ECDH secret via X25519 |

HMAC-SHA256 (first 2 bytes) is prepended to ciphertext for integrity verification.  
Default public channel PSK: `8b3387e9c5cdea6ac9e5edbaa115cd72`

See [A Hitchhiker's Guide to MeshCore Cryptography](https://jacksbrain.com/2026/01/a-hitchhiker-s-guide-to-meshcore-cryptography/) for full protocol details.

---

## Dependencies

| Dependency | Purpose |
|-----------|---------|
| `python3-serial` | Serial port communication |
| `python3-cryptography` | AES-128-ECB decryption |
| `python3-nacl` | X25519 ECDH for direct message decryption |
| `arduino-cli` | Firmware compilation and flashing |
| `RadioLib` | SX1262 LoRa radio driver |
| `Adafruit SSD1306` + `Adafruit GFX` | OLED display |

---

## License

MIT — see [LICENSE](LICENSE).
