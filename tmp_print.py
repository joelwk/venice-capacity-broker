from pathlib import Path
lines = Path("runtime.log").read_text().splitlines()
for i, line in enumerate(lines, 1):
    print(f"{i:03}: {line}")
