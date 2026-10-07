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
import tempfile
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
DEFAULT_MAX_RECORDINGS = 2
DEFAULT_BIT_DEPTH = 16
DEFAULT_BIT_RATE = 128
DEFAULT_SAMPLE_RATE = 44100
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


FORMAT_ESTIMATES = {
    'wav': 1.0,
    'mp3': lambda args: (args.bit_rate * 1000) / (args.sample_rate * args.channels * args.bit_depth),
    'flac': 0.5,
    'ogg': 0.2,
}


def estimate_output_bytes(raw_bytes, fmt, args):
    est = FORMAT_ESTIMATES.get(fmt, 1.0)
    if callable(est):
        return raw_bytes * est(args)
    return raw_bytes * est


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
                        help='Max number of recording files to keep (default: 2)')
    parser.add_argument('-b', '--bit-rate', type=int, default=DEFAULT_BIT_RATE,
                        help='Bit rate in kbps for compressed formats (default: 128)')
    parser.add_argument('-d', '--bit-depth', type=int, default=DEFAULT_BIT_DEPTH,
                        choices=[8, 16, 24, 32],
                        help='Bit depth for recording (default: 16)')
    parser.add_argument('-r', '--sample-rate', type=int, default=DEFAULT_SAMPLE_RATE,
                        help='Sample rate in Hz (default: 44100)')
    parser.add_argument('-c', '--channels', type=int, default=DEFAULT_CHANNELS,
                        help='Number of audio channels (default: 2)')
    parser.add_argument('-l', '--length', type=int, default=DEFAULT_LENGTH,
                        help='Max recording length in seconds (0=unlimited, default: 999999)')
    parser.add_argument('--output-dir', default='.',
                        help='Directory for output files (default: current)')
    parser.add_argument('--device', default=None,
                        help='Audio input device name or index (default: default)')
    args = parser.parse_args()

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


def convert_wav(wav_path, out_path, fmt, br, sr, ch):
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-i', wav_path, '-b:a', f'{br}k',
             '-ar', str(sr), '-ac', str(ch), out_path],
            capture_output=True, check=True
        )
    except subprocess.CalledProcessError as e:
        print(f'ffmpeg error: {e.stderr.decode()}', file=sys.stderr)
        raise
    except FileNotFoundError:
        print('Error: ffmpeg not found. Install ffmpeg for MP3/FLAC/OGG.',
              file=sys.stderr)
        raise


