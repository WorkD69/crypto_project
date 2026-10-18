"""One-shot local Ollama content test. Reads input only; stores no media or messages."""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

import requests


MODEL = "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path)
    parser.add_argument("--text-stdin", action="store_true")
    args = parser.parse_args()
    content = (sys.stdin.read() if args.text_stdin else "").strip()
    if not content and not args.image:
        parser.error("Provide --image and/or --text-stdin")
    prompt = ("Извлеки только явно видимые/написанные торговые термины, актив, таймфрейм, "
              "ценовые зоны, направление и условные правила. Отдельно укажи неясное. "
              "Не создавай торговый сигнал, не додумывай уровни. Ответ кратко на русском.\n")
    message = {"role": "user", "content": prompt + content}
    if args.image:
        message["images"] = [base64.b64encode(args.image.read_bytes()).decode("ascii")]
    started = time.perf_counter()
    response = requests.post("http://127.0.0.1:11434/api/chat", json={
        "model": MODEL, "stream": False, "think": False,
        "options": {"num_ctx": 8192, "num_predict": 500, "temperature": 0},
        "messages": [message],
    }, timeout=180)
    response.raise_for_status()
    data = response.json()
    print(json.dumps({"model": data.get("model"),
                      "seconds": round(time.perf_counter() - started, 2),
                      "prompt_tokens": data.get("prompt_eval_count"),
                      "output_tokens": data.get("eval_count"),
                      "answer": data.get("message", {}).get("content", "")},
                     ensure_ascii=True))


if __name__ == "__main__":
    main()
