> **Superseded.** This 2026-10-14 audit is historical (v1.2.x). FEAT-01 (config file) and FEAT-03 (pre-flight device test) have since been implemented, and several "fixed" items were later found incomplete (e.g. Unix key input). Current status: see README and the later audit remediation.

# blakebox-v1.2 — Comprehensive Audit Report
**Date:** 2026-10-14  
**Auditor:** claude-sonnet-4-6  
**Branch:** main | **Commit:** e40ebde  
**Implementation session:** 2026-10-14 (claude-sonnet-4-6)

---

## Project Context (for agent handoff)

**blakbox-cli** is a single-file Python terminal audio recorder designed for professional field use. It streams microphone input to rotating WAV/MP3/FLAC/OGG files with a full-screen ANSI HUD showing level meters, disk space, gain control, and file progress.

**Architecture:**
- `audio_recorder.py` — sole main module. No GUI. No config files.
- Dependencies: `numpy`, `sounddevice`, external `ffmpeg` (for compressed formats).
- Windows-primary (uses `msvcrt` for keyboard). Unix/macOS supported via `tty`/`termios`/`select`.
- Two recording paths: `record_wav()` (native) and `record_compressed()` (pipes PCM to ffmpeg stdin in real-time).
- `UIState` class holds all mutable runtime state (gain, levels, flags).
- `draw_hud()` redraws the entire screen each audio chunk (~every 500ms).
- `tests/test_audio_recorder.py` — 66 unit tests, all passing.

**Key design patterns:**
- File rotation: `cleanup_oldest()` deletes oldest files when at limit; `next_filename()` generates the next path using monotonic index.
- Audio chunks: 500ms blocks (`CHUNK_DURATION = 0.5`), processed synchronously in the main loop.
- Gain: manual +/- 0.5x steps (0.1–3.0 range); `0` key resets to unity; 3-state auto-gain (off/normal/fast).
- ffmpeg: launched as a subprocess with stdin pipe; `-y` flag overwrites output files.

---

## Bug Tracker

### CRITICAL — Data Loss / Corruption

- [x] **[BUG-01] File overwrite on rotation — data loss** ✅ FIXED
  - **Fix applied:** `next_filename()` now finds `max(existing_index) + 1` using `_parse_index()` helper. After cleanup of `_0001`, returns `_0005` (not `_0004`). 10 unit tests confirm correctness.

- [x] **[BUG-02] 24-bit WAV sample width** ✅ VERIFIED OK
  - `wf.setsampwidth(3)` + `pack_int24()` is correct. `write_wav_frames()` routes to `pack_int24` for 24-bit.

---

### HIGH — Incorrect Behavior

- [x] **[BUG-03] `total_bytes` in compressed mode tracked PCM input, not output file size** ✅ FIXED
  - **Fix:** `record_compressed()` now polls `_get_file_size(cur_file)` (actual OS file size) for the size-limit check, HUD display, and `total_bytes` accumulation. `total_override=st.total_bytes + cur_actual` passed to `draw_hud()`.

- [x] **[BUG-04] Clipping counter not reset on automatic file rotation** ✅ FIXED
  - **Fix:** `st.clipping = 0` added to the auto-rotation block in both `record_wav()` and `record_compressed()`.

- [x] **[BUG-05] Unix keyboard input non-functional (required Enter key)** ✅ FIXED
  - **Fix:** Added `set_raw_terminal()` / `restore_terminal()` using `tty.setcbreak()` + `termios`. Called at start of both recording functions; restored in outer `finally`. `_readline_echo` temporarily restores cooked mode for rename input. `draw_startup` uses `sys.stdin.read(1)` on Unix (works in cbreak).

- [x] **[BUG-06] Double-start of ffmpeg on simultaneous N-key + auto-rotation** ✅ FIXED
  - **Fix:** Auto-rotation block now starts ffmpeg once then `continue`s to the top of the loop. The `new_file` handler at the bottom is only reached on the next iteration when `new_file` was pending from a prior key press — always a distinct file.

- [x] **[BUG-07] Timer (`t0`) reset inconsistently** ✅ FIXED
  - **Fix:** `t0` is set once at session start and never reset. All `t0 = time.time()` resets in N-key and rename handlers removed. Elapsed now shows total session time consistently.

- [x] **[BUG-08] No ffmpeg availability check at startup** ✅ FIXED
  - **Fix:** `check_ffmpeg()` uses `shutil.which('ffmpeg')` and exits with a clear error message before the startup screen is shown. Called at the top of `record_compressed()`.

---

### MEDIUM — Reliability / UX Degradation

- [x] **[BUG-09] Silent ffmpeg crash mid-recording** ✅ FIXED
  - **Fix:** `BrokenPipeError` handler sets `st.error_msg` (displayed in HUD in red). ffmpeg stderr is now captured (not `DEVNULL`) and included in restart failure messages.

- [x] **[BUG-10] FLAC size estimate used hardcoded 0.55 ratio** ✅ FIXED
  - **Fix:** Resolved by BUG-03/FEAT-02 fix — all compressed formats now use `os.path.getsize()` for actual file size rather than any estimate.

- [x] **[BUG-11] `total_bytes` over-counts on rename in compressed mode** ✅ FIXED
  - **Fix:** Resolved by BUG-03 fix — `_get_file_size(cur_file)` used throughout.

