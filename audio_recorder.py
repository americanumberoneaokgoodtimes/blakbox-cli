#!/usr/bin/env python3
"""
blakbox-cli - Interactive terminal audio recorder with file rotation.

Streams recorded audio to rotating files (WAV, MP3, FLAC, OGG).
Live HUD: progress bars, disk space, microphone level graph, gain control.
"""

VERSION = '1.2.0'

import argparse
import atexit
import glob
import os
import queue
import signal
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
    register_cleanup_and_signals()
    if HAS_MSVCRT or not HAS_TERMIOS:
        return
    try:
        fd = sys.stdin.fileno()
        _orig_termios_state = _termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except Exception:
        _orig_termios_state = None


def restore_terminal():
    """Restore terminal to cooked mode and ensure cursor is visible."""
    global _orig_termios_state
    try:
        sys.stdout.write(ANSI['show_cursor'])
        sys.stdout.flush()
    except Exception:
        pass
    if HAS_MSVCRT or not HAS_TERMIOS or _orig_termios_state is None:
        return
    try:
        _termios.tcsetattr(sys.stdin.fileno(), _termios.TCSADRAIN, _orig_termios_state)
        _orig_termios_state = None
    except Exception:
        pass


def _signal_handler(sig, frame):
    restore_terminal()
    sys.exit(0)


def register_cleanup_and_signals():
    """Register exit handlers and termination signals to guarantee terminal restoration."""
    atexit.register(restore_terminal)
    for sig_name in ('SIGTERM', 'SIGHUP'):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _signal_handler)
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


def parse_size_str(val):
    """Parse size string with optional unit suffix (e.g. '4GB', '500MB', '100K') to bytes."""
    if val is None:
        return DEFAULT_SIZE_LIMIT
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        v = val.strip().upper()
        multiplier = 1
        if v.endswith('GIB'):
            multiplier = 1024 ** 3
            v = v[:-3]
        elif v.endswith('GB'):
            multiplier = 1_000_000_000
            v = v[:-2]
        elif v.endswith('G'):
            multiplier = 1_000_000_000
            v = v[:-1]
        elif v.endswith('MIB'):
            multiplier = 1024 ** 2
            v = v[:-3]
        elif v.endswith('MB'):
            multiplier = 1_000_000
            v = v[:-2]
        elif v.endswith('M'):
            multiplier = 1_000_000
            v = v[:-1]
        elif v.endswith('KIB'):
            multiplier = 1024
            v = v[:-3]
        elif v.endswith('KB'):
            multiplier = 1_000
            v = v[:-2]
        elif v.endswith('K'):
            multiplier = 1_000
            v = v[:-1]
        elif v.endswith('B'):
            v = v[:-1]
        try:
            return int(float(v) * multiplier)
        except ValueError:
            pass
    return int(val)


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
    parser.add_argument('-s', '--size', type=parse_size_str, default=argparse.SUPPRESS,
                        help='Max file size in bytes, or with unit like 4GB, 500MB (default: 4000000000)')
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
    parser.add_argument('--monitor-device', default=argparse.SUPPRESS,
                        help='Audio output device name or index for monitoring (default: system default output)')
    parser.add_argument('--list-devices', '--list-device', action='store_true', dest='list_devices',
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
        'monitor_device': None,
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
                'monitor_device': 'monitor_device',
            }.get(key, key)
            if config_key in config:
                val = config[config_key]
                if config_key in ('device', 'monitor_device') and val == '':
                    val = None
                setattr(args, key, val)
            else:
                setattr(args, key, default_val)

    # Strict configuration schema validation and sanitization
    if args.file_format not in SUPPORTED_FORMATS:
        print(f'{ANSI["yellow"]}Warning: unsupported format "{args.file_format}", defaulting to "wav"{ANSI["reset"]}', file=sys.stderr)
        args.file_format = 'wav'

    try:
        args.bit_depth = int(args.bit_depth)
        if args.bit_depth not in (8, 16, 24, 32):
            print(f'{ANSI["yellow"]}Warning: invalid bit depth {args.bit_depth}, defaulting to 16{ANSI["reset"]}', file=sys.stderr)
            args.bit_depth = 16
    except (ValueError, TypeError):
        args.bit_depth = 16

    try:
        args.sample_rate = int(args.sample_rate)
        if args.sample_rate <= 0:
            print(f'{ANSI["yellow"]}Warning: invalid sample rate {args.sample_rate}, defaulting to 48000{ANSI["reset"]}', file=sys.stderr)
            args.sample_rate = 48000
    except (ValueError, TypeError):
        args.sample_rate = 48000

    try:
        args.channels = int(args.channels)
        if args.channels <= 0:
            print(f'{ANSI["yellow"]}Warning: invalid channels {args.channels}, defaulting to 2{ANSI["reset"]}', file=sys.stderr)
            args.channels = 2
    except (ValueError, TypeError):
        args.channels = 2

    try:
        args.bit_rate = int(args.bit_rate)
        if args.bit_rate <= 0:
            print(f'{ANSI["yellow"]}Warning: invalid bit rate {args.bit_rate}, defaulting to 320{ANSI["reset"]}', file=sys.stderr)
            args.bit_rate = 320
    except (ValueError, TypeError):
        args.bit_rate = 320

    try:
        args.max_recordings = int(args.max_recordings)
        if args.max_recordings <= 0:
            print(f'{ANSI["yellow"]}Warning: invalid max_recordings {args.max_recordings}, defaulting to 4{ANSI["reset"]}', file=sys.stderr)
            args.max_recordings = 4
    except (ValueError, TypeError):
        args.max_recordings = 4

    try:
        args.size = parse_size_str(args.size)
        if args.size <= 0:
            args.size = DEFAULT_SIZE_LIMIT
    except (ValueError, TypeError):
        args.size = DEFAULT_SIZE_LIMIT

    try:
        args.length = int(args.length)
        if args.length < 0:
            args.length = 0
    except (ValueError, TypeError):
        args.length = DEFAULT_LENGTH

    if args.list_devices:
        print(f'{ANSI["bold"]}Available audio input devices:{ANSI["reset"]}')
        def_in = -1
        try:
            def_in = sd.default.device[0]
        except Exception:
            pass
        for i, dev in enumerate(sd.query_devices()):
            if dev['max_input_channels'] > 0:
                marker = f'{ANSI["green"]}*{ANSI["reset"]}' if i == def_in else ' '
                print(f'  {marker} [{i}] {dev["name"]}'
                      f' ({dev["max_input_channels"]} ch, {int(dev["default_samplerate"])} Hz)')
        sys.exit(0)

    if args.device is None:
        try:
            def_in = sd.default.device[0]
            if def_in == -1:
                for i, d in enumerate(sd.query_devices()):
                    if d['max_input_channels'] > 0:
                        args.device = i
                        break
        except Exception:
            pass

    if args.device is not None:
        if isinstance(args.device, str):
            try:
                args.device = int(args.device)
            except (ValueError, TypeError):
                pass

        dev_info = None
        try:
            dev_info = sd.query_devices(args.device, 'input')
        except Exception:
            if isinstance(args.device, str):
                try:
                    for i, d in enumerate(sd.query_devices()):
                        if d['max_input_channels'] > 0 and args.device.lower() in d['name'].lower():
                            args.device = i
                            dev_info = d
                            break
                except Exception:
                    pass

        if dev_info is not None:
            if dev_info['max_input_channels'] < args.channels:
                args.channels = max(1, dev_info['max_input_channels'])
                print(f'Notice: adjusted channels to {args.channels} for "{dev_info["name"]}"', file=sys.stderr)
            default_sr = int(dev_info.get('default_samplerate', args.sample_rate))
            if args.sample_rate != default_sr:
                try:
                    sd.check_input_settings(device=args.device, channels=args.channels, samplerate=args.sample_rate)
                except Exception:
                    args.sample_rate = default_sr

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
    if HAS_MSVCRT and sys.stdin.isatty():
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
        # Piped stdin or Unix:
        if not HAS_MSVCRT and sys.stdin.isatty():
            restore_terminal()
        try:
            line = sys.stdin.readline().rstrip('\r\n')
        finally:
            if not HAS_MSVCRT and sys.stdin.isatty():
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


