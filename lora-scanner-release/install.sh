#!/usr/bin/env bash
# LoRa MeshCore Scanner — installer
# Supports: Heltec WiFi LoRa 32 V3

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERR ]${NC}  $*"; }
section() { echo -e "\n${BOLD}━━━  $*  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.lora-scanner"
BIN_DIR="$HOME/.local/bin"
FQBN="Heltec-esp32:esp32:heltec_wifi_lora_32_V3"
HELTEC_URL="https://resource.heltec.cn/download/package_heltec_esp32_index.json"
ARDUINO_CLI_URL="https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Linux_64bit.tar.gz"
SKIP_FLASH=0
SKIP_COMPILE=0
PORT=""

for arg in "$@"; do
    case "$arg" in
        --skip-flash)   SKIP_FLASH=1 ;;
        --skip-compile) SKIP_COMPILE=1; SKIP_FLASH=1 ;;
        --port=*)       PORT="${arg#--port=}" ;;
        --help|-h)
            echo "Usage: $0 [--skip-flash] [--skip-compile] [--port=/dev/ttyUSBx]"
            exit 0 ;;
    esac
done

echo -e "${CYAN}"
cat << 'EOF'
  ██╗      ██████╗ ██████╗  █████╗     ███████╗ ██████╗ █████╗ ███╗  ██╗
  ██║     ██╔═══██╗██╔══██╗██╔══██╗    ██╔════╝██╔════╝██╔══██╗████╗ ██║
  ██║     ██║   ██║██████╔╝███████║    ███████╗██║     ███████║██╔██╗██║
  ██║     ██║   ██║██╔══██╗██╔══██║    ╚════██║██║     ██╔══██║██║╚████║
  ███████╗╚██████╔╝██║  ██║██║  ██║    ███████║╚██████╗██║  ██║██║ ╚███║
  ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝   ╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚══╝
               MeshCore Scanner — Heltec WiFi LoRa 32 V3
EOF
echo -e "${NC}"

# ── 1. System packages ────────────────────────────────────────────────────────
section "System dependencies"
MISSING=()
for pkg in python3 python3-serial python3-nacl curl tar; do
    dpkg -s "$pkg" &>/dev/null || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    info "Installing: ${MISSING[*]}"
    sudo apt-get install -y "${MISSING[@]}" 2>&1 | grep -E "^(Setting up|Err)" || true
fi
# cryptography lib (for AES)
python3 -c "from cryptography.hazmat.primitives.ciphers import Cipher" 2>/dev/null || \
    sudo apt-get install -y python3-cryptography 2>&1 | grep -E "^(Setting up|Err)" || true
ok "System packages ready"

# ── 2. arduino-cli ────────────────────────────────────────────────────────────
section "arduino-cli"
if ! command -v arduino-cli &>/dev/null; then
    info "Downloading arduino-cli..."
    curl -fsSL "$ARDUINO_CLI_URL" -o /tmp/arduino-cli.tar.gz
    sudo tar -xzf /tmp/arduino-cli.tar.gz -C /usr/local/bin arduino-cli
    rm -f /tmp/arduino-cli.tar.gz
fi
ok "arduino-cli $(arduino-cli version | head -1)"

# ── 3. Board manager ──────────────────────────────────────────────────────────
section "Heltec board manager"
YAML="$HOME/.arduino15/arduino-cli.yaml"
[[ -f "$YAML" ]] || arduino-cli config init &>/dev/null
grep -q "heltec" "$YAML" 2>/dev/null || \
    arduino-cli config add board_manager.additional_urls "$HELTEC_URL" &>/dev/null
info "Updating index..."
arduino-cli core update-index &>/dev/null
ok "Index updated"

# ── 4. Heltec ESP32 core ──────────────────────────────────────────────────────
section "Heltec ESP32 core"
if ! arduino-cli core list 2>/dev/null | grep -q "Heltec-esp32"; then
    info "Installing Heltec-esp32:esp32 (may take a minute)..."
    arduino-cli core install Heltec-esp32:esp32 2>&1 | grep -E "^(Installing|installed)" | head -5 || true
fi
ok "Core: $(arduino-cli core list 2>/dev/null | grep Heltec-esp32 | awk '{print $1, $2}')"

# ── 5. Arduino libraries ──────────────────────────────────────────────────────
section "Arduino libraries"
for lib in "RadioLib" "Adafruit SSD1306" "Adafruit GFX Library"; do
    arduino-cli lib list 2>/dev/null | grep -qi "^${lib%%\ *}" || \
        arduino-cli lib install "$lib" &>/dev/null
    ok "  $lib"
