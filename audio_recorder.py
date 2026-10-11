#!/usr/bin/env python3
"""
audio-recorder - Interactive terminal audio recorder with file rotation.

Streams recorded audio to rotating files (WAV, MP3, FLAC, OGG).
Live HUD: progress bars, disk space, microphone level graph, gain control.
"""

import argparse
import glob
import os
import sys
import time
import wave
import subprocess
import shutil
from datetime import datetime

import numpy as np
import sounddevice as sd

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False


def enable_windows_ansi():
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mode = ctypes.c_ulong()
        handle = kernel32.GetStdHandle(-11)
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value |= 4
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass

SUPPORTED_FORMATS = ['wav', 'mp3', 'flac', 'ogg']
DEFAULT_SIZE_LIMIT = 4000000000
DEFAULT_MAX_RECORDINGS = 4
DEFAULT_BIT_DEPTH = 16
DEFAULT_BIT_RATE = 320
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_LENGTH = 999999
CHUNK_DURATION = 0.5

ANSI = {
    'reset': '\033[0m',
    'bold': '\033[1m',
    'dim': '\033[2m',
    'red': '\033[91m',
    'green': '\033[92m',
    'yellow': '\033[93m',
    'blue': '\033[94m',
    'magenta': '\033[95m',
    'cyan': '\033[96m',
    'white': '\033[97m',
    'hide_cursor': '\033[?25l',
    'show_cursor': '\033[?25h',
    'clear': '\033[2J',
    'home': '\033[H',
    'up': '\033[A',
    'clear_line': '\033[K',
    'clear_to_end': '\033[J',
}


def parse_args():
    class CF(argparse.RawDescriptionHelpFormatter):
        C = ANSI
        def _format_action_invocation(self, action):
            if not action.option_strings:
                return super()._format_action_invocation(action)
            parts = [f'{self.C["cyan"]}{o}{self.C["reset"]}' for o in action.option_strings]
            if action.nargs != 0:
                m = self._get_default_metavar_for_optional(action)
                parts.append(f'{self.C["yellow"]}{m}{self.C["reset"]}')
            return ', '.join(parts)
        def _get_default_metavar(self, action):
            r = super()._get_default_metavar(action)
            return f'{self.C["yellow"]}{r}{self.C["reset"]}' if r else r
        def start_section(self, h):
            super().start_section(f'{self.C["bold"]}{h}{self.C["reset"]}')

    parser = argparse.ArgumentParser(
        description='Record audio from default input with file rotation.',
        formatter_class=CF,
        epilog=f'\n{ANSI["bold"]}Examples:{ANSI["reset"]}\n'
               f'  {ANSI["cyan"]}python audio_recorder.py{ANSI["reset"]}\n'
               f'  {ANSI["cyan"]}python audio_recorder.py -f mp3 -b 192 -l 600{ANSI["reset"]}',
    )
    parser.add_argument('-o', '--output', default=None,
                        help='Output filename base (default: {datetime}.{format})')
    parser.add_argument('-f', '--file-format', dest='file_format', default='wav',
                        choices=SUPPORTED_FORMATS,
                        help='Output format: wav, mp3, flac, ogg (default: wav)')
    parser.add_argument('-s', '--size', type=int, default=DEFAULT_SIZE_LIMIT,
                        help='Max file size in bytes (default: 4000000000)')
    parser.add_argument('-m', '--max-recordings', type=int, default=DEFAULT_MAX_RECORDINGS,
                        help='Max number of recording files to keep (default: 4)')
    parser.add_argument('-b', '--bit-rate', type=int, default=DEFAULT_BIT_RATE,
                        help='Bit rate in kbps for compressed formats (default: 320)')
    parser.add_argument('-d', '--bit-depth', type=int, default=DEFAULT_BIT_DEPTH,
                        choices=[8, 16, 24, 32],
                        help='Bit depth for recording (default: 16)')
    parser.add_argument('-r', '--sample-rate', type=int, default=DEFAULT_SAMPLE_RATE,
                        help='Sample rate in Hz (default: 48000)')
    parser.add_argument('-c', '--channels', type=int, default=DEFAULT_CHANNELS,
                        help='Number of audio channels (default: 2)')
    parser.add_argument('-l', '--length', type=int, default=DEFAULT_LENGTH,
                        help='Max recording length in seconds (0=unlimited, default: 999999)')
    parser.add_argument('--output-dir', default='.',
                        help='Directory for output files (default: current)')
    parser.add_argument('--device', default=None,
                        help='Audio input device name or index (default: default)')
    parser.add_argument('--list-devices', action='store_true',
                        help='List available audio input devices and exit')
    args = parser.parse_args()

    if args.list_devices:
        print(f'{ANSI["bold"]}Available audio input devices:{ANSI["reset"]}')
        for i, dev in enumerate(sd.query_devices()):
            if dev['max_input_channels'] > 0:
                marker = f'{ANSI["green"]}*{ANSI["reset"]}' if i == sd.default.device[0] else ' '
                print(f'  {marker} [{i}] {dev["name"]} ({dev["max_input_channels"]} ch, {int(dev["default_samplerate"])} Hz)')
        sys.exit(0)

    # Validate device if specified
    if args.device is not None:
        try:
            dev_idx = sd.query_devices(args.device, 'input')
        except (ValueError, OSError):
            try:
                dev_idx = sd.query_devices(int(args.device), 'input')
            except (ValueError, OSError, IndexError):
                print(f'Error: audio input device not found: {args.device}', file=sys.stderr)
                sys.exit(1)
        if dev_idx['max_input_channels'] < args.channels:
            print(f'Error: device "{dev_idx["name"]}" has only {dev_idx["max_input_channels"]} input channels, {args.channels} requested', file=sys.stderr)
            sys.exit(1)
        # Check sample rate compatibility (allow close match)
        default_sr = int(dev_idx['default_samplerate'])
        if args.sample_rate != default_sr:
            print(f'Warning: device default sample rate is {default_sr} Hz, using {args.sample_rate} Hz', file=sys.stderr)

    if args.output is None:
        ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        args.output = f'{ts}.{args.file_format}'

    for fmt in SUPPORTED_FORMATS:
        if args.output.lower().endswith(f'.{fmt}'):
            args.output = args.output[:-(len(fmt) + 1)]
            break

    out_dir = os.path.abspath(args.output_dir)
    if not os.path.isdir(out_dir):
        print(f'Error: output directory does not exist: {out_dir}', file=sys.stderr)
        sys.exit(1)

    return args


