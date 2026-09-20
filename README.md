# Voice Studio

A local tool that takes a pre-recorded video shot with a mini/lav mic and
makes the voice sound closer to a studio recording: less hiss and room
noise, tighter dynamics, more presence and warmth, and a consistent,
broadcast-standard loudness. The picture is never touched or re-encoded —
only the audio track changes.

## What it does to the audio

1. **High-pass filter** — removes rumble, handling noise, and AC hum below the voice band.
2. **Noise reduction (`afftdn`)** — removes broadband hiss, fan noise, room tone.
3. **3-band EQ** — cuts boxy "mud" around 250 Hz, adds vocal presence around 3.5 kHz, adds "air" around 10 kHz.
4. **De-esser** — tames harsh "s"/"sh" sibilance that mini mics tend to exaggerate.
5. **Compressor** — evens out volume swings the way a studio compressor/limiter chain does, so quiet and loud words sit closer together.
6. **Limiter** — a safety ceiling so nothing clips.
7. **Two-pass loudness normalization (EBU R128 / `loudnorm`)** — brings the final track to a consistent, professional loudness target (e.g. -16 LUFS for YouTube/podcasts, -14 LUFS for social).

## Presets

| Preset | Best for |
|---|---|
| Light Clean-up | Audio that's already decent — just needs polish |
| **Studio Voice** (default) | Most talking-head / interview / vlog footage |
| Podcast / YouTube Loud | Louder, punchier levels for spoken-word platforms |
| Heavy Noise Removal | Noisy rooms, fans, street/outdoor recordings |

## Requirements

- Python 3.10+
- `ffmpeg` on your PATH (with the standard filters — most builds have them)

## Running it

```bash
cd voice-studio
pip install -r requirements.txt
python3 app.py
```

Then open **http://127.0.0.1:7860** in your browser. Drag in a video,
pick a preset, click **Enhance Voice**, and download the result when it's
done. Everything runs locally — nothing leaves your machine.

## Command-line use

You can also run the enhancer directly without the web UI:

```bash
python3 enhance.py input.mp4 output.mp4 --preset studio
```

Presets: `light`, `studio`, `podcast`, `aggressive`.

## Notes

- Large/long videos take roughly real-time or a bit less to process (ffmpeg does two passes over the audio; the video stream is only copied, not re-encoded, so it's fast).
- If a preset over-processes a particular voice or room (e.g. sounds a little "underwater" or robotic), try a lighter preset — heavier noise reduction always trades off some naturalness.
- The tool re-encodes the audio track to AAC 192kbps/48kHz; the video stream is copied bit-for-bit, so there's no quality loss or long re-render on the picture.
