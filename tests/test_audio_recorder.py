"""
Unit tests for audio_recorder.py

Run with: python -m pytest tests/ -v
  or:     python -m unittest tests/test_audio_recorder.py -v
"""

import os
import sys
import tempfile
import unittest

import numpy as np

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_recorder as ar


class TestNextFilename(unittest.TestCase):
    """BUG-01: next_filename must never collide with an existing file after cleanup."""

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _touch(self, name):
        path = os.path.join(self.td, name)
        open(path, 'wb').close()
        return path

    def test_first_file(self):
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0001.wav')

    def test_sequential(self):
        self._touch('rec_0001.wav')
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0002.wav')

    def test_monotonic_after_gap(self):
        # _0001 deleted, _0002 and _0003 exist → next should be _0004, not _0002
        self._touch('rec_0002.wav')
        self._touch('rec_0003.wav')
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0004.wav')

    def test_no_overwrite_after_cleanup(self):
        # Simulate: max=4, files _0001–_0004 exist, _0001 was deleted
        self._touch('rec_0002.wav')
        self._touch('rec_0003.wav')
        self._touch('rec_0004.wav')
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0005.wav')
        # Critical: _0004 must NOT be returned (was the old broken behaviour)
        self.assertNotEqual(os.path.basename(path), 'rec_0004.wav')

    def test_different_formats(self):
        self._touch('sess_0001.mp3')
        self._touch('sess_0002.mp3')
        path = ar.next_filename('sess', 'mp3', self.td)
        self.assertEqual(os.path.basename(path), 'sess_0003.mp3')

    def test_ignores_wrong_extension(self):
        self._touch('rec_0001.wav')
        self._touch('rec_0001.mp3')   # different format, should not affect wav counter
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0002.wav')

    def test_ignores_non_indexed_files(self):
        self._touch('rec_extra.wav')  # no _NNNN suffix
        path = ar.next_filename('rec', 'wav', self.td)
        self.assertEqual(os.path.basename(path), 'rec_0001.wav')

    def test_parse_index_valid(self):
        self._touch('rec_0042.wav')
        self.assertEqual(ar._parse_index(os.path.join(self.td, 'rec_0042.wav'), 'rec', 'wav'), 42)

    def test_parse_index_invalid(self):
        self.assertIsNone(ar._parse_index('other_0001.wav', 'rec', 'wav'))

    def test_parse_index_non_numeric(self):
        self.assertIsNone(ar._parse_index('rec_abc.wav', 'rec', 'wav'))


class TestCleanupOldest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _touch(self, name):
        path = os.path.join(self.td, name)
        open(path, 'wb').close()
        return path

    def test_removes_oldest_when_at_limit(self):
        for i in range(1, 5):
            self._touch(f'rec_{i:04d}.wav')
        ar.cleanup_oldest('rec', 'wav', self.td, 4)
        remaining = sorted(os.listdir(self.td))
        self.assertNotIn('rec_0001.wav', remaining)
        self.assertIn('rec_0004.wav', remaining)
        self.assertEqual(len(remaining), 3)

    def test_no_removal_below_limit(self):
        for i in range(1, 4):
            self._touch(f'rec_{i:04d}.wav')
        ar.cleanup_oldest('rec', 'wav', self.td, 4)
        self.assertEqual(len(os.listdir(self.td)), 3)

    def test_removes_multiple_when_over_limit(self):
        for i in range(1, 7):
            self._touch(f'rec_{i:04d}.wav')
        ar.cleanup_oldest('rec', 'wav', self.td, 4)
        remaining = os.listdir(self.td)
        self.assertEqual(len(remaining), 3)


class TestFmtSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(ar.fmt_size(512), '512 B')

    def test_kilobytes(self):
        self.assertIn('KB', ar.fmt_size(2048))

    def test_megabytes(self):
        self.assertIn('MB', ar.fmt_size(5 * 1024 * 1024))

    def test_gigabytes(self):
        self.assertIn('GB', ar.fmt_size(2 * 1024 ** 3))

    def test_zero(self):
        self.assertEqual(ar.fmt_size(0), '0 B')