def fmt_size(b):
    if b >= 1024 ** 3:
        return f'{b / 1024 ** 3:.2f} GB'
    elif b >= 1024 ** 2:
        return f'{b / 1024 ** 2:.2f} MB'
    elif b >= 1024:
        return f'{b / 1024:.2f} KB'
    return f'{b} B'


def fmt_time(s):
    h = int(s) // 3600
    m = (int(s) % 3600) // 60
    s = int(s) % 60
    return f'{h:02d}:{m:02d}:{s:02d}'


def _readline_echo(prompt):
    """Read a line with echo, cross-platform."""
    import sys
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if HAS_MSVCRT:
        # Windows: use msvcrt for character-by-character input
        chars = []
        while True:
            ch = msvcrt.getwch()
            if ch == '\r' or ch == '\n':
                sys.stdout.write('\n')
                sys.stdout.flush()
                break
            elif ch == '\x08':  # Backspace
                if chars:
                    chars.pop()
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
            elif ch >= ' ':
                chars.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
        return ''.join(chars)
    else:
        # Unix: use stdin.readline (blocks but that's fine for rename)
        return sys.stdin.readline().rstrip('\n')


def prompt_rename(cur_file, args):
    """Prompt user to rename the current file."""
    base = os.path.basename(cur_file)
    # Remove extension
    for fmt in SUPPORTED_FORMATS:
        if base.lower().endswith(f'.{fmt}'):
            base = base[:-(len(fmt) + 1)]
            break
    print(f'{ANSI["show_cursor"]}', end='')
    print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
    print(f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}RENAME FILE{ANSI["reset"]}{" " * 39}{ANSI["cyan"]}|{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}')
    print(f'')
    print(f'  Current: {ANSI["yellow"]}{base}{ANSI["reset"]}')
    print(f'')
    new_base = _readline_echo(f'  New name: {ANSI["cyan"]}')
    print(f'{ANSI["reset"]}', end='')
    print(f'{ANSI["hide_cursor"]}', end='')
    if not new_base:
        return os.path.basename(cur_file).split('.')[0]
    # Remove any extension user might have typed
    for fmt in SUPPORTED_FORMATS:
        if new_base.lower().endswith(f'.{fmt}'):
            new_base = new_base[:-(len(fmt) + 1)]
            break
    return new_base


