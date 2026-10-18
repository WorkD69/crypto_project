"""Local, one-shot ffmpeg + faster-whisper test for a short permitted video."""
import argparse
import json
import subprocess
import time
from pathlib import Path

from faster_whisper import WhisperModel


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path, nargs="?", default=ROOT / "artifacts/test_video.mp4")
    parser.add_argument("--language", default="en")
    args = parser.parse_args()
    out = ROOT / "artifacts" / "video_probe"
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    audio = out / "audio.wav"
    run("-i", str(args.video), "-vn", "-ac", "1", "-ar", "16000", str(audio))
    frame_times = [1, 5, 8]
    frames = []
    for second in frame_times:
        path = out / f"frame_{second:03d}.jpg"
        run("-ss", str(second), "-i", str(args.video), "-frames:v", "1", str(path))
        frames.append(str(path))
    extract_seconds = time.perf_counter() - started
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4,
                         download_root=str(ROOT / "artifacts/models"), local_files_only=True)
    segments, info = model.transcribe(str(audio), language=args.language, beam_size=1)
    aligned = []
    for segment in segments:
        midpoint = (segment.start + segment.end) / 2
        second = min(frame_times, key=lambda t: abs(t - midpoint))
        aligned.append({"start": round(segment.start, 2), "end": round(segment.end, 2),
                        "frame_second": second, "text": segment.text.strip()})
    print(json.dumps({"input_bytes": args.video.stat().st_size,
                      "audio_bytes": audio.stat().st_size,
                      "frames": frames, "language": info.language,
                      "segments": aligned, "extract_seconds": round(extract_seconds, 2),
                      "total_seconds": round(time.perf_counter() - started, 2)},
                     ensure_ascii=True))


if __name__ == "__main__":
    main()
