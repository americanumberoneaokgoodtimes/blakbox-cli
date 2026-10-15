#!/usr/bin/env python3
"""
blakbox-cli - Interactive terminal audio recorder with file rotation.

Streams recorded audio to rotating files (WAV, MP3, FLAC, OGG).
Live HUD: progress bars, disk space, microphone level graph, gain control.
"""

VERSION = '1.2.0'

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

try:
    import tty
    import termios as _termios
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

_orig_termios_state = None


def set_raw_terminal():
    """Put Unix terminal in cbreak mode: immediate single-char input, signals still work."""
    global _orig_termios_state
    if HAS_MSVCRT or not HAS_TERMIOS:
        return
    try:
        fd = sys.stdin.fileno()
        _orig_termios_state = _termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except Exception:
        _orig_termios_state = None


def restore_terminal():
    """Restore Unix terminal to its original cooked mode."""
    global _orig_termios_state
    if HAS_MSVCRT or not HAS_TERMIOS or _orig_termios_state is None:
        return
    try:
        _termios.tcsetattr(sys.stdin.fileno(), _termios.TCSADRAIN, _orig_termios_state)
        _orig_termios_state = None
    except Exception:
        pass


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
WAV_HEADER_BYTES = 44

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


CONFIG_PATH = os.path.expanduser('~/.blakboxrc')


