# blakbox-cli

Interactive terminal audio recorder with file rotation. Streams microphone input to rotating WAV, MP3, FLAC, or OGG files with a live HUD showing levels, disk space, and gain control.

## Setup

```bash
pip install -r requirements.txt
```

Requires **ffmpeg** in PATH for MP3/FLAC/OGG output.

## Usage

```bash
# Basic usage — starts interactive HUD
python audio_recorder.py

# Custom output name
python audio_recorder.py -o my_recording

# MP3, 192kbps, 2GB files, keep 5, 10min limit
python audio_recorder.py -f mp3 -b 192 -s 2000000000 -m 5 -l 600

# FLAC, 44100Hz, mono
python audio_recorder.py -f flac -r 44100 -c 1

# OGG, 24-bit, 1GB size limit
python audio_recorder.py -f ogg -d 24 -s 1000000000

# Specify audio input device
python audio_recorder.py --device "Microphone (Realtek)"
```

## Defaults

| Flag | Default | Description |
|------|---------|-------------|
| `-o` | `{datetime}.{format}` | Output filename base |
| `-f` | `wav` | Format: wav, mp3, flac, ogg |
| `-s` | `4000000000` | Max file size per file |
| `-m` | `2` | Max files to keep |
| `-b` | `128` | Bit rate (kbps, compressed formats) |
| `-d` | `16` | Bit depth: 8, 16, 24, 32 |
| `-r` | `44100` | Sample rate (Hz) |
| `-c` | `2` | Audio channels |
| `-l` | `999999` | Max duration (seconds, 0=unlimited) |
| `--output-dir` | `.` | Output directory |
| `--device` | `None` | Audio input device name or index |

## Interactive HUD

Live display during recording:
- **Size progress bar** — current file size vs limit
- **Disk space bar** — free space on drive
- **Microphone level meter** — real-time RMS + waveform sparkline
- **Peak level** — peak indicator with color-coded zones
- **Gain control** — dB and linear multiplier, auto-gain toggle
- **Clipping counter** — number of clipped samples detected
- **Timer** — elapsed time and total recorded data

## Keyboard Controls

| Key | Action |
|-----|--------|
| `Q` | Quit recording |
| `N` | Start new file (rotate now) |
| `+` | Increase gain (+0.5 dB) |
| `-` | Decrease gain (-0.5 dB) |
| `A` | Toggle auto-gain |
| `B` | Boss Mode — blank screen, show only % |
| `b` | Unblank — return to dashboard |
| `Ctrl+C` | Stop recording |