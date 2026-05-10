#!/usr/bin/env python3
import curses, serial, threading, time, datetime, sys, os, json, re, math
import meshcore, meshcore_decrypt, meshcore_keys

PORT     = os.environ.get('LORA_PORT', sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB0')
BAUD     = 115200
LOG_TXT  = os.path.expanduser('~/lora_log.txt')
LOG_JSON = os.path.expanduser('~/lora_log.jsonl')

packets       = []
crc_errors    = 0
lock          = threading.Lock()
running       = True
serial_status = "CONNECTING"
device_config = {}
anim_tick     = 0
last_pkt_time = None

LOGO = [
    " ██╗      ██████╗ ██████╗  █████╗     ███████╗ ██████╗ █████╗ ███╗  ██╗",
    " ██║     ██╔═══██╗██╔══██╗██╔══██╗    ██╔════╝██╔════╝██╔══██╗████╗ ██║",
    " ██║     ██║   ██║██████╔╝███████║    ███████╗██║     ███████║██╔██╗██║",
    " ██║     ██║   ██║██╔══██╗██╔══██║    ╚════██║██║     ██╔══██║██║╚████║",
    " ███████╗╚██████╔╝██║  ██║██║  ██║    ███████║╚██████╗██║  ██║██║ ╚███║",
    " ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝   ╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚══╝",
]
SPIN  = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
PULSE = ['·  ·  ·','o  ·  ·','O  o  ·','◉  O  o','·  ◉  O','·  ·  ◉']
BLOCK = ' ▁▂▃▄▅▆▇█'

LORAWAN_MTYPES = {
    0:"JOIN_REQ", 1:"JOIN_ACC", 2:"UNCONF_UP", 3:"UNCONF_DN",
    4:"CONF_UP",  5:"CONF_DN",  6:"LORAWAN",   7:"PROP"
}

def bar(val, mn, mx, width=30):
    pct = max(0.0, min(1.0, (val - mn) / (mx - mn)))
    filled = int(pct * width)
    return '█' * filled + '░' * (width - filled), pct

def sig_block(rssi):
    idx = max(0, min(8, int((rssi - (-120)) / 90 * 9)))
    return BLOCK[idx]

def hex_fmt(h, group=2, max_bytes=24):
    pairs = [h[i:i+2] for i in range(0, min(len(h), max_bytes*2), 2)]
    s = ' '.join(pairs)
    if len(h) > max_bytes*2:
        s += '…'
    return s

def parse_packet(line):
    # Split on at most 11 pipes so ASCII (last field) can contain '|' safely
    parts = line.split('|', 11)
    if len(parts) < 12:
        return None
    try:
        return {
            'idx':      int(parts[1]),
            'ts_ms':    int(parts[2]),
            'rssi':     float(parts[3]),
            'snr':      float(parts[4]),
            'freq_err': float(parts[5]),
            'toa_ms':   float(parts[6]),
            'len':      int(parts[7]),
            'protocol': parts[8],
            'devaddr':  parts[9].strip(),
            'hex':      parts[10],
            'ascii':    parts[11].strip(),
            'time':     datetime.datetime.now().strftime('%H:%M:%S.%f')[:12],
            'wall_ts':  datetime.datetime.now().isoformat(),
        }
    except (ValueError, IndexError):
        return None

def parse_config(line):
    cfg = {}
    for part in line.split('|')[1:]:
        if '=' in part:
            k, v = part.split('=', 1)
            cfg[k.strip()] = v.strip()
    return cfg

def hex_pretty(h):
    pairs = [h[i:i+2] for i in range(0, len(h), 2)]
    lines = []
    for i in range(0, len(pairs), 16):
        chunk = pairs[i:i+16]
        hex_part = ' '.join(chunk).ljust(47)
        raw_bytes = bytes(int(b, 16) for b in chunk)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in raw_bytes)
        lines.append(f"             {i:04x}:  {hex_part}  |{asc_part}|")
    return '\n'.join(lines)