done

# ── 6. Compile firmware ───────────────────────────────────────────────────────
section "Compile firmware"
SKETCH="$SCRIPT_DIR/firmware/LoraScanner"
if [[ $SKIP_COMPILE -eq 0 ]]; then
    info "Compiling for $FQBN..."
    OUT=$(arduino-cli compile --fqbn "$FQBN" "$SKETCH" 2>&1)
    if echo "$OUT" | grep -q "Sketch uses"; then
        ok "$(echo "$OUT" | grep 'Sketch uses' | awk '{print $3, $4, $5}')"
    else
        err "Compile failed:"; echo "$OUT"; exit 1
    fi
else
    warn "Skipping compile"
fi

# ── 7. Device detection ───────────────────────────────────────────────────────
section "Device detection"
if [[ $SKIP_FLASH -eq 0 && -z "$PORT" ]]; then
    # CP210x = Heltec V3 USB-to-serial chip
    if lsusb 2>/dev/null | grep -q "10c4:ea60"; then
        PORT=$(ls /dev/ttyUSB* 2>/dev/null | head -1 || echo "")
    fi
    if [[ -z "$PORT" ]]; then
        warn "No Heltec device detected — skipping flash"
        warn "Connect board then re-run: $0 --skip-compile"
        SKIP_FLASH=1
    else
        ok "Detected at $PORT"
    fi
fi

# ── 8. Serial permissions ─────────────────────────────────────────────────────
section "Serial port permissions"
if ! groups | grep -q dialout; then
    sudo usermod -a -G dialout "$USER"
    sudo chmod a+rw /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true
    ok "Added $USER to dialout (re-login may be needed)"
else
    ok "Already in dialout group"
fi

# ── 9. Flash firmware ─────────────────────────────────────────────────────────
section "Flash firmware"
if [[ $SKIP_FLASH -eq 0 ]]; then
    sudo chmod a+rw "$PORT" 2>/dev/null || true
    info "Flashing to $PORT..."
    OUT=$(arduino-cli upload --fqbn "$FQBN" -p "$PORT" "$SKETCH" 2>&1)
    if echo "$OUT" | grep -q "Hard resetting"; then
        ok "Firmware flashed"
    else
        err "Flash failed:"; echo "$OUT"; exit 1
    fi
else
    warn "Flash skipped"
fi

# ── 10. Install monitor ───────────────────────────────────────────────────────
section "Monitor scripts"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
cp "$SCRIPT_DIR"/monitor/*.py "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/lora_monitor.py"

cat > "$BIN_DIR/lora-scan" << LAUNCHER
#!/usr/bin/env bash
PORT="\${1:-}"
if [[ -z "\$PORT" ]]; then
    for p in /dev/ttyUSB* /dev/ttyACM*; do [[ -e "\$p" ]] && PORT="\$p" && break; done
fi
if [[ -z "\$PORT" ]]; then
    echo "No serial device found. Pass port as argument or connect the board."
    exit 1
fi
cd "$INSTALL_DIR"
LORA_PORT="\$PORT" python3 lora_monitor.py
LAUNCHER
chmod +x "$BIN_DIR/lora-scan"

grep -q "$BIN_DIR" "$HOME/.bashrc" 2>/dev/null || \
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"

ok "Scripts installed to $INSTALL_DIR"
ok "Command: lora-scan"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}  Installation complete!${NC}"
echo ""
echo -e "  ${BOLD}Run scanner:${NC}      ${CYAN}lora-scan${NC}  (or ${CYAN}lora-scan /dev/ttyUSB0${NC})"
echo -e "  ${BOLD}Flash only:${NC}       ${CYAN}./install.sh --skip-compile${NC}"
echo ""
echo -e "  ${BOLD}Decrypt messages:${NC}"
echo -e "    Extract your private key:   ${CYAN}python3 $INSTALL_DIR/meshcore_decrypt.py keys extract-key${NC}"
echo -e "    Harvest node public keys:   ${CYAN}python3 $INSTALL_DIR/meshcore_decrypt.py keys harvest ~/lora_log.jsonl${NC}"
echo -e "    Scan + decrypt log:         ${CYAN}python3 $INSTALL_DIR/meshcore_decrypt.py scan ~/lora_log.jsonl${NC}"
echo ""
echo -e "  ${BOLD}Logs:${NC}  ~/lora_log.txt  (human)   ~/lora_log.jsonl  (JSON)"
echo ""