def prompt_select_device(current_device=None):
    """Prompt the user to select an input device from available input devices.
    Returns:
        int: Selected device index
        None: Cancelled (keep current device)
        -1: Invalid input
    """
    print(f'{ANSI["show_cursor"]}', end='')
    print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
    print(f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}INPUT DEVICE LIST'
          f'{ANSI["reset"]}{" " * 33}{ANSI["cyan"]}|{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}')
    print()

    input_devices = []
    try:
        all_devs = sd.query_devices()
        for i, dev in enumerate(all_devs):
            if dev['max_input_channels'] > 0:
                input_devices.append((i, dev))
    except Exception as exc:
        print(f'  {ANSI["red"]}Error querying devices: {exc}{ANSI["reset"]}')
        _readline_echo('  Press Enter to continue: ')
        print(f'{ANSI["hide_cursor"]}', end='')
        return None

    if not input_devices:
        print(f'  {ANSI["red"]}No input devices found.{ANSI["reset"]}')
        _readline_echo('  Press Enter to continue: ')
        print(f'{ANSI["hide_cursor"]}', end='')
        return None

    # Determine currently active device index
    curr_idx = None
    if current_device is not None:
        try:
            curr_idx = int(current_device)
        except (ValueError, TypeError):
            for i, dev in input_devices:
                if str(current_device).lower() in dev['name'].lower():
                    curr_idx = i
                    break
    if curr_idx is None:
        try:
            curr_idx = sd.default.device[0]
        except Exception:
            curr_idx = None

    PAGE_SIZE = 10
    total_devs = len(input_devices)
    total_pages = (total_devs + PAGE_SIZE - 1) // PAGE_SIZE
    page = 0
    valid_indices = {i for i, _ in input_devices}

    while True:
        print(f'{ANSI["show_cursor"]}', end='')
        print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
        print(f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}')
        page_tag = f' [Page {page + 1}/{total_pages}]' if total_pages > 1 else ''
        pad = max(0, 52 - 19 - len(page_tag))
        print(f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}INPUT DEVICE LIST{page_tag}'
              f'{ANSI["reset"]}{" " * pad}{ANSI["cyan"]}|{ANSI["reset"]}')
        print(f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}')
        print()

        start_i = page * PAGE_SIZE
        end_i = min(total_devs, start_i + PAGE_SIZE)
        print(f'  {ANSI["bold"]}Available audio input devices:{ANSI["reset"]}')
        for idx, dev in input_devices[start_i:end_i]:
            marker = f'{ANSI["green"]}*{ANSI["reset"]}' if idx == curr_idx else ' '
            print(f'  {marker} [{idx}] {dev["name"]}'
                  f' ({dev["max_input_channels"]} ch, {int(dev["default_samplerate"])} Hz)')
        print()
        prompt_txt = f'  Select device number [Enter to cancel]: {ANSI["cyan"]}'
        if total_pages > 1:
            prompt_txt = f'  Select #, [N]ext/[P]rev page, or Enter to cancel: {ANSI["cyan"]}'
        resp = _readline_echo(prompt_txt)
        print(f'{ANSI["reset"]}', end='')
        print(f'{ANSI["hide_cursor"]}', end='')
        resp = resp.strip()
        if not resp:
            return None
        if total_pages > 1 and resp.lower() in ('n', 'next'):
            page = (page + 1) % total_pages
            continue
        if total_pages > 1 and resp.lower() in ('p', 'prev', 'previous'):
            page = (page - 1) % total_pages
            continue
        try:
            chosen = int(resp)
            if chosen in valid_indices:
                return chosen
            return -1
        except ValueError:
            matches = [i for i, dev in input_devices if resp.lower() in dev['name'].lower()]
            if len(matches) == 1:
                return matches[0]
            return -1