def rssi_bar_txt(rssi, width=36):
    pct = max(0.0, min(1.0, (rssi - (-120)) / 90))
    filled = int(pct * width)
    bar = '█' * filled + '░' * (width - filled)
    level = 'STRONG' if pct > 0.6 else 'MEDIUM' if pct > 0.3 else 'WEAK'
    return f"[{bar}] {level}"

def write_txt_entry(pkt):
    div = '─' * 72
    fe  = pkt['freq_err']
    fe_note = ("(drifting high)" if fe > 500 else
               "(drifting low)"  if fe < -500 else
               "(within tolerance)")
    lines = [
        '',
        div,
        f"  Packet #{pkt['idx']:>5}    {pkt['wall_ts']}",
        div,
        f"  Signal",
        f"    RSSI        {pkt['rssi']:>8.2f} dBm   {rssi_bar_txt(pkt['rssi'])}",
        f"    SNR         {pkt['snr']:>8.2f} dB",
        f"    Freq Error  {fe:>+8.1f} Hz    {fe_note}",
        f"    Time on Air {pkt['toa_ms']:>8.2f} ms",
        f"",
        f"  Frame",
        f"    Length      {pkt['len']} bytes",
        f"    Protocol    {pkt['protocol']}",
    ]
    if pkt['devaddr'] and pkt['devaddr'] != 'N/A':
        lines.append(f"    DevAddr     {pkt['devaddr']}")
    lines += [
        f"",
        f"  Payload",
        hex_pretty(pkt['hex']) if pkt['hex'] else "             (empty)",
        f"",
        meshcore.format_decoded(meshcore.decode(pkt['hex'])),
    ]
    if pkt.get('decrypted'):
        lines += [
            f"",
            f"  Decrypted Message",
            f"    {pkt['decrypted']}",
        ]
    lines.append(f"")
    return '\n'.join(lines)

def serial_reader():
    global serial_status, running, crc_errors, device_config, last_pkt_time
    session_ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
        serial_status = "ONLINE"
        with open(LOG_TXT, 'a') as f:
            f.write(
                f"\n{'═'*72}\n"
                f"  LoRa Scanner Session\n"
                f"  Started : {session_ts}\n"
                f"  Port    : {PORT} @ {BAUD} baud\n"
                f"{'═'*72}\n"
            )
        while running:
            try:
                raw = ser.readline().decode('utf-8', errors='replace').strip()
            except Exception:
                break
            if not raw:
                continue
            if raw.startswith('PKT|'):
                pkt = parse_packet(raw)
                if pkt:
                    mc = meshcore.decode(pkt['hex'])
                    if mc:
                        pkt['meshcore'] = mc
                        pkt['mc_type']  = mc.get('payload_type', '?')
                        pkt['mc_hops']  = mc.get('hop_count', 0)
                        pkt['mc_name']  = mc.get('advert_name', '')
                        # Auto-decrypt
                        ptype = mc.get('_ptype_code', -1)
                        raw_payload = bytes.fromhex(pkt['hex'])
                        offset = mc.get('payload_offset', 0)
                        payload_slice = raw_payload[offset:]
                        if ptype in (0x05, 0x06) and mc.get('payload_len', 0) > 3:
                            dec = meshcore_decrypt.decrypt_group(payload_slice)
                            if dec.get('ok'):
                                pkt['decrypted'] = dec.get('message', '')
                                pkt['mc_type']   = 'GRP✓'
                        elif ptype in (0x00, 0x01, 0x02, 0x07) and mc.get('payload_len', 0) > 4:
                            dec = meshcore_decrypt.try_decrypt_direct(payload_slice, mc)
                            if dec.get('ok'):
                                src = dec.get('src_node', '')
                                pkt['decrypted'] = f"{src}: {dec.get('message','')}" if src else dec.get('message','')
                                pkt['mc_type']   = 'DIR✓'
                    with open(LOG_TXT, 'a') as f:
                        f.write(write_txt_entry(pkt))
                    with open(LOG_JSON, 'a') as f:
                        f.write(json.dumps(pkt) + '\n')
                    with lock:
                        packets.append(pkt)
                        last_pkt_time = time.time()
            elif raw.startswith('CFG|'):
                device_config = parse_config(raw)
                with open(LOG_TXT, 'a') as f:
                    f.write(
                        f"  Radio   : {device_config.get('freq','?')} MHz  "
                        f"BW {device_config.get('bw','?')} kHz  "
                        f"SF {device_config.get('sf','?')}  "
                        f"CR 4/{device_config.get('cr','?')}  "
                        f"Sync {device_config.get('sync','?')}\n"
                        f"{'─'*72}\n"
                    )
            elif raw.startswith('CRC|'):
                ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:12]
                with open(LOG_TXT, 'a') as f:
                    f.write(f"\n  [{ts}]  *** CRC ERROR — packet received but checksum failed ***\n")
                with lock:
                    crc_errors += 1
    except serial.SerialException as e:
        serial_status = f"ERR:{e}"

