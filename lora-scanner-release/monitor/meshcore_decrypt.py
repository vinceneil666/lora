"""
MeshCore AES-128-ECB decryptor.

All MeshCore message types use AES-128-ECB (no IV).
Key sources:
  - Group / channel messages: 16-byte PSK (shared passphrase)
  - Direct messages:          16-byte ECDH-derived shared secret

Plaintext structure after decryption:
  [4 bytes: Unix timestamp LE] [1 byte: flags] [message text] [zero padding to 16-byte boundary]

Ref: https://github.com/meshcore-dev/MeshCore
     https://jacksbrain.com/2026/01/a-hitchhiker-s-guide-to-meshcore-cryptography/
"""

import struct, hmac as _hmac, hashlib, datetime, base64, sys, os

# ── AES backend (cryptography lib, standard on Ubuntu) ────────────────────────
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# Default public channel key — universally known, all traffic should be
# treated as public.
DEFAULT_PSK_HEX = "8b3387e9c5cdea6ac9e5edbaa115cd72"
DEFAULT_PSK     = bytes.fromhex(DEFAULT_PSK_HEX)

# ── Low-level AES-128-ECB ─────────────────────────────────────────────────────

def _aes128_ecb_decrypt(key16: bytes, ciphertext: bytes) -> bytes:
    """Decrypt ciphertext with AES-128-ECB. Input must be a multiple of 16 bytes."""
    decryptor = Cipher(
        algorithms.AES(key16),
        modes.ECB(),
        backend=default_backend(),
    ).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _verify_mac(key16: bytes, ciphertext: bytes, mac2: bytes) -> bool:
    """HMAC-SHA256 over ciphertext, key zero-padded to 32 bytes, first 2 bytes."""
    key32 = key16.ljust(32, b'\x00')
    expected = _hmac.new(key32, ciphertext, hashlib.sha256).digest()[:2]
    return expected == mac2


def _parse_plaintext(raw: bytes) -> dict:
    """Parse decrypted MeshCore plaintext into structured fields."""
    if len(raw) < 5:
        return {'error': 'plaintext too short'}
    ts_raw  = struct.unpack_from('<I', raw, 0)[0]
    flags   = raw[4]
    message = raw[5:].rstrip(b'\x00').decode('utf-8', errors='replace')
    ts_str  = datetime.datetime.fromtimestamp(ts_raw).strftime('%Y-%m-%d %H:%M:%S')
    attempt = flags & 0x03
    mtype   = (flags >> 2) & 0x3F
    return {
        'timestamp':   ts_str,
        'ts_raw':      ts_raw,
        'flags':       f"0x{flags:02X}",
        'attempt':     attempt,
        'msg_subtype': mtype,
        'message':     message,
    }

# ── Group / channel message decryption ───────────────────────────────────────
#
# GRP_TXT payload wire layout (after the MeshCore path bytes):
#   [1 byte:  channel_hash  = sha256(key)[0]]
#   [2 bytes: MAC           = HMAC-SHA256(key_32, ciphertext)[:2]]
#   [N bytes: ciphertext    = AES-128-ECB blocks]

def decrypt_group(payload_bytes: bytes, channel_key: bytes = DEFAULT_PSK) -> dict:
    """
    Decrypt a MeshCore group/channel message payload.

    Args:
        payload_bytes: raw payload bytes (after path field)
        channel_key:   16-byte PSK (default = public channel key)

    Returns dict with 'ok', 'message', 'timestamp', etc.
    On failure returns dict with 'ok': False and 'error'.
    """
    if len(payload_bytes) < 3:
        return {'ok': False, 'error': 'payload too short'}

    channel_hash = payload_bytes[0]
    mac          = payload_bytes[1:3]
    ciphertext   = payload_bytes[3:]

    # Verify channel hash — first byte of SHA256(key)
    expected_hash = hashlib.sha256(channel_key).digest()[0]
    if channel_hash != expected_hash:
        return {
            'ok':    False,
            'error': f"channel hash mismatch (got 0x{channel_hash:02X}, "
                     f"expected 0x{expected_hash:02X}) — wrong key?",
        }

    # Verify MAC
    if not _verify_mac(channel_key, ciphertext, mac):
        return {'ok': False, 'error': 'MAC verification failed — corrupted or wrong key'}

    # Must be a multiple of 16 bytes
    if len(ciphertext) % 16 != 0:
        return {'ok': False, 'error': f'ciphertext length {len(ciphertext)} not multiple of 16'}

    plaintext = _aes128_ecb_decrypt(channel_key[:16], ciphertext)
    result    = _parse_plaintext(plaintext)
    result['ok']          = True
    result['key_source']  = 'default_psk' if channel_key == DEFAULT_PSK else 'custom_psk'
    result['channel_hash'] = f"0x{channel_hash:02X}"
    return result


