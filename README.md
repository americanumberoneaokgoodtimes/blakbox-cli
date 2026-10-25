# blakbox-cli

Interactive terminal audio recorder with file rotation. Streams microphone input to rotating WAV, MP3, FLAC, or OGG files with a live HUD showing levels, disk space, and gain control.

## Setup

```bash
pip install -r requirements.txt
```

Requires **ffmpeg** in PATH for MP3/FLAC/OGG output. Install from [ffmpeg.org](https://ffmpeg.org/download.html).

On Linux, PortAudio must also be installed (`sudo apt install libportaudio2`), including when running the prebuilt binary. Windows and macOS wheels bundle it.

## Usage

```bash
# Basic usage — starts interactive HUD
python audio_recorder.py

# Show version
python audio_recorder.py --version

# List available audio input devices
python audio_recorder.py --list-devices

# Custom output name
python audio_recorder.py -o my_recording

# MP3, 192kbps, 2GB files, keep 5, 10min limit
python audio_recorder.py -f mp3 -b 192 -s 2000000000 -m 5 -l 600

# FLAC, 48000Hz, mono
python audio_recorder.py -f flac -r 48000 -c 1

# OGG, 24-bit, 1GB size limit
python audio_recorder.py -f ogg -d 24 -s 1000000000

# Specify audio input device by name or index
python audio_recorder.py --device "Microphone (Realtek)"
python audio_recorder.py --device 2

# Output to a specific directory
python audio_recorder.py --output-dir D:\recordings
```

## Defaults

| Flag | Default | Description |
|------|---------|-------------|
| `-o` | `{datetime}` | Output filename base |
| `-f` | `wav` | Format: wav, mp3, flac, ogg |
| `-s` | `4000000000` | Max file size per file (bytes or units: e.g. 4GB, 500MB) |
| `-m` | `4` | Max files to keep |
| `-b` | `320` | Bit rate (kbps, compressed formats) |
| `-d` | `16` | Bit depth: 8, 16, 24, 32 |
| `-r` | `48000` | Sample rate (Hz) |
| `-c` | `2` | Audio channels |
| `-l` | `999999` | Max duration (seconds, 0=unlimited) |
| `--output-dir` | `.` | Output directory |
| `--device` | system default | Audio input device name or index |
| `--monitor-device` | system default | Audio output device name or index for monitoring |
| `--list-devices` | — | List input devices and exit |

## Interactive HUD

Live display during recording:
- **Size progress bar** — current file size vs limit
- **Disk space bar** — disk used / total (color-coded: blue → yellow → red as disk fills; emergency halt if < 50MB free)
- **Microphone level meter** — real-time RMS + waveform sparkline
- **Peak level** — peak indicator with color-coded zones
- **Gain control** — dB and linear multiplier, auto-gain toggle (with fast attack limiter)
- **Clipping & drop counters** — true saturation clips (`Clip`) and hardware buffer drops (`Drop: X`)
- **Timer** — total session elapsed time
- **Error line** — ffmpeg or device errors shown in red if they occur

## Keyboard Controls

| Key | Action |
|-----|--------|
| `Q` | Quit recording |
| `N` | Start new file (rotate now) |
| `R` | Rename: prompts for a new base name, then starts a new file `{newbase}_NNNN` (Enter, Esc or Ctrl+C on Windows cancels) |
| `D` | Select audio input device (triggers new file rotation) |
| `M` | Toggle headphone monitor mode (interactive output device list) |
| `P` | Pause / resume recording |
| `+` | Increase gain (+1 dB, max +30 dB) |
| `-` | Decrease gain (-1 dB, min -30 dB) |
| `0` | Reset gain to unity (0 dB, ×1.00) |
| `A` | Cycle auto-gain (off → normal → fast) |
| `Shift+B` | Boss Mode — blank screen, show only fill % (`R`, `D`, `M` are ignored while blanked) |
| `b` | Unblank — return to full dashboard |
| `Ctrl+C` | Stop recording |

## Configuration file

Defaults can be stored in `~/.blakboxrc` (TOML; needs Python 3.11+ or the `tomli` package). Command-line flags override it. Recognised keys:

```toml
format = "flac"          # wav | mp3 | flac | ogg   (CLI: --file-format)
size = "2GB"             # bytes or with unit        (CLI: --size)
max_recordings = 6
bit_rate = 192
bit_depth = 24           # 8 | 16 | 24 | 32
sample_rate = 48000
channels = 2
length = 0               # seconds, 0 = unlimited
output_dir = "~/recordings"
device = ""              # name or index, "" = system default
monitor_device = ""
```

Invalid values fall back to the defaults with a warning. Unknown keys are ignored.

## Safety behaviour

- The input device is tested (and must deliver audio) **before** any old recording is deleted.
- Rotation only deletes files named exactly `{base}_{NNNN}.{fmt}`; other files that merely share the prefix are never touched.
- WAV size limits are clamped to just under 4 GiB (the WAV format limit). WAV data is flushed to disk after every chunk.
- If the output fails repeatedly (ffmpeg dies, disk full) recording halts with the reason printed in the session summary instead of rotating endlessly. Low disk space (< 50 MB) also halts recording cleanly.
- MP3 output is resampled to the nearest rate libmp3lame supports and downmixed to stereo when needed.
- The audio queue is bounded (about 2 minutes). While a prompt (rename/device/monitor) is open nothing is written to disk; if it stays open long enough chunks are dropped and counted under `Drop`.
- `-o` / rename names may not contain path separators or characters illegal on Windows.

## File Rotation

Files are named `{base}_{NNNN}.{fmt}` (e.g. `2026-10-14_10-43-07_0001.wav`). When the size limit is hit, the oldest file is deleted and a new one starts. The file index is always monotonically increasing — no file is ever overwritten.

On exit, a summary of all files created this session is printed with their sizes.

## System Audio Capture (Windows)

To record system audio (e.g. music, video playback), enable **Stereo Mix** (or another virtual input such as a loopback driver) in the sound settings. WASAPI loopback capture is **not** supported, because PortAudio's WASAPI loopback devices are not listed as inputs. `--list-devices` shows the host API of each device in brackets (a microphone appears once per host API on Windows):

```bash
python audio_recorder.py --list-devices   # find the Stereo Mix device
python audio_recorder.py --device "Stereo Mix (Realtek)"
```

## Running Tests

```bash
python -m pytest tests/ -v
```