def get_disk_info(path='.'):
    total, used, free = shutil.disk_usage(path)
    return total, used, free


def pbar(pct, width=26):
    f = int(width * min(1.0, max(0.0, pct)))
    return '#' * f + '-' * (width - f)


def lvl_bar(db, width=20):
    n = 0 if db < -60 else 1 if db > 0 else (db + 60) / 60
    f = int(width * n)
    return '#' * f + '-' * (width - f)


def sparkline(levels, width=28):
    if not levels:
        return ' ' * width
    chars = '._-+=*^#'
    mn, mx = min(levels), max(levels)
    rng = mx - mn if mx != mn else 1
    return ''.join(chars[int((l - mn) / rng * (len(chars) - 1))]
                   for l in levels[-width:])


def cleanup_oldest(base, fmt, out_dir, max_r):
    files = sorted(glob.glob(os.path.join(out_dir, f'{base}_*.{fmt}')))
    while len(files) >= max_r:
        os.remove(files[0])
        files.pop(0)


def next_filename(base, fmt, out_dir):
    existing = sorted(glob.glob(os.path.join(out_dir, f'{base}_*.{fmt}')))
    return os.path.join(out_dir, f'{base}_{len(existing) + 1:04d}.{fmt}')


def pack_int24(data):
    """Convert int32 array to int24 little-endian bytes (3 bytes per sample)."""
    # data is int32, we need to take lower 24 bits and pack as 3 bytes LE
    d = data.astype(np.int32)
    # Extract 24 bits: (d & 0xFFFFFF).to_bytes(3, 'little') per sample
    # Vectorized approach:
    b0 = (d & 0xFF).astype(np.uint8)
    b1 = ((d >> 8) & 0xFF).astype(np.uint8)
    b2 = ((d >> 16) & 0xFF).astype(np.uint8)
    # Interleave: b0, b1, b2, b0, b1, b2, ...
    out = np.empty(d.size * 3, dtype=np.uint8)
    out[0::3] = b0
    out[1::3] = b1
    out[2::3] = b2
    return out.tobytes()


def to_bytes_int24(data):
    """Convert int32 array to int24 little-endian bytes for ffmpeg stdin."""
    return pack_int24(data)


def write_wav_frames(wf, data, bd):
    """Write audio data to wave file, handling 24-bit correctly."""
    if bd == 24:
        wf.writeframes(pack_int24(data))
    else:
        wf.writeframes(data.tobytes())


def apply_gain(data, gain):
    if gain == 1.0:
        return data
    mn = np.iinfo(data.dtype).min
    mx = np.iinfo(data.dtype).max
    d = data.astype(np.float64) * gain
    return np.clip(d, mn, mx).astype(data.dtype)


def rms_db(data, bit_depth):
    if len(data) == 0:
        return -100.0
    mx = 2 ** (bit_depth - 1)
    f = data.astype(np.float64) / mx
    r = np.sqrt(np.mean(f ** 2))
    return 20 * np.log10(r) if r > 1e-10 else -100.0


