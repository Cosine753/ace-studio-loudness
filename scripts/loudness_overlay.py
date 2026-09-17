#!/usr/bin/env python3
"""Always-on-top loudness HUD for ACE Studio.

Docks to the ACE window, follows one open vocal plus a pasted 参考 vocal
(歌声_本家 / 和声_本家) on a shared time axis. Click the bar to seek.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from plot_loudness import DEFAULT_FLOOR_DB, db_fs, envelopes, load_audio, lufs_stats

CLI = Path(r"C:\Program Files\ACE Studio\acestudio-cli.exe")
CREATE_NO_WINDOW = 0x08000000
PID_FILE = Path(os.environ.get("TEMP", ".")) / "ace-loudness-overlay.pid"
HEADER_H = 44
MIN_H = 132
MIN_W = 480
DEFAULT_W = 720
DEFAULT_H = 184
FOLLOW_SPAN = 8.0
BG = "#101218"
CHROME = "#181c24"
BTN = "#2a303c"
BTN_ON = "#3d8f6e"
BTN_DANGER = "#8b3a48"
LINE = "#2c3340"
FG = "#f2f5fa"
MUTED = "#9aa3b2"
ACCENT = "#7dffc4"
REF_FG = "#ffc56e"
VOCAL_AUDIO_PREFIXES = ("歌声", "和声")


def _dpi_aware() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ace_json(*args: str, timeout: float = 12.0) -> dict:
    if not CLI.exists():
        raise FileNotFoundError(str(CLI))
    r = subprocess.run(
        [str(CLI), "--json", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    out = (r.stdout or "").strip()
    if not out:
        err = (r.stderr or "").strip()[-400:]
        raise RuntimeError(err or f"cli empty: {args}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(out[:400]) from e
    if r.returncode != 0 and "error" in data:
        raise RuntimeError(str(data.get("error") or data))
    return data


def kill_previous() -> None:
    if not PID_FILE.exists():
        return
    try:
        old = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return
    if old == os.getpid():
        return
    subprocess.run(
        ["taskkill", "/PID", str(old), "/F"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def find_ace_rect() -> tuple[int, int, int, int, int] | None:
    """Return (hwnd, x, y, w, h) of the main ACE Studio window."""
    import win32con
    import win32gui

    best = None
    best_score = -1

    def cb(hwnd, _):
        nonlocal best, best_score
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        if win32gui.GetWindowText(hwnd) != "ACE Studio":
            return True
        cls = win32gui.GetClassName(hwnd)
        if not cls.startswith("Qt"):
            return True
        r = win32gui.GetWindowRect(hwnd)
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 480 or h < 280:
            return True
        owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
        parent = win32gui.GetParent(hwnd)
        score = w * h
        if owner == 0 and parent == 0:
            score += 10**9
        if score > best_score:
            best_score = score
            best = (hwnd, r[0], r[1], w, h)
        return True

    win32gui.EnumWindows(cb, None)
    return best


@dataclass
class Env:
    t: np.ndarray
    peak: np.ndarray
    rms: np.ndarray
    duration: float
    stats: dict
    path: str
    clip_uuid: str
    track_uuid: str
    title: str


@dataclass
class Hud:
    stick: bool = False
    follow: bool = False
    page: bool = False
    click_through: bool = False
    width: int = DEFAULT_W
    height: int = DEFAULT_H
    view0: float = 0.0
    view1: float = 1.0
    play_t: float = 0.0
    play_status: str = "stopped"
    play_wall: float = field(default_factory=time.monotonic)
    clip_uuid: str = ""
    track_uuid: str = ""
    track_index: int = 0
    clip_name: str = ""
    track_name: str = ""
    begin_sec: float = 0.0
    end_sec: float = 0.0
    env: Env | None = None
    ref_env: Env | None = None
    ref_name: str = ""
    ref_uuid: str = ""
    current_locked: bool = False
    ref_locked: bool = False
    pick_current_uuid: str = ""
    pick_ref_uuid: str = ""
    vocal_choices: list = field(default_factory=list)
    msg: str = "寻找 ACE Studio…"
    ace: tuple[int, int, int, int, int] | None = None
    exporting: bool = False
    error: str = ""


def current_play(hud: Hud) -> float:
    t = hud.play_t
    if hud.play_status == "playing":
        t = hud.play_t + (time.monotonic() - hud.play_wall)
    return max(0.0, t)


def uuid_stem(uuid: str) -> str:
    return (uuid or "").replace("{", "").replace("}", "").replace("-", "")[:12]


def is_vocal_track(
    *,
    track_type: str = "",
    editor_type: str = "",
    clip_type: str = "",
    track_name: str = "",
) -> bool:
    kind = (track_type or editor_type or clip_type or "").strip().lower()
    if kind == "sing":
        return True
    if kind != "audio":
        return False
    name = (track_name or "").strip()
    if not name:
        return False
    if name.lower() in ("vocal", "vocals", "vox"):
        return True
    if "参考" in name:
        return True
    return name.startswith(VOCAL_AUDIO_PREFIXES)


def track_choice(track: dict) -> dict:
    idx = track.get("trackIndex")
    name = track.get("trackName") or ""
    label = f"{int(idx) + 1}. {name}" if idx is not None else name
    return {
        "uuid": track.get("trackUuid") or "",
        "label": label,
        "name": name,
        "type": track.get("trackType") or "",
    }


def list_vocal_choices(tracks: list) -> list:
    out = []
    for t in tracks:
        name = t.get("trackName") or ""
        ttype = t.get("trackType") or ""
        if is_vocal_track(track_type=ttype, track_name=name):
            out.append(track_choice(t))
    return out


def is_harmony_name(name: str) -> bool:
    n = (name or "").lower()
    return "harm" in n or "和声" in (name or "")


def reference_score(track: dict, current_uuid: str, current_name: str) -> int:
    uuid = track.get("trackUuid") or ""
    if not uuid or uuid == current_uuid:
        return 0
    name = track.get("trackName") or ""
    ttype = track.get("trackType") or ""
    if not is_vocal_track(track_type=ttype, track_name=name):
        return 0
    score = 0
    if "参考" in name:
        score += 100
    if name.lower() in ("ref", "reference"):
        score += 90
    if is_harmony_name(current_name) and name.startswith("和声"):
        score += 80
    if not is_harmony_name(current_name) and name.startswith("歌声"):
        score += 70
    if "本家" in name and name.startswith(("歌声", "和声")):
        score += 25
    return score


def pick_reference_track(tracks: list, current_uuid: str, current_name: str) -> dict | None:
    best = None
    best_s = 0
    for t in tracks:
        s = reference_score(t, current_uuid, current_name)
        if s > best_s:
            best_s = s
            best = t
    return best if best_s > 0 else None


def apply_audio_clip_offset(env: Env, clip: dict | None) -> Env:
    if not clip:
        return env
    media = clip.get("audioMedia") or {}
    src = media.get("sourcePath")
    if not src:
        return env
    try:
        if Path(src).resolve() != Path(env.path).resolve():
            return env
    except OSError:
        return env
    begin = float(clip.get("clipBeginSec") or 0.0)
    cin = float(media.get("clipInSec") or 0.0)
    env.t = env.t + (begin - cin)
    return env


def loudness_dir(project_name: str) -> Path:
    desktop = Path.home() / "Desktop" / project_name
    if desktop.exists():
        d = desktop / "loudness"
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = Path.home() / "Documents" / "ACE-loudness" / (project_name or "untitled")
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_audio(
    track_uuid: str,
    clip: dict | None,
    out_dir: Path,
    track_name: str,
    fingerprint: str = "",
) -> Path | None:
    if clip and clip.get("clipType", "").lower() == "audio":
        media = clip.get("audioMedia") or {}
        src = media.get("sourcePath")
        if src and Path(src).exists():
            return Path(src)
    stem = uuid_stem(track_uuid)
    if not stem or not fingerprint:
        return None
    hits = sorted(out_dir.glob(f"iso_{stem}_{fingerprint}*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in hits:
        if p.stat().st_size > 1024:
            return p
    # Instant switch: newest bounce for this track, even if fingerprint not fetched yet.
    hits = sorted(out_dir.glob(f"iso_{stem}_*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in hits:
        if p.stat().st_size > 1024:
            return p
    return None


def load_env(path: Path, clip_uuid: str, track_uuid: str, title: str, quick: bool = True) -> Env:
    sr, x = load_audio(path, None)
    t, peak, rms = envelopes(x, sr, 10.0)
    peak_db = float(20.0 * np.log10(max(float(np.max(np.abs(x))), 1e-12))) if x.size else None
    rms_db = float(20.0 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-12))) if x.size else None
    integ = None
    if not quick:
        stats = lufs_stats(x, sr)
        integ = None if stats.get("integrated_lufs") is None else round(float(stats["integrated_lufs"]), 1)
    elif rms_db is not None:
        integ = round(rms_db + 0.7, 1)
    stats_out = {
        "peak_db": None if peak_db is None else round(peak_db, 1),
        "rms_db": None if rms_db is None else round(rms_db, 1),
        "integrated_lufs": integ,
    }
    return Env(
        t=t,
        peak=peak,
        rms=rms,
        duration=float(len(x) / sr) if sr else 0.0,
        stats=stats_out,
        path=str(path),
        clip_uuid=clip_uuid,
        track_uuid=track_uuid,
        title=title,
    )


def clip_fingerprint(track_index: int, clip_index: int = 0) -> str:
    """OpenUTAU-style content hash: ACE's clip note-content fingerprint."""
    data = ace_json(
        "clip",
        "note-content",
        "--track-index",
        str(track_index),
        "--clip-index",
        str(clip_index),
        timeout=30.0,
    )
    raw = str(data.get("fingerprint") or "")
    return raw.split(":")[-1][:16] if raw else ""