def convert_temp_to_format(tmp_path, out_path, fmt, br, sr, ch, bd):
    wp = tmp_path + '.wav'
    wt = wave.open(wp, 'wb')
    wt.setnchannels(ch)
    wt.setsampwidth(bd // 8)
    wt.setframerate(sr)
    raw = open(tmp_path, 'rb').read()
    if raw:
        pd = np.frombuffer(raw, dtype=f'<i{bd // 8}').reshape(-1, ch)
        wt.writeframes(pd.tobytes())
    wt.close()
    convert_wav(wp, out_path, fmt, br, sr, ch)
    os.unlink(wp)


def apply_gain(data, gain):
    if gain == 1.0:
        return data
    mx = np.iinfo(data.dtype).max
    d = data.astype(np.float64) * gain
    return np.clip(d, -mx, mx).astype(data.dtype)


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
        self.gain = 1.0
        self.auto_gain = False
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
        if not HAS_MSVCRT:
            return
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
                self.auto_gain = not self.auto_gain
            elif k in (b'B',):
                self.blank = True
            elif k in (b'b',):
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
    ac = ANSI['green'] if S.auto_gain else ANSI['dim']
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
        f'  {ANSI["white"]}Gain{ANSI["reset"]}  {gc}+{gb:.1f} dB{ANSI["reset"]}  (x{S.gain:.1f})'
        f'  {ac}AUTO:{S.auto_gain}{ANSI["reset"]}',
        f'  {ANSI["white"]}Clip{ANSI["reset"]}  {cl}{S.clipping}{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Time{ANSI["reset"]}  {ANSI["green"]}{fmt_time(elapsed)}{ANSI["reset"]}',
        f'  {ANSI["white"]}Total{ANSI["reset"]} {fmt_size(S.total_bytes)}',
        '',
        f'  {ANSI["green"]}[Q]{ANSI["reset"]}uit  {ANSI["cyan"]}[N]{ANSI["reset"]}ew File  {ANSI["yellow"]}[+]{ANSI["reset"]}/[-] Gain'
        f'  {ANSI["magenta"]}[A]{ANSI["reset"]}utoGain  {ANSI["dim"]}Ctrl+C{ANSI["reset"]} Stop',
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

    cleanup_oldest(base, 'wav', out_dir, max_r)
    cur_file = next_filename(base, 'wav', out_dir)

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
        dt = f'int{bd}'

        with sd.InputStream(samplerate=sr, channels=ch, dtype=dt, blocksize=bs,
                            device=args.device) as stream:
            while st.running:
                el = time.time() - t0
                if md > 0 and el >= md:
                    break

                cs = os.path.getsize(cur_file) if os.path.exists(cur_file) else 0
                if cs >= size_lim:
                    wf.close()
                    cleanup_oldest(base, 'wav', out_dir, max_r)
                    cur_file = next_filename(base, 'wav', out_dir)
                    wf = wave.open(cur_file, 'wb')
                    wf.setnchannels(ch)
                    wf.setsampwidth(bd // 8)
                    wf.setframerate(sr)
                    cs = 0
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
                    if peak < target_peak:
                        st.gain = min(3.0, st.gain * 1.01)
                    elif peak > target_peak * 1.1:
                        st.gain = max(0.1, st.gain * 0.99)

                data = apply_gain(data, st.gain)
                db = rms_db(data, bd)
                st.current_db = db
                st.levels.append(db)
                if len(st.levels) > 40:
                    st.levels.pop(0)

                wf.writeframes(data.tobytes())
                st.total_bytes += len(data.tobytes())

                st.read_key()
                draw_hud(st, args, cur_file, cs + len(data.tobytes()), size_lim, el)

                if st.new_file:
                    wf.close()
                    cleanup_oldest(base, 'wav', out_dir, max_r)
                    cur_file = next_filename(base, 'wav', out_dir)
                    wf = wave.open(cur_file, 'wb')
                    wf.setnchannels(ch)
                    wf.setsampwidth(bd // 8)
                    wf.setframerate(sr)
                    st.new_file = False
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

    cleanup_oldest(base, fmt, out_dir, max_r)
    cur_file = next_filename(base, fmt, out_dir)

    draw_startup(args)
    print(ANSI['hide_cursor'], end='')

    tmp_path = None
    t0 = time.time()
    raw_bytes = 0
    tmp_fp = None

    try:
        tmp_path = tempfile.mktemp(suffix='.raw')
        tmp_fp = open(tmp_path, 'wb')

        bs = int(sr * CHUNK_DURATION)
        dt = f'int{bd}'

        with sd.InputStream(samplerate=sr, channels=ch, dtype=dt, blocksize=bs,
                            device=args.device) as stream:
            while st.running:
                el = time.time() - t0
                if md > 0 and el >= md:
                    break

                est_out = estimate_output_bytes(raw_bytes, fmt, args)
                if est_out >= size_lim:
                    tmp_fp.close()
                    convert_temp_to_format(tmp_path, cur_file, fmt, br, sr, ch, bd)
                    st.total_bytes += os.path.getsize(cur_file)

                    open(tmp_path, 'wb').close()
                    raw_bytes = 0
                    tmp_fp = open(tmp_path, 'ab')

                    cleanup_oldest(base, fmt, out_dir, max_r)
                    cur_file = next_filename(base, fmt, out_dir)
                    st._drawn = False

                    st.read_key()
                    draw_hud(st, args, cur_file, 0, size_lim, el)

                    if st.new_file:
                        tmp_fp.close()
                        open(tmp_path, 'wb').close()
                        raw_bytes = 0
                        tmp_fp = open(tmp_path, 'ab')
                        st.new_file = False
                        t0 = time.time()
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
                    if peak < target_peak:
                        st.gain = min(3.0, st.gain * 1.01)
                    elif peak > target_peak * 1.1:
                        st.gain = max(0.1, st.gain * 0.99)

                data = apply_gain(data, st.gain)
                db = rms_db(data, bd)
                st.current_db = db
                st.levels.append(db)
                if len(st.levels) > 40:
                    st.levels.pop(0)

                tmp_fp.write(data.tobytes())
                tmp_fp.flush()
                raw_bytes += len(data.tobytes())

                st.read_key()
                draw_hud(st, args, cur_file, est_out, size_lim, el)

                if st.new_file:
                    tmp_fp.close()
                    convert_temp_to_format(tmp_path, cur_file, fmt, br, sr, ch, bd)
                    st.total_bytes += os.path.getsize(cur_file)

                    open(tmp_path, 'wb').close()
                    raw_bytes = 0
                    tmp_fp = open(tmp_path, 'ab')

                    cleanup_oldest(base, fmt, out_dir, max_r)
                    cur_file = next_filename(base, fmt, out_dir)
                    st.new_file = False
                    st._drawn = False
                    t0 = time.time()

        tmp_fp.close()
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            convert_temp_to_format(tmp_path, cur_file, fmt, br, sr, ch, bd)
            st.total_bytes += os.path.getsize(cur_file)

    except KeyboardInterrupt:
        pass
    except Exception:
        if tmp_fp:
            tmp_fp.close()
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    finally:
        if tmp_fp:
            tmp_fp.close()
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
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