class UIState:
    def __init__(self):
        self.running = True
        self.new_file = False
        self.rename = False
        self.gain = 1.0
        self.auto_gain = 0  # 0=off, 1=normal, 2=fast
        self.levels = []
        self.current_db = -100.0
        self.peak_db = -100.0
        self.total_bytes = 0
        self.clipping = 0
        self.blank = False
        self._drawn = False
        self._last_disk_info = None
        self._last_disk_time = 0

    def gain_db(self):
        return 20 * np.log10(self.gain) if self.gain > 0 else -100.0

    def read_key(self):
        if HAS_MSVCRT:
            while msvcrt.kbhit():
                k = msvcrt.getch()
                if k in (b'q', b'Q'):
                    self.running = False
                elif k in (b'n', b'N'):
                    self.new_file = True
                elif k in (b'+', b'='):
                    self.gain = min(3.0, self.gain + 0.5)
                elif k in (b'-', b'_'):
                    self.gain = max(0.1, self.gain - 0.5)
                elif k in (b'a', b'A'):
                    self.auto_gain = (self.auto_gain + 1) % 3
                elif k in (b'r', b'R'):
                    self.rename = True
                elif k in (b'B',):
                    self.blank = True
                elif k in (b'b',):
                    self.blank = False
                    self._drawn = False
        else:
            # Unix: use select for non-blocking stdin
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                k = sys.stdin.read(1)
                if k in ('q', 'Q'):
                    self.running = False
                elif k in ('n', 'N'):
                    self.new_file = True
                elif k in ('+', '='):
                    self.gain = min(3.0, self.gain + 0.5)
                elif k in ('-', '_'):
                    self.gain = max(0.1, self.gain - 0.5)
                elif k in ('a', 'A'):
                    self.auto_gain = (self.auto_gain + 1) % 3
                elif k in ('r', 'R'):
                    self.rename = True
                elif k in ('B',):
                    self.blank = True
                elif k in ('b',):
                    self.blank = False
                    self._drawn = False


