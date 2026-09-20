#!/usr/bin/env python3
"""
Voice Studio — turns rough mic audio in a video into a cleaner, more
"studio" sounding voice track, using an ffmpeg filter chain:

  1. highpass       — remove rumble / handling noise below the voice band
  2. afftdn         — broadband noise reduction (hiss, fan, room noise)
  3. equalizer x3   — carve mud, add presence, add "air"
  4. deesser        — tame harsh sibilance ("s"/"sh" sounds)
  5. acompressor    — even out level swings like a studio compressor
  6. alimiter        — safety ceiling so nothing clips
  7. loudnorm (2-pass) — normalize to a broadcast-consistent loudness target

The video stream is stream-copied (never re-encoded), so picture quality
and file size stay untouched — only the audio changes.
"""

import json
import re
import subprocess
import shutil
import os
import sys
from dataclasses import dataclass, field

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class EnhanceError(RuntimeError):
    pass


@dataclass
class Preset:
    key: str
    label: str
    description: str
    # noise reduction strength in dB (afftdn "nr"): higher = more aggressive
    nr: float
    # loudness target in LUFS integrated
    lufs: float = -16.0
    lra: float = 11.0
    tp: float = -1.5
    # compressor settings
    comp_threshold: float = 0.10   # linear (~ -20dB)
    comp_ratio: float = 3.0
    comp_attack: float = 5.0       # ms
    comp_release: float = 60.0     # ms
    comp_makeup: float = 2.0
    # EQ (frequency Hz, gain dB, width Hz or Q depending on filter)
    hp_freq: int = 90
    mud_freq: int = 250
    mud_gain: float = -3.0
    presence_freq: int = 3500
    presence_gain: float = 3.0
    air_freq: int = 10000
    air_gain: float = 2.0
    deess_intensity: float = 0.4   # 0-1


PRESETS: dict[str, Preset] = {
    "light": Preset(
        key="light", label="Light Clean-up",
        description="Subtle noise reduction and gentle polish for already-decent audio.",
        nr=6, comp_ratio=2.0, presence_gain=1.5, air_gain=1.0, deess_intensity=0.25,
    ),
    "studio": Preset(
        key="studio", label="Studio Voice (recommended)",
        description="Balanced noise removal, warmth, and presence for a clean, professional voice.",
        nr=12, comp_ratio=3.0, presence_gain=3.0, air_gain=2.0, deess_intensity=0.4,
    ),
    "podcast": Preset(
        key="podcast", label="Podcast / YouTube Loud",
        description="Studio Voice processing plus louder, tighter dynamics for talking-head content.",
        nr=12, lufs=-14.0, comp_ratio=4.0, comp_threshold=0.08, presence_gain=3.5,
        air_gain=2.0, deess_intensity=0.45,
    ),
    "aggressive": Preset(
        key="aggressive", label="Heavy Noise Removal",
        description="Strong denoising for noisy rooms, fans, or street recordings. Can sound slightly processed.",
        nr=20, comp_ratio=3.5, presence_gain=2.5, air_gain=1.5, deess_intensity=0.5,
    ),
}

DEFAULT_PRESET = "studio"


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise EnhanceError(f"Command failed ({' '.join(cmd[:3])}...):\n{proc.stdout[-4000:]}")
    return proc.stdout


def probe_has_audio(path: str) -> bool:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return bool(out.stdout.strip())


def probe_duration(path: str) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def build_pre_loudnorm_chain(p: Preset) -> str:
    """Filter chain applied before the loudness-normalization stage."""
    parts = [
        f"highpass=f={p.hp_freq}",
        f"afftdn=nr={p.nr}:nf=-25",
        f"equalizer=f={p.mud_freq}:t=q:w=1.0:g={p.mud_gain}",
        f"equalizer=f={p.presence_freq}:t=q:w=1.2:g={p.presence_gain}",
        f"equalizer=f={p.air_freq}:t=q:w=0.8:g={p.air_gain}",
        f"deesser=i={p.deess_intensity}:m=0.5:f=0.5:s=o",
        (
            f"acompressor=threshold={p.comp_threshold}:ratio={p.comp_ratio}:"
            f"attack={p.comp_attack}:release={p.comp_release}:makeup={p.comp_makeup}"
        ),
        "alimiter=limit=0.97:attack=5:release=50",
    ]
    return ",".join(parts)


def measure_loudness(input_path: str, pre_chain: str, preset: Preset) -> dict:
    """First pass: run the enhancement chain + loudnorm in measure mode to
    get the actual input loudness stats, for an accurate second pass."""
    filt = (
        f"{pre_chain},"
        f"loudnorm=I={preset.lufs}:LRA={preset.lra}:TP={preset.tp}:print_format=json"
    )
    cmd = [FFMPEG, "-y", "-i", input_path, "-af", filt, "-f", "null", "-"]
    out = _run(cmd)
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.DOTALL)
    if not match:
        raise EnhanceError("Could not measure loudness (loudnorm pass 1 produced no stats).")
    return json.loads(match.group(0))


def enhance_video(
    input_path: str,
    output_path: str,
    preset_key: str = DEFAULT_PRESET,
    progress_cb=None,
) -> dict:
    """Run the full two-pass enhancement pipeline. Returns a dict of
    before/after loudness stats. Raises EnhanceError on failure."""
    if preset_key not in PRESETS:
        raise EnhanceError(f"Unknown preset '{preset_key}'")
    preset = PRESETS[preset_key]

    if not probe_has_audio(input_path):
        raise EnhanceError("This video has no audio track to enhance.")

    if progress_cb:
        progress_cb("analyzing", 10)

    pre_chain = build_pre_loudnorm_chain(preset)
    stats = measure_loudness(input_path, pre_chain, preset)

    if progress_cb:
        progress_cb("processing", 40)

    final_filt = (
        f"{pre_chain},"
        f"loudnorm=I={preset.lufs}:LRA={preset.lra}:TP={preset.tp}:"
        f"measured_I={stats['input_i']}:measured_LRA={stats['input_lra']}:"
        f"measured_TP={stats['input_tp']}:measured_thresh={stats['input_thresh']}:"
        f"offset={stats.get('target_offset', 0)}:linear=true:print_format=json"
    )

    cmd = [
        FFMPEG, "-y", "-i", input_path,
        "-map", "0:v:0?", "-map", "0:a:0",
        "-c:v", "copy",
        "-af", final_filt,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ]
    out = _run(cmd)

    if progress_cb:
        progress_cb("finalizing", 90)

    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out, re.DOTALL)
    final_stats = json.loads(match.group(0)) if match else {}

    if progress_cb:
        progress_cb("done", 100)

    return {
        "preset": preset.label,
        "before": {
            "integrated_lufs": float(stats.get("input_i", 0)),
            "true_peak_dbtp": float(stats.get("input_tp", 0)),
            "loudness_range": float(stats.get("input_lra", 0)),
        },
        "after": {
            "integrated_lufs": float(final_stats.get("output_i", preset.lufs)),
            "true_peak_dbtp": float(final_stats.get("output_tp", preset.tp)),
            "loudness_range": float(final_stats.get("output_lra", preset.lra)),
        },
        "target_lufs": preset.lufs,
    }


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Enhance the voice audio in a video.")
    ap.add_argument("input", help="input video file")
    ap.add_argument("output", help="output video file")
    ap.add_argument("--preset", choices=list(PRESETS), default=DEFAULT_PRESET)
    args = ap.parse_args()

    def cb(stage, pct):
        print(f"[{pct:3d}%] {stage}", file=sys.stderr)

    result = enhance_video(args.input, args.output, args.preset, progress_cb=cb)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
