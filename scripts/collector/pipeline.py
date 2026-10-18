"""One explicit own-test handoff to the existing one-shot Qwen probe."""
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.collector.config import ROOT


def pipeline_input(item, root):
    if not item or item["source_key"] != "saved" or not item["text"].startswith("CRP-STAGE0"):
        raise ValueError("Stage 0 pipeline bridge accepts only own marked test messages")
    image = None
    for media in item["media"]:
        if media.get("state") == "DOWNLOADED" and media.get("kind") == "photo":
            candidate = (Path(root) / media["path"]).resolve()
            if not candidate.is_relative_to(Path(root).resolve()):
                raise ValueError("Media path escapes runtime")
            if candidate.is_file() and candidate.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                image = candidate
                break
    return item["text"], image


def run_pipeline(store, root, source, message_id):
    if not message_id:
        raise ValueError("--message-id is required")
    item = store.get(source, message_id)
    text, image = pipeline_input(item, root)
    target = Path(root) / "pipeline" / f"{source}-{message_id}-v{item['version']}.json"
    if target.exists():
        cached = json.loads(target.read_text(encoding="utf-8"))
        if cached.get("state") == "PASS":
            return {**{k: v for k, v in cached.items() if k != "output"}, "cached": True}
    command = [sys.executable, str(ROOT / "scripts" / "local_content_probe.py"), "--text-stdin"]
    if image:
        command += ["--image", str(image)]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    completed = subprocess.run(command, input=text, text=True, encoding="utf-8",
                               capture_output=True, timeout=210, env=env, cwd=ROOT)
    result = {"state": "PASS" if completed.returncode == 0 else "WAITING_LOCAL_MODEL",
              "processing_state": "NEEDS_REVIEW", "source": source, "message_id": message_id,
              "version": item["version"], "input_sha256": item["content_sha256"],
              "pipeline": "scripts/local_content_probe.py", "image_included": image is not None,
              "hermes_calls": 0, "plus_calls": 0}
    if completed.returncode == 0:
        result["output"] = json.loads(completed.stdout)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    # The model answer stays in the private runtime, never in command logs.
    return {k: v for k, v in result.items() if k != "output"}