def draw_hud(S, args, cur_file, cur_size, size_lim, elapsed):
    now = time.time()
    if S._last_disk_info is None or now - S._last_disk_time > 5:
        S._last_disk_info = get_disk_info(args.output_dir)
        S._last_disk_time = now
    total, _, free = S._last_disk_info

    base = os.path.basename(cur_file)
    free_pct = (free / total) * 100 if total > 0 else 0
    p = min(100.0, (cur_size / size_lim) * 100) if size_lim > 0 else 0

    if S.blank:
        print(f'{ANSI["clear"]}{ANSI["home"]}', end='', flush=True)
        print(f'{ANSI["bold"]}{ANSI["white"]}{p:.0f}%{ANSI["reset"]}', end='', flush=True)
        S._drawn = True
        return

    lb = lvl_bar(S.current_db)
    sp = sparkline(S.levels)
    gb = S.gain_db()
    gc = ANSI['red'] if abs(gb) > 12 else ANSI['yellow'] if abs(gb) > 6 else ANSI['green']
    bc = ANSI['green'] if p < 80 else ANSI['yellow'] if p < 95 else ANSI['red']
    lc = ANSI['green'] if S.current_db < -6 else ANSI['yellow'] if S.current_db < -3 else ANSI['red']
    pc = ANSI['green'] if S.peak_db < -3 else ANSI['yellow'] if S.peak_db < 0 else ANSI['red']
    if S.auto_gain == 2:
        ac, ag_str = ANSI['red'], 'AUTO 2x'
    elif S.auto_gain == 1:
        ac, ag_str = ANSI['green'], 'AUTO'
    else:
        ac, ag_str = ANSI['dim'], 'OFF'
    cl = ANSI['red'] if S.clipping > 0 else ANSI['dim']

    lines = [
        f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}',
        f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}{ANSI["red"]}*{ANSI["reset"]}  {ANSI["bold"]}RECORDING{ANSI["reset"]}'
        f'{" " * 33}{ANSI["cyan"]}|{ANSI["reset"]}',
        f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Format{ANSI["reset"]}: {ANSI["yellow"]}{args.file_format.upper():5s}{ANSI["reset"]}'
        f'  {ANSI["white"]}Bit{ANSI["reset"]}: {args.bit_depth}-bit'
        f'  {ANSI["white"]}Rate{ANSI["reset"]}: {args.sample_rate}Hz'
        f'  {ANSI["white"]}Ch{ANSI["reset"]}: {args.channels}',
        f'  {ANSI["white"]}File{ANSI["reset"]}: {ANSI["cyan"]}{base}{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Size{ANSI["reset"]}  [{bc}{pbar(p)}{ANSI["reset"]}] {bc}{p:.1f}%{ANSI["reset"]}',
        f'         {fmt_size(cur_size)} / {fmt_size(size_lim)}',
        '',
        f'  {ANSI["white"]}Disk{ANSI["reset"]}  [{ANSI["blue"]}{pbar(free_pct / 100)}{ANSI["reset"]}] {ANSI["green"]}{fmt_size(free)}{ANSI["reset"]} free',
        f'         {fmt_size(total)} total',
        '',
        f'  {ANSI["white"]}Level{ANSI["reset"]} [{lc}{lb}{ANSI["reset"]}] {lc}{S.current_db:.1f} dB{ANSI["reset"]}',
        f'  {ANSI["white"]}Peak{ANSI["reset"]}  [{pc}{lvl_bar(S.peak_db)}{ANSI["reset"]}] {pc}{S.peak_db:.1f} dB{ANSI["reset"]}',
        f'  {ANSI["white"]}Wave{ANSI["reset"]}  {ANSI["magenta"]}{sp}{ANSI["reset"]}',
        f'  {ANSI["white"]}Gain{ANSI["reset"]}  {gc}{gb:+.1f} dB{ANSI["reset"]}  (x{S.gain:.1f})'
        f'  {ac}{ag_str}{ANSI["reset"]}',
        f'  {ANSI["white"]}Clip{ANSI["reset"]}  {cl}{S.clipping}{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Time{ANSI["reset"]}  {ANSI["green"]}{fmt_time(elapsed)}{ANSI["reset"]}',
        f'  {ANSI["white"]}Total{ANSI["reset"]} {fmt_size(S.total_bytes)}',
        '',
        f'  {ANSI["green"]}[Q]{ANSI["reset"]}uit  {ANSI["cyan"]}[N]{ANSI["reset"]}ew File  {ANSI["yellow"]}[+]{ANSI["reset"]}/[-] Gain'
        f'  {ANSI["magenta"]}[A]{ANSI["reset"]}utoGain  {ANSI["blue"]}[R]{ANSI["reset"]}ename  {ANSI["dim"]}Ctrl+C{ANSI["reset"]} Stop',
        f'{ANSI["cyan"]}{"-" * 54}{ANSI["reset"]}',
    ]

    output = f'{ANSI["clear"]}{ANSI["home"]}' + '\n'.join(lines)
    print(output, end='', flush=True)
    S._drawn = True


def draw_startup(args):
    print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
    lines = [
        f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}',
        f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}(*) AUDIO RECORDER{ANSI["reset"]}{" " * 29}{ANSI["cyan"]}|{ANSI["reset"]}',
        f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Format{ANSI["reset"]}: {ANSI["yellow"]}{args.file_format.upper()}{ANSI["reset"]}',
        f'  {ANSI["white"]}Size{ANSI["reset"]}: {fmt_size(args.size)} per file',
        f'  {ANSI["white"]}Max Files{ANSI["reset"]}: {args.max_recordings}',
        f'  {ANSI["white"]}Bit Depth{ANSI["reset"]}: {args.bit_depth}-bit',
        f'  {ANSI["white"]}Bit Rate{ANSI["reset"]}: {args.bit_rate} kbps',
        f'  {ANSI["white"]}Sample Rate{ANSI["reset"]}: {args.sample_rate} Hz',
        f'  {ANSI["white"]}Channels{ANSI["reset"]}: {args.channels}',
        f'  {ANSI["white"]}Duration{ANSI["reset"]}: {"unlimited" if args.length >= 999999 else str(args.length) + "s"}',
        f'  {ANSI["white"]}Output{ANSI["reset"]}: {ANSI["cyan"]}{args.output}{ANSI["reset"]}',
    ]
    if args.device is not None:
        lines.append(f'  {ANSI["white"]}Device{ANSI["reset"]}: {ANSI["yellow"]}{args.device}{ANSI["reset"]}')
    lines += [
        '',
        f'  {ANSI["green"]}Press any key to start recording...{ANSI["reset"]}',
        f'{ANSI["cyan"]}{"-" * 54}{ANSI["reset"]}',
    ]
    print('\n'.join(lines))
    sys.stdout.flush()
    if HAS_MSVCRT:
        msvcrt.getch()
    else:
        input()