def prompt_monitor_warning():
    """Prompt the user with headphone warning before enabling monitor mode."""
    print(f'{ANSI["show_cursor"]}', end='')
    print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
    print(f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}AUDIO MONITORING WARNING'
          f'{ANSI["reset"]}{" " * 26}{ANSI["cyan"]}|{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}')
    print()
    warning_text = 'Make sure you have headphones on, audio will feedback (noise) loop (echo) if the input (mic) receives (hears) the output (spkr) signal (sound)'
    print(f'  {ANSI["yellow"]}{warning_text}{ANSI["reset"]}')
    print()
    resp = _readline_echo(f'  Enable monitor mode? [y/N]: {ANSI["cyan"]}')
    print(f'{ANSI["reset"]}', end='')
    print(f'{ANSI["hide_cursor"]}', end='')
    return resp.strip().lower() in ('y', 'yes')


def prompt_select_output_device(current_output_device=None):
    """Prompt the user to select an output device from available output devices.
    Returns:
        int: Selected device index
        False: Cancelled (do not enable monitoring)
        -1: Invalid input
    """
    print(f'{ANSI["show_cursor"]}', end='')
    print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
    print(f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}OUTPUT DEVICE LIST (MONITOR)'
          f'{ANSI["reset"]}{" " * 22}{ANSI["cyan"]}|{ANSI["reset"]}')
    print(f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}')
    print()

    output_devices = []
    try:
        all_devs = sd.query_devices()
        for i, dev in enumerate(all_devs):
            if dev['max_output_channels'] > 0:
                output_devices.append((i, dev))
    except Exception as exc:
        print(f'  {ANSI["red"]}Error querying devices: {exc}{ANSI["reset"]}')
        _readline_echo('  Press Enter to continue: ')
        print(f'{ANSI["hide_cursor"]}', end='')
        return False

    if not output_devices:
        print(f'  {ANSI["red"]}No output devices found.{ANSI["reset"]}')
        _readline_echo('  Press Enter to continue: ')
        print(f'{ANSI["hide_cursor"]}', end='')
        return False

    curr_idx = None
    if current_output_device is not None:
        try:
            curr_idx = int(current_output_device)
        except (ValueError, TypeError):
            for idx, dev in output_devices:
                if str(current_output_device).lower() in dev['name'].lower():
                    curr_idx = idx
                    break
    if curr_idx is None:
        try:
            curr_idx = sd.default.device[1]
        except Exception:
            curr_idx = None

    valid_indices = {i for i, _ in output_devices}
    if curr_idx is None or curr_idx not in valid_indices:
        if output_devices:
            curr_idx = output_devices[0][0]

    PAGE_SIZE = 10
    total_devs = len(output_devices)
    total_pages = (total_devs + PAGE_SIZE - 1) // PAGE_SIZE
    page = 0

    while True:
        print(f'{ANSI["show_cursor"]}', end='')
        print(f'{ANSI["clear"]}{ANSI["home"]}', end='')
        print(f'{ANSI["cyan"]}{"=" * 54}{ANSI["reset"]}')
        page_tag = f' [Page {page + 1}/{total_pages}]' if total_pages > 1 else ''
        pad = max(0, 52 - 30 - len(page_tag))
        print(f'{ANSI["cyan"]}|{ANSI["reset"]}  {ANSI["bold"]}OUTPUT DEVICE LIST (MONITOR){page_tag}'
              f'{ANSI["reset"]}{" " * pad}{ANSI["cyan"]}|{ANSI["reset"]}')
        print(f'{ANSI["cyan"]}+{"=" * 52}+{ANSI["reset"]}')
        print()

        start_i = page * PAGE_SIZE
        end_i = min(total_devs, start_i + PAGE_SIZE)
        print(f'  {ANSI["bold"]}Available audio output devices:{ANSI["reset"]}')
        for idx, dev in output_devices[start_i:end_i]:
            marker = f'{ANSI["green"]}*{ANSI["reset"]}' if idx == curr_idx else ' '
            print(f'  {marker} [{idx}] {dev["name"]}'
                  f' ({dev["max_output_channels"]} ch, {int(dev["default_samplerate"])} Hz)')
        print()
        prompt_txt = f'  Select output device number [Enter for default, "c" to cancel]: {ANSI["cyan"]}'
        if total_pages > 1:
            prompt_txt = f'  Select #, [N]ext/[P]rev page, Enter for default, "c" to cancel: {ANSI["cyan"]}'
        resp = _readline_echo(prompt_txt)
        print(f'{ANSI["reset"]}', end='')
        print(f'{ANSI["hide_cursor"]}', end='')
        resp = resp.strip()
        if resp.lower() in ('c', 'cancel'):
            return False
        if total_pages > 1 and resp.lower() in ('n', 'next'):
            page = (page + 1) % total_pages
            continue
        if total_pages > 1 and resp.lower() in ('p', 'prev', 'previous'):
            page = (page - 1) % total_pages
            continue
        if not resp:
            return curr_idx
        try:
            chosen = int(resp)
            if chosen in valid_indices:
                return chosen
            return -1
        except ValueError:
            matches = [i for i, dev in output_devices if resp.lower() in dev['name'].lower()]
            if len(matches) == 1:
                return matches[0]
            return -1


