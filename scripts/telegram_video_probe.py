"""Local-only video probe for a specifically permitted file; no Telegram credentials."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from faster_whisper import WhisperModel


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "mvp_02_03" / "video_probe"


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--seconds", type=int, default=240)
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error("Video file not found")
    OUT.mkdir(parents=True, exist_ok=True)
    audio = OUT / "audio_16khz.wav"
    ffmpeg("-i", str(args.video), "-t", str(args.seconds), "-vn", "-ac", "1",
           "-ar", "16000", str(audio))
    frame_seconds = [15, 60, 120, 180, 225]
    frames = []
    for second in frame_seconds:
        if second >= args.seconds:
            continue
        target = OUT / f"frame_{second:03d}.jpg"
        ffmpeg("-ss", str(second), "-i", str(args.video), "-frames:v", "1", str(target))
        frames.append({"second": second, "path": str(target)})
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=4,
                         download_root=str(ROOT / "artifacts" / "models"))
    segments, info = model.transcribe(str(audio), language="ru", beam_size=3,
                                      vad_filter=True)
    aligned = []
    for segment in segments:
        midpoint = (segment.start + segment.end) / 2
        nearest = min(frames, key=lambda f: abs(f["second"] - midpoint))
        aligned.append({"start": round(segment.start, 1), "end": round(segment.end, 1),
                        "nearest_frame_second": nearest["second"],
                        "text": segment.text.strip()})
    print(json.dumps({"input_bytes": args.video.stat().st_size,
                      "audio_bytes": audio.stat().st_size, "language": info.language,
                      "model": args.model, "frames": frames, "segments": aligned},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
