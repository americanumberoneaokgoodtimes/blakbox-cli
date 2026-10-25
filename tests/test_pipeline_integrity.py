"""
End-to-end integrity tests for the file pipeline.

A fake input stream feeds a deterministic ramp through the real recording engine
while [N]ew-file and device-switch events fire. Lossless formats (WAV, FLAC) must
reproduce the fed samples exactly across file boundaries; lossy formats (MP3, OGG)
must preserve total duration. Compressed formats are skipped without ffmpeg.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_recorder as ar

HAVE_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None
SR = 8000
BS = int(SR * ar.CHUNK_DURATION)


class Feed:
    """Shared sample counter so every chunk across streams is globally ordered."""

    def __init__(self, bd, ch):
        self.bd, self.ch, self.n = bd, ch, 0
        self.fed = []

    def take(self, frames):
        idx = np.arange(self.n, self.n + frames)
        self.n += frames
        base = ((idx % 200) - 100).astype(np.int64)
        if self.bd in (24, 32):
            base = base * 1000
        arr = np.repeat(base[:, None], self.ch, axis=1).astype(ar.get_audio_dtype(self.bd))
        self.fed.append(arr)
        return arr


class FakeStream:
    def __init__(self, feed, extra_on_drain=0):
        self.feed, self.extra = feed, extra_on_drain
        self.paused = False

    def start(self): pass
    def stop(self): pass
    def close(self): pass
    def pause(self): self.paused = True
    def resume(self): self.paused = False

    def read(self, blocksize):
        return self.feed.take(blocksize), False

    def drain(self):
        if self.extra:
            n, self.extra = self.extra, 0
            return [self.feed.take(n)]  # audio still queued when the device is swapped
        return []


def run_session(td, fmt, bd, ch, events):
    """events: {iteration: 'new' | 'device'}; the session ends after max(events)+2 iterations."""
    feed = Feed(bd, ch)

    class Args:
        output_dir = td
        output = 'it'
        size = 10 ** 9
        max_recordings = 50
        bit_depth = bd
        bit_rate = 128
        sample_rate = SR
        channels = ch
        length = 0
        file_format = fmt
        device = 0

    stream_a = FakeStream(feed, extra_on_drain=1000)
    stream_b = FakeStream(feed)
    made = []

    def factory(*a, **k):
        made.append(1)
        return stream_a if len(made) == 1 else stream_b

    calls = {'n': 0}
    last = max(events) + 2

    def key(self):
        calls['n'] += 1
        ev = events.get(calls['n'])
        if ev == 'new':
            self.new_file = True
        elif ev == 'device':
            self.select_device = True
        if calls['n'] >= last:
            self.running = False

    info = {'name': 'Mic2', 'max_input_channels': ch, 'default_samplerate': SR}
    summary = MagicMock()
    with patch('audio_recorder.draw_startup'), \
         patch('audio_recorder.AudioInputStream', side_effect=factory), \
         patch('audio_recorder.draw_hud'), \
         patch('audio_recorder.UIState.read_key', key), \
         patch('audio_recorder.prompt_select_device', return_value=1), \
         patch('sounddevice.query_devices', return_value=info), \
         patch('sounddevice.check_input_settings'), \
         patch('audio_recorder.set_raw_terminal'), \
         patch('audio_recorder.restore_terminal'), \
         patch('audio_recorder._print_session_summary', summary):
        ar.record_engine(Args(), ar.WavSink() if fmt == 'wav' else ar.CompressedSink(fmt, 128))
    files = sorted(os.path.join(td, f) for f in os.listdir(td))
    return feed, files, summary.call_args[0][0]


def read_wav(path, bd, ch):
    with wave.open(path, 'rb') as wf:
        assert wf.getsampwidth() == bd // 8 and wf.getnchannels() == ch and wf.getframerate() == SR
        raw = wf.readframes(wf.getnframes())
    if bd == 8:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.int64) - 128).reshape(-1, ch)
    if bd == 16:
        return np.frombuffer(raw, dtype='<i2').astype(np.int64).reshape(-1, ch)
    if bd == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int64)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        return v.reshape(-1, ch)
    return np.frombuffer(raw, dtype='<i4').astype(np.int64).reshape(-1, ch)


def decode_flac(path, bd, ch):
    fmt = 's16le' if bd == 16 else 's32le'
    out = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', fmt, '-'],
                         capture_output=True, check=True).stdout
    arr = np.frombuffer(out, dtype='<i2' if bd == 16 else '<i4').astype(np.int64)
    if bd == 24:
        arr = arr >> 8
    return arr.reshape(-1, ch)


def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=nk=1:nw=1', path], capture_output=True, check=True)
    return float(out.stdout.decode().strip())


EVENTS = {2: 'new', 4: 'device', 6: 'new'}


class TestLosslessPipelines(unittest.TestCase):
    def _check(self, fmt, bd, ch, reader):
        td = tempfile.mkdtemp()
        try:
            feed, files, st = run_session(td, fmt, bd, ch, EVENTS)
            self.assertEqual(len(files), 4, files)  # initial + N + device + N
            got = np.concatenate([reader(f, bd, ch) for f in files])
            want = np.concatenate(feed.fed).astype(np.int64)
            self.assertEqual(got.shape, want.shape, 'sample count differs: audio lost or duplicated')
            np.testing.assert_array_equal(got, want)
            self.assertFalse(st.fatal_msg)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_wav_all_bit_depths(self):
        for bd in (8, 16, 24, 32):
            with self.subTest(bd=bd):
                self._check('wav', bd, 1, read_wav)

    def test_wav_stereo(self):
        self._check('wav', 16, 2, read_wav)

    @unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg not available')
    def test_flac(self):
        for bd in (16, 24):
            with self.subTest(bd=bd):
                self._check('flac', bd, 1, decode_flac)


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg not available')
class TestLossyPipelines(unittest.TestCase):
    def test_duration_preserved_across_rotations(self):
        for fmt in ('mp3', 'ogg'):
            with self.subTest(fmt=fmt):
                td = tempfile.mkdtemp()
                try:
                    feed, files, st = run_session(td, fmt, 16, 1, EVENTS)
                    self.assertEqual(len(files), 4, files)
                    total = sum(duration(f) for f in files)
                    expected = sum(len(a) for a in feed.fed) / SR
                    # Encoder delay/padding is a few tens of ms per file
                    self.assertAlmostEqual(total, expected, delta=0.15 * len(files))
                    self.assertFalse(st.fatal_msg)
                finally:
                    shutil.rmtree(td, ignore_errors=True)


class TestPauseAndQuitFlush(unittest.TestCase):
    def test_audio_left_in_queue_is_written_on_quit(self):
        td = tempfile.mkdtemp()
        try:
            feed = Feed(16, 1)

            class Args:
                output_dir = td
                output = 'q'
                size = 10 ** 9
                max_recordings = 5
                bit_depth = 16
                bit_rate = 128
                sample_rate = SR
                channels = 1
                length = 0
                file_format = 'wav'
                device = 0

            stream = FakeStream(feed, extra_on_drain=700)
            n = {'c': 0}

            def key(self):
                n['c'] += 1
                if n['c'] >= 2:
                    self.running = False

            with patch('audio_recorder.draw_startup'), \
                 patch('audio_recorder.AudioInputStream', return_value=stream), \
                 patch('audio_recorder.draw_hud'), \
                 patch('audio_recorder.UIState.read_key', key), \
                 patch('audio_recorder.set_raw_terminal'), \
                 patch('audio_recorder.restore_terminal'), \
                 patch('audio_recorder._print_session_summary'):
                ar.record_engine(Args(), ar.WavSink())
            got = read_wav(os.path.join(td, 'q_0001.wav'), 16, 1)
            self.assertEqual(len(got), sum(len(a) for a in feed.fed))
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