class AudioOutputStream:
    """Audio output stream using PortAudio callback and thread-safe queue.
    Prevents blocking the main recording loop and works with all host APIs (e.g. WDM-KS).
    """
    def __init__(self, samplerate, channels, dtype, device=None, max_queue=10):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        if isinstance(device, str):
            try:
                device = int(device)
            except (ValueError, TypeError):
                pass
        self.device = device
        self._queue = queue.Queue(maxsize=max_queue)
        self._underflow = False
        self._remainder = np.empty((0, channels), dtype=dtype)

        def _callback(outdata, frames, time_info, status):
            if status:
                self._underflow = True
            written = 0
            if len(self._remainder) > 0:
                take = min(frames, len(self._remainder))
                outdata[:take] = self._remainder[:take]
                self._remainder = self._remainder[take:]
                written += take

            while written < frames:
                try:
                    chunk = self._queue.get_nowait()
                    need = frames - written
                    if len(chunk) <= need:
                        outdata[written:written + len(chunk)] = chunk
                        written += len(chunk)
                    else:
                        outdata[written:frames] = chunk[:need]
                        self._remainder = chunk[need:]
                        written = frames
                except queue.Empty:
                    outdata[written:] = 0
                    break

        self._stream = sd.OutputStream(
            samplerate=samplerate,
            channels=channels,
            dtype=dtype,
            device=device,
            callback=_callback
        )

    def write(self, data):
        """Non-blocking write: enqueue audio data; drop frame if queue is full."""
        try:
            self._queue.put_nowait(data.copy())
        except queue.Full:
            pass

    def start(self):
        self._stream.start()

    def stop(self):
        self._stream.stop()

    def close(self):
        self._stream.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
        self.close()


def open_monitor_stream(sr, ch, dt, device=None):
    """Open an AudioOutputStream on the default or specified output device for real-time monitoring.
    Returns (stream, out_channels).
    """
    for test_ch in (ch, 2, 1):
        try:
            stream = AudioOutputStream(samplerate=sr, channels=test_ch, dtype=dt, device=device)
            stream.start()
            return stream, test_ch
        except Exception:
            continue
    raise RuntimeError('Could not open output audio device for monitoring')


def write_monitor_audio(monitor_stream, monitor_channels, data):
    """Write audio data to monitor stream, adjusting channel layout if needed."""
    if monitor_stream is None:
        return
    if data.ndim == 1:
        if monitor_channels == 2:
            mdata = np.column_stack([data, data])
        else:
            mdata = data
    else:
        if monitor_channels == 2 and data.shape[1] == 1:
            mdata = np.repeat(data, 2, axis=1)
        elif monitor_channels == 1 and data.shape[1] > 1:
            mdata = data[:, :1]
        else:
            mdata = data
    monitor_stream.write(mdata)