- [x] **[BUG-12] No keyboard shortcut to reset gain to 1.0** ✅ FIXED
  - **Fix:** `0` key added in both Windows (`msvcrt`) and Unix (`select`) key handlers. HUD controls line updated to show `[0]Reset`.

- [x] **[BUG-13] Output file path not shown after recording ends** ✅ FIXED
  - **Fix:** `_print_session_summary()` prints a formatted table of all files created with their sizes. Removed files (deleted by rotation) are tagged `(removed by rotation)`.

---

### LOW — Code Quality / Minor Issues

- [x] **[BUG-14] `blakbox1.0.py` untracked legacy file** ✅ FIXED
  - **Fix:** Added `blakbox1.0.py` to `.gitignore`.

- [x] **[BUG-15] Transcription/translation `.txt` files not gitignored** ✅ FIXED
  - **Fix:** Added glob patterns `* (transcribed on *.txt` and `* (translated on *.txt` to `.gitignore`.

- [x] **[BUG-16] `_readline_echo` redundant `import sys`** ✅ FIXED
  - **Fix:** Removed the `import sys` inside `_readline_echo`. `sys` is already imported at module level.

- [x] **[BUG-17] No `--version` flag** ✅ FIXED
  - **Fix:** `VERSION = '1.2.1'` constant added. `parser.add_argument('--version', action='version', version=f'blakebox-cli {VERSION}')` added to `parse_args()`.

- [x] **[BUG-18] Default `--output-dir` is current working directory** ✅ PARTIALLY ADDRESSED
  - **Fix:** Startup screen now prominently shows `Output dir` and `Base name` fields so users know where files are going. Directory is normalized to absolute path via `os.path.abspath()`.

- [x] **[BUG-19] Boss Mode controls not clearly documented** ✅ FIXED
  - **Fix:** HUD now shows two keyboard hint lines: first for main controls, second for `[B]lank(boss)  [b]Unblank  Ctrl+C Stop`. Boss mode unblank screen also shows `[b] unblank` hint.

- [x] **[BUG-20] Disk space bar showed free space (filled = good), no warning color** ✅ FIXED
  - **Fix:** Disk bar now shows **used** space (fills as disk fills). Color-coded: blue < 70% used, yellow 70–90%, red > 90% (i.e., free < 30% / < 10%).

- [x] **[BUG-21] Size progress bar normalization bug** ✅ FIXED
  - **Fix (also found during impl):** Original `pbar(p)` where `p` was 0–100 always showed full bar. Fixed to `pbar(size_frac)` where `size_frac` is 0.0–1.0. `pbar()` docstring updated to clarify it expects a fraction.
  - WAV header offset: `cur_file_size` initialised to `WAV_HEADER_BYTES = 44`.

---

### ARCHITECTURE / MISSING FEATURES

- [ ] **[FEAT-01] No configuration persistence**
  - Users must specify all CLI flags on every invocation. No `~/.blakboxrc` or `blakebox.toml`.
  - **Status:** Not yet implemented.

- [x] **[FEAT-02] No real-time file size polling for compressed formats** ✅ FIXED
  - **Fix:** Resolved with BUG-03. `_get_file_size()` polls `os.path.getsize()` each chunk.

- [ ] **[FEAT-03] No pre-flight audio device test on startup screen**
  - The startup screen doesn't verify the device is accessible before prompting to start.
  - **Status:** Not yet implemented.

- [ ] **[FEAT-04] No multi-device / monitor mix documentation**
  - **Status:** README could note Windows Stereo Mix / WASAPI loopback as `--device` argument.

- [x] **[FEAT-05] No post-recording summary** ✅ FIXED
  - **Fix:** `_print_session_summary()` prints all created files with sizes.

---

## Additional Fix Found During Implementation

- [x] **[BUG-22] Size progress bar always showed full (pbar normalization)** ✅ FIXED
  - `pbar()` expects 0.0–1.0 fraction. Original code passed `p` (0–100) making the bar always appear full for any file > 1% full. Fixed throughout `draw_hud()`.

---

## File Inventory (current state)

| File | Status | Notes |
|------|--------|-------|
| `audio_recorder.py` | Active — updated | All bug fixes applied, v1.2.1 |
| `blakbox1.0.py` | Gitignored (legacy) | Still on disk; gitignore prevents commit |
| `requirements.txt` | OK | `numpy>=1.20`, `sounddevice>=0.4` |
| `README.md` | Needs update | Keyboard table missing `[0]`, Boss Mode clarity, `--version` |
| `.gitignore` | Updated | Audio artifacts + `.txt` transcripts + legacy file |
| `tests/test_audio_recorder.py` | New | 66 unit tests, all passing |

---

## Remaining Open Items

1. **[FEAT-01]** Config file persistence (`~/.blakboxrc` or `blakebox.toml`)
2. **[FEAT-03]** Pre-flight device test on startup screen
3. **[FEAT-04]** README note on WASAPI loopback / Stereo Mix for system audio capture
4. **README update** — add `[0]Reset` to keyboard table, clarify Boss Mode `Shift+B` / `b`, add `--version` mention

---

## Test Results

```
66 passed in 4.53s
```
All tests passing on Python 3.10 / Windows 11.