def bounce_track(
    track_uuid: str,
    out_dir: Path,
    track_name: str,
    begin_sec: float = 0.0,
    end_sec: float = 0.0,
    fingerprint: str = "",
) -> Path:
    """Export ONE track, clip range only, mono, no external FX.

    OpenUTAU caches per-phrase wavs; we cache per (track, fingerprint).
    Cropping to the clip avoids ACE's full-arrangement tail leaking into the bar.
    """
    stem = uuid_stem(track_uuid) or "track"
    fp = fingerprint or "x"
    template = out_dir / f"iso_{stem}_{fp}.wav"
    hits = sorted(out_dir.glob(f"iso_{stem}_{fp}*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in hits:
        if p.stat().st_size > 1024:
            return p
    args = [
        "export",
        "audio",
        "--wait",
        "--scope",
        "tracks",
        "--track-uuid",
        track_uuid,
        "--without-effects",
        "true",
        "--channels",
        "1",
        "--path",
        str(template),
    ]
    if end_sec > begin_sec + 0.05:
        args += ["--from", f"{begin_sec:.3f}s", "--to", f"{end_sec:.3f}s"]
    try:
        ace_json(*args, timeout=600.0)
    except Exception:
        pass
    for _ in range(40):
        hits = sorted(out_dir.glob(f"iso_{stem}_{fp}*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in hits:
            if p.stat().st_size > 1024:
                return p
        time.sleep(0.4)
    raise RuntimeError("export finished but no isolated wav for this vocal track")


class Worker(threading.Thread):
    def __init__(self, hud: Hud, cmds: queue.Queue):
        super().__init__(daemon=True)
        self.hud = hud
        self.cmds = cmds
        self.stop_flag = threading.Event()
        self._n = 0
        self._loaded_clip = ""
        self._loaded_track = ""
        self._loaded_ref = ""
        self._loaded_fp = ""
        self._saw_synth = False
        self._pending_refresh = 0.0
        self._project = ""
        self._out = Path.home() / "Desktop"
        self._env_cache: dict[str, Env] = {}

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                while True:
                    kind, payload = self.cmds.get_nowait()
                    self._on_cmd(kind, payload)
            except queue.Empty:
                pass
            try:
                self._tick()
            except Exception as e:
                self.hud.error = str(e)[:180]
            self._n += 1
            self.stop_flag.wait(0.12)

    def _on_cmd(self, kind: str, payload) -> None:
        if kind == "seek":
            t = max(0.0, float(payload))
            ace_json("transport", "seek", "--time", f"{t:.3f}s")
        elif kind == "refresh":
            self._reload(force_bounce=True)
        elif kind == "reload":
            self._reload(force_bounce=False)
        elif kind == "set_current":
            uuid = str(payload or "")
            self.hud.current_locked = True
            self.hud.pick_current_uuid = uuid
            for c in self.hud.vocal_choices:
                if c.get("uuid") == uuid:
                    self.hud.track_name = c.get("name") or self.hud.track_name
                    break
            cached = self._env_cache.get(uuid)
            if cached is not None:
                self.hud.env = cached
                self.hud.track_uuid = uuid
                self._loaded_track = uuid
                self._loaded_clip = cached.clip_uuid
                self.hud.msg = (
                    f"{self.hud.track_name}  ↔  {self.hud.ref_name}"
                    if self.hud.ref_env is not None
                    else f"人声 · {self.hud.track_name}"
                )
                return
            tgt = self._target_from_uuid(uuid)
            if tgt:
                self.hud.clip_uuid = tgt["clip_uuid"]
                self.hud.track_uuid = tgt["track_uuid"]
                self.hud.track_index = int(tgt.get("track_index") or 0)
                self.hud.clip_name = tgt["clip_name"]
                self.hud.track_name = tgt["track_name"]
                self.hud.begin_sec = tgt["begin_sec"]
                self.hud.end_sec = tgt["end_sec"]
            self._loaded_clip = ""
            self._loaded_track = ""
            self._reload(force_bounce=False)
        elif kind == "set_ref":
            self.hud.ref_locked = True
            self.hud.pick_ref_uuid = str(payload or "")
            self._loaded_ref = ""
            if not self.hud.exporting:
                self._ensure_reference()

    def _tick(self) -> None:
        tr = ace_json("transport", "state")
        self.hud.play_t = float(tr.get("position") or 0.0)
        self.hud.play_status = str(tr.get("status") or "stopped")
        self.hud.play_wall = time.monotonic()
        if self._n % 3 == 0:
            self.hud.ace = find_ace_rect()
        try:
            syn = False
            if self._n % 4 == 0:
                syn = bool(ace_json("project", "synthesis-status").get("isSynthesizing"))
        except Exception:
            syn = False
        if syn:
            self._saw_synth = True
            self.hud.msg = "渲染中…"
        elif self._saw_synth:
            self._saw_synth = False
            self._pending_refresh = time.monotonic() + 0.7
            self._env_cache.pop(self.hud.track_uuid, None)
        if self._pending_refresh and time.monotonic() >= self._pending_refresh and not self.hud.exporting:
            self._pending_refresh = 0.0
            self._loaded_fp = ""
            self._loaded_track = ""
            self._reload(force_bounce=False)

        if self._n % 4 != 0:
            return
        try:
            info = ace_json("project", "info")
            self._project = info.get("projectName") or self._project
            self._out = loudness_dir(self._project)
        except Exception:
            pass
        try:
            tracks = ace_json("track", "list").get("tracks") or []
            self.hud.vocal_choices = list_vocal_choices(tracks)
        except Exception:
            tracks = []
        target = self._vocal_target()
        if not target:
            if self.hud.env is not None or self.hud.ref_env is not None:
                self.hud.env = None
                self.hud.ref_env = None
                self._loaded_clip = ""
                self._loaded_track = ""
                self._loaded_ref = ""
            self.hud.clip_uuid = ""
            self.hud.track_uuid = ""
            self.hud.ref_uuid = ""
            self.hud.ref_name = ""
            self.hud.msg = "只显示人声 · 点开一条人声轨"
            self.hud.error = ""
            return
        self.hud.clip_uuid = target["clip_uuid"]
        self.hud.track_uuid = target["track_uuid"]
        self.hud.track_index = int(target.get("track_index") or 0)
        self.hud.clip_name = target["clip_name"]
        self.hud.track_name = target["track_name"]
        if not self.hud.current_locked:
            self.hud.pick_current_uuid = self.hud.track_uuid
        self.hud.begin_sec = target["begin_sec"]
        self.hud.end_sec = target["end_sec"]
        self.hud.error = ""
        switched = (
            self.hud.clip_uuid != self._loaded_clip or self.hud.track_uuid != self._loaded_track
        )
        if switched and not self.hud.exporting:
            self.hud.env = None
            self._reload(force_bounce=False)
        elif not self.hud.exporting:
            self._ensure_reference()

    def _target_from_uuid(self, track_uuid: str) -> dict | None:
        if not track_uuid:
            return None
        try:
            tg = ace_json("track", "get", "--track-uuid", track_uuid)
        except Exception:
            return None
        track_name = tg.get("trackName") or ""
        clips = []
        try:
            clips = ace_json("clip", "list", "--track-uuid", track_uuid).get("clips") or []
        except Exception:
            pass
        cur = clips[0] if clips else None
        track_index = int(tg.get("trackIndex") or 0)
        if cur is None:
            return {
                "clip_uuid": "",
                "track_uuid": track_uuid,
                "track_index": track_index,
                "clip_name": track_name,
                "track_name": track_name,
                "begin_sec": 0.0,
                "end_sec": 0.0,
            }
        return {
            "clip_uuid": cur.get("clipUuid") or "",
            "track_uuid": track_uuid,
            "track_index": track_index,
            "clip_name": cur.get("clipName") or track_name,
            "track_name": track_name,
            "begin_sec": float(cur.get("clipBeginSec") or 0.0),
            "end_sec": float(cur.get("clipEndSec") or 0.0),
        }

    def _vocal_target(self) -> dict | None:
        """Locked dropdown choice, else the open editor / caret vocal."""
        if self.hud.current_locked and self.hud.pick_current_uuid:
            return self._target_from_uuid(self.hud.pick_current_uuid)
        st: dict = {}
        try:
            st = ace_json("editor", "status")
        except Exception:
            st = {}
        if st.get("isAvailable"):
            track_uuid = st.get("trackUuid") or ""
            track_name = st.get("clipName") or ""
            track_type = st.get("editorType") or ""
            try:
                tg = ace_json("track", "get", "--track-uuid", track_uuid)
                track_name = tg.get("trackName") or track_name
                track_type = tg.get("trackType") or track_type
            except Exception:
                pass
            if is_vocal_track(
                track_type=track_type,
                editor_type=st.get("editorType") or "",
                track_name=track_name,
            ):
                begin_sec = end_sec = 0.0
                try:
                    rng = ace_json("editor", "tick-range")
                    begin_sec = float(rng.get("beginSec") or 0.0)
                    end_sec = float(rng.get("endSec") or 0.0)
                except Exception:
                    pass
                return {
                    "clip_uuid": st.get("clipUuid") or "",
                    "track_uuid": track_uuid,
                    "track_index": int(st.get("trackIndex") or 0),
                    "clip_name": st.get("clipName") or track_name,
                    "track_name": track_name,
                    "begin_sec": begin_sec,
                    "end_sec": end_sec,
                }
        try:
            caret = ace_json("caret", "get")
        except Exception:
            return None
        track_uuid = caret.get("trackUuid") or ""
        if not track_uuid:
            return None
        try:
            tg = ace_json("track", "get", "--track-uuid", track_uuid)
        except Exception:
            return None
        track_name = tg.get("trackName") or ""
        if not is_vocal_track(track_type=tg.get("trackType") or "", track_name=track_name):
            return None
        clips = []
        try:
            clips = ace_json("clip", "list", "--track-uuid", track_uuid).get("clips") or []
        except Exception:
            pass
        sec = float(caret.get("sec") or 0.0)
        cur = None
        for c in clips:
            b = float(c.get("clipBeginSec") if c.get("clipBeginSec") is not None else -1)
            e = float(c.get("clipEndSec") if c.get("clipEndSec") is not None else -1)
            if b >= 0 and e > b and b <= sec < e:
                cur = c
                break
        if cur is None and clips:
            cur = clips[0]
        if cur is None:
            return None
        return {
            "clip_uuid": cur.get("clipUuid") or "",
            "track_uuid": track_uuid,
            "track_index": int(tg.get("trackIndex") or 0),
            "clip_name": cur.get("clipName") or track_name,
            "track_name": track_name,
            "begin_sec": float(cur.get("clipBeginSec") or 0.0),
            "end_sec": float(cur.get("clipEndSec") or 0.0),
        }

    def _load_env(
        self,
        track_uuid: str,
        clip_uuid: str,
        track_name: str,
        force_bounce: bool,
        title: str,
        track_index: int | None = None,
    ) -> Env:
        clips = ace_json("clip", "list", "--track-uuid", track_uuid).get("clips") or []
        cur = None
        for c in clips:
            if clip_uuid and c.get("clipUuid") == clip_uuid:
                cur = c
                break
        if cur is None and clips:
            cur = clips[0]
        clip_type = (cur or {}).get("clipType") or ""
        begin = float((cur or {}).get("clipBeginSec") or self.hud.begin_sec or 0.0)
        end = float((cur or {}).get("clipEndSec") or self.hud.end_sec or 0.0)
        if not force_bounce and track_uuid in self._env_cache:
            return self._env_cache[track_uuid]
        fp = ""
        path = None if force_bounce else resolve_audio(track_uuid, cur, self._out, track_name, fingerprint="")
        if path is None and clip_type.lower() == "sing":
            try:
                idx = self.hud.track_index if track_index is None else track_index
                fp = clip_fingerprint(idx, 0)
            except Exception:
                fp = ""
            path = resolve_audio(track_uuid, cur, self._out, track_name, fingerprint=fp)
        if path is None:
            if clip_type.lower() == "audio":
                path = resolve_audio(track_uuid, cur, self._out, track_name, fingerprint=fp)
            if path is None:
                self.hud.msg = "正在导出当前人声（单轨）…"
                path = bounce_track(track_uuid, self._out, track_name, begin, end, fp)
        env = load_env(path, (cur or {}).get("clipUuid") or clip_uuid, track_uuid, title, quick=True)
        env = apply_audio_clip_offset(env, cur)
        if clip_type.lower() == "sing" and end > begin and env.t.size:
            # keep only the clip window (drop arrangement tail)
            dur = end - begin
            keep = env.t <= (env.t[0] + dur + 0.05)
            if np.any(keep) and not np.all(keep):
                env.t = env.t[keep]
                env.peak = env.peak[keep]
                env.rms = env.rms[keep]
                env.duration = float(dur)
        if clip_type.lower() != "audio" and begin:
            env.t = env.t + begin
        self._loaded_fp = fp
        self._env_cache[track_uuid] = env
        return env

    def _ensure_reference(self, force_bounce: bool = False) -> None:
        try:
            tracks = ace_json("track", "list").get("tracks") or []
        except Exception:
            tracks = []
        self.hud.vocal_choices = list_vocal_choices(tracks)
        ref = None
        if self.hud.ref_locked:
            if not self.hud.pick_ref_uuid:
                self.hud.ref_env = None
                self.hud.ref_uuid = ""
                self.hud.ref_name = ""
                self._loaded_ref = ""
                self.hud.msg = f"人声 · {self.hud.track_name}"
                return
            for t in tracks:
                if t.get("trackUuid") == self.hud.pick_ref_uuid:
                    ref = t
                    break
        else:
            ref = pick_reference_track(tracks, self.hud.track_uuid, self.hud.track_name)
        if not ref:
            self.hud.ref_env = None
            self.hud.ref_uuid = ""
            self.hud.ref_name = ""
            self._loaded_ref = ""
            self.hud.msg = f"人声 · {self.hud.track_name}"
            return
        self.hud.ref_uuid = ref.get("trackUuid") or ""
        self.hud.ref_name = ref.get("trackName") or "参考"
        if not self.hud.ref_locked:
            self.hud.pick_ref_uuid = self.hud.ref_uuid
        if self._loaded_ref == self.hud.ref_uuid and self.hud.ref_env is not None and not force_bounce:
            self.hud.msg = f"{self.hud.track_name}  ↔  {self.hud.ref_name}"
            return
        try:
            self.hud.ref_env = self._load_env(
                self.hud.ref_uuid,
                "",
                self.hud.ref_name,
                force_bounce=False,
                title=self.hud.ref_name,
                track_index=int(ref.get("trackIndex") or 0),
            )
            self._loaded_ref = self.hud.ref_uuid
            self.hud.msg = f"{self.hud.track_name}  ↔  {self.hud.ref_name}"
        except Exception as e:
            self.hud.ref_env = None
            self.hud.error = f"参考轨: {e}"[:180]

    def _reload(self, force_bounce: bool) -> None:
        if not self.hud.track_uuid:
            return
        if self.hud.exporting:
            return
        if force_bounce:
            self._env_cache.pop(self.hud.track_uuid, None)
        self.hud.exporting = True
        self.hud.msg = "加载音频…"
        try:
            env = self._load_env(
                self.hud.track_uuid,
                self.hud.clip_uuid,
                self.hud.track_name,
                force_bounce,
                f"{self._project} · {self.hud.clip_name}",
            )
            self.hud.env = env
            if self.hud.end_sec <= self.hud.begin_sec:
                self.hud.view0, self.hud.view1 = 0.0, env.duration
            else:
                self.hud.view0, self.hud.view1 = self.hud.begin_sec, self.hud.end_sec
            self._loaded_clip = self.hud.clip_uuid
            self._loaded_track = self.hud.track_uuid
            self._ensure_reference(force_bounce=False)
            if self.hud.ref_env is None:
                self.hud.msg = f"人声 · {self.hud.track_name}"
        except Exception as e:
            self.hud.error = str(e)[:180]
            self.hud.msg = "加载失败，点刷新重试"
        finally:
            self.hud.exporting = False


def _font(size: int = 11):
    from PIL import ImageFont

    for fp in (
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
    ):
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _cols(env: Env | None, t0: float, t1: float, w: int) -> tuple[np.ndarray, np.ndarray]:
    peak_col = np.zeros(w, dtype=np.float64)
    rms_col = np.zeros(w, dtype=np.float64)
    if env is None or env.t.size == 0 or t1 <= t0:
        return peak_col, rms_col
    xs = ((env.t - t0) / (t1 - t0) * w).astype(np.int32)
    valid = (xs >= 0) & (xs < w)
    if np.any(valid):
        np.maximum.at(peak_col, xs[valid], env.peak[valid])
        np.maximum.at(rms_col, xs[valid], env.rms[valid])
    return peak_col, rms_col


def _fill_lane(arr: np.ndarray, x0: int, y0: int, y1: int, peak, rms, ymax, peak_rgb, rms_rgb) -> None:
    mid = (y0 + y1) // 2
    half = max(2, (y1 - y0) // 2 - 2)
    arr[mid, x0:] = (48, 54, 66)
    ww = peak.shape[0]
    for x in range(ww):
        px = x0 + x
        if px >= arr.shape[1]:
            break
        ph = int(peak[x] / ymax * half)
        rh = int(rms[x] / ymax * half)
        if ph > 0:
            arr[max(y0, mid - ph) : min(y1, mid + ph + 1), px] = peak_rgb
        if rh > 0:
            arr[max(y0, mid - rh) : min(y1, mid + rh + 1), px] = rms_rgb


def render_bar(w: int, h: int, hud: Hud) -> "Image.Image":
    from PIL import Image, ImageDraw

    w, h = max(int(w), 1), max(int(h), 1)
    arr = np.full((h, w, 3), (16, 18, 24), dtype=np.uint8)
    gutter = 44
    time_h = 18
    t0, t1 = hud.view0, hud.view1
    if t1 <= t0:
        t1 = t0 + 1.0
    wave_w = max(1, w - gutter)
    cur_p, cur_r = _cols(hud.env, t0, t1, wave_w)
    ref_p, ref_r = _cols(hud.ref_env, t0, t1, wave_w)
    shared = max(
        float(np.max(cur_p)) if cur_p.size else 0.0,
        float(np.max(ref_p)) if ref_p.size else 0.0,
        1e-6,
    )
    ymax = max(1.0, shared * 1.04)
    compare = hud.ref_env is not None and hud.ref_env.t.size > 0
    body_h = h - time_h
    if compare:
        split = body_h // 2
        arr[0:split, 0:3] = (125, 255, 196)
        arr[split:body_h, 0:3] = (255, 197, 110)
        arr[split, gutter:] = (44, 51, 64)
        _fill_lane(arr, gutter, 1, split - 1, cur_p, cur_r, ymax, (210, 220, 232), (92, 230, 170))
        _fill_lane(arr, gutter, split + 1, body_h - 1, ref_p, ref_r, ymax, (255, 210, 150), (232, 150, 72))
    elif hud.env is not None and hud.env.t.size:
        arr[0:body_h, 0:3] = (125, 255, 196)
        _fill_lane(arr, gutter, 1, body_h - 1, cur_p, cur_r, ymax, (210, 220, 232), (92, 230, 170))
    arr[body_h:, :] = (20, 22, 28)
    arr[body_h, :] = (44, 51, 64)

    img = Image.fromarray(arr, "RGB")
    draw = ImageDraw.Draw(img)
    font = _font(11)
    font_s = _font(10)
    if compare:
        split = body_h // 2
        draw.text((8, 6), "当前", fill=(125, 255, 196), font=font_s)
        draw.text((8, split + 6), "参考", fill=(255, 197, 110), font=font_s)
    elif hud.env is None or not hud.env.t.size:
        draw.text((gutter + 10, h // 2 - 8), hud.msg or "无波形", fill=(154, 163, 178), font=font)

    pt = current_play(hud)
    px = gutter + int((pt - t0) / (t1 - t0) * wave_w)
    if gutter <= px < w:
        draw.line([(px, 0), (px, body_h)], fill=(255, 88, 96), width=2)

    draw.text((gutter + 6, h - 15), f"{t0:.1f}s", fill=(154, 163, 178), font=font_s)
    label = f"{t1:.1f}s"
    tw = draw.textlength(label, font=font_s) if hasattr(draw, "textlength") else 36
    draw.text((w - tw - 8, h - 15), label, fill=(154, 163, 178), font=font_s)
    return img


class OverlayApp:
    def __init__(self, hud: Hud, cmds: queue.Queue):
        import tkinter as tk

        self.hud = hud
        self.cmds = cmds
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("ACE 响度")
        self.root.configure(bg=CHROME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{DEFAULT_W}x{HEADER_H}+80+80")
        self.root.minsize(MIN_W, HEADER_H)
        self.root.pack_propagate(False)
        self.wave = tk.Toplevel(self.root)
        self.wave.overrideredirect(True)
        self.wave.attributes("-topmost", True)
        self.wave.configure(bg=BG)
        self.wave.geometry(f"{DEFAULT_W}x{DEFAULT_H - HEADER_H}+80+{80 + HEADER_H}")
        self._photo = None
        self._drag = None
        self._last_size = (0, 0)
        self._last_view = (0.0, 0.0, 0.0)
        self._last_env = None
        self._last_pos = None
        self._through = False
        self._placed = False
        self._syncing = False
        self._hot_p = False
        self._last_pan = 0.0
        self._choice_labels: list[str] = []

        self.header = tk.Frame(self.root, bg=CHROME, height=HEADER_H)
        self.header.pack(fill="both", expand=True)
        self.header.pack_propagate(False)
        bar = tk.Frame(self.header, bg=CHROME)
        bar.pack(fill="both", expand=True, padx=8, pady=6)

        def chip(text, cmd, parent):
            b = tk.Label(
                parent,
                text=text,
                bg=BTN,
                fg=FG,
                padx=9,
                pady=3,
                font=("Segoe UI", 9),
                cursor="hand2",
            )
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(bg="#343b4a") if w.cget("bg") == BTN else None)
            b.bind("<Leave>", lambda e, w=b: w.configure(bg=BTN_ON if self._chip_on(w) else BTN))
            b.pack(side="left", padx=(0, 4))
            return b

        def menu_btn(parent, fg):
            mb = tk.Menubutton(
                parent,
                text="…",
                bg=BTN,
                fg=fg,
                activebackground="#343b4a",
                activeforeground=fg,
                relief="flat",
                font=("Segoe UI", 9),
                padx=10,
                pady=3,
                cursor="hand2",
                direction="below",
            )
            menu = tk.Menu(
                mb,
                tearoff=0,
                bg=CHROME,
                fg=FG,
                activebackground=BTN_ON,
                activeforeground=FG,
                bd=0,
                font=("Segoe UI", 9),
            )
            mb.config(menu=menu)
            mb.pack(side="left", padx=(0, 6))
            return mb, menu

        self.grip = tk.Label(bar, text="☰", bg=CHROME, fg=MUTED, font=("Segoe UI", 11), cursor="fleur")
        self.grip.pack(side="left", padx=(0, 8))
        tk.Label(bar, text="LOUD", bg=CHROME, fg=ACCENT, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))
        self.cur_btn, self.cur_menu = menu_btn(bar, ACCENT)
        tk.Label(bar, text="↔", bg=CHROME, fg=MUTED, font=("Segoe UI", 10)).pack(side="left", padx=(0, 6))
        self.ref_btn, self.ref_menu = menu_btn(bar, REF_FG)

        close = tk.Label(bar, text="✕", bg=BTN_DANGER, fg=FG, font=("Segoe UI", 9), padx=8, pady=3, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self._close())
        self.stats = tk.Label(bar, text="", bg=CHROME, fg=MUTED, font=("Consolas", 9))
        self.stats.pack(side="right", padx=10)
        tools = tk.Frame(bar, bg=CHROME)
        tools.pack(side="left", padx=(10, 0))
        self.b_stick = chip("钉", self.toggle_stick, tools)
        self.b_follow = chip("跟", self.toggle_follow, tools)
        self.b_page = chip("页", self.toggle_page, tools)
        self.b_through = chip("穿", self.toggle_through, tools)
        self.b_refresh = chip("刷", lambda: self.cmds.put(("refresh", None)), tools)
        self.title = tk.Label(bar, text="", bg=CHROME, fg=MUTED)

        self.canvas = tk.Label(self.wave, bg=BG, bd=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        for wdg in (self.header, self.grip, self.stats, bar):
            wdg.bind("<ButtonPress-1>", self._on_drag_start)
            wdg.bind("<B1-Motion>", self._on_drag)
            wdg.bind("<ButtonRelease-1>", self._on_drag_end)
            wdg.bind("<ButtonPress-3>", self._on_height_start)
            wdg.bind("<B3-Motion>", self._on_drag)
            wdg.bind("<ButtonRelease-3>", self._on_drag_end)
        self.canvas.bind("<ButtonPress-1>", self._on_wave_down)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.root.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.wave.bind("<MouseWheel>", self._on_wheel)
        self.root.bind("<Escape>", lambda e: self._close())
        self.wave.bind("<Escape>", lambda e: self._close())
        self.root.bind("<Shift-Left>", lambda e: self._pan(-1))
        self.root.bind("<Shift-Right>", lambda e: self._pan(1))
        self.wave.bind("<Shift-Left>", lambda e: self._pan(-1))
        self.wave.bind("<Shift-Right>", lambda e: self._pan(1))
        self.root.after(30, self._ui_tick)
        self.root.after(80, self._poll_hotkeys)

    def _chip_on(self, w) -> bool:
        return (
            (w is self.b_stick and self.hud.stick)
            or (w is self.b_follow and self.hud.follow)
            or (w is self.b_page and self.hud.page)
            or (w is self.b_through and self.hud.click_through)
        )

    def _short_label(self, label: str) -> str:
        if not label:
            return ""
        if ". " in label:
            label = label.split(". ", 1)[-1]
        return label[:18]

    def _on_menu_current(self, uuid: str) -> None:
        if uuid:
            self.cmds.put(("set_current", uuid))

    def _on_menu_ref(self, uuid: str) -> None:
        self.cmds.put(("set_ref", uuid or ""))

    def _sync_menus(self) -> None:
        choices = self.hud.vocal_choices or []
        labels = [c["label"] for c in choices]
        if labels != self._choice_labels:
            self._choice_labels = labels
            self.cur_menu.delete(0, "end")
            self.ref_menu.delete(0, "end")
            self.ref_menu.add_command(label="（无）", command=lambda: self._on_menu_ref(""))
            for c in choices:
                u, lab = c.get("uuid") or "", c.get("label") or ""
                self.cur_menu.add_command(label=lab, command=lambda uu=u: self._on_menu_current(uu))
                self.ref_menu.add_command(label=lab, command=lambda uu=u: self._on_menu_ref(uu))
        cur = next((c["label"] for c in choices if c.get("uuid") == self.hud.track_uuid), "")
        self.cur_btn.configure(text=self._short_label(cur) or "当前轨")
        if self.hud.ref_locked and not self.hud.pick_ref_uuid:
            self.ref_btn.configure(text="无参考")
        else:
            ref = next((c["label"] for c in choices if c.get("uuid") == self.hud.ref_uuid), "")
            self.ref_btn.configure(text=self._short_label(ref) or "参考轨")

    def _paint_btn(self, b, on: bool) -> None:
        b.configure(bg=BTN_ON if on else BTN, fg=("#07140e" if on else FG))

    def toggle_stick(self) -> None:
        self.hud.stick = not self.hud.stick
        self._paint_btn(self.b_stick, self.hud.stick)

    def toggle_follow(self) -> None:
        self.hud.follow = not self.hud.follow
        if self.hud.follow:
            self.hud.page = False
        self._paint_btn(self.b_follow, self.hud.follow)
        self._paint_btn(self.b_page, self.hud.page)

    def toggle_page(self) -> None:
        self.hud.page = not self.hud.page
        if self.hud.page:
            self.hud.follow = False
        self._paint_btn(self.b_page, self.hud.page)
        self._paint_btn(self.b_follow, self.hud.follow)

    def toggle_through(self) -> None:
        self.hud.click_through = not self.hud.click_through
        self._apply_through()
        self._paint_btn(self.b_through, self.hud.click_through)

    def _close(self) -> None:
        try:
            self.wave.destroy()
        except Exception:
            pass
        self.root.destroy()

    def _poll_hotkeys(self) -> None:
        import ctypes

        try:
            u = ctypes.windll.user32
            ctrl = bool(u.GetAsyncKeyState(0x11) & 0x8000)
            alt = bool(u.GetAsyncKeyState(0x12) & 0x8000)
            if ctrl and alt and (u.GetAsyncKeyState(0x58) & 0x8000):  # X
                self._close()
                return
            p = bool(u.GetAsyncKeyState(0x50) & 0x8000)  # P
            if ctrl and alt and p and not self._hot_p:
                self._hot_p = True
                self.toggle_through()
            if not p:
                self._hot_p = False
            shift = bool(u.GetAsyncKeyState(0x10) & 0x8000)
            left = bool(u.GetAsyncKeyState(0x25) & 0x8000)
            right = bool(u.GetAsyncKeyState(0x27) & 0x8000)
            if shift and (left or right) and self._cursor_over_us():
                now = time.monotonic()
                if now - self._last_pan > 0.11:
                    self._last_pan = now
                    self._pan(-1 if left else 1)
        except Exception:
            pass
        try:
            self.root.after(80, self._poll_hotkeys)
        except Exception:
            pass

    def _apply_through(self) -> None:
        import win32con
        import win32gui

        # Only the waveform is click-through. The title bar (and ×) stays clickable.
        hwnd = self._hwnd_of(self.wave)
        gwl = win32con.GWL_EXSTYLE
        ex = win32gui.GetWindowLong(hwnd, gwl)
        if self.hud.click_through:
            ex |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
        else:
            ex &= ~win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(hwnd, gwl, ex)
        self._through = self.hud.click_through

    def _on_drag_start(self, e) -> None:
        self._drag = (
            e.x_root,
            e.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
            self.hud.height,
            "move",
            self.hud.width,
            self.hud.view0,
            self.hud.view1,
        )

    def _on_height_start(self, e) -> None:
        self._drag = (
            e.x_root,
            e.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
            self.hud.height,
            "height",
            self.hud.width,
            0.0,
            0.0,
        )

    def _on_wave_down(self, e) -> None:
        if e.state & 0x0001:  # Shift: pan the timeline
            self._drag = (
                e.x_root,
                e.y_root,
                self.root.winfo_x(),
                self.root.winfo_y(),
                self.hud.height,
                "pan",
                self.canvas.winfo_width(),
                self.hud.view0,
                self.hud.view1,
            )
            return
        self._on_seek(e)

    def _on_drag(self, e) -> None:
        if not self._drag:
            return
        dx = e.x_root - self._drag[0]
        dy = e.y_root - self._drag[1]
        mode = self._drag[5]
        if mode == "height":
            self.hud.height = max(MIN_H, min(420, self._drag[4] + dy))
            self._place_pair(self._drag[2], self._drag[3], self.hud.width, self.hud.height)
            self._last_pos = None
        elif mode == "pan":
            w = max(1, int(self._drag[6]))
            span = self._drag[8] - self._drag[7]
            dt = -dx / w * span
            self._set_view(self._drag[7] + dt, self._drag[8] + dt)
            self.hud.follow = False
            self._paint_btn(self.b_follow, False)
        else:
            if abs(dx) < 2 and abs(dy) < 2:
                return
            self._place_pair(self._drag[2] + dx, self._drag[3] + dy, self.hud.width, self.hud.height)
            if self.hud.stick:
                self.hud.stick = False
                self._paint_btn(self.b_stick, False)

    def _on_drag_end(self, e) -> None:
        self._drag = None

    def _clip_bounds(self) -> tuple[float, float]:
        lo = self.hud.begin_sec
        hi = self.hud.end_sec
        if hi <= lo:
            env = self.hud.env
            hi = env.duration if env else lo + 8.0
        return lo, hi

    def _set_view(self, v0: float, v1: float) -> None:
        lo, hi = self._clip_bounds()
        span = max(0.8, v1 - v0)
        if v0 < lo:
            v0 = lo
            v1 = min(hi, v0 + span)
        if v1 > hi:
            v1 = hi
            v0 = max(lo, v1 - span)
        if v1 <= v0:
            v1 = v0 + 1.0
        self.hud.view0, self.hud.view1 = v0, v1

    def _pan(self, direction: int) -> None:
        span = max(0.8, self.hud.view1 - self.hud.view0)
        self._set_view(self.hud.view0 + span * 0.4 * direction, self.hud.view1 + span * 0.4 * direction)
        self.hud.follow = False
        self._paint_btn(self.b_follow, False)

    def _apply_view_mode(self) -> None:
        if self.hud.follow:
            pt = current_play(self.hud)
            span = self.hud.view1 - self.hud.view0
            if span > FOLLOW_SPAN * 1.5 or span < 1.0:
                span = FOLLOW_SPAN
            self._set_view(pt - span * 0.35, pt + span * 0.65)
            return
        if self.hud.page and self.hud.play_status == "playing":
            pt = current_play(self.hud)
            span = max(0.8, self.hud.view1 - self.hud.view0)
            if pt >= self.hud.view1 - span * 0.04 or pt < self.hud.view0:
                self._set_view(pt - span * 0.08, pt - span * 0.08 + span)

    def _time_at(self, x: int) -> float:
        gutter = 44
        w = max(1, self.canvas.winfo_width() - gutter)
        x = max(0, x - gutter)
        t0, t1 = self.hud.view0, self.hud.view1
        if t1 <= t0:
            t1 = t0 + 1.0
        return t0 + (x / w) * (t1 - t0)

    def _view(self) -> tuple[float, float]:
        t0, t1 = self.hud.view0, self.hud.view1
        if t1 <= t0:
            t1 = t0 + 1.0
        return t0, t1

    def _cursor_over_us(self) -> bool:
        import win32gui

        try:
            x, y = win32gui.GetCursorPos()
            for wdg in (self.root, self.wave):
                hx = self._hwnd_of(wdg)
                l, t, r, b = win32gui.GetWindowRect(hx)
                if l <= x <= r and t <= y <= b:
                    return True
        except Exception:
            return False
        return False

    def _on_seek(self, e) -> None:
        t = self._time_at(e.x)
        self.cmds.put(("seek", t))

    def _on_wheel(self, e) -> None:
        t0, t1 = self.hud.view0, self.hud.view1
        span = max(0.6, t1 - t0)
        factor = 0.8 if e.delta > 0 else 1.25
        pt = self._time_at(e.x) if e.widget is self.canvas else current_play(self.hud)
        nspan = max(0.8, min(600.0, span * factor))
        left = pt - (pt - t0) / span * nspan
        env = self.hud.env
        lo = self.hud.begin_sec
        hi = self.hud.end_sec if self.hud.end_sec > lo else (env.duration if env else left + nspan)
        if left < lo:
            left = lo
        right = left + nspan
        if right > hi:
            right = hi
            left = max(lo, right - nspan)
        self.hud.view0, self.hud.view1 = left, right
        self.hud.follow = False
        self._paint_btn(self.b_follow, False)

    def _hwnd_of(self, widget) -> int:
        import win32gui

        widget.update_idletasks()
        wid = int(widget.winfo_id())
        return win32gui.GetParent(wid) or wid

    def _ui_scale(self) -> float:
        try:
            import ctypes

            hwnd = self.hud.ace[0] if self.hud.ace else self._hwnd_of(self.root)
            dpi = ctypes.windll.user32.GetDpiForWindow(int(hwnd))
            return max(1.0, float(dpi) / 96.0)
        except Exception:
            return 1.5

    def _place_pair(self, ox: int, oy: int, ow: int, oh: int) -> None:
        ow = max(MIN_W, int(ow))
        oh = max(MIN_H, int(oh))
        self.hud.width, self.hud.height = ow, oh
        hh = HEADER_H
        wh = max(56, oh - hh)
        self.root.geometry(f"{ow}x{hh}+{int(ox)}+{int(oy)}")
        self.wave.geometry(f"{ow}x{wh}+{int(ox)}+{int(oy) + hh}")
        try:
            self.root.attributes("-topmost", True)
            self.wave.attributes("-topmost", True)
        except Exception:
            pass

    def _stick_geom(self) -> None:
        if self._drag:
            return
        ace = self.hud.ace
        scale = self._ui_scale()
        if not self.hud.stick:
            if not self._placed:
                if ace:
                    _ah, x, y, w, h = ace
                    ow, oh = self.hud.width, self.hud.height
                    ox = int((x + w) / scale - ow - 14)
                    oy = int((y + h) / scale - oh - 14)
                    self._place_pair(ox, oy, ow, oh)
                self._placed = True
            return
        if not ace:
            return
        _ah, x, y, w, h = ace
        ow, oh = self.hud.width, self.hud.height
        ox = int((x + w) / scale - ow - 14)
        oy = int((y + h) / scale - oh - 14)
        pos = (ox, oy, ow, oh)
        if pos == self._last_pos:
            return
        self._place_pair(ox, oy, ow, oh)
        self._last_pos = pos
        self._placed = True

    def _ui_tick(self) -> None:
        try:
            self._stick_geom()
            self._apply_view_mode()
            w = max(1, self.canvas.winfo_width())
            h = max(1, self.canvas.winfo_height())
            pt = current_play(self.hud)
            view = (self.hud.view0, self.hud.view1, round(pt, 2) if (self.hud.follow or self.hud.page) else round(pt, 2))
            size = (w, h)
            env_sig = (id(self.hud.env), id(self.hud.ref_env), self.hud.track_name, self.hud.ref_name)
            moving = (
                self.hud.play_status == "playing"
                or view != self._last_view
                or size != self._last_size
                or env_sig != self._last_env
            )
            self._last_env = env_sig
            if moving and w > 10 and h > 10:
                img = render_bar(w, h, self.hud)
                from PIL import ImageTk

                self._photo = ImageTk.PhotoImage(img)
                self.canvas.configure(image=self._photo)
                self._last_size = size
                self._last_view = view
            env = self.hud.env
            st = env.stats if env else {}
            lufs = st.get("integrated_lufs")
            stt = self.hud.play_status
            stt = "播" if stt == "playing" else ("等" if "interrupt" in stt else "停")
            bits = [f"{current_play(self.hud):.1f}s", stt]
            if lufs is not None:
                bits.append(f"{lufs}")
            rst = self.hud.ref_env.stats if self.hud.ref_env else {}
            rlufs = rst.get("integrated_lufs")
            if rlufs is not None:
                bits.append(f"参 {rlufs}")
                if lufs is not None:
                    bits.append(f"Δ{lufs - rlufs:+.1f}")
            if self.hud.error:
                bits.append(self.hud.error)
            self.stats.configure(text="  ".join(bits))
            self._sync_menus()
            self._paint_btn(self.b_stick, self.hud.stick)
            self._paint_btn(self.b_follow, self.hud.follow)
            self._paint_btn(self.b_page, self.hud.page)
            self._paint_btn(self.b_through, self.hud.click_through)
        except Exception:
            pass
        self.root.after(33, self._ui_tick)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    # Leave DPI unaware so Tk sizes (640x184) match on-screen CSS pixels.
    kill_previous()
    write_pid()
    hud = Hud()
    cmds: queue.Queue = queue.Queue()
    worker = Worker(hud, cmds)
    worker.start()
    try:
        OverlayApp(hud, cmds).run()
    finally:
        worker.stop_flag.set()
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