class AudioInputStream:
    """Audio input stream using a PortAudio callback and thread-safe queue.
    Provides identical API to sd.InputStream (read, start, stop, close, context manager)
    while working on all host APIs including Windows WDM-KS (where blocking read() fails).
    """
    def __init__(self, samplerate, channels, dtype, blocksize, device=None):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        if isinstance(device, str):
            try:
                device = int(device)
            except (ValueError, TypeError):
                pass
        self.device = device
        self._queue = queue.Queue()
        self._overflow = False
        self._paused = False
        self._drops = 0

        def _callback(indata, frames, time_info, status):
            if status:
                self._overflow = True
                self._drops += 1
            if not self._paused:
                self._queue.put(indata.copy())

        self._stream = sd.InputStream(
            samplerate=samplerate,
            channels=channels,
            dtype=dtype,
            blocksize=blocksize,
            device=device,
            callback=_callback
        )

    def pause(self):
        self._paused = True
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def resume(self):
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._paused = False

    def start(self):
        self._stream.start()

    def stop(self):
        self._stream.stop()

    def close(self):
        self._stream.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
        self.close()

    def read(self, blocksize):
        try:
            data = self._queue.get(timeout=2.0)
            ov = self._overflow
            self._overflow = False
            return data, ov
        except queue.Empty:
            self._drops += 1
            return np.zeros((blocksize, self.channels), dtype=self.dtype), True


def handle_device_switch(st, args, current_stream, sr, ch, dt, bs):
    """Handle device selection prompt and stream restart if requested.
    Always triggers a New File upon successful device switch to cleanly handle
    mismatched sample rates, channel layouts, and stream continuity.
    """
    if not st.select_device:
        return current_stream
    st.select_device = False
    new_dev = prompt_select_device(args.device)
    st._drawn = False
    if new_dev is None:
        return current_stream
    if new_dev == -1:
        st.error_msg = 'Invalid device selection'
        return current_stream

    curr_int = None
    if args.device is not None:
        try:
            curr_int = int(args.device)
        except (ValueError, TypeError):
            pass
    if new_dev == args.device or (curr_int is not None and new_dev == curr_int):
        return current_stream

    try:
        dev_info = sd.query_devices(new_dev, 'input')
        max_in = dev_info.get('max_input_channels', 0)
        if max_in <= 0:
            st.error_msg = 'Device has no input channels'
            return current_stream
    except Exception as exc:
        st.error_msg = f'Invalid device: {exc}'
        return current_stream

    orig_ch = getattr(args, '_orig_channels', args.channels)
    target_ch = min(orig_ch, dev_info['max_input_channels'])
    default_sr = int(dev_info.get('default_samplerate', args.sample_rate))
    target_sr = args.sample_rate
    try:
        sd.check_input_settings(device=new_dev, channels=target_ch, samplerate=target_sr)
    except Exception:
        target_sr = default_sr
        try:
            sd.check_input_settings(device=new_dev, channels=target_ch, samplerate=target_sr)
        except Exception as exc:
            st.error_msg = f'Device settings unsupported: {exc}'
            return current_stream

    target_bs = int(target_sr * CHUNK_DURATION)

    if current_stream is not None:
        try:
            current_stream.stop()
            current_stream.close()
        except Exception:
            pass

    try:
        new_stream = AudioInputStream(samplerate=target_sr, channels=target_ch, dtype=dt,
                                      blocksize=target_bs, device=new_dev)
        new_stream.start()
        args.device = new_dev
        args.channels = target_ch
        args.sample_rate = target_sr
        st._cached_dev_str = None
        st.error_msg = None
        # Always trigger New File on device switch
        st.new_file = True
        return new_stream
    except Exception as exc:
        st.error_msg = f'Failed to switch device: {exc}'
        try:
            restored_stream = AudioInputStream(samplerate=sr, channels=ch, dtype=dt,
                                                blocksize=bs, device=args.device)
            restored_stream.start()
            return restored_stream
        except Exception:
            return None


