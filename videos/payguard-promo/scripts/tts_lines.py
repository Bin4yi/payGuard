"""Synthesize each SCRIPT.md line to assets/vo/lineNN.wav with local Kokoro,
then write assets/vo/durations.json (seconds per line).

Calls the hyperframes CLI's own Kokoro synth script directly with the cached
model: the CLI's 10-second `import kokoro_onnx` probe times out on a cold
import and falsely reports the package missing.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "vo"
OUT.mkdir(parents=True, exist_ok=True)
CACHE = Path.home() / ".cache" / "hyperframes" / "tts"
SYNTH, MODEL, VOICES = CACHE / "synth-v3.py", CACHE / "models" / "kokoro-v1.0.onnx", CACHE / "voices" / "voices-v1.0.bin"
VOICE, SPEED = "af_heart", "0.95"

script = (ROOT / "SCRIPT.md").read_text(encoding="utf-8")
lines = [m.group(1).strip() for m in re.finditer(r"^    (\S.*)$", script, re.M)]
only = {int(a) for a in sys.argv[1:]}

durations = {}
for i, text in enumerate(lines, 1):
    wav = OUT / f"line{i:02d}.wav"
    if not only or i in only:
        subprocess.run([sys.executable, str(SYNTH), str(MODEL), str(VOICES), text, VOICE, SPEED, str(wav), "en-us"],
                       check=True, capture_output=True)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
                         capture_output=True, text=True, check=True).stdout.strip()
    durations[f"line{i:02d}"] = round(float(dur), 3)
    print(f"line{i:02d} {durations[f'line{i:02d}']:6.2f}s  {text[:60]}", flush=True)

(OUT / "durations.json").write_text(json.dumps(durations, indent=2), encoding="utf-8")
print("total", round(sum(durations.values()), 1))