class TestFmtTime(unittest.TestCase):
    def test_seconds_only(self):
        self.assertEqual(ar.fmt_time(45), '00:00:45')

    def test_minutes_and_seconds(self):
        self.assertEqual(ar.fmt_time(90), '00:01:30')

    def test_hours(self):
        self.assertEqual(ar.fmt_time(3661), '01:01:01')

    def test_zero(self):
        self.assertEqual(ar.fmt_time(0), '00:00:00')


class TestPbar(unittest.TestCase):
    def test_empty(self):
        result = ar.pbar(0.0)
        self.assertTrue(all(c == '-' for c in result))

    def test_full(self):
        result = ar.pbar(1.0)
        self.assertTrue(all(c == '#' for c in result))

    def test_half(self):
        result = ar.pbar(0.5, width=10)
        self.assertEqual(result, '#####-----')

    def test_clamps_above_one(self):
        result = ar.pbar(2.0, width=10)
        self.assertEqual(result, '##########')

    def test_clamps_below_zero(self):
        result = ar.pbar(-1.0, width=10)
        self.assertEqual(result, '----------')

    def test_width_respected(self):
        for w in (10, 20, 30):
            self.assertEqual(len(ar.pbar(0.5, width=w)), w)


class TestSparkline(unittest.TestCase):
    def test_empty_returns_spaces(self):
        result = ar.sparkline([], width=10)
        self.assertEqual(result, ' ' * 10)

    def test_uniform_values_returns_first_char(self):
        result = ar.sparkline([-30, -30, -30], width=5)
        # All equal → all map to index 0 ('.')
        self.assertTrue(all(c == '.' for c in result))

    def test_length_matches_width(self):
        levels = list(range(-60, 0, 3))
        result = ar.sparkline(levels, width=15)
        self.assertEqual(len(result), 15)


class TestRmsDb(unittest.TestCase):
    def test_silence_returns_low_db(self):
        data = np.zeros(1024, dtype=np.int16)
        self.assertAlmostEqual(ar.rms_db(data, 16), -100.0)

    def test_full_scale_sine_near_zero_db(self):
        t = np.linspace(0, 2 * np.pi, 1024, endpoint=False)
        data = (np.sin(t) * 32767).astype(np.int16)
        db = ar.rms_db(data, 16)
        # Full-scale sine RMS ≈ -3 dB
        self.assertAlmostEqual(db, -3.0, delta=0.5)

    def test_empty_returns_low_db(self):
        data = np.array([], dtype=np.int16)
        self.assertEqual(ar.rms_db(data, 16), -100.0)


class TestApplyGain(unittest.TestCase):
    def test_unity_gain_no_change(self):
        data = np.array([100, 200, 300], dtype=np.int16)
        result = ar.apply_gain(data, 1.0)
        np.testing.assert_array_equal(result, data)

    def test_gain_doubles_values(self):
        data = np.array([100, 200], dtype=np.int16)
        result = ar.apply_gain(data, 2.0)
        np.testing.assert_array_equal(result, np.array([200, 400], dtype=np.int16))

    def test_gain_clips_at_max(self):
        data = np.array([20000], dtype=np.int16)
        result = ar.apply_gain(data, 3.0)
        self.assertEqual(result[0], np.iinfo(np.int16).max)

    def test_gain_clips_at_min(self):
        data = np.array([-20000], dtype=np.int16)
        result = ar.apply_gain(data, 3.0)
        self.assertEqual(result[0], np.iinfo(np.int16).min)

    def test_output_dtype_preserved(self):
        data = np.array([100, 200], dtype=np.int32)
        result = ar.apply_gain(data, 2.0)
        self.assertEqual(result.dtype, np.int32)


