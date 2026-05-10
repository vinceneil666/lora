"""
MeshCore packet decoder.
Spec: https://github.com/meshcore-dev/MeshCore
"""
import struct

ROUTE_TYPES = {
    0x00: "FLOOD+TRANSPORT",
    0x01: "FLOOD",
    0x02: "DIRECT",
    0x03: "DIRECT+TRANSPORT",
}

PAYLOAD_TYPES = {
    0x00: "REQ",
    0x01: "RESPONSE",
    0x02: "TXT_MSG",
    0x03: "ACK",
    0x04: "ADVERT",
    0x05: "GRP_TXT",
    0x06: "GRP_DATA",
    0x07: "ANON_REQ",
    0x08: "PATH",
    0x09: "TRACE",
    0x0A: "MULTIPART",
    0x0B: "CONTROL",
    0x0F: "RAW_CUSTOM",
}

HASH_SIZES = [1, 2, 3, 4]   # indexed by hash_size_code bits 6-7

def _safe_str(b):
    """Convert bytes to printable string."""
    return ''.join(chr(c) if 32 <= c < 127 else '.' for c in b)

def decode(hex_str):
    """
    Decode a MeshCore packet from a hex string.
    Returns a dict with all decoded fields, plus 'errors' list if anything failed.
    Returns None if hex_str is empty or not parseable at all.
    """
    if not hex_str:
        return None
    try:
        data = bytes.fromhex(hex_str)
    except ValueError:
        return None
    if len(data) < 2:
        return None

    result = {'raw_len': len(data), 'errors': [], 'notes': []}
    offset = 0

    # ── Header byte ──────────────────────────────────────────────────────────
    header        = data[offset]; offset += 1
    route_type    = header & 0x03
    payload_type  = (header >> 2) & 0x0F
    version       = (header >> 6) & 0x03

    result['header']       = f"0x{header:02X}"
    result['route_type']   = ROUTE_TYPES.get(route_type, f"UNKNOWN(0x{route_type:X})")
    result['payload_type'] = PAYLOAD_TYPES.get(payload_type, f"UNKNOWN(0x{payload_type:X})")
    result['version']      = version
    result['_route_code']  = route_type
    result['_ptype_code']  = payload_type

    # ── Transport codes (only for FLOOD+TRANSPORT or DIRECT+TRANSPORT) ───────
    if route_type in (0x00, 0x03):
        if offset + 4 <= len(data):
            tc1, tc2 = struct.unpack_from('<HH', data, offset)
            result['transport_code_1'] = f"0x{tc1:04X}"
            result['transport_code_2'] = f"0x{tc2:04X}"
            offset += 4
        else:
            result['errors'].append("truncated: missing transport codes")
            return result

    # ── Path length byte ─────────────────────────────────────────────────────
    if offset >= len(data):
        result['errors'].append("truncated: missing path length byte")
        return result

    path_len_byte  = data[offset]; offset += 1
    hop_count      = path_len_byte & 0x3F
    hash_size_code = (path_len_byte >> 6) & 0x03
    hash_size      = HASH_SIZES[hash_size_code]

    result['hop_count']  = hop_count
    result['hash_size']  = hash_size

    # ── Path (hop_count × hash_size bytes) ───────────────────────────────────
    path_byte_len = hop_count * hash_size
    if offset + path_byte_len > len(data):
        result['errors'].append(f"truncated: path needs {path_byte_len} bytes, only {len(data)-offset} available")
        return result

    path_data = data[offset:offset + path_byte_len]
    offset += path_byte_len
    result['payload_offset'] = offset   # byte index where payload starts in raw packet

    if hop_count > 0:
        hops = []
        for i in range(hop_count):
            h = path_data[i*hash_size:(i+1)*hash_size]
            hops.append(h.hex().upper())
        result['path_hops'] = hops
    else:
        result['path_hops'] = []

    # ── Payload ───────────────────────────────────────────────────────────────
    payload = data[offset:]
    result['payload_len'] = len(payload)

    pt = payload_type

    # ACK — 4-byte CRC32
    if pt == 0x03:
        if len(payload) >= 4:
            crc = struct.unpack_from('<I', payload)[0]
            result['ack_crc32'] = f"0x{crc:08X}"
        else:
            result['errors'].append("ACK payload too short")

    # ADVERT — public key + timestamp + signature + app data
    elif pt == 0x04:
        if len(payload) >= 100:
            pub_key   = payload[0:32]
            timestamp = struct.unpack_from('<I', payload, 32)[0]
            signature = payload[36:100]
            app_data  = payload[100:]

            result['advert_pubkey']    = pub_key.hex().upper()
            result['advert_timestamp'] = timestamp
            result['advert_signature'] = signature.hex().upper()[:32] + '…'

            # App data: flags + optional lat/lon + node name
            if len(app_data) >= 1:
                flags = app_data[0]
                result['advert_flags'] = f"0x{flags:02X}"
                ad_offset = 1
                if flags & 0x01 and len(app_data) >= ad_offset + 8:
                    lat, lon = struct.unpack_from('<ff', app_data, ad_offset)
                    result['advert_lat'] = round(lat, 6)
                    result['advert_lon'] = round(lon, 6)
                    ad_offset += 8
                name_bytes = app_data[ad_offset:]
                name = name_bytes.decode('utf-8', errors='replace').rstrip('\x00')
                result['advert_name'] = name
                result['notes'].append(f"Node '{name}' advertising itself")
        else:
            result['errors'].append(f"ADVERT too short ({len(payload)} bytes, need 100+)")

    # Encrypted types: REQ, RESPONSE, TXT_MSG, GRP_TXT, GRP_DATA
    elif pt in (0x00, 0x01, 0x02, 0x05, 0x06):
        if len(payload) >= 4:
            dst_hash = payload[0]
            src_hash = payload[1]
            mac      = struct.unpack_from('<H', payload, 2)[0]
            cipher   = payload[4:]
            result['dst_hash']    = f"0x{dst_hash:02X}"
            result['src_hash']    = f"0x{src_hash:02X}"
            result['msg_mac']     = f"0x{mac:04X}"
            result['cipher_len']  = len(cipher)
            result['notes'].append("Payload is AES-encrypted — cannot decode without key")
            if pt == 0x02:
                result['notes'].append("Direct text message (TXT_MSG)")
            elif pt in (0x05, 0x06):
                result['notes'].append("Group channel message — may use default PSK")
        else:
            result['errors'].append("Encrypted payload too short")

    # CONTROL — network discovery
    elif pt == 0x0B:
        if len(payload) >= 1:
            subtype = payload[0]
            result['control_subtype'] = ('DISCOVER_REQ' if subtype == 0 else
                                          'DISCOVER_RESP' if subtype == 1 else
                                          f"0x{subtype:02X}")
            if subtype == 1 and len(payload) >= 37:
                snr    = struct.unpack_from('<b', payload, 1)[0]
                tag    = struct.unpack_from('<I', payload, 2)[0]
                pubkey = payload[6:38]
                result['control_snr']    = snr
                result['control_tag']    = f"0x{tag:08X}"
                result['control_pubkey'] = pubkey.hex().upper()

    # TRACE — path trace with SNR values
    elif pt == 0x09:
        result['notes'].append("Path trace packet — contains per-hop SNR data")

    # MULTIPART — fragmented
    elif pt == 0x0A:
        result['notes'].append("Fragmented multipart packet")

    return result