def load_config():
    """Load configuration from ~/.blakboxrc (TOML format). Returns dict or empty dict."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    if tomllib is None:
        print(f'{ANSI["yellow"]}Warning: tomli not installed, cannot parse {CONFIG_PATH}{ANSI["reset"]}', file=sys.stderr)
        return {}
    try:
        with open(CONFIG_PATH, 'rb') as f:
            return tomllib.load(f)
    except Exception as e:
        print(f'{ANSI["yellow"]}Warning: failed to parse {CONFIG_PATH}: {e}{ANSI["reset"]}', file=sys.stderr)
        return {}


def check_ffmpeg():
    """Verify ffmpeg is in PATH; exit with a clear error if not."""
    if shutil.which('ffmpeg') is None:
        print(f'{ANSI["red"]}Error: ffmpeg not found in PATH.{ANSI["reset"]}', file=sys.stderr)
        print('Install ffmpeg (https://ffmpeg.org/download.html) and add it to PATH.',
              file=sys.stderr)
        sys.exit(1)


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
    parser.add_argument('--version', action='version', version=f'blakbox-cli {VERSION}')
    parser.add_argument('-o', '--output', default=None,
                        help='Output filename base (default: {datetime})')
    parser.add_argument('-f', '--file-format', dest='file_format', default=argparse.SUPPRESS,
                        choices=SUPPORTED_FORMATS,
                        help='Output format: wav, mp3, flac, ogg (default: wav)')
    parser.add_argument('-s', '--size', type=int, default=argparse.SUPPRESS,
                        help='Max file size in bytes (default: 4000000000)')
    parser.add_argument('-m', '--max-recordings', type=int, default=argparse.SUPPRESS,
                        help='Max number of recording files to keep (default: 4)')
    parser.add_argument('-b', '--bit-rate', type=int, default=argparse.SUPPRESS,
                        help='Bit rate in kbps for compressed formats (default: 320)')
    parser.add_argument('-d', '--bit-depth', type=int, default=argparse.SUPPRESS,
                        choices=[8, 16, 24, 32],
                        help='Bit depth for recording (default: 16)')
    parser.add_argument('-r', '--sample-rate', type=int, default=argparse.SUPPRESS,
                        help='Sample rate in Hz (default: 48000)')
    parser.add_argument('-c', '--channels', type=int, default=argparse.SUPPRESS,
                        help='Number of audio channels (default: 2)')
    parser.add_argument('-l', '--length', type=int, default=argparse.SUPPRESS,
                        help='Max recording length in seconds (0=unlimited, default: 999999)')
    parser.add_argument('--output-dir', default=argparse.SUPPRESS,
                        help='Directory for output files (default: current)')
    parser.add_argument('--device', default=argparse.SUPPRESS,
                        help='Audio input device name or index (default: system default)')
    parser.add_argument('--list-devices', action='store_true',
                        help='List available audio input devices and exit')
    args = parser.parse_args()

    # Apply config file, then hardcoded defaults for any missing values
    config = load_config()
    defaults = {
        'file_format': 'wav',
        'size': DEFAULT_SIZE_LIMIT,
        'max_recordings': DEFAULT_MAX_RECORDINGS,
        'bit_rate': DEFAULT_BIT_RATE,
        'bit_depth': DEFAULT_BIT_DEPTH,
        'sample_rate': DEFAULT_SAMPLE_RATE,
        'channels': DEFAULT_CHANNELS,
        'length': DEFAULT_LENGTH,
        'output_dir': '.',
        'device': None,
    }
    for key, default_val in defaults.items():
        if not hasattr(args, key):
            # Not provided via CLI; check config file
            config_key = {
                'file_format': 'format',
                'max_recordings': 'max_recordings',
                'bit_rate': 'bit_rate',
                'bit_depth': 'bit_depth',
                'sample_rate': 'sample_rate',
                'output_dir': 'output_dir',
            }.get(key, key)
            if config_key in config:
                val = config[config_key]
                if config_key == 'device' and val == '':
                    val = None
                setattr(args, key, val)
            else:
                setattr(args, key, default_val)

    if args.list_devices:
        print(f'{ANSI["bold"]}Available audio input devices:{ANSI["reset"]}')
        for i, dev in enumerate(sd.query_devices()):
            if dev['max_input_channels'] > 0:
                marker = f'{ANSI["green"]}*{ANSI["reset"]}' if i == sd.default.device[0] else ' '
                print(f'  {marker} [{i}] {dev["name"]}'
                      f' ({dev["max_input_channels"]} ch, {int(dev["default_samplerate"])} Hz)')
        sys.exit(0)

    if args.device is not None:
        try:
            dev_info = sd.query_devices(args.device, 'input')
        except (ValueError, OSError):
            try:
                dev_info = sd.query_devices(int(args.device), 'input')
            except (ValueError, OSError, IndexError):
                print(f'Error: audio input device not found: {args.device}', file=sys.stderr)
                sys.exit(1)
        if dev_info['max_input_channels'] < args.channels:
            print(f'Error: device "{dev_info["name"]}" has only '
                  f'{dev_info["max_input_channels"]} input channels, '
                  f'{args.channels} requested', file=sys.stderr)
            sys.exit(1)
        default_sr = int(dev_info['default_samplerate'])
        if args.sample_rate != default_sr:
            print(f'Warning: device default sample rate is {default_sr} Hz, '
                  f'using {args.sample_rate} Hz', file=sys.stderr)

    if args.output is None:
        args.output = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

    for fmt in SUPPORTED_FORMATS:
        if args.output.lower().endswith(f'.{fmt}'):
            args.output = args.output[:-(len(fmt) + 1)]
            break

    out_dir = os.path.abspath(args.output_dir)
    if not os.path.isdir(out_dir):
        print(f'Error: output directory does not exist: {out_dir}', file=sys.stderr)
        sys.exit(1)
    args.output_dir = out_dir

    return args


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Rename prompt (cross-platform line input)
# ---------------------------------------------------------------------------

def _readline_echo(prompt):
    """Read a line with character echo. On Unix, temporarily restores cooked mode."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if HAS_MSVCRT:
        chars = []
        while True:
            ch = msvcrt.getwch()
            if ch in ('\r', '\n'):
                sys.stdout.write('\n')
                sys.stdout.flush()
                break
            elif ch == '\x08':  # backspace
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
        # Temporarily restore cooked mode so readline works normally
        restore_terminal()
        try:
            line = sys.stdin.readline().rstrip('\n')
        finally:
            set_raw_terminal()
        return line


def prompt_rename(cur_file):
    """Prompt the user to rename the current recording base name."""
    base = os.path.basename(cur_file)
    for fmt in SUPPORTED_FORMATS:
        if base.lower().endswith(f'.{fmt}'):
            base = base[:-(len(fmt) + 1)]
            break
    print(f'{ANSI["show_cursor"]}', end='')
    print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
    print(f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}RENAME FILE'
          f'{ANSI["reset"]}{" " * 39}{ANSI["cyan"]}|{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}')
    print()
    print(f'  Current: {ANSI["yellow"]}{base}{ANSI["reset"]}')
    print()
    new_base = _readline_echo(f'  New name: {ANSI["cyan"]}')
    print(f'{ANSI["reset"]}', end='')
    print(f'{ANSI["hide_cursor"]}', end='')
    if not new_base.strip():
        return None  # cancelled
    new_base = new_base.strip()
    for fmt in SUPPORTED_FORMATS:
        if new_base.lower().endswith(f'.{fmt}'):
            new_base = new_base[:-(len(fmt) + 1)]
            break
    return new_base


# ---------------------------------------------------------------------------
# Disk / filesystem helpers
# ---------------------------------------------------------------------------

