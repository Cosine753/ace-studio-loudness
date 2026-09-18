# ACE Studio loudness HUD

Small always-on-top window for ACE Studio that shows **SynthV-style audio-bar width** (fatter = louder) for **one vocal track**, optionally compared to a pasted **参考** vocal.

Inspired by:

- **Synthesizer V** — engine-output waveform on the piano roll, refresh after synthesis
- **OpenUtau** — per-phrase / per-track wav cache (`iso_<track>_<fingerprint>.wav`), not the mix

ACE has no piano-roll overlay SDK, so this is a HUD beside the editor.

## Requirements

- Windows
- ACE Studio running (CLI at `C:\Program Files\ACE Studio\acestudio-cli.exe`)
- Python 3 with `numpy`, `scipy`, `Pillow`, `pywin32`
- Anaconda at `D:\Anaconda3` on the original machine; any Python with those packages works

## Run (as an app)

On this Windows machine a Start Menu / Desktop shortcut **ACE Loudness** launches it with `pythonw` (no console). Double-click:

```
scripts/ace-loudness.vbs
```

or:

```powershell
pythonw scripts/loudness_overlay.py
```

Settings (opacity, size, position) are saved to `%APPDATA%\ACELoudness\settings.json`.

Drag the **透** slider to change window background opacity (40%–100%).

## Use

| Control | Action |
|---|---|
| 当前 / 参考 | Pick vocal tracks (Sing, or audio named `歌声*` / `和声*` / `参考`) |
| 钉 | Pin small window to ACE bottom-right |
| 跟 | Smooth playhead follow |
| 页 | Auto-page when the playhead leaves the view |
| 穿 | Click-through the waveform only (title bar stays clickable) |
| 刷 | Re-bounce the current vocal dry |
| Shift+← / → | Pan the bar (also Shift-drag on the waveform) |
| Wheel | Zoom |
| Click bar | Seek ACE |
| Ctrl+Alt+X | Force close |

Current track is exported **alone** (`--scope tracks`, clip range, mono, `--without-effects`). Reference audio uses the clip's source file when present.

## Layout

```
scripts/loudness_overlay.py   HUD
scripts/plot_loudness.py      shared envelope / LUFS helpers
SKILL.md                      Grok Build skill
```