def record_wav(args):
    st = UIState()
    out_dir = args.output_dir
    base = args.output
    size_lim = args.size
    max_r = args.max_recordings
    bd = args.bit_depth
    sr = args.sample_rate
    ch = args.channels
    md = args.length

    rotation_base = base  # constant for file rotation naming
    cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
    cur_file = next_filename(rotation_base, 'wav', out_dir)

    draw_startup(args)
    print(ANSI['hide_cursor'], end='')

    wf = None
    t0 = time.time()

    try:
        wf = wave.open(cur_file, 'wb')
        wf.setnchannels(ch)
        wf.setsampwidth(bd // 8)
        wf.setframerate(sr)

        bs = int(sr * CHUNK_DURATION)
        dt = get_audio_dtype(bd)
        cur_file_size = 0

        with sd.InputStream(samplerate=sr, channels=ch, dtype=dt, blocksize=bs,
                            device=args.device) as stream:
            while st.running:
                el = time.time() - t0
                if md > 0 and el >= md:
                    break

                if cur_file_size >= size_lim:
                    wf.close()
                    cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
                    cur_file = next_filename(rotation_base, 'wav', out_dir)
                    wf = wave.open(cur_file, 'wb')
                    wf.setnchannels(ch)
                    wf.setsampwidth(bd // 8)
                    wf.setframerate(sr)
                    cur_file_size = 0
                    st.clipping = 0
                    st._drawn = False
                    continue

                data, ov = stream.read(bs)
                if ov:
                    print('Warning: input overflow', file=sys.stderr)

                mx = np.iinfo(data.dtype).max
                peak = np.max(np.abs(data))
                peak_db = 20 * np.log10(peak / mx) if peak > 0 else -100.0
                st.peak_db = peak_db

                clip_count = int(np.sum(np.abs(data) >= mx * 0.95))
                st.clipping += clip_count

                if st.auto_gain and peak > 0:
                    target_peak = mx * 0.8
                    rate = 0.02 if st.auto_gain == 2 else 0.01
                    if peak < target_peak:
                        st.gain = min(3.0, st.gain * (1.0 + rate))
                    elif peak > target_peak * 1.1:
                        st.gain = max(0.1, st.gain * (1.0 - rate))

                data = apply_gain(data, st.gain)
                db = rms_db(data, bd)
                st.current_db = db
                st.levels.append(db)
                if len(st.levels) > 40:
                    st.levels.pop(0)

                write_wav_frames(wf, data, bd)
                # 24-bit writes 3 bytes/sample; int32 container gives 4
                frame_bytes = data.size * 3 if bd == 24 else len(data.tobytes())
                cur_file_size += frame_bytes
                st.total_bytes += frame_bytes

                st.read_key()
                draw_hud(st, args, cur_file, cur_file_size, size_lim, el)

                if st.new_file:
                    wf.close()
                    cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
                    cur_file = next_filename(rotation_base, 'wav', out_dir)
                    wf = wave.open(cur_file, 'wb')
                    wf.setnchannels(ch)
                    wf.setsampwidth(bd // 8)
                    wf.setframerate(sr)
                    st.new_file = False
                    st.clipping = 0
                    cur_file_size = 0
                    st._drawn = False
                    t0 = time.time()

                if st.rename:
                    wf.close()
                    new_base = prompt_rename(cur_file, args)
                    if new_base != base:
                        base = new_base
                        rotation_base = new_base
                    cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
                    cur_file = next_filename(rotation_base, 'wav', out_dir)
                    wf = wave.open(cur_file, 'wb')
                    wf.setnchannels(ch)
                    wf.setsampwidth(bd // 8)
                    wf.setframerate(sr)
                    st.rename = False
                    st.clipping = 0
                    cur_file_size = 0
                    st._drawn = False
                    t0 = time.time()

    except KeyboardInterrupt:
        pass
    finally:
        if wf is not None:
            wf.close()
        print(f'{ANSI["show_cursor"]}', end='')
        print(f'\n{ANSI["green"]}Recording stopped.{ANSI["reset"]}')
        print(f'{ANSI["dim"]}Total: {fmt_size(st.total_bytes)}{ANSI["reset"]}')


def get_audio_dtype(bd):
    """Return numpy dtype for given bit depth."""
    if bd == 8:
        return np.int8
    elif bd == 16:
        return np.int16
    elif bd == 24:
        return np.int32  # 24-bit stored in 32-bit int
    elif bd == 32:
        return np.int32
    return np.int16


def get_ffmpeg_fmt(bd):
    """Return ffmpeg input format for given bit depth."""
    if bd == 8:
        return 's8'
    elif bd == 16:
        return 's16le'
    elif bd == 24:
        return 's24le'
    elif bd == 32:
        return 's32le'
    return 's16le'


def build_ffmpeg_cmd(fmt, out_path, br, sr, ch, bd):
    """Build ffmpeg command for streaming encoding."""
    infmt = get_ffmpeg_fmt(bd)
    if fmt == 'mp3':
        return ['ffmpeg', '-y', '-f', infmt, '-ar', str(sr), '-ac', str(ch), '-i', '-',
                '-c:a', 'libmp3lame', '-b:a', f'{br}k', out_path]
    elif fmt == 'flac':
        return ['ffmpeg', '-y', '-f', infmt, '-ar', str(sr), '-ac', str(ch), '-i', '-',
                '-c:a', 'flac', '-compression_level', '5', out_path]
    elif fmt == 'ogg':
        return ['ffmpeg', '-y', '-f', infmt, '-ar', str(sr), '-ac', str(ch), '-i', '-',
                '-c:a', 'libvorbis', '-b:a', f'{br}k', out_path]
    else:
        raise ValueError(f'Unsupported format: {fmt}')


def record_compressed(args):
    st = UIState()
    out_dir = args.output_dir
    base = args.output
    size_lim = args.size
    max_r = args.max_recordings
    bd = args.bit_depth
    br = args.bit_rate
    sr = args.sample_rate
    ch = args.channels
    md = args.length
    fmt = args.file_format

    rotation_base = base  # constant for file rotation naming
    cleanup_oldest(rotation_base, fmt, out_dir, max_r)
    cur_file = next_filename(rotation_base, fmt, out_dir)

    draw_startup(args)
    print(ANSI['hide_cursor'], end='')

    t0 = time.time()
    ffmpeg_proc = None

    def start_ffmpeg(path):
        nonlocal ffmpeg_proc
        if ffmpeg_proc:
            ffmpeg_proc.stdin.close()
            ffmpeg_proc.wait()
        cmd = build_ffmpeg_cmd(fmt, path, br, sr, ch, bd)
        ffmpeg_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # Verify ffmpeg started successfully
        time.sleep(0.05)
        if ffmpeg_proc.poll() is not None:
            raise RuntimeError(f'ffmpeg failed to start (exit code: {ffmpeg_proc.returncode})')

    def stop_ffmpeg():
        nonlocal ffmpeg_proc
        if ffmpeg_proc:
            try:
                ffmpeg_proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait()
            ffmpeg_proc = None

    try:
        start_ffmpeg(cur_file)

        bs = int(sr * CHUNK_DURATION)
        dt = get_audio_dtype(bd)
        cur_file_size = 0

        with sd.InputStream(samplerate=sr, channels=ch, dtype=dt, blocksize=bs,
                            device=args.device) as stream:
            while st.running:
                el = time.time() - t0
                if md > 0 and el >= md:
                    break

                est_size = int(estimate_output_bytes(cur_file_size, fmt, args))
                if est_size >= size_lim:
                    stop_ffmpeg()
                    st.total_bytes += cur_file_size
                    cur_file_size = 0  # reset before continue to prevent infinite rotation

                    cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                    cur_file = next_filename(rotation_base, fmt, out_dir)
                    st._drawn = False

                    start_ffmpeg(cur_file)

                    st.read_key()
                    draw_hud(st, args, cur_file, 0, size_lim, el)

                    if st.new_file:
                        stop_ffmpeg()
                        st.new_file = False
                        st.clipping = 0
                        t0 = time.time()
                        start_ffmpeg(cur_file)
                    continue

                data, ov = stream.read(bs)
                if ov:
                    print('Warning: input overflow', file=sys.stderr)

                mx = np.iinfo(data.dtype).max
                peak = np.max(np.abs(data))
                peak_db = 20 * np.log10(peak / mx) if peak > 0 else -100.0
                st.peak_db = peak_db

                clip_count = int(np.sum(np.abs(data) >= mx * 0.95))
                st.clipping += clip_count

                if st.auto_gain and peak > 0:
                    target_peak = mx * 0.8
                    rate = 0.02 if st.auto_gain == 2 else 0.01
                    if peak < target_peak:
                        st.gain = min(3.0, st.gain * (1.0 + rate))
                    elif peak > target_peak * 1.1:
                        st.gain = max(0.1, st.gain * (1.0 - rate))

                data = apply_gain(data, st.gain)
                db = rms_db(data, bd)
                st.current_db = db
                st.levels.append(db)
                if len(st.levels) > 40:
                    st.levels.pop(0)

                # Write to ffmpeg stdin with error handling
                try:
                    if ffmpeg_proc and ffmpeg_proc.poll() is None:
                        if bd == 24:
                            frame_bytes = len(to_bytes_int24(data))
                            ffmpeg_proc.stdin.write(to_bytes_int24(data))
                        else:
                            frame_bytes = len(data.tobytes())
                            ffmpeg_proc.stdin.write(data.tobytes())
                        ffmpeg_proc.stdin.flush()
                        cur_file_size += frame_bytes
                    else:
                        # ffmpeg died, restart
                        raise BrokenPipeError('ffmpeg process died')
                except (BrokenPipeError, OSError) as e:
                    print(f'ffmpeg write error: {e}', file=sys.stderr)
                    # Attempt to restart ffmpeg
                    try:
                        stop_ffmpeg()
                        start_ffmpeg(cur_file)
                    except Exception as re:
                        print(f'Failed to restart ffmpeg: {re}', file=sys.stderr)
                        st.running = False
                        break

                st.read_key()
                draw_hud(st, args, cur_file,
                         int(estimate_output_bytes(cur_file_size, fmt, args)),
                         size_lim, el)

                if st.new_file:
                    stop_ffmpeg()
                    st.total_bytes += cur_file_size

                    cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                    cur_file = next_filename(rotation_base, fmt, out_dir)
                    st.new_file = False
                    st.clipping = 0
                    cur_file_size = 0
                    st._drawn = False
                    t0 = time.time()
                    start_ffmpeg(cur_file)

                if st.rename:
                    stop_ffmpeg()
                    st.total_bytes += cur_file_size
                    new_base = prompt_rename(cur_file, args)
                    if new_base != base:
                        base = new_base
                        rotation_base = new_base
                    cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                    cur_file = next_filename(rotation_base, fmt, out_dir)
                    start_ffmpeg(cur_file)
                    st.rename = False
                    st.clipping = 0
                    cur_file_size = 0
                    st._drawn = False
                    t0 = time.time()

    except KeyboardInterrupt:
        pass
    except Exception:
        stop_ffmpeg()
        raise
    finally:
        stop_ffmpeg()
        st.total_bytes += cur_file_size
        print(f'{ANSI["show_cursor"]}', end='')
        print(f'\n{ANSI["green"]}Recording stopped.{ANSI["reset"]}')
        print(f'{ANSI["dim"]}Total: {fmt_size(st.total_bytes)}{ANSI["reset"]}')


def main():
    enable_windows_ansi()
    args = parse_args()
    if args.file_format == 'wav':
        record_wav(args)
    else:
        record_compressed(args)


if __name__ == '__main__':
    main()