def get_disk_info(path='.'):
    total, used, free = shutil.disk_usage(path)
    return total, used, free


def _get_file_size(path):
    """Return file size in bytes, 0 on any error."""
    try:
        return os.path.getsize(path) if os.path.exists(path) else 0
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Progress / level bar renderers
# ---------------------------------------------------------------------------

def pbar(frac, width=26):
    """Render a progress bar. frac is 0.0–1.0."""
    f = int(width * min(1.0, max(0.0, frac)))
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


# ---------------------------------------------------------------------------
# File rotation helpers  (BUG-01 fix: monotonic index to avoid overwrites)
# ---------------------------------------------------------------------------

def _parse_index(path, base, fmt):
    """Extract the numeric suffix from a rotated filename, or None."""
    name = os.path.splitext(os.path.basename(path))[0]
    prefix = f'{base}_'
    if not name.startswith(prefix):
        return None
    try:
        return int(name[len(prefix):])
    except ValueError:
        return None


def cleanup_oldest(base, fmt, out_dir, max_r):
    files = sorted(glob.glob(os.path.join(out_dir, f'{base}_*.{fmt}')))
    while len(files) >= max_r:
        try:
            os.remove(files[0])
        except OSError:
            pass
        files.pop(0)


def next_filename(base, fmt, out_dir):
    """Return the next output path using a monotonic index.

    Finds the highest existing index and adds 1, so rotation after cleanup
    never collides with a file that was just kept (fixes BUG-01 data-loss).
    """
    existing = glob.glob(os.path.join(out_dir, f'{base}_*.{fmt}'))
    max_idx = 0
    for f in existing:
        idx = _parse_index(f, base, fmt)
        if idx is not None and idx > max_idx:
            max_idx = idx
    return os.path.join(out_dir, f'{base}_{max_idx + 1:04d}.{fmt}')


# ---------------------------------------------------------------------------
# Audio processing
# ---------------------------------------------------------------------------

def pack_int24(data):
    """Convert int32 array to 24-bit little-endian byte string (3 bytes/sample)."""
    d = data.astype(np.int32)
    b0 = (d & 0xFF).astype(np.uint8)
    b1 = ((d >> 8) & 0xFF).astype(np.uint8)
    b2 = ((d >> 16) & 0xFF).astype(np.uint8)
    out = np.empty(d.size * 3, dtype=np.uint8)
    out[0::3] = b0
    out[1::3] = b1
    out[2::3] = b2
    return out.tobytes()


def write_wav_frames(wf, data, bd):
    """Write audio frames to an open wave.Wave_write, handling 24-bit correctly."""
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


def get_audio_dtype(bd):
    """Return numpy dtype for given bit depth."""
    if bd == 8:
        return np.int8
    elif bd == 16:
        return np.int16
    elif bd in (24, 32):
        return np.int32
    return np.int16


def get_ffmpeg_fmt(bd):
    """Return ffmpeg PCM input format string for given bit depth."""
    return {8: 's8', 16: 's16le', 24: 's24le', 32: 's32le'}.get(bd, 's16le')


def build_ffmpeg_cmd(fmt, out_path, br, sr, ch, bd):
    """Build the ffmpeg command list for streaming encode from stdin."""
    infmt = get_ffmpeg_fmt(bd)
    base = ['ffmpeg', '-y', '-f', infmt, '-ar', str(sr), '-ac', str(ch), '-i', '-']
    if fmt == 'mp3':
        return base + ['-c:a', 'libmp3lame', '-b:a', f'{br}k', out_path]
    elif fmt == 'flac':
        return base + ['-c:a', 'flac', '-compression_level', '5', out_path]
    elif fmt == 'ogg':
        return base + ['-c:a', 'libvorbis', '-b:a', f'{br}k', out_path]
    raise ValueError(f'Unsupported format: {fmt}')


# ---------------------------------------------------------------------------
# UI state
# ---------------------------------------------------------------------------