def decrypt_group_hex(payload_hex: str, channel_key_hex: str = DEFAULT_PSK_HEX) -> dict:
    """Convenience wrapper — accepts hex strings."""
    try:
        key = bytes.fromhex(channel_key_hex)
    except ValueError:
        return {'ok': False, 'error': 'invalid key hex'}
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        return {'ok': False, 'error': 'invalid payload hex'}
    return decrypt_group(payload, key)


# ── Direct message decryption (ECDH shared secret) ───────────────────────────
#
# Direct message (TXT_MSG) payload wire layout:
#   [1 byte:  dst_hash = pub_key[0]]
#   [1 byte:  src_hash = pub_key[0]]
#   [2 bytes: MAC]
#   [N bytes: ciphertext]
#
# Shared secret = X25519 ECDH using:
#   - Sender's clamped Ed25519 private scalar (first 32 bytes of 64-byte priv key)
#   - Recipient's Ed25519 public key converted to X25519

def _clamp_scalar(scalar32: bytes) -> bytes:
    s = bytearray(scalar32[:32])
    s[0]  &= 248
    s[31] &= 63
    s[31] |= 64
    return bytes(s)


def derive_shared_secret(my_priv_key_hex: str, their_pub_key_hex: str) -> bytes:
    """
    Derive the 32-byte MeshCore shared secret via ECDH.
    my_priv_key_hex:    64-byte (128 hex chars) Ed25519 private key
    their_pub_key_hex:  32-byte (64 hex chars)  Ed25519 public key
    Returns 32-byte shared secret; first 16 bytes = AES key.
    """
    try:
        from nacl.bindings import crypto_sign_ed25519_pk_to_curve25519, crypto_scalarmult
    except ImportError:
        raise RuntimeError("PyNaCl required for direct message decryption: pip install pynacl")

    priv = bytes.fromhex(my_priv_key_hex)
    pub  = bytes.fromhex(their_pub_key_hex)

    clamped    = _clamp_scalar(priv[:32])
    x25519_pub = crypto_sign_ed25519_pk_to_curve25519(pub)
    return crypto_scalarmult(clamped, x25519_pub)


def decrypt_direct(payload_bytes: bytes, shared_secret: bytes) -> dict:
    """
    Decrypt a MeshCore direct message (TXT_MSG / REQ / RESPONSE).

    Args:
        payload_bytes: raw payload bytes (after path field)
        shared_secret: 32-byte ECDH shared secret
    """
    if len(payload_bytes) < 4:
        return {'ok': False, 'error': 'payload too short'}

    dst_hash   = payload_bytes[0]
    src_hash   = payload_bytes[1]
    mac        = payload_bytes[2:4]
    ciphertext = payload_bytes[4:]

    key16 = shared_secret[:16]

    if not _verify_mac(key16, ciphertext, mac):
        return {'ok': False, 'error': 'MAC verification failed — wrong key pair?'}

    if len(ciphertext) % 16 != 0:
        return {'ok': False, 'error': f'ciphertext length {len(ciphertext)} not multiple of 16'}

    plaintext = _aes128_ecb_decrypt(key16, ciphertext)
    result    = _parse_plaintext(plaintext)
    result['ok']         = True
    result['key_source'] = 'ecdh'
    result['dst_hash']   = f"0x{dst_hash:02X}"
    result['src_hash']   = f"0x{src_hash:02X}"
    return result


