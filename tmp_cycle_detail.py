import ast
from pathlib import Path

for idx, line in enumerate(Path("runtime.log").read_text().splitlines(), 1):
    if line.startswith("single-loop cycle: "):
        payload = ast.literal_eval(line.split(": ", 1)[1])
        print(f"cycle line {idx}")
        prog = payload.get("progressive")
        print(f"  progressive: {prog}")
        arbi = payload["arbi"]
        print(f"  arbi why: {arbi['why']}")