class UIState:
    def __init__(self):
        self.running = True
        self.new_file = False
        self.rename = False
        self.gain = 1.0
        self.auto_gain = 0        # 0=off, 1=normal, 2=fast
        self.levels = []
        self.current_db = -100.0
        self.peak_db = -100.0
        self.total_bytes = 0      # completed-file bytes; current file added on stop
        self.clipping = 0
        self.blank = False
        self.paused = False       # pause/resume state
        self.error_msg = None     # transient error string shown in HUD
        self.files_created = []   # all output paths opened this session
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
                elif k == b'0':
                    self.gain = 1.0          # reset to unity (BUG-12)
                elif k in (b'a', b'A'):
                    self.auto_gain = (self.auto_gain + 1) % 3
                elif k in (b'r', b'R'):
                    self.rename = True
                elif k in (b'p', b'P'):
                    self.paused = not self.paused
                elif k == b'B':
                    self.blank = True        # Shift+B → boss mode
                elif k == b'b':
                    self.blank = False       # b → unblank
                    self._drawn = False
        elif HAS_SELECT:
            try:
                if _select_mod.select([sys.stdin], [], [], 0)[0]:
                    k = sys.stdin.read(1)
                    if k in ('q', 'Q'):
                        self.running = False
                    elif k in ('n', 'N'):
                        self.new_file = True
                    elif k in ('+', '='):
                        self.gain = min(3.0, self.gain + 0.5)
                    elif k in ('-', '_'):
                        self.gain = max(0.1, self.gain - 0.5)
                    elif k == '0':
                        self.gain = 1.0
                    elif k in ('a', 'A'):
                        self.auto_gain = (self.auto_gain + 1) % 3
                    elif k in ('r', 'R'):
                        self.rename = True
                    elif k in ('p', 'P'):
                        self.paused = not self.paused
                    elif k == 'B':
                        self.blank = True
                    elif k == 'b':
                        self.blank = False
                        self._drawn = False
            except Exception:
                pass


# ---------------------------------------------------------------------------
# HUD rendering
# ---------------------------------------------------------------------------

