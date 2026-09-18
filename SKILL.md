---
name: ace-studio-loudness
description: >
  Draw Synthesizer V-style audio-bar width (fatter = louder) for ACE Studio
  tracks so you can see loudness over time. Includes a floating overlay that
  docks to the ACE window and follows the playhead. Use when the user wants
  ACE waveform bars, 音频条宽度, 响度条, 悬浮窗, loudness envelope, LUFS
  over a vocal, or runs /ace-studio-loudness.
---

# ACE Studio loudness bars

ACE Studio's piano roll has no SynthV-style engine-output waveform overlay, and
there is no SDK to inject one. This skill bounces (or reuses) a track's audio
and draws the same visual: **bar width = amplitude**, plus RMS dB and LUFS.

Do not add a VST to the project unless the user asks. For live meters they
already have **iZotope Insight 2** and **SPAN** in the VST3 folder.

## Floating overlay (default)

This is the thing that matches “做成悬浮窗 / 自动识别位置”. It cannot paint
inside ACE's piano roll (no overlay SDK). It is a topmost HUD that:

- starts near the ACE window, then you **drag the title bar** to place it
  (right-drag resizes height). **钉住 ACE** is off unless you turn it on
- **当前** / **参考** dropdowns pick which vocal to compare. First open
  pre-selects the piano-roll vocal and `歌声_本家` / `和声_本家` / a track
  named `参考`; after that the menus are the source of truth
- same time axis and amplitude scale — fatter bar = louder
- current Sing track uses **OpenUtau-style phrases**: notes with a gap > 0.45s
  become a new phrase; each phrase is hashed and cached as
  `ph_<track>_<hash>.wav`. Unchanged phrases reload from disk. Missing phrases
  bounce `--from/--to` one at a time and **appear as soon as they finish**
  (blank where not ready). Waveform is per-pixel min/max like
  `WaveformImage.cs`. After ACE `synthesis-status` goes idle, only changed
  phrases re-export.
- click the bar → `transport seek`; mouse wheel zooms

Launch (kills any previous HUD first):

```powershell
D:\Anaconda3\pythonw.exe "$HOME\.grok\skills\ace-studio-loudness\scripts\loudness_overlay.py"
```

If `pythonw` is missing, use `D:\Anaconda3\python.exe` the same way. ACE Studio
must already be open. The HUD reuses `<project>\loudness\vocal_<track-uuid>*.wav`
for that one track; **刷新** bounces the current vocal dry (`--without-effects true`).
If the piano roll is on a non-vocal track, the bar stays empty until a vocal is opened.
Header `Δ` is current LUFS minus reference (negative = quieter than 本家).

Small HUD (default ~540×150, pins to ACE bottom-right if **钉** is on).
Buttons: **钉** pin, **跟** smooth follow, **页** auto-page (jump when playhead leaves the window), **穿** click-through waveform only, **刷** re-bounce, **×**.
**Shift+← / Shift+→** pans the bar (also Shift-drag on the waveform). Wheel zooms. Drag **⋮⋮** or the chrome to move; right-drag resizes height.
**穿透** only the waveform — title bar and **×** stay clickable. Stuck window: **Ctrl+Alt+X** closes, **Ctrl+Alt+P** toggles 穿透.

Do not screenshot the HUD as a substitute for launching it.

## Static PNG (only if they want a file, not the window)

## When the user names a track

1. Confirm ACE is open:

```powershell
& "C:\Program Files\ACE Studio\acestudio-cli.exe" --json project info
```

2. Resolve the track from `track list`. Prefer the name they used (`main`, a
   singer, `歌声_本家`). If they say "current vocal" and several Sing tracks
   exist, pick the one whose name matches, else ask once.

3. Get audio without bouncing when possible. `clip list --track-uuid` on an
   **Audio** track often has `audioMedia.sourcePath` already on disk — plot
   that file directly.

4. **Sing / instrument tracks must bounce:**

```powershell
& "C:\Program Files\ACE Studio\acestudio-cli.exe" --json export audio --wait --scope tracks --track-uuid "{UUID}" --without-effects true --path "<out-dir>\<name>.wav"
```

`--without-effects true` is the default for "how loud is the performance".
Pass `--without-effects false` (or omit the flag) only when they want mix-bus
FX in the picture. `--from` / `--to` if they named a section.

`--wait` blocks until the wav exists. Do not plot until the file is on disk
and non-empty. Per-track export treats `--path` as a template and **appends
the track name** (`main.wav` → `main_main.wav`). Plot that file.

5. Plot (Anaconda Python; ffmpeg fallback for ogg/mp3):

```powershell
D:\Anaconda3\python.exe "$HOME\.grok\skills\ace-studio-loudness\scripts\plot_loudness.py" "<audio>" -o "<audio>_loudness.png" --html "<audio>_loudness.html" --json "<audio>_loudness.json" --title "<track name>"
```

Put bounces and plots in `<project>\loudness\` when a clip `sourcePath` reveals
the project folder; otherwise next to the wav.

6. Show the PNG. Report the JSON numbers: peak dBFS, RMS dBFS, integrated LUFS,
   momentary max, short-term max. Point at the HTML for pan/zoom.

## How to read the picture

- **White fill** — peak envelope. This is the SynthV audio bar. Fatter = louder.
- **Green fill** — RMS (closer to perceived loudness than peak).
- **Bottom lane** — RMS in dBFS, with −6 / −12 / −18 guides.

Dynamic / Energy in ACE's parameter panel is *performance intent*, not measured
loudness. This plot is the rendered sound.

## Guardrails

- Do not change notes, faders, or FX.
- Do not export `master` unless they asked for the full mix.
- Reuse an existing bounce in `loudness\` if it is newer than the last edit
  they mentioned; otherwise bounce again.
