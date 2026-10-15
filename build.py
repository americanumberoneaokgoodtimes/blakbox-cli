#!/usr/bin/env python3
"""
Cross-platform build script for blakbox-cli.
Uses PyInstaller to create standalone executables.

Run on each target platform:
  Windows: python build.py
  Linux:   python build.py
  macOS:   python build.py

For GitHub Actions cross-platform builds, see .github/workflows/build.yml
"""

import os
import sys
import platform
import subprocess
import shutil
from pathlib import Path


def get_platform_tag():
    """Return a tag like 'win-x64', 'linux-x64', 'linux-arm64', 'macos-x64', 'macos-arm64'."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == 'windows':
        arch = 'x64' if machine in ('amd64', 'x86_64') else 'x86'
        return f'win-{arch}'
    elif system == 'linux':
        if machine in ('x86_64', 'amd64'):
            return 'linux-x64'
        elif machine in ('aarch64', 'arm64'):
            return 'linux-arm64'
        elif machine in ('i386', 'i686'):
            return 'linux-x86'
        else:
            return f'linux-{machine}'
    elif system == 'darwin':
        arch = 'arm64' if machine == 'arm64' else 'x64'
        return f'macos-{arch}'
    else:
        return f'{system}-{machine}'


def build():
    tag = get_platform_tag()
    print(f'Building for {tag}...')

    # Clean previous builds
    for d in ('build', 'dist'):
        if os.path.exists(d):
            shutil.rmtree(d)

    # PyInstaller command
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--name', 'blakbox',
        '--clean',
        'audio_recorder.py'
    ]

    # Run PyInstaller
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print('Build failed!')
        print(result.stdout)
        print(result.stderr)
        return False

    # Rename output with platform tag
    exe_name = 'blakbox.exe' if platform.system() == 'Windows' else 'blakbox'
    src = Path('dist') / exe_name
    if src.exists():
        dst = Path('dist') / f'blakbox-{tag}{src.suffix}'
        shutil.move(str(src), str(dst))
        print(f'Created: {dst}')
        print(f'Size: {dst.stat().st_size / 1024 / 1024:.1f} MB')
    else:
        print(f'Error: {src} not found')
        return False

    return True


if __name__ == '__main__':
    ok = build()
    sys.exit(0 if ok else 1)