def draw_hud(S, args, cur_file, cur_size, size_lim, elapsed, total_override=None):
    now = time.time()
    if S._last_disk_info is None or now - S._last_disk_time > 5:
        S._last_disk_info = get_disk_info(args.output_dir)
        S._last_disk_time = now
    total_disk, _, free = S._last_disk_info

    base = os.path.basename(cur_file)
    free_pct = (free / total_disk * 100) if total_disk > 0 else 100.0
    disk_used_frac = (total_disk - free) / total_disk if total_disk > 0 else 0.0
    size_frac = min(1.0, cur_size / size_lim) if size_lim > 0 else 0.0
    total_shown = total_override if total_override is not None else S.total_bytes

    if S.blank:
        print(f'{ANSI["clear"]}{ANSI["home"]}', end='', flush=True)
        pct_disp = f'{size_frac * 100:.0f}%'
        print(f'{ANSI["bold"]}{ANSI["white"]}{pct_disp}{ANSI["reset"]}'
              f'  {ANSI["dim"]}[b] unblank{ANSI["reset"]}', end='', flush=True)
        S._drawn = True
        return

    lb = lvl_bar(S.current_db)
    sp = sparkline(S.levels)
    gb = S.gain_db()
    gc = ANSI['red'] if abs(gb) > 12 else ANSI['yellow'] if abs(gb) > 6 else ANSI['green']
    bc = ANSI['green'] if size_frac < 0.80 else ANSI['yellow'] if size_frac < 0.95 else ANSI['red']
    lc = ANSI['green'] if S.current_db < -6 else ANSI['yellow'] if S.current_db < -3 else ANSI['red']
    pc = ANSI['green'] if S.peak_db < -3 else ANSI['yellow'] if S.peak_db < 0 else ANSI['red']
    dc = ANSI['red'] if free_pct < 10 else ANSI['yellow'] if free_pct < 30 else ANSI['blue']
    if S.auto_gain == 2:
        ac, ag_str = ANSI['red'], 'AUTO 2x'
    elif S.auto_gain == 1:
        ac, ag_str = ANSI['green'], 'AUTO'
    else:
        ac, ag_str = ANSI['dim'], 'OFF'
    cl = ANSI['red'] if S.clipping > 0 else ANSI['dim']
    rec_label = 'PAUSED' if S.paused else 'RECORDING'
    rec_color = ANSI['yellow'] if S.paused else ANSI['red']

    lines = [
        f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}',
        f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}{rec_color}*{ANSI["reset"]}'
        f'  {ANSI["bold"]}{rec_label}{ANSI["reset"]}{" " * (33 - len(rec_label) + 9)}{ANSI["cyan"]}|{ANSI["reset"]}',
        f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Format{ANSI["reset"]}: {ANSI["yellow"]}{args.file_format.upper():5s}{ANSI["reset"]}'
        f'  {ANSI["white"]}Bit{ANSI["reset"]}: {args.bit_depth}-bit'
        f'  {ANSI["white"]}Rate{ANSI["reset"]}: {args.sample_rate}Hz'
        f'  {ANSI["white"]}Ch{ANSI["reset"]}: {args.channels}',
        f'  {ANSI["white"]}File{ANSI["reset"]}: {ANSI["cyan"]}{base}{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Size{ANSI["reset"]}  [{bc}{pbar(size_frac)}{ANSI["reset"]}]'
        f' {bc}{size_frac * 100:.1f}%{ANSI["reset"]}',
        f'         {fmt_size(cur_size)} / {fmt_size(size_lim)}',
        '',
        f'  {ANSI["white"]}Disk{ANSI["reset"]}  [{dc}{pbar(disk_used_frac)}{ANSI["reset"]}]'
        f' {dc}{fmt_size(free)}{ANSI["reset"]} free',
        f'         {fmt_size(total_disk)} total',
        '',
        f'  {ANSI["white"]}Level{ANSI["reset"]} [{lc}{lb}{ANSI["reset"]}] {lc}{S.current_db:.1f} dB{ANSI["reset"]}',
        f'  {ANSI["white"]}Peak{ANSI["reset"]}  [{pc}{lvl_bar(S.peak_db)}{ANSI["reset"]}] {pc}{S.peak_db:.1f} dB{ANSI["reset"]}',
        f'  {ANSI["white"]}Wave{ANSI["reset"]}  {ANSI["magenta"]}{sp}{ANSI["reset"]}',
        f'  {ANSI["white"]}Gain{ANSI["reset"]}  {gc}{gb:+.1f} dB{ANSI["reset"]}  (x{S.gain:.2f})'
        f'  {ac}{ag_str}{ANSI["reset"]}',
        f'  {ANSI["white"]}Clip{ANSI["reset"]}  {cl}{S.clipping}{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Time{ANSI["reset"]}  {ANSI["green"]}{fmt_time(elapsed)}{ANSI["reset"]}',
        f'  {ANSI["white"]}Total{ANSI["reset"]} {fmt_size(total_shown)}',
        '',
    ]
    if S.error_msg:
        lines.append(f'  {ANSI["red"]}! {S.error_msg}{ANSI["reset"]}')
        lines.append('')
    lines += [
        f'  {ANSI["green"]}[Q]{ANSI["reset"]}uit'
        f'  {ANSI["cyan"]}[N]{ANSI["reset"]}ew'
        f'  {ANSI["yellow"]}[+]{ANSI["reset"]}/{ANSI["yellow"]}[-]{ANSI["reset"]}Gain'
        f'  {ANSI["dim"]}[0]{ANSI["reset"]}Reset'
        f'  {ANSI["magenta"]}[A]{ANSI["reset"]}uto'
        f'  {ANSI["blue"]}[R]{ANSI["reset"]}ename',
        f'  {ANSI["dim"]}[P]{ANSI["reset"]}ause/Resume'
        f'  {ANSI["dim"]}[B]{ANSI["reset"]}lank(boss)'
        f'  {ANSI["dim"]}[b]{ANSI["reset"]}Unblank'
        f'  {ANSI["dim"]}Ctrl+C{ANSI["reset"]} Stop',
        f'{ANSI["cyan"]}{"-" * 54}{ANSI["reset"]}',
    ]

    print(f'{ANSI["clear"]}{ANSI["home"]}' + '\n'.join(lines), end='', flush=True)
    S._drawn = True