def handle_monitor_toggle(st, monitor_stream, sr, ch, dt):
    """Handle monitor toggle and prompt. Returns (monitor_stream, monitor_channels)."""
    if not st.toggle_monitor:
        out_ch = getattr(monitor_stream, 'channels', ch)
        return monitor_stream, out_ch
    st.toggle_monitor = False
    if st.monitor:
        st.monitor = False
        if monitor_stream is not None:
            try:
                monitor_stream.stop()
                monitor_stream.close()
            except Exception:
                pass
            monitor_stream = None
        st._drawn = False
        return None, ch
    else:
        confirmed = prompt_monitor_warning()
        st._drawn = False
        if not confirmed:
            st.monitor = False
            return None, ch

        chosen_out = prompt_select_output_device(st.monitor_device)
        st._drawn = False
        if chosen_out is False:
            st.monitor = False
            return None, ch
        if chosen_out == -1:
            st.monitor = False
            st.error_msg = 'Invalid output device selection'
            return None, ch

        st.monitor_device = chosen_out
        try:
            stream, out_ch = open_monitor_stream(sr, ch, dt, device=st.monitor_device)
            st.monitor = True
            st.error_msg = None
            return stream, out_ch
        except Exception as exc:
            st.monitor = False
            st.error_msg = f'Monitor error: {exc}'
            return None, ch


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
    d = np.asarray(data).reshape(-1).astype(np.int32)
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
        self.select_device = False
        self.toggle_monitor = False
        self.monitor = False
        self.monitor_device = None
        self.gain = 1.0
        self.auto_gain = 0        # 0=off, 1=normal, 2=fast
        self.levels = []
        self.current_db = -100.0
        self.peak_db = -100.0
        self.total_bytes = 0      # completed-file bytes; current file added on stop
        self.clipping = 0
        self.drops = 0
        self.blank = False
        self.paused = False       # pause/resume state
        self.error_msg = None     # transient error string shown in HUD
        self.files_created = []   # all output paths opened this session
        self._drawn = False
        self._last_disk_info = None
        self._last_disk_time = 0
        self._cached_dev = -999
        self._cached_dev_str = 'Default'

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
                elif k in (b'd', b'D'):
                    self.select_device = True
                elif k in (b'm', b'M'):
                    self.toggle_monitor = True
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
                    elif k in ('d', 'D'):
                        self.select_device = True
                    elif k in ('m', 'M'):
                        self.toggle_monitor = True
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

    # Emergency disk space guard (< 50MB halts recording cleanly)
    MIN_DISK_FREE_BYTES = 50 * 1024 * 1024
    if free < MIN_DISK_FREE_BYTES:
        S.error_msg = f'CRITICAL: Low disk space ({fmt_size(free)} free)! Halting recording.'
        S.running = False

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

    if args.device != S._cached_dev:
        S._cached_dev = args.device
        if args.device is None:
            S._cached_dev_str = 'Default'
        else:
            try:
                d_info = sd.query_devices(args.device, 'input')
                S._cached_dev_str = f"[{args.device}] {d_info['name']}"
            except Exception:
                S._cached_dev_str = str(args.device)
        if len(S._cached_dev_str) > 22:
            S._cached_dev_str = S._cached_dev_str[:19] + '...'
    dev_str = S._cached_dev_str
    mon_status = 'ON' if S.monitor else 'OFF'
    mon_color = ANSI['green'] if S.monitor else ANSI['dim']

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
    drop_str = f'  {ANSI["red"]}Drop: {S.drops}{ANSI["reset"]}' if S.drops > 0 else ''

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
        f'  {ANSI["white"]}Dev{ANSI["reset"]}:  {ANSI["cyan"]}{dev_str}{ANSI["reset"]}'
        f'  {ANSI["white"]}Monitor{ANSI["reset"]}: {mon_color}{mon_status}{ANSI["reset"]}',
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
        f'  {ANSI["white"]}Clip{ANSI["reset"]}  {cl}{S.clipping}{ANSI["reset"]}{drop_str}',
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
        f'  {ANSI["cyan"]}[D]{ANSI["reset"]}evice'
        f'  {ANSI["yellow"]}[M]{ANSI["reset"]}onitor'
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
        f'{"unlimited" if args.length <= 0 or args.length >= 999999 else str(args.length) + "s"}',
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
        print(f'{ANSI["yellow"]}  Device unreachable: {err}{ANSI["reset"]}')
        print(f'{ANSI["cyan"]}  Opening interactive device list...{ANSI["reset"]}')
        time.sleep(0.5)
        new_dev = prompt_select_device(args.device)
        if new_dev is not None and new_dev != -1:
            args.device = new_dev
            ok, err = test_audio_device(args)
            if ok:
                return draw_startup(args)
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
        dev = args.device
        if isinstance(dev, str):
            try:
                dev = int(dev)
                args.device = dev
            except (ValueError, TypeError):
                pass

        if dev is not None:
            try:
                dev_info = sd.query_devices(dev, 'input')
                default_sr = int(dev_info.get('default_samplerate', args.sample_rate))
                try:
                    sd.check_input_settings(device=dev, channels=args.channels, samplerate=args.sample_rate)
                except Exception:
                    try:
                        sd.check_input_settings(device=dev, channels=args.channels, samplerate=default_sr)
                        args.sample_rate = default_sr
                        bs = int(args.sample_rate * CHUNK_DURATION)
                    except Exception:
                        pass
            except Exception:
                pass

        with AudioInputStream(samplerate=args.sample_rate, channels=args.channels,
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
# Audio Sink Abstractions (TASK-P4-01: unified loop architecture)
# ---------------------------------------------------------------------------

class AudioSink:
    """Abstract base class for audio recording sinks."""
    fmt = 'wav'

    def open(self, path, sr, ch, bd, br=None):
        raise NotImplementedError

    def write(self, data, bd):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def get_current_size(self):
        raise NotImplementedError

    def on_rotate(self, st):
        pass

    def get_total_bytes(self, st, cur_size):
        raise NotImplementedError


class WavSink(AudioSink):
    """Audio sink for writing uncompressed WAV audio files."""
    fmt = 'wav'

    def __init__(self):
        self.wf = None
        self.cur_file_size = WAV_HEADER_BYTES
        self.cur_path = None

    def open(self, path, sr, ch, bd, br=None):
        self.close()
        self.cur_path = path
        self.wf = wave.open(path, 'wb')
        self.wf.setnchannels(ch)
        self.wf.setsampwidth(bd // 8)
        self.wf.setframerate(sr)
        self.cur_file_size = WAV_HEADER_BYTES

    def write(self, data, bd):
        if self.wf is not None:
            write_wav_frames(self.wf, data, bd)
            fb = data.size * 3 if bd == 24 else len(data.tobytes())
            self.cur_file_size += fb
            return fb
        return 0

    def close(self):
        if self.wf is not None:
            try:
                self.wf.close()
            except Exception:
                pass
            self.wf = None

    def get_current_size(self):
        return self.cur_file_size

    def on_rotate(self, st):
        self.close()

    def get_total_bytes(self, st, cur_size):
        return st.total_bytes


class CompressedSink(AudioSink):
    """Audio sink streaming PCM audio to ffmpeg subprocess via stdin pipe."""
    def __init__(self, fmt, bit_rate):
        self.fmt = fmt
        self.bit_rate = bit_rate
        self.proc = None
        self.cur_path = None
        self.sr = None
        self.ch = None
        self.bd = None

    def open(self, path, sr, ch, bd, br=None):
        self.close()
        self.cur_path = path
        self.sr = sr
        self.ch = ch
        self.bd = bd
        if br is not None:
            self.bit_rate = br
        cmd = build_ffmpeg_cmd(self.fmt, path, self.bit_rate, sr, ch, bd)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.05)
        if self.proc.poll() is not None:
            err = self.proc.stderr.read().decode(errors='replace')
            raise RuntimeError(f'ffmpeg failed to start: {err[:300]}')

    def write(self, data, bd):
        if self.proc and self.proc.poll() is None:
            raw = pack_int24(data) if bd == 24 else data.tobytes()
            self.proc.stdin.write(raw)
            self.proc.stdin.flush()
            return len(raw)
        else:
            raise BrokenPipeError('ffmpeg process died unexpectedly')

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    def get_current_size(self):
        return _get_file_size(self.cur_path) if self.cur_path else 0

    def on_rotate(self, st):
        actual = self.get_current_size()
        self.close()
        st.total_bytes += actual

    def get_total_bytes(self, st, cur_size):
        return st.total_bytes + cur_size


def record_engine(args, sink):
    """Unified audio recording event loop supporting all audio sinks."""
    st = UIState()
    st.monitor_device = getattr(args, 'monitor_device', None)
    out_dir = args.output_dir
    rotation_base = args.output
    size_lim = args.size
    max_r = args.max_recordings
    bd = args.bit_depth
    sr = args.sample_rate
    ch = args.channels
    md = args.length
    fmt = sink.fmt

    cleanup_oldest(rotation_base, fmt, out_dir, max_r)
    cur_file = next_filename(rotation_base, fmt, out_dir)
    st.files_created.append(cur_file)
    args._orig_channels = ch

    set_raw_terminal()
    try:
        draw_startup(args)
        sr = args.sample_rate
        ch = args.channels
        print(ANSI['hide_cursor'], end='')

        t0 = time.time()
        was_paused = False

        try:
            sink.open(cur_file, sr, ch, bd, getattr(args, 'bit_rate', DEFAULT_BIT_RATE))

            bs = int(sr * CHUNK_DURATION)
            dt = get_audio_dtype(bd)

            stream = AudioInputStream(samplerate=sr, channels=ch, dtype=dt, blocksize=bs,
                                      device=args.device)
            stream.start()
            monitor_stream = None
            monitor_channels = ch

            try:
                while st.running:
                    el = time.time() - t0
                    if md > 0 and el >= md:
                        break

                    # Handle pause: read keys and update HUD, but don't process audio
                    if st.paused:
                        if not was_paused:
                            stream.pause()
                            was_paused = True
                        st.read_key()
                        stream = handle_device_switch(st, args, stream, sr, ch, dt, bs)
                        monitor_stream, monitor_channels = handle_monitor_toggle(st, monitor_stream, args.sample_rate, args.channels, dt)
                        if st.new_file:
                            sink.on_rotate(st)
                            cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                            cur_file = next_filename(rotation_base, fmt, out_dir)
                            st.files_created.append(cur_file)
                            ch = args.channels
                            sr = args.sample_rate
                            bd = args.bit_depth
                            bs = int(sr * CHUNK_DURATION)
                            dt = get_audio_dtype(bd)
                            st.new_file = False
                            st.clipping = 0
                            st._drawn = False
                            sink.open(cur_file, sr, ch, bd, getattr(args, 'bit_rate', DEFAULT_BIT_RATE))
                        cur_actual = sink.get_current_size()
                        draw_hud(st, args, cur_file, cur_actual, size_lim, el,
                                 total_override=sink.get_total_bytes(st, cur_actual))
                        time.sleep(0.1)
                        continue

                    if was_paused:
                        stream.resume()
                        was_paused = False

                    # Automatic size-limit rotation
                    cur_actual = sink.get_current_size()
                    if cur_actual >= size_lim:
                        sink.on_rotate(st)
                        cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                        cur_file = next_filename(rotation_base, fmt, out_dir)
                        st.files_created.append(cur_file)
                        st.clipping = 0
                        st._drawn = False
                        sink.open(cur_file, sr, ch, bd, getattr(args, 'bit_rate', DEFAULT_BIT_RATE))
                        st.read_key()
                        draw_hud(st, args, cur_file, 0, size_lim, el,
                                 total_override=sink.get_total_bytes(st, 0))
                        continue

                    data, ov = stream.read(bs)
                    if ov:
                        st.drops += 1

                    mx_val = np.iinfo(data.dtype).max
                    peak = np.max(np.abs(data))
                    st.peak_db = 20 * np.log10(peak / mx_val) if peak > 0 else -100.0
                    st.clipping += int(np.sum(np.abs(data) >= mx_val * 0.999))

                    if st.auto_gain and peak > 0:
                        target = mx_val * 0.8
                        rate = 0.02 if st.auto_gain == 2 else 0.01
                        if peak < target:
                            st.gain = min(3.0, st.gain * (1.0 + rate))
                        elif peak > target * 1.1:
                            attack_rate = rate * 4
                            st.gain = max(0.1, st.gain * (1.0 - attack_rate))

                    data = apply_gain(data, st.gain)
                    if st.monitor and monitor_stream is not None:
                        try:
                            write_monitor_audio(monitor_stream, monitor_channels, data)
                        except Exception as exc:
                            st.error_msg = f'Monitor error: {exc}'
                            try:
                                monitor_stream.stop()
                                monitor_stream.close()
                            except Exception:
                                pass
                            monitor_stream = None
                            st.monitor = False

                    db = rms_db(data, bd)
                    st.current_db = db
                    st.levels.append(db)
                    if len(st.levels) > 40:
                        st.levels.pop(0)

                    # Write audio chunk to sink with crash recovery
                    try:
                        fb = sink.write(data, bd)
                        if isinstance(sink, WavSink):
                            st.total_bytes += fb
                        st.error_msg = None
                    except (BrokenPipeError, OSError):
                        st.error_msg = 'Sink process died — rotating to new file...'
                        try:
                            sink.on_rotate(st)
                            cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                            cur_file = next_filename(rotation_base, fmt, out_dir)
                            st.files_created.append(cur_file)
                            st.clipping = 0
                            st._drawn = False
                            sink.open(cur_file, sr, ch, bd, getattr(args, 'bit_rate', DEFAULT_BIT_RATE))
                            st.error_msg = None
                        except Exception as exc:
                            st.error_msg = f'Sink restart failed: {exc}'
                            st.running = False
                            break

                    st.read_key()
                    stream = handle_device_switch(st, args, stream, sr, ch, dt, bs)
                    monitor_stream, monitor_channels = handle_monitor_toggle(st, monitor_stream, args.sample_rate, args.channels, dt)
                    cur_actual = sink.get_current_size()
                    draw_hud(st, args, cur_file, cur_actual, size_lim, el,
                             total_override=sink.get_total_bytes(st, cur_actual))

                    # Manual new-file rotation (or device switch rotation)
                    if st.new_file:
                        sink.on_rotate(st)
                        cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                        cur_file = next_filename(rotation_base, fmt, out_dir)
                        st.files_created.append(cur_file)
                        ch = args.channels
                        sr = args.sample_rate
                        bd = args.bit_depth
                        bs = int(sr * CHUNK_DURATION)
                        dt = get_audio_dtype(bd)
                        st.new_file = False
                        st.clipping = 0
                        st._drawn = False
                        sink.open(cur_file, sr, ch, bd, getattr(args, 'bit_rate', DEFAULT_BIT_RATE))

                    # Rename
                    if st.rename:
                        sink.on_rotate(st)
                        new_base = prompt_rename(cur_file)
                        if new_base:
                            rotation_base = new_base
                        cleanup_oldest(rotation_base, fmt, out_dir, max_r)
                        cur_file = next_filename(rotation_base, fmt, out_dir)
                        st.files_created.append(cur_file)
                        st.rename = False
                        st.clipping = 0
                        st._drawn = False
                        sink.open(cur_file, sr, ch, bd, getattr(args, 'bit_rate', DEFAULT_BIT_RATE))
            finally:
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
                if monitor_stream is not None:
                    try:
                        monitor_stream.stop()
                        monitor_stream.close()
                    except Exception:
                        pass

        except KeyboardInterrupt:
            pass
        finally:
            sink.close()
            if isinstance(sink, CompressedSink):
                st.total_bytes += _get_file_size(cur_file)
    finally:
        restore_terminal()
        _print_session_summary(st)


def record_wav(args):
    sink = WavSink()
    return record_engine(args, sink)


def record_compressed(args):
    check_ffmpeg()
    sink = CompressedSink(args.file_format, getattr(args, 'bit_rate', DEFAULT_BIT_RATE))
    return record_engine(args, sink)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    enable_windows_ansi()
    register_cleanup_and_signals()
    args = parse_args()
    if args.file_format == 'wav':
        record_wav(args)
    else:
        record_compressed(args)


if __name__ == '__main__':
    main()
