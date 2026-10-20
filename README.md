# blakbox-cli

Interactive terminal audio recorder with file rotation. Streams microphone input to rotating WAV, MP3, FLAC, or OGG files with a live HUD showing levels, disk space, and gain control.

## Setup

```bash
pip install -r requirements.txt
```

Requires **ffmpeg** in PATH for MP3/FLAC/OGG output. Install from [ffmpeg.org](https://ffmpeg.org/download.html).

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
| `R` | Rename current file base name |
| `D` | Select audio input device (triggers new file rotation) |
| `M` | Toggle headphone monitor mode (interactive output device list) |
| `P` | Pause / resume recording |
| `+` | Increase gain (+0.5x) |
| `-` | Decrease gain (-0.5x) |
| `0` | Reset gain to unity (0 dB, ×1.00) |
| `A` | Cycle auto-gain (off → normal → fast) |
| `Shift+B` | Boss Mode — blank screen, show only fill % |
| `b` | Unblank — return to full dashboard |
| `Ctrl+C` | Stop recording |

## File Rotation

Files are named `{base}_{NNNN}.{fmt}` (e.g. `2026-10-14_10-43-07_0001.wav`). When the size limit is hit, the oldest file is deleted and a new one starts. The file index is always monotonically increasing — no file is ever overwritten.

On exit, a summary of all files created this session is printed with their sizes.

## System Audio Capture (Windows)

To record system audio (e.g. music, video playback), enable **Stereo Mix** or use a WASAPI loopback device:

```bash
python audio_recorder.py --list-devices   # find the Stereo Mix device
python audio_recorder.py --device "Stereo Mix (Realtek)"
```

## Running Tests

```bash
python -m pytest tests/ -v
```