def decrypt_direct_hex(payload_hex: str,
                        my_priv_key_hex: str,
                        their_pub_key_hex: str) -> dict:
    """Convenience wrapper — accepts hex strings."""
    try:
        secret = derive_shared_secret(my_priv_key_hex, their_pub_key_hex)
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        return {'ok': False, 'error': 'invalid payload hex'}
    return decrypt_direct(payload, secret)


# ── Try all known keys ────────────────────────────────────────────────────────

def try_decrypt(payload_hex: str,
                ptype_code: int,
                extra_keys: list = None) -> dict:
    """
    Auto-attempt decryption given a payload hex string and MeshCore payload type code.
    Tries the default public PSK and any extra PSKs provided.
    Returns best result found, or failure info.
    """
    if ptype_code in (0x05, 0x06):
        # Group message — try PSK list
        keys = [DEFAULT_PSK]
        for k in (extra_keys or []):
            try:
                keys.append(bytes.fromhex(k) if len(k) == 32 else
                             base64.b64decode(k) if '=' in k else bytes.fromhex(k))
            except Exception:
                pass
        for key in keys:
            r = decrypt_group(bytes.fromhex(payload_hex), key)
            if r.get('ok'):
                return r
        return {'ok': False, 'error': 'all PSK attempts failed', 'tried': len(keys)}

    elif ptype_code in (0x00, 0x01, 0x02, 0x07):
        return {'ok': False, 'error': 'direct message — requires private key (use decrypt_direct_hex)'}

    else:
        return {'ok': False, 'error': f'payload type 0x{ptype_code:02X} is not encrypted'}


# ── Try direct message with key database ─────────────────────────────────────