def format_decoded(d):
    """
    Format a decoded MeshCore dict into human-readable lines for the log file.
    Returns a multi-line string.
    """
    if d is None:
        return "    (no MeshCore data)"

    lines = []
    lines.append(f"  MeshCore Decode")
    lines.append(f"    Header        {d.get('header','?')}  "
                 f"v{d.get('version','?')}  "
                 f"Route: {d.get('route_type','?')}  "
                 f"Type: {d.get('payload_type','?')}")

    if 'transport_code_1' in d:
        lines.append(f"    Transport     {d['transport_code_1']} / {d['transport_code_2']}")

    hops = d.get('path_hops', [])
    if hops:
        lines.append(f"    Hops ({d['hop_count']})      " + " → ".join(hops))
    else:
        lines.append(f"    Hops          0 (origin packet)")

    pt = d.get('_ptype_code', -1)

    if pt == 0x03:
        lines.append(f"    ACK CRC32     {d.get('ack_crc32','?')}")

    elif pt == 0x04:
        lines.append(f"    Node Name     {d.get('advert_name','?')}")
        lines.append(f"    Public Key    {d.get('advert_pubkey','?')[:32]}…")
        ts = d.get('advert_timestamp')
        if ts:
            import datetime
            dt = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f"    Timestamp     {dt}")
        if 'advert_lat' in d:
            lines.append(f"    Location      {d['advert_lat']}, {d['advert_lon']}")

    elif pt in (0x00, 0x01, 0x02, 0x05, 0x06):
        lines.append(f"    Src Hash      {d.get('src_hash','?')}")
        lines.append(f"    Dst Hash      {d.get('dst_hash','?')}")
        lines.append(f"    MAC           {d.get('msg_mac','?')}")
        lines.append(f"    Ciphertext    {d.get('cipher_len','?')} bytes (encrypted)")

    elif pt == 0x0B:
        lines.append(f"    Control       {d.get('control_subtype','?')}")
        if 'control_snr' in d:
            lines.append(f"    SNR           {d['control_snr']} dB")
            lines.append(f"    Tag           {d.get('control_tag','?')}")
            lines.append(f"    Public Key    {d.get('control_pubkey','?')[:32]}…")

    for note in d.get('notes', []):
        lines.append(f"    Note          {note}")

    for err in d.get('errors', []):
        lines.append(f"    ERROR         {err}")

    return '\n'.join(lines)