def draw_startup(args):
    print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
    title = f'BLAKBOX AUDIO RECORDER  v{VERSION}'
    pad = max(0, 50 - len(title))
    lines = [
        f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}',
        f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}{title}{ANSI["reset"]}'
        f'{" " * pad}{ANSI["cyan"]}|{ANSI["reset"]}',
        f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}',
        '',
        f'  {ANSI["white"]}Format    {ANSI["reset"]}: {ANSI["yellow"]}{args.file_format.upper()}{ANSI["reset"]}',
        f'  {ANSI["white"]}Size limit{ANSI["reset"]}: {fmt_size(args.size)} per file',
        f'  {ANSI["white"]}Max files {ANSI["reset"]}: {args.max_recordings}',
        f'  {ANSI["white"]}Bit depth {ANSI["reset"]}: {args.bit_depth}-bit',
        f'  {ANSI["white"]}Bit rate  {ANSI["reset"]}: {args.bit_rate} kbps',
        f'  {ANSI["white"]}Sample rate{ANSI["reset"]}: {args.sample_rate} Hz',
        f'  {ANSI["white"]}Channels  {ANSI["reset"]}: {args.channels}',
        f'  {ANSI["white"]}Duration  {ANSI["reset"]}: '
        f'{"unlimited" if args.length >= 999999 else str(args.length) + "s"}',
        f'  {ANSI["white"]}Output dir{ANSI["reset"]}: {ANSI["cyan"]}{args.output_dir}{ANSI["reset"]}',
        f'  {ANSI["white"]}Base name {ANSI["reset"]}: {ANSI["cyan"]}{args.output}{ANSI["reset"]}',
    ]
    if args.device is not None:
        lines.append(
            f'  {ANSI["white"]}Device    {ANSI["reset"]}: {ANSI["yellow"]}{args.device}{ANSI["reset"]}')
    
    # Pre-flight device test (FEAT-03)
    lines.append('')
    lines.append(f'  {ANSI["white"]}Testing audio device...{ANSI["reset"]}')
    print('\n'.join(lines))
    sys.stdout.flush()
    
    ok, err = test_audio_device(args)
    if not ok:
        print(f'{ANSI["red"]}  Device test failed: {err}{ANSI["reset"]}')
        print(f'{ANSI["red"]}  Check device name/index with --list-devices{ANSI["reset"]}')
        sys.exit(1)
    lines.append(f'  {ANSI["green"]}  Device OK{ANSI["reset"]}')
    
    lines += [
        '',
        f'{ANSI["cyan"]}{"-" * 54}{ANSI["reset"]}',
    ]
    print('\n'.join(lines))
    sys.stdout.flush()


def test_audio_device(args):
    """Pre-flight test: verify the audio input device can be opened with given params.
    Returns (success: bool, error_msg: str or None).
    """
    try:
        dt = get_audio_dtype(args.bit_depth)
        bs = int(args.sample_rate * CHUNK_DURATION)
        with sd.InputStream(samplerate=args.sample_rate, channels=args.channels,
                            dtype=dt, blocksize=bs, device=args.device) as stream:
            stream.read(bs)
        return True, None
    except Exception as e:
        return False, str(e)


def _print_session_summary(st):
    """Print end-of-session stats and file list."""
    print(f'{ANSI["show_cursor"]}', end='')
    print(f'\n{ANSI["green"]}Recording stopped.{ANSI["reset"]}')
    print(f'{ANSI["dim"]}Total: {fmt_size(st.total_bytes)}{ANSI["reset"]}')
    if st.files_created:
        print(f'{ANSI["white"]}Files created:{ANSI["reset"]}')
        for f in st.files_created:
            sz = _get_file_size(f)
            exists = os.path.exists(f)
            tag = '' if exists else f'  {ANSI["dim"]}(removed by rotation){ANSI["reset"]}'
            print(f'  {ANSI["cyan"]}{f}{ANSI["reset"]}'
                  f'  {ANSI["dim"]}{fmt_size(sz)}{ANSI["reset"]}{tag}')


# ---------------------------------------------------------------------------
# WAV recording path
# ---------------------------------------------------------------------------