def try_decrypt_direct(payload_bytes: bytes, decoded_mc: dict) -> dict:
    """
    Try to decrypt a direct message using the key database.
    Looks up the sender's public key by src_hash, then tries ECDH with own key.
    """
    import meshcore_keys as _keys

    own = _keys.get_own_key()
    if not own.get('priv'):
        return {'ok': False, 'error': 'own private key not in key database (run: keys extract-key)'}

    if len(payload_bytes) < 4:
        return {'ok': False, 'error': 'payload too short'}

    src_hash_byte = payload_bytes[1]
    dst_hash_byte = payload_bytes[0]

    candidates = _keys.get_nodes_by_hash(src_hash_byte)
    if not candidates:
        return {
            'ok':    False,
            'error': f'no known node with src_hash 0x{src_hash_byte:02X} — '
                     f'harvest ADVERTs first (run: scan --harvest)',
        }

    for node in candidates:
        try:
            secret = derive_shared_secret(own['priv'], node['pub'])
            r = decrypt_direct(payload_bytes, secret)
            if r.get('ok'):
                r['src_node'] = node.get('name') or node['pub'][:16] + '…'
                return r
        except Exception as e:
            continue

    return {'ok': False, 'error': f'ECDH decrypt failed for all {len(candidates)} candidate(s) with hash 0x{src_hash_byte:02X}'}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt(r: dict) -> str:
    if not r.get('ok'):
        return f"  FAIL  {r.get('error','unknown error')}"
    lines = [f"  OK    [{r.get('key_source','?')}]"]
    if r.get('src_node'):
        lines.append(f"  From  {r['src_node']}")
    lines.append(f"  Time  {r.get('timestamp','?')}")
    lines.append(f"  Msg   {r.get('message','(empty)')}")
    if r.get('attempt'):
        lines.append(f"  Retry #{r['attempt']}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import argparse, json, meshcore as _mc, meshcore_keys as _keys

    parser = argparse.ArgumentParser(
        description='MeshCore AES-128-ECB decryptor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Step 1 — extract your private key from a MeshCore node:
  python3 meshcore_decrypt.py keys extract-key --port /dev/ttyUSB0

  # Step 1 (alt) — enter your private key manually:
  python3 meshcore_decrypt.py keys set-key <128-hex-char-private-key>

  # Step 2 — harvest known node public keys from the log:
  python3 meshcore_decrypt.py keys harvest ~/lora_log.jsonl

  # Step 3 — show known nodes:
  python3 meshcore_decrypt.py keys list

  # Scan log — decrypt all group AND direct messages:
  python3 meshcore_decrypt.py scan ~/lora_log.jsonl

  # Decrypt a single group packet (hex = full raw packet):
  python3 meshcore_decrypt.py group <full-packet-hex>

  # Decrypt a single direct packet:
  python3 meshcore_decrypt.py direct <full-packet-hex>
""")

    sub = parser.add_subparsers(dest='cmd', required=True)

    # keys
    p_keys = sub.add_parser('keys', help='Manage key database')
    keys_sub = p_keys.add_subparsers(dest='keys_cmd', required=True)

    p_extract = keys_sub.add_parser('extract-key', help='Extract private key from MeshCore device via serial')
    p_extract.add_argument('--port', default='/dev/ttyUSB0', help='Serial port')
    p_extract.add_argument('--baud', type=int, default=115200)
    p_extract.add_argument('--name', default='', help='Optional node name')

    p_setkey = keys_sub.add_parser('set-key', help='Enter private key manually')
    p_setkey.add_argument('priv', help='128 hex char private key')
    p_setkey.add_argument('--pub',  default='', help='Optional 64 hex char public key')
    p_setkey.add_argument('--name', default='', help='Optional node name')

    p_harvest = keys_sub.add_parser('harvest', help='Harvest public keys from ADVERT packets in log')
    p_harvest.add_argument('logfile', help='Path to lora_log.jsonl')

    p_list = keys_sub.add_parser('list', help='List all known nodes')

    # group
    p_grp = sub.add_parser('group', help='Decrypt a single group message (full packet hex)')
    p_grp.add_argument('packet', help='Full raw packet hex')
    p_grp.add_argument('--key', help='PSK as hex or base64 (default: public channel key)')

    # direct
    p_dir = sub.add_parser('direct', help='Decrypt a single direct message (full packet hex)')
    p_dir.add_argument('packet', help='Full raw packet hex')
    p_dir.add_argument('--priv', help='Override private key (128 hex chars)')
    p_dir.add_argument('--pub',  help='Override sender public key (64 hex chars)')

    # scan
    p_scan = sub.add_parser('scan', help='Scan log file and decrypt all packets')
    p_scan.add_argument('logfile', help='Path to lora_log.jsonl')
    p_scan.add_argument('--key',     help='Extra group PSK (hex or base64)')
    p_scan.add_argument('--harvest', action='store_true',
                        help='Also harvest ADVERT public keys before scanning')

    args = parser.parse_args()

    # ── keys subcommands ──────────────────────────────────────────────────────
    if args.cmd == 'keys':
        if args.keys_cmd == 'extract-key':
            try:
                priv = _keys.extract_from_device(args.port, args.baud)
                print(f"Private key: {priv}")
                _keys.store_own_key(priv, name=args.name)
            except Exception as e:
                print(f"Error: {e}"); sys.exit(1)

        elif args.keys_cmd == 'set-key':
            try:
                _keys.store_own_key(args.priv, args.pub, args.name)
            except ValueError as e:
                print(f"Error: {e}"); sys.exit(1)

        elif args.keys_cmd == 'harvest':
            print(f"Harvesting public keys from {args.logfile}...")
            n = _keys.harvest_from_log(args.logfile)
            print(f"Added {n} new node(s)")
            print(f"Total known nodes: {len(_keys.list_nodes())}")

        elif args.keys_cmd == 'list':
            own = _keys.get_own_key()
            if own.get('priv'):
                print(f"Own key:  {own.get('name','(unnamed)'):20s}  "
                      f"pub_hash={own.get('pub_hash','?')}  "
                      f"pub={own.get('pub','?')[:16]}…")
            else:
                print("Own key:  (not set — run: keys extract-key or keys set-key)")
            print()
            nodes = _keys.list_nodes()
            if nodes:
                print(f"{'Name':<22} {'Hash':<6} {'Public Key':<36} Source")
                print('─' * 75)
                for n in sorted(nodes, key=lambda x: x.get('name','')):
                    print(f"  {n.get('name','(unnamed)'):<20} {n['pub_hash']:<6} "
                          f"{n['pub'][:32]}…  {n.get('source','?')}")
            else:
                print("No known nodes — run: keys harvest <logfile>")

    # ── group ─────────────────────────────────────────────────────────────────
    elif args.cmd == 'group':
        decoded = _mc.decode(args.packet)
        if not decoded:
            print("Could not decode MeshCore packet"); sys.exit(1)
        offset        = decoded.get('payload_offset', 0)
        payload_bytes = bytes.fromhex(args.packet)[offset:]
        key = DEFAULT_PSK
        if args.key:
            try:
                key = base64.b64decode(args.key) if '=' in args.key else bytes.fromhex(args.key)
            except Exception as e:
                print(f"Bad key: {e}"); sys.exit(1)
        r = decrypt_group(payload_bytes, key)
        print(_fmt(r))

    # ── direct ────────────────────────────────────────────────────────────────
    elif args.cmd == 'direct':
        decoded = _mc.decode(args.packet)
        if not decoded:
            print("Could not decode MeshCore packet"); sys.exit(1)
        offset        = decoded.get('payload_offset', 0)
        payload_bytes = bytes.fromhex(args.packet)[offset:]

        if args.priv and args.pub:
            r = decrypt_direct_hex(bytes.fromhex(args.packet)[offset:].hex(), args.priv, args.pub)
        else:
            r = try_decrypt_direct(payload_bytes, decoded)
        print(_fmt(r))

    # ── scan ──────────────────────────────────────────────────────────────────
    elif args.cmd == 'scan':
        if args.harvest:
            print(f"Harvesting keys from {args.logfile}...")
            n = _keys.harvest_from_log(args.logfile)
            print(f"Added {n} new node(s)\n")

        extra_key = DEFAULT_PSK
        if args.key:
            try:
                extra_key = base64.b64decode(args.key) if '=' in args.key else bytes.fromhex(args.key)
            except Exception as e:
                print(f"Bad key: {e}"); sys.exit(1)

        grp_total = grp_ok = dir_total = dir_ok = 0

        with open(args.logfile) as f:
            for line in f:
                try:
                    pkt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_hex = pkt.get('hex', '')
                if not raw_hex:
                    continue
                decoded = _mc.decode(raw_hex)
                if not decoded:
                    continue
                ptype  = decoded.get('_ptype_code', -1)
                offset = decoded.get('payload_offset', 0)
                payload_bytes = bytes.fromhex(raw_hex)[offset:]
                ts   = pkt.get('time', '?')
                rssi = pkt.get('rssi', '?')
                hops = decoded.get('hop_count', 0)

                if ptype in (0x05, 0x06):
                    grp_total += 1
                    for key in [DEFAULT_PSK, extra_key]:
                        r = decrypt_group(payload_bytes, key)
                        if r.get('ok'):
                            grp_ok += 1
                            print(f"\n[{ts}] GRP  RSSI {rssi} dBm  hops={hops}")
                            print(f"  {r.get('timestamp','?')}  {r.get('message','')}")
                            break
                    else:
                        print(f"\n[{ts}] GRP  FAIL (wrong PSK?)")

                elif ptype in (0x00, 0x01, 0x02, 0x07):
                    dir_total += 1
                    r = try_decrypt_direct(payload_bytes, decoded)
                    if r.get('ok'):
                        dir_ok += 1
                        print(f"\n[{ts}] DIR  RSSI {rssi} dBm  hops={hops}  "
                              f"from={r.get('src_node','?')}")
                        print(f"  {r.get('timestamp','?')}  {r.get('message','')}")
                    else:
                        print(f"\n[{ts}] DIR  FAIL: {r.get('error','')}")

        print(f"\n{'─'*55}")
        print(f"  Group   : {grp_ok}/{grp_total} decrypted")
        print(f"  Direct  : {dir_ok}/{dir_total} decrypted")
        print(f"{'─'*55}")
