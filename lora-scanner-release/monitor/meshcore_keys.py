"""
MeshCore key database.

Stores:
  - User's own private key (extracted from device or entered manually)
  - Known node public keys (harvested from captured ADVERT packets)

Key file: ~/.meshcore_keys.json
"""

import json, os, hashlib, datetime

KEY_FILE = os.path.expanduser('~/.meshcore_keys.json')

def _load() -> dict:
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {'own': {}, 'nodes': {}}

def _save(db: dict):
    with open(KEY_FILE, 'w') as f:
        json.dump(db, f, indent=2)

# ── Own identity ──────────────────────────────────────────────────────────────

def store_own_key(priv_hex: str, pub_hex: str = '', name: str = ''):
    """Save the user's own private key (and optionally public key + name)."""
    priv_hex = priv_hex.strip().lower()
    if len(priv_hex) != 128:
        raise ValueError(f"Private key must be 128 hex chars (64 bytes), got {len(priv_hex)}")
    db = _load()
    # Public key hash = first byte of pub key, used to match incoming packets
    pub_hash = ''
    if pub_hex:
        pub_hash = f"0x{bytes.fromhex(pub_hex)[0]:02X}"
    db['own'] = {
        'priv':     priv_hex,
        'pub':      pub_hex.strip().lower(),
        'pub_hash': pub_hash,
        'name':     name,
        'added':    datetime.datetime.now().isoformat(),
    }
    _save(db)
    print(f"Saved own private key{f' ({name})' if name else ''}")

def get_own_key() -> dict:
    return _load().get('own', {})

# ── Known nodes ───────────────────────────────────────────────────────────────

def store_node(pub_hex: str, name: str = '', source: str = 'manual'):
    """Store a known node's public key."""
    pub_hex = pub_hex.strip().lower()
    if len(pub_hex) != 64:
        raise ValueError(f"Public key must be 64 hex chars (32 bytes), got {len(pub_hex)}")
    pub_bytes = bytes.fromhex(pub_hex)
    pub_hash  = f"0x{pub_bytes[0]:02X}"
    db = _load()
    db['nodes'][pub_hex] = {
        'pub':      pub_hex,
        'pub_hash': pub_hash,
        'name':     name,
        'source':   source,
        'added':    datetime.datetime.now().isoformat(),
    }
    _save(db)
    return pub_hash

def get_nodes_by_hash(hash_byte: int) -> list:
    """Return all known nodes whose public key starts with hash_byte."""
    db = _load()
    target = f"0x{hash_byte:02X}"
    return [n for n in db['nodes'].values() if n.get('pub_hash') == target]

def list_nodes() -> list:
    return list(_load().get('nodes', {}).values())

# ── Harvest public keys from ADVERT packets in a JSONL log ───────────────────

def harvest_from_log(log_path: str) -> int:
    """
    Scan a lora_log.jsonl file for ADVERT packets and extract public keys.
    Returns number of new keys added.
    """
    import meshcore
    added = 0
    db = _load()

    with open(log_path) as f:
        for line in f:
            try:
                pkt = json.loads(line)
            except json.JSONDecodeError:
                continue

            raw_hex = pkt.get('hex', '')
            if not raw_hex:
                continue

            decoded = meshcore.decode(raw_hex)
            if not decoded or decoded.get('_ptype_code') != 0x04:
                continue

            pub_key = decoded.get('advert_pubkey', '')
            name    = decoded.get('advert_name', '')
            if not pub_key or len(pub_key) != 64:
                continue

            pub_hex = pub_key.lower()
            if pub_hex not in db['nodes']:
                pub_hash = store_node(pub_hex, name, source='advert_harvest')
                print(f"  Found node: {name or '(unnamed)':20s}  pubkey {pub_hex[:16]}…  hash {pub_hash}")
                added += 1
                db = _load()  # reload after each save

    return added

# ── Extract private key from MeshCore device via serial ──────────────────────

def extract_from_device(port: str = '/dev/ttyUSB0', baud: int = 115200) -> str:
    """
    Send 'get prv.key' to a MeshCore node and return the private key hex string.
    The node must be running original MeshCore firmware (not the scanner).
    """
    import serial, time

    print(f"Connecting to {port} @ {baud}...")
    try:
        s = serial.Serial(port, baud, timeout=3)
    except serial.SerialException as e:
        raise RuntimeError(f"Cannot open {port}: {e}")

    time.sleep(0.5)
    s.reset_input_buffer()

    s.write(b'get prv.key\r\n')
    time.sleep(1.0)

    response = s.read(s.in_waiting or 512).decode('utf-8', errors='replace')
    s.close()

    # Response is typically: "prv.key=<128 hex chars>" or just the hex string
    for line in response.splitlines():
        line = line.strip()
        # Strip any "prv.key=" prefix
        if '=' in line:
            line = line.split('=', 1)[1].strip()
        # Validate: 128 hex characters
        if len(line) == 128 and all(c in '0123456789abcdefABCDEF' for c in line):
            return line.lower()

    raise RuntimeError(f"Could not parse private key from response:\n{response}")