def stats(pkts):
    if not pkts:
        return {}
    rssis = [p['rssi'] for p in pkts]
    snrs  = [p['snr']  for p in pkts]
    fes   = [p['freq_err'] for p in pkts]
    toas  = [p['toa_ms']   for p in pkts]
    # packet rate over last 60s
    now = time.time()
    recent = [p for p in pkts if (now - time.mktime(
        time.strptime(p['time'][:8], '%H:%M:%S'))) < 60]
    return {
        'rssi_min': min(rssis), 'rssi_max': max(rssis),
        'rssi_avg': sum(rssis)/len(rssis),
        'snr_min':  min(snrs),  'snr_max':  max(snrs),
        'snr_avg':  sum(snrs)/len(snrs),
        'fe_avg':   sum(fes)/len(fes),
        'toa_avg':  sum(toas)/len(toas),
        'rate':     len(recent),
        'protos':   {},
    }

def draw_screen(scr):
    global anim_tick, running
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN,    -1)
    curses.init_pair(2, curses.COLOR_GREEN,   -1)
    curses.init_pair(3, curses.COLOR_YELLOW,  -1)
    curses.init_pair(4, curses.COLOR_RED,     -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    curses.init_pair(6, curses.COLOR_WHITE,   -1)
    curses.init_pair(7, curses.COLOR_BLUE,    -1)
    curses.curs_set(0)
    scr.nodelay(True)

    C = {
        'logo': curses.color_pair(1)|curses.A_BOLD,
        'good': curses.color_pair(2),
        'mid':  curses.color_pair(3),
        'weak': curses.color_pair(4),
        'acc':  curses.color_pair(5),
        'norm': curses.color_pair(6),
        'bold': curses.color_pair(6)|curses.A_BOLD,
        'info': curses.color_pair(7),
    }

    def put(r, c, txt, attr=0):
        if r >= h or c >= w: return
        try: scr.addstr(r, c, txt[:w-c].replace('\x00', '.'), attr)
        except curses.error: pass

    def hline(r, char='─'):
        put(r, 0, char * (w-1), C['acc'])

    def box_top(r, title='', pw=None):
        pw = pw or min(w-2, 76)
        put(r, 0, f"┌─ {title} " + '─'*max(0,pw-len(title)-4) + '┐', C['acc'])

    def box_bot(r, pw=None):
        pw = pw or min(w-2, 76)
        put(r, 0, f"└{'─'*pw}┘", C['acc'])

    while running:
        try:
            scr.erase()
            h, w = scr.getmaxyx()
            row = 0

            # Logo
            for line in LOGO:
                if row >= h: break
                put(row, max(0,(w-len(line))//2), line, C['logo'])
                row += 1

            # Config subtitle
            if device_config:
                sub = (f"  {device_config.get('freq','?')} MHz  │  "
                       f"BW {device_config.get('bw','?')} kHz  │  "
                       f"SF {device_config.get('sf','?')}  │  "
                       f"CR 4/{device_config.get('cr','?').replace('4/','').replace('cr','')}  │  "
                       f"Sync {device_config.get('sync','?')}  ")
            else:
                sub = "  869.618 MHz  │  BW 62.5 kHz  │  SF 8  │  CR 4/8  "
            put(row, max(0,(w-len(sub))//2), sub, C['mid'])
            row += 1

            # Status bar
            spin  = SPIN[anim_tick % len(SPIN)]
            pulse = PULSE[anim_tick % len(PULSE)]
            with lock:
                total = len(packets)
                crcs  = crc_errors
            online_color = C['good'] if serial_status == "ONLINE" else C['weak']
            put(row, 0, f" {spin} {serial_status}  {pulse}  │  Pkts:{total}  "
                f"CRC-errs:{crcs}  │  log:{os.path.basename(LOG_JSON)}  │  q=quit",
                online_color | curses.A_BOLD)
            row += 1
            hline(row); row += 1

            # ── Last packet ─────────────────────────────────────────────
            with lock:
                last = packets[-1] if packets else None

            pw = min(w-2, 76)
            box_top(row, 'LAST PACKET', pw); row += 1

            if last:
                rssi_b, rssi_p = bar(last['rssi'], -120, -30, 26)
                snr_b,  snr_p  = bar(last['snr'],   -20,  10, 26)
                fe_b,   fe_p   = bar(last['freq_err'], -3000, 3000, 26)
                toa_str = f"{last['toa_ms']:.1f}ms"
                fe_str  = f"{last['freq_err']:+.0f}Hz"

                def pcolor(p):
                    return C['good'] if p > 0.6 else C['mid'] if p > 0.3 else C['weak']

                rows_d = [
                    (f"│  RSSI     {last['rssi']:8.2f} dBm  [{rssi_b}]  │", pcolor(rssi_p)),
                    (f"│  SNR      {last['snr']:8.2f} dB   [{snr_b}]  │", pcolor(snr_p)),
                    (f"│  Freq Err {last['freq_err']:8.1f} Hz   [{fe_b}]  │", C['mid']),
                    (f"│  Time on air: {toa_str:<8}  Len: {last['len']} bytes"
                     f"   @{last['time']}  │", C['norm']),
                    (f"│  MC Type:  {last.get('mc_type', last['protocol']):<12}  "
                     f"Hops: {last.get('mc_hops','-'):<4}  "
                     f"Node: {last.get('mc_name', last['devaddr']):<16}│", C['info']),
                    (f"│  HEX:  {hex_fmt(last['hex']):<{pw-10}}│", C['mid']),
                    (f"│  DATA: {last['ascii'][:pw-10]:<{pw-10}}│", C['good']),
                ]
                for txt, col in rows_d:
                    if row >= h: break
                    put(row, 0, txt, col); row += 1
            else:
                put(row, 2, "  ···  Waiting for LoRa packets  ···", C['mid'])
                row += 4

            box_bot(row, pw); row += 1

            # ── Statistics panel ────────────────────────────────────────
            with lock:
                st = stats(packets)
            box_top(row, 'SESSION STATS', pw); row += 1
            if st:
                put(row, 2,
                    f"RSSI  min:{st['rssi_min']:.1f}  avg:{st['rssi_avg']:.1f}  max:{st['rssi_max']:.1f} dBm"
                    f"    SNR avg:{st['snr_avg']:.1f} dB    FE avg:{st['fe_avg']:+.0f}Hz    "
                    f"ToA avg:{st['toa_avg']:.1f}ms    Rate:{st['rate']}/min",
                    C['norm'])
            else:
                put(row, 2, "  No data yet", C['mid'])
            row += 1
            box_bot(row, pw); row += 1

            # ── Dual graph: RSSI + Freq error ────────────────────────────
            with lock:
                hist = packets[:]
            graph_w = min(w - 10, 60)
            box_top(row, 'SIGNAL GRAPH  (RSSI ▲  FreqErr ▼)', pw); row += 1
            rssi_vals = [p['rssi']     for p in hist[-graph_w:]]
            fe_vals   = [p['freq_err'] for p in hist[-graph_w:]]

            graph_rows = 4
            if rssi_vals and row + graph_rows * 2 < h:
                # RSSI rows (upper half)
                levels = [-40, -60, -80, -100]
                for gi, thr in enumerate(levels):
                    if row >= h: break
                    line = f" {thr:4d} │"
                    for v in rssi_vals:
                        line += BLOCK[min(8, int((v-(-120))/90*9))] if v >= thr else ' '
                    put(row, 0, line, C['good']); row += 1
                # Freq error rows (lower half)
                fe_levels = [2000, 1000, 0, -1000]
                for gi, thr in enumerate(fe_levels):
                    if row >= h: break
                    line = f"{thr:5d} │"
                    for v in fe_vals:
                        c = '▲' if v > 0 else '▼' if v < 0 else '─'
                        line += c if abs(v) >= abs(thr) else ' '
                    put(row, 0, line, C['mid']); row += 1
                # x-axis
                if row < h:
                    put(row, 0, "      └" + "─"*min(len(rssi_vals), graph_w), C['norm'])
                row += 1
            else:
                put(row, 2, "  No data", C['mid']); row += graph_rows * 2 + 1

            box_bot(row, pw); row += 1

            # ── Packet history table ─────────────────────────────────────
            box_top(row, 'PACKET LOG', pw); row += 1
            if row < h:
                hdr = (f"│ {'TIME':<13} {'RSSI':>7} {'SNR':>6} {'FE Hz':>7} "
                       f"{'ToA':>6} {'LEN':>4} {'MC TYPE':<11} {'HOPS':>4} INFO")
                put(row, 0, hdr, C['bold']); row += 1

            with lock:
                show = list(reversed(packets))
            for p in show[:max(0, h - row - 2)]:
                if row >= h - 1: break
                col     = C['good'] if p['rssi'] > -80 else C['mid'] if p['rssi'] > -100 else C['weak']
                sb      = sig_block(p['rssi'])
                mc_type = p.get('mc_type', p.get('protocol', '?'))
                mc_hops = p.get('mc_hops', '-')
                mc_name = p.get('mc_name', '')
                info    = p.get('decrypted') or mc_name or p['ascii'][:20]
                line = (f"│ {p['time']:<13} {p['rssi']:>7.1f} {p['snr']:>6.1f} "
                        f"{p['freq_err']:>+7.0f} {p['toa_ms']:>5.1f}ms "
                        f"{p['len']:>4} {sb}{mc_type:<11} {str(mc_hops):>4}  {info[:22]}")
                put(row, 0, line, col); row += 1

            box_bot(row, pw)
            scr.refresh()
            anim_tick += 1

            if scr.getch() in (ord('q'), ord('Q'), 27):
                running = False; break

            time.sleep(0.12)
        except curses.error:
            pass

def main():
    threading.Thread(target=serial_reader, daemon=True).start()
    try:
        curses.wrapper(draw_screen)
    except KeyboardInterrupt:
        pass
    finally:
        global running
        running = False
        with lock:
            total = len(packets)
        print(f"\nText log: {LOG_TXT}")
        print(f"JSON log: {LOG_JSON}")
        print(f"Packets : {total}")
        if total:
            with lock: st = stats(packets)
            print(f"RSSI    : min {st['rssi_min']:.1f}  avg {st['rssi_avg']:.1f}  max {st['rssi_max']:.1f} dBm")
            print(f"SNR     : avg {st['snr_avg']:.1f} dB")
            print(f"Freq err: avg {st['fe_avg']:+.0f} Hz")

if __name__ == '__main__':
    main()