class TestPackInt24(unittest.TestCase):
    def test_output_length(self):
        data = np.array([0, 1, 2, 3], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(len(result), 12)  # 4 samples × 3 bytes

    def test_zero_encodes_to_three_zero_bytes(self):
        data = np.array([0], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(result, b'\x00\x00\x00')

    def test_value_1_encodes_correctly(self):
        data = np.array([1], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(result, b'\x01\x00\x00')

    def test_value_256_encodes_correctly(self):
        data = np.array([256], dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(result, b'\x00\x01\x00')

    def test_roundtrip_bytes_count(self):
        n = 100
        data = np.arange(n, dtype=np.int32)
        result = ar.pack_int24(data)
        self.assertEqual(len(result), n * 3)


class TestGetAudioDtype(unittest.TestCase):
    def test_8bit(self):
        self.assertEqual(ar.get_audio_dtype(8), np.int8)

    def test_16bit(self):
        self.assertEqual(ar.get_audio_dtype(16), np.int16)

    def test_24bit(self):
        self.assertEqual(ar.get_audio_dtype(24), np.int32)

    def test_32bit(self):
        self.assertEqual(ar.get_audio_dtype(32), np.int32)


class TestGetFfmpegFmt(unittest.TestCase):
    def test_8bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(8), 's8')

    def test_16bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(16), 's16le')

    def test_24bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(24), 's24le')

    def test_32bit(self):
        self.assertEqual(ar.get_ffmpeg_fmt(32), 's32le')


class TestBuildFfmpegCmd(unittest.TestCase):
    def _cmd(self, fmt):
        return ar.build_ffmpeg_cmd(fmt, '/out.' + fmt, 320, 48000, 2, 16)

    def test_mp3_uses_libmp3lame(self):
        cmd = self._cmd('mp3')
        self.assertIn('libmp3lame', cmd)

    def test_flac_uses_flac_codec(self):
        cmd = self._cmd('flac')
        self.assertIn('flac', cmd)

    def test_ogg_uses_libvorbis(self):
        cmd = self._cmd('ogg')
        self.assertIn('libvorbis', cmd)

    def test_unsupported_raises(self):
        with self.assertRaises(ValueError):
            ar.build_ffmpeg_cmd('avi', '/out.avi', 320, 48000, 2, 16)

    def test_cmd_starts_with_ffmpeg(self):
        self.assertEqual(self._cmd('mp3')[0], 'ffmpeg')

    def test_sample_rate_included(self):
        cmd = ar.build_ffmpeg_cmd('mp3', '/out.mp3', 128, 44100, 1, 16)
        self.assertIn('44100', cmd)


class TestGetFileSize(unittest.TestCase):
    def test_missing_file_returns_zero(self):
        self.assertEqual(ar._get_file_size('/nonexistent/path/file.wav'), 0)

    def test_existing_file_returns_size(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'hello')
            path = f.name
        try:
            self.assertEqual(ar._get_file_size(path), 5)
        finally:
            os.unlink(path)


class TestUIStateGainReset(unittest.TestCase):
    """BUG-12: '0' key should reset gain to unity."""

    def test_gain_starts_at_unity(self):
        s = ar.UIState()
        self.assertEqual(s.gain, 1.0)

    def test_gain_db_at_unity(self):
        s = ar.UIState()
        self.assertAlmostEqual(s.gain_db(), 0.0, places=3)

    def test_files_created_starts_empty(self):
        s = ar.UIState()
        self.assertEqual(s.files_created, [])

    def test_error_msg_starts_none(self):
        s = ar.UIState()
        self.assertIsNone(s.error_msg)

    def test_auto_gain_starts_off(self):
        s = ar.UIState()
        self.assertEqual(s.auto_gain, 0)


class TestWavHeaderConstant(unittest.TestCase):
    def test_wav_header_size(self):
        """WAV_HEADER_BYTES should equal 44 (standard PCM WAV header)."""
        self.assertEqual(ar.WAV_HEADER_BYTES, 44)


if __name__ == '__main__':
    unittest.main(verbosity=2)