def record_wav(args):
    st = UIState()
    out_dir = args.output_dir
    rotation_base = args.output
    size_lim = args.size
    max_r = args.max_recordings
    bd = args.bit_depth
    sr = args.sample_rate
    ch = args.channels
    md = args.length

    cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
    cur_file = next_filename(rotation_base, 'wav', out_dir)
    st.files_created.append(cur_file)

    set_raw_terminal()
    try:
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
            cur_file_size = WAV_HEADER_BYTES   # account for WAV header (BUG-21)

            with sd.InputStream(samplerate=sr, channels=ch, dtype=dt, blocksize=bs,
                                device=args.device) as stream:
                while st.running:
                    el = time.time() - t0
                    if md > 0 and el >= md:
                        break

                    # Handle pause: read keys and update HUD, but don't process audio
                    if st.paused:
                        st.read_key()
                        draw_hud(st, args, cur_file, cur_file_size, size_lim, el)
                        time.sleep(0.1)
                        continue

                    # Automatic size-limit rotation
                    if cur_file_size >= size_lim:
                        wf.close()
                        cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
                        cur_file = next_filename(rotation_base, 'wav', out_dir)
                        st.files_created.append(cur_file)
                        wf = wave.open(cur_file, 'wb')
                        wf.setnchannels(ch)
                        wf.setsampwidth(bd // 8)
                        wf.setframerate(sr)
                        cur_file_size = WAV_HEADER_BYTES
                        st.clipping = 0       # reset per-file clip counter (BUG-04)
                        st._drawn = False
                        continue

                    data, ov = stream.read(bs)
                    if ov:
                        pass  # overflow noted; stderr suppressed during HUD

                    mx_val = np.iinfo(data.dtype).max
                    peak = np.max(np.abs(data))
                    st.peak_db = 20 * np.log10(peak / mx_val) if peak > 0 else -100.0
                    st.clipping += int(np.sum(np.abs(data) >= mx_val * 0.95))

                    if st.auto_gain and peak > 0:
                        target = mx_val * 0.8
                        rate = 0.02 if st.auto_gain == 2 else 0.01
                        if peak < target:
                            st.gain = min(3.0, st.gain * (1.0 + rate))
                        elif peak > target * 1.1:
                            st.gain = max(0.1, st.gain * (1.0 - rate))

                    data = apply_gain(data, st.gain)
                    db = rms_db(data, bd)
                    st.current_db = db
                    st.levels.append(db)
                    if len(st.levels) > 40:
                        st.levels.pop(0)

                    write_wav_frames(wf, data, bd)
                    frame_bytes = data.size * 3 if bd == 24 else len(data.tobytes())
                    cur_file_size += frame_bytes
                    st.total_bytes += frame_bytes

                    st.read_key()
                    draw_hud(st, args, cur_file, cur_file_size, size_lim, el)

                    # Manual new-file rotation
                    if st.new_file:
                        wf.close()
                        cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
                        cur_file = next_filename(rotation_base, 'wav', out_dir)
                        st.files_created.append(cur_file)
                        wf = wave.open(cur_file, 'wb')
                        wf.setnchannels(ch)
                        wf.setsampwidth(bd // 8)
                        wf.setframerate(sr)
                        st.new_file = False
                        st.clipping = 0
                        cur_file_size = WAV_HEADER_BYTES
                        st._drawn = False

                    # Rename
                    if st.rename:
                        wf.close()
                        new_base = prompt_rename(cur_file)
                        if new_base:
                            rotation_base = new_base
                        cleanup_oldest(rotation_base, 'wav', out_dir, max_r)
                        cur_file = next_filename(rotation_base, 'wav', out_dir)
                        st.files_created.append(cur_file)
                        wf = wave.open(cur_file, 'wb')
                        wf.setnchannels(ch)
                        wf.setsampwidth(bd // 8)
                        wf.setframerate(sr)
                        st.rename = False
                        st.clipping = 0
                        cur_file_size = WAV_HEADER_BYTES
                        st._drawn = False

        except KeyboardInterrupt:
            pass
        finally:
            if wf is not None:
                wf.close()
    finally:
        restore_terminal()
        _print_session_summary(st)


# ---------------------------------------------------------------------------
# Compressed recording path (MP3 / FLAC / OGG via ffmpeg stdin pipe)
# ---------------------------------------------------------------------------

def record_compressed(args):
    check_ffmpeg()   # fail fast before showing startup screen (BUG-08)

    st = UIState()
    out_dir = args.output_dir
    rotation_base = args.output
    size_lim = args.size
    max_r = args.max_recordings
    bd = args.bit_depth
    br = args.bit_rate
    sr = args.sample_rate
    ch = args.channels
    md = args.length
    fmt = args.file_format

    cleanup_oldest(rotation_base, fmt, out_dir, max_r)
    cur_file = next_filename(rotation_base, fmt, out_dir)
    st.files_created.append(cur_file)

    t0 = time.time()
    ffmpeg_proc = None

    def start_ffmpeg(path):
        nonlocal ffmpeg_proc
        if ffmpeg_proc is not None:
            try:
                ffmpeg_proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            ffmpeg_proc.wait()
            ffmpeg_proc = None
        cmd = build_ffmpeg_cmd(fmt, path, br, sr, ch, bd)
        ffmpeg_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.05)
        if ffmpeg_proc.poll() is not None:
            err = ffmpeg_proc.stderr.read().decode(errors='replace')
            raise RuntimeError(f'ffmpeg failed to start: {err[:300]}')

    def stop_ffmpeg():
        nonlocal ffmpeg_proc
        if ffmpeg_proc is None:
            return
        try:
            ffmpeg_proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            ffmpeg_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ffmpeg_proc.kill()
            ffmpeg_proc.wait()
        ffmpeg_proc = None

    set_raw_terminal()
    try:
        draw_startup(args)
        print(ANSI['hide_cursor'], end='')

        try:
            start_ffmpeg(cur_file)

            bs = int(sr * CHUNK_DURATION)
            dt = get_audio_dtype(bd)

            with sd.InputStream(samplerate=sr, channels=ch, dtype=dt, blocksize=bs,
                                device=args.device) as stream:
                while st.running:
                    el = time.time() - t0
                    if md > 0 and el >= md:
                        break

                    # Handle pause: read keys and update HUD, but don't process audio
                    if st.paused:
                        st.read_key()
                        cur_actual = _get_file_size(cur_file)
                        draw_hud(st, args, cur_file, cur_actual, size_lim, el,
                                 total_override=st.total_bytes + cur_actual)
                        time.sleep(0.1)
                        continue

                    # Use real output file size for limit check and HUD (BUG-03/FEAT-02)
                    actual_size = _get_file_size(cur_file)

                    # Automatic size-limit rotation
                    if actual_size >= size_lim:
                        stop_ffmpeg()
                        st.total_bytes += actual_size    # track completed file
                        cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                        cur_file = next_filename(rotation_base, fmt, out_dir)
                        st.files_created.append(cur_file)
                        st.clipping = 0                  # reset per-file clip counter (BUG-04)
                        st._drawn = False
                        start_ffmpeg(cur_file)           # start next file (BUG-06 fix: only once)
                        st.read_key()
                        draw_hud(st, args, cur_file, 0, size_lim, el,
                                 total_override=st.total_bytes)
                        continue

                    data, ov = stream.read(bs)

                    mx_val = np.iinfo(data.dtype).max
                    peak = np.max(np.abs(data))
                    st.peak_db = 20 * np.log10(peak / mx_val) if peak > 0 else -100.0
                    st.clipping += int(np.sum(np.abs(data) >= mx_val * 0.95))

                    if st.auto_gain and peak > 0:
                        target = mx_val * 0.8
                        rate = 0.02 if st.auto_gain == 2 else 0.01
                        if peak < target:
                            st.gain = min(3.0, st.gain * (1.0 + rate))
                        elif peak > target * 1.1:
                            st.gain = max(0.1, st.gain * (1.0 - rate))

                    data = apply_gain(data, st.gain)
                    db = rms_db(data, bd)
                    st.current_db = db
                    st.levels.append(db)
                    if len(st.levels) > 40:
                        st.levels.pop(0)

                    # Write PCM to ffmpeg with crash recovery (BUG-09)
                    try:
                        if ffmpeg_proc and ffmpeg_proc.poll() is None:
                            raw = pack_int24(data) if bd == 24 else data.tobytes()
                            ffmpeg_proc.stdin.write(raw)
                            ffmpeg_proc.stdin.flush()
                            st.error_msg = None
                        else:
                            raise BrokenPipeError('ffmpeg process died unexpectedly')
                    except (BrokenPipeError, OSError):
                        st.error_msg = 'ffmpeg crashed — restarting...'
                        try:
                            stop_ffmpeg()
                            start_ffmpeg(cur_file)
                            st.error_msg = None
                        except Exception as exc:
                            st.error_msg = f'ffmpeg restart failed: {exc}'
                            st.running = False
                            break

                    st.read_key()
                    cur_actual = _get_file_size(cur_file)
                    draw_hud(st, args, cur_file, cur_actual, size_lim, el,
                             total_override=st.total_bytes + cur_actual)

                    # Manual new-file rotation
                    if st.new_file:
                        stop_ffmpeg()
                        st.total_bytes += _get_file_size(cur_file)
                        cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                        cur_file = next_filename(rotation_base, fmt, out_dir)
                        st.files_created.append(cur_file)
                        st.new_file = False
                        st.clipping = 0
                        st._drawn = False
                        start_ffmpeg(cur_file)

                    # Rename
                    if st.rename:
                        stop_ffmpeg()
                        st.total_bytes += _get_file_size(cur_file)
                        new_base = prompt_rename(cur_file)
                        if new_base:
                            rotation_base = new_base
                        cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                        cur_file = next_filename(rotation_base, fmt, out_dir)
                        st.files_created.append(cur_file)
                        start_ffmpeg(cur_file)
                        st.rename = False
                        st.clipping = 0
                        st._drawn = False

        except KeyboardInterrupt:
            pass
        except Exception:
            stop_ffmpeg()
            raise
        finally:
            stop_ffmpeg()
            # Add current (just-finalized) file to total
            st.total_bytes += _get_file_size(cur_file)
    finally:
        restore_terminal()
        _print_session_summary(st)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    enable_windows_ansi()
    args = parse_args()
    if args.file_format == 'wav':
        record_wav(args)
    else:
        record_compressed(args)


if __name__ == '__main__':
    main()
