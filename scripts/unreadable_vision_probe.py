"""Generate an intentionally under-specified original chart and ask local vision."""
import base64
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests

path = Path(__file__).resolve().parents[1] / "artifacts" / "unreadable_test.png"
path.parent.mkdir(exist_ok=True)
fig, ax = plt.subplots(figsize=(2, 1), dpi=80)
ax.plot([1, 2, 3, 4], [2, 4, 3, 5], color="black")
ax.axhspan(2.5, 3.5, color="blue", alpha=.2)
ax.set_xticks([])
ax.set_yticks([])
ax.set_title("chart", fontsize=4)
fig.savefig(path)
plt.close(fig)
image = base64.b64encode(path.read_bytes()).decode("ascii")
data = requests.post("http://127.0.0.1:11434/api/chat", json={
    "model": "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "think": False, "stream": False,
    "options": {"num_predict": 180, "temperature": 0},
    "messages": [{"role": "user", "content": "What exact asset, timeframe, and zone price bounds are printed in this image? If any are absent or unreadable, say so. Do not guess.", "images": [image]}]
}, timeout=90).json()
print(json.dumps({"image_bytes": path.stat().st_size,
                  "response": data.get("message", {}).get("content", "")[:1000],
                  "prompt_eval_count": data.get("prompt_eval_count"), "eval_count": data.get("eval_count")}, ensure_ascii=True))
