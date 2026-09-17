#!/usr/bin/env python3
"""SV-style audio-bar (peak width) + RMS loudness plot.

The outer filled shape is peak amplitude — the same visual cue as Synthesizer V's
engine-output waveform: louder = fatter bar. The inner fill is RMS. A second
lane shows RMS in dBFS. Optional HTML lets you pan/zoom and hover time + dB.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

DEFAULT_FFMPEG = r"D:\Little_C_project\ffmpeg\bin\ffmpeg.exe"
DEFAULT_FLOOR_DB = -48.0


def _ffmpeg_bin(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env = os.environ.get("FFMPEG")
    if env and Path(env).exists():
        return env
    if Path(DEFAULT_FFMPEG).exists():
        return DEFAULT_FFMPEG
    return shutil.which("ffmpeg")


def _to_mono_float(data: np.ndarray) -> np.ndarray:
    x = np.asarray(data)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if np.issubdtype(x.dtype, np.integer):
        info = np.iinfo(x.dtype)
        x = x.astype(np.float64) / max(abs(info.min), info.max)
    else:
        x = x.astype(np.float64)
        peak = np.max(np.abs(x)) if x.size else 0.0
        if peak > 1.5:
            x = x / peak
    return x


def _read_wav(path: Path) -> tuple[int, np.ndarray]:
    from scipy.io import wavfile

    sr, data = wavfile.read(str(path))
    return sr, _to_mono_float(data)


def load_audio(path: Path, ffmpeg: str | None) -> tuple[int, np.ndarray]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            return _read_wav(path)
        except Exception:
            pass
    ff = _ffmpeg_bin(ffmpeg)
    if not ff:
        raise RuntimeError(
            "need ffmpeg to decode this file (wav fallback failed). "
            "Pass --ffmpeg or install ffmpeg."
        )
    tmp = Path(tempfile.mkstemp(prefix="ace_loud_", suffix=".wav")[1])
    try:
        cmd = [
            ff,
            "-y",
            "-i",
            str(path),
            "-ac",
            "1",
            "-c:a",
            "pcm_f32le",
            "-f",
            "wav",
            str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 64:
            err = (proc.stderr or proc.stdout or "")[-800:]
            raise RuntimeError(f"ffmpeg decode failed:\n{err}")
        return _read_wav(tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def envelopes(x: np.ndarray, sr: int, hop_ms: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    hop = max(1, int(round(sr * hop_ms / 1000.0)))
    n = len(x)
    if n == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    n_h = n // hop
    if n_h == 0:
        peak = np.array([float(np.max(np.abs(x)))], dtype=np.float64)
        rms = np.array([float(np.sqrt(np.mean(x * x)))], dtype=np.float64)
        t = np.array([0.5 * n / sr], dtype=np.float64)
        return t, peak, rms
    frames = x[: n_h * hop].reshape(n_h, hop)
    peak = np.max(np.abs(frames), axis=1)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    t = (np.arange(n_h) + 0.5) * hop / sr
    return t, peak, rms


def db_fs(mag: np.ndarray, floor_db: float) -> np.ndarray:
    mag = np.maximum(mag, 10 ** (floor_db / 20.0))
    return 20.0 * np.log10(mag)


def _biquad_filt(x: np.ndarray, b, a) -> np.ndarray:
    from scipy.signal import lfilter

    return lfilter(b, a, x)


def k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    """ITU-R BS.1770 K-weighting (pre-filter shelf + RLB highpass)."""

    # Stage 1: high shelf ~4 dB, f0 ≈ 1681.97 Hz
    f0 = 1681.974450955533
    G = 3.999843853973347
    Q = 0.7071752369554196
    K = np.tan(np.pi * f0 / sr)
    Vh = 10 ** (G / 20.0)
    Vb = Vh**0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    b0 = (Vh + Vb * K / Q + K * K) / a0
    b1 = 2.0 * (K * K - Vh) / a0
    b2 = (Vh - Vb * K / Q + K * K) / a0
    a1 = 2.0 * (K * K - 1.0) / a0
    a2 = (1.0 - K / Q + K * K) / a0
    y = _biquad_filt(x, [b0, b1, b2], [1.0, a1, a2])

    # Stage 2: highpass f0 ≈ 38.135 Hz
    f0 = 38.13547087602444
    Q = 0.5003270373238773
    K = np.tan(np.pi * f0 / sr)
    a0 = 1.0 + K / Q + K * K
    b0 = 1.0 / a0
    b1 = -2.0 / a0
    b2 = 1.0 / a0
    a1 = 2.0 * (K * K - 1.0) / a0
    a2 = (1.0 - K / Q + K * K) / a0
    return _biquad_filt(y, [b0, b1, b2], [1.0, a1, a2])


def lufs_stats(x: np.ndarray, sr: int) -> dict:
    """Gated integrated LUFS + max momentary (400 ms) + max short-term (3 s)."""
    if x.size < sr // 10:
        return {
            "integrated_lufs": None,
            "momentary_max_lufs": None,
            "short_term_max_lufs": None,
        }
    y = k_weight(x, sr)
    hop = max(1, int(sr * 0.1))  # 100 ms
    block = max(1, int(sr * 0.4))  # 400 ms momentary
    n = len(y)
    ms = []
    i = 0
    while i + block <= n:
        sl = y[i : i + block]
        ms.append(float(np.mean(sl * sl)))
        i += hop
    if not ms:
        return {
            "integrated_lufs": None,
            "momentary_max_lufs": None,
            "short_term_max_lufs": None,
        }
    ms = np.asarray(ms)
    lufs_m = -0.691 + 10.0 * np.log10(np.maximum(ms, 1e-12))
    # absolute gate -70 LUFS
    abs_ok = lufs_m > -70.0
    if not np.any(abs_ok):
        integ = None
    else:
        gated = ms[abs_ok]
        rel = -0.691 + 10.0 * np.log10(np.mean(gated)) - 10.0
        rel_ok = abs_ok & (lufs_m > rel)
        use = ms[rel_ok] if np.any(rel_ok) else gated
        integ = float(-0.691 + 10.0 * np.log10(np.mean(use)))
    # short-term 3 s = 30 overlapping 100 ms hops of 400 ms? Use 3 s windows of y
    st_block = max(1, int(sr * 3.0))
    st = []
    i = 0
    st_hop = hop
    while i + st_block <= n:
        sl = y[i : i + st_block]
        st.append(float(-0.691 + 10.0 * np.log10(max(np.mean(sl * sl), 1e-12))))
        i += st_hop
    return {
        "integrated_lufs": integ,
        "momentary_max_lufs": float(np.max(lufs_m)),
        "short_term_max_lufs": float(np.max(st)) if st else None,
    }


def _fmt(v, unit: str, nd: int = 1) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{v:.{nd}f} {unit}"


def plot_png(
    t: np.ndarray,
    peak: np.ndarray,
    rms: np.ndarray,
    stats: dict,
    title: str,
    out: Path,
    floor_db: float,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "Segoe UI", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    duration = float(t[-1]) if t.size else 0.0
    width = min(28.0, max(14.0, duration / 12.0))
    fig = plt.figure(figsize=(width, 5.2), facecolor="#12141a")
    gs = fig.add_gridspec(2, 1, height_ratios=[1.7, 1.0], hspace=0.08)
    ax_bar = fig.add_subplot(gs[0])
    ax_db = fig.add_subplot(gs[1], sharex=ax_bar)

    for ax in (ax_bar, ax_db):
        ax.set_facecolor("#1a1d24")
        ax.tick_params(colors="#c5c9d1", labelsize=8)
        for sp in ax.spines.values():
            sp.set_color("#2c313c")
        ax.grid(True, color="#2c313c", linewidth=0.6, alpha=0.9)

    # Lane 1: SV-style audio bars (linear amplitude = visual width)
    ax_bar.fill_between(t, -peak, peak, color="#d7dde8", linewidth=0, alpha=0.92, zorder=2)
    ax_bar.fill_between(t, -rms, rms, color="#5ee0a8", linewidth=0, alpha=0.75, zorder=3)
    ax_bar.axhline(0, color="#3a4050", linewidth=0.6, zorder=1)
    ymax = max(1.02, float(np.max(peak)) * 1.06 if peak.size else 1.02)
    ax_bar.set_ylim(-ymax, ymax)
    ax_bar.set_ylabel("bar width\n(peak / RMS)", color="#c5c9d1", fontsize=8)
    ax_bar.set_yticks([-1, -0.5, 0, 0.5, 1])
    ax_bar.set_yticklabels(["−1", "−0.5", "0", "0.5", "1"])
    plt.setp(ax_bar.get_xticklabels(), visible=False)

    # Lane 2: RMS dBFS
    rms_db = db_fs(rms, floor_db)
    ax_db.fill_between(t, floor_db, rms_db, color="#5ee0a8", alpha=0.35, linewidth=0)
    ax_db.plot(t, rms_db, color="#5ee0a8", linewidth=0.9)
    for y, c, lab in ((-6, "#e85d5d", "−6"), (-12, "#e0b25e", "−12"), (-18, "#6aa6e8", "−18")):
        ax_db.axhline(y, color=c, linewidth=0.7, alpha=0.7, linestyle="--")
        ax_db.text(
            0.002,
            y + 0.4,
            lab,
            transform=ax_db.get_yaxis_transform(),
            color=c,
            fontsize=7,
            va="bottom",
        )
    ax_db.set_ylim(floor_db, 0)
    ax_db.set_ylabel("RMS dBFS", color="#c5c9d1", fontsize=8)
    ax_db.set_xlabel("time (s)", color="#c5c9d1", fontsize=8)

    head = (
        f"{title}   ·   peak {_fmt(stats['peak_db'], 'dBFS')}   ·   "
        f"RMS {_fmt(stats['rms_db'], 'dBFS')}   ·   "
        f"LUFS {_fmt(stats['integrated_lufs'], 'LUFS')}   ·   "
        f"Mmax {_fmt(stats['momentary_max_lufs'], 'LUFS')}   ·   "
        f"STmax {_fmt(stats['short_term_max_lufs'], 'LUFS')}"
    )
    fig.suptitle(head, color="#eef1f6", fontsize=10, x=0.01, ha="left", y=0.98)
    fig.text(
        0.01,
        0.015,
        "white = peak bar width (SV-style)    green = RMS    fatter = louder",
        color="#8b909a",
        fontsize=7.5,
    )
    fig.subplots_adjust(left=0.06, right=0.995, top=0.90, bottom=0.10)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)


def write_html(
    t: np.ndarray,
    peak: np.ndarray,
    rms: np.ndarray,
    stats: dict,
    title: str,
    out: Path,
    floor_db: float,
) -> None:
    payload = {
        "title": title,
        "t": [round(float(v), 4) for v in t],
        "peak": [round(float(v), 5) for v in peak],
        "rms": [round(float(v), 5) for v in rms],
        "floor_db": floor_db,
        "stats": stats,
    }
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"/>
<title>{_esc(title)} · ACE loudness bars</title>
<style>
  html,body {{ margin:0; height:100%; background:#12141a; color:#eef1f6;
    font:13px/1.4 "Segoe UI","Microsoft YaHei",sans-serif; }}
  header {{ padding:10px 14px 6px; }}
  header h1 {{ font-size:14px; font-weight:600; margin:0 0 4px; }}
  header p {{ margin:0; color:#8b909a; font-size:12px; }}
  #c {{ display:block; width:100%; height:calc(100% - 64px); cursor:grab; }}
  #tip {{ position:fixed; pointer-events:none; background:#1a1d24; border:1px solid #2c313c;
    padding:4px 8px; font-size:12px; display:none; }}
</style></head><body>
<header>
  <h1>{_esc(title)}</h1>
  <p>滚轮缩放 · 拖动平移 · 越胖越响（白=peak 绿=RMS） · peak {stats.get('peak_db')} dBFS ·
     RMS {stats.get('rms_db')} dBFS · LUFS {stats.get('integrated_lufs')}</p>
</header>
<canvas id="c"></canvas>
<div id="tip"></div>
<script>
const D = {json.dumps(payload, ensure_ascii=False)};
const c = document.getElementById('c');
const tip = document.getElementById('tip');
const ctx = c.getContext('2d');
let x0 = 0, x1 = D.t.length ? D.t[D.t.length-1] : 1;
let drag = null;
function resize() {{
  const r = c.getBoundingClientRect();
  c.width = r.width * devicePixelRatio;
  c.height = r.height * devicePixelRatio;
  ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
  draw();
}}
function xToPx(t) {{
  const w = c.getBoundingClientRect().width;
  return (t - x0) / (x1 - x0) * w;
}}
function pxToT(px) {{
  const w = c.getBoundingClientRect().width;
  return x0 + px / w * (x1 - x0);
}}
function db(v) {{
  const f = Math.pow(10, D.floor_db/20);
  return 20*Math.log10(Math.max(v, f));
}}
function draw() {{
  const w = c.getBoundingClientRect().width;
  const h = c.getBoundingClientRect().height;
  ctx.clearRect(0,0,w,h);
  const split = h * 0.62;
  ctx.fillStyle = '#1a1d24';
  ctx.fillRect(0,0,w,h);
  // grid
  ctx.strokeStyle = '#2c313c';
  ctx.lineWidth = 1;
  const dur = x1-x0;
  const step = dur > 60 ? 10 : dur > 20 ? 5 : dur > 8 ? 2 : 1;
  ctx.fillStyle = '#8b909a';
  ctx.font = '11px sans-serif';
  for (let t = Math.ceil(x0/step)*step; t < x1; t += step) {{
    const x = xToPx(t);
    ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,h); ctx.stroke();
    ctx.fillText(t.toFixed(0)+'s', x+3, h-6);
  }}
  const mid = split/2;
  ctx.strokeStyle = '#3a4050';
  ctx.beginPath(); ctx.moveTo(0,mid); ctx.lineTo(w,mid); ctx.stroke();
  // bars
  ctx.beginPath();
  for (let i=0;i<D.t.length;i++) {{
    const x = xToPx(D.t[i]);
    const y = mid - D.peak[i]*mid*0.95;
    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }}
  for (let i=D.t.length-1;i>=0;i--) {{
    const x = xToPx(D.t[i]);
    const y = mid + D.peak[i]*mid*0.95;
    ctx.lineTo(x,y);
  }}
  ctx.closePath();
  ctx.fillStyle = '#d7dde8';
  ctx.fill();
  ctx.beginPath();
  for (let i=0;i<D.t.length;i++) {{
    const x = xToPx(D.t[i]);
    const y = mid - D.rms[i]*mid*0.95;
    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }}
  for (let i=D.t.length-1;i>=0;i--) {{
    const x = xToPx(D.t[i]);
    const y = mid + D.rms[i]*mid*0.95;
    ctx.lineTo(x,y);
  }}
  ctx.closePath();
  ctx.fillStyle = 'rgba(94,224,168,0.75)';
  ctx.fill();
  // dB lane
  const dbTop = split, dbH = h-split-18;
  const floor = D.floor_db;
  function dbY(v) {{ return dbTop + (0-v)/(0-floor)*dbH; }}
  ctx.fillStyle = 'rgba(94,224,168,0.3)';
  ctx.beginPath();
  ctx.moveTo(xToPx(D.t[0]||0), dbY(floor));
  for (let i=0;i<D.t.length;i++) ctx.lineTo(xToPx(D.t[i]), dbY(db(D.rms[i])));
  ctx.lineTo(xToPx(D.t[D.t.length-1]||0), dbY(floor));
  ctx.closePath(); ctx.fill();
  ctx.strokeStyle = '#5ee0a8'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  for (let i=0;i<D.t.length;i++) {{
    const y = dbY(db(D.rms[i]));
    if (i===0) ctx.moveTo(xToPx(D.t[i]), y); else ctx.lineTo(xToPx(D.t[i]), y);
  }}
  ctx.stroke();
  [[-6,'#e85d5d'],[-12,'#e0b25e'],[-18,'#6aa6e8']].forEach(([lv,col]) => {{
    ctx.strokeStyle = col; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(0, dbY(lv)); ctx.lineTo(w, dbY(lv)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = col; ctx.fillText(lv+' dB', 6, dbY(lv)-3);
  }});
}}
c.addEventListener('wheel', ev => {{
  ev.preventDefault();
  const rect = c.getBoundingClientRect();
  const t = pxToT(ev.clientX - rect.left);
  const f = ev.deltaY < 0 ? 0.8 : 1.25;
  let n0 = t - (t-x0)*f, n1 = t + (x1-t)*f;
  const tot = D.t.length ? D.t[D.t.length-1] : 1;
  const minSpan = 0.4;
  if (n1-n0 < minSpan) {{ const m=(n0+n1)/2; n0=m-minSpan/2; n1=m+minSpan/2; }}
  if (n0 < 0) {{ n1 -= n0; n0 = 0; }}
  if (n1 > tot) {{ n0 -= (n1-tot); n1 = tot; }}
  x0 = Math.max(0, n0); x1 = Math.min(tot, n1);
  draw();
}}, {{passive:false}});
c.addEventListener('mousedown', ev => {{
  drag = {{ x: ev.clientX, x0, x1 }};
  c.style.cursor = 'grabbing';
}});
window.addEventListener('mouseup', () => {{ drag=null; c.style.cursor='grab'; }});
window.addEventListener('mousemove', ev => {{
  const rect = c.getBoundingClientRect();
  const px = ev.clientX - rect.left;
  const t = pxToT(px);
  let i = 0, best=1e9;
  for (let k=0;k<D.t.length;k++) {{
    const d = Math.abs(D.t[k]-t); if (d<best) {{ best=d; i=k; }}
  }}
  tip.style.display = 'block';
  tip.style.left = (ev.clientX+12)+'px';
  tip.style.top = (ev.clientY+12)+'px';
  const pdb = db(D.peak[i]), rdb = db(D.rms[i]);
  tip.textContent = t.toFixed(2)+'s  peak '+pdb.toFixed(1)+' dB  RMS '+rdb.toFixed(1)+' dB';
  if (!drag) return;
  const w = rect.width;
  const dt = (drag.x - ev.clientX) / w * (drag.x1 - drag.x0);
  const tot = D.t.length ? D.t[D.t.length-1] : 1;
  let n0 = drag.x0 + dt, n1 = drag.x1 + dt;
  if (n0 < 0) {{ n1 -= n0; n0 = 0; }}
  if (n1 > tot) {{ n0 -= (n1-tot); n1 = tot; }}
  x0 = Math.max(0, n0); x1 = Math.min(tot, n1);
  draw();
}});
window.addEventListener('resize', resize);
resize();
</script></body></html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def summarize(x: np.ndarray, lufs: dict) -> dict:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    peak_db = 20.0 * np.log10(max(peak, 1e-12))
    rms_db = 20.0 * np.log10(max(rms, 1e-12))
    out = {
        "peak_db": round(peak_db, 2),
        "rms_db": round(rms_db, 2),
        "crest_db": round(peak_db - rms_db, 2),
        "integrated_lufs": None
        if lufs.get("integrated_lufs") is None
        else round(float(lufs["integrated_lufs"]), 2),
        "momentary_max_lufs": None
        if lufs.get("momentary_max_lufs") is None
        else round(float(lufs["momentary_max_lufs"]), 2),
        "short_term_max_lufs": None
        if lufs.get("short_term_max_lufs") is None
        else round(float(lufs["short_term_max_lufs"]), 2),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SV-style loudness audio-bar plot")
    p.add_argument("audio", type=Path)
    p.add_argument("-o", "--png", type=Path, default=None)
    p.add_argument("--html", type=Path, default=None)
    p.add_argument("--json", type=Path, default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--hop-ms", type=float, default=10.0)
    p.add_argument("--floor-db", type=float, default=DEFAULT_FLOOR_DB)
    p.add_argument("--ffmpeg", default=None)
    p.add_argument("--from-s", type=float, default=None)
    p.add_argument("--to-s", type=float, default=None)
    args = p.parse_args(argv)

    sr, x = load_audio(args.audio, args.ffmpeg)
    t0 = 0.0
    if args.from_s is not None or args.to_s is not None:
        t0 = 0.0 if args.from_s is None else max(0.0, float(args.from_s))
        a = 0 if args.from_s is None else max(0, int(args.from_s * sr))
        b = len(x) if args.to_s is None else min(len(x), int(args.to_s * sr))
        x = x[a:b]
    t, peak, rms = envelopes(x, sr, args.hop_ms)
    if t0:
        t = t + t0
    lufs = lufs_stats(x, sr)
    stats = summarize(x, lufs)
    stats["duration_s"] = round(len(x) / sr, 3)
    stats["sample_rate"] = sr
    stats["source"] = str(args.audio)

    title = args.title or args.audio.stem
    png = args.png
    if png is None and args.html is None:
        png = args.audio.with_name(args.audio.stem + "_loudness.png")
    if png is not None:
        plot_png(t, peak, rms, stats, title, png, args.floor_db)
    if args.html is not None:
        write_html(t, peak, rms, stats, title, args.html, args.floor_db)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"png": str(png) if png else None, "html": str(args.html) if args.html else None, **stats}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        raise
