"""Write build/compile_commands.json for the demo (useful on Windows without CMake/Ninja).

Usage:  python tools/gen_compile_commands.py --compiler clang++
Then point the extractor's --build-directory at the build folder and --repository-root at this folder.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler", default="clang++")
    parser.add_argument("--std", default="c++17")
    parser.add_argument("--define", action="append", default=[], help="extra -D macros, e.g. --define _WIN32")
    args = parser.parse_args()

    entries = []
    for source in sorted((ROOT / "src").rglob("*.cpp")):
        flags = " ".join(f"-D{d}" for d in args.define)
        entries.append({
            "directory": str(ROOT),
            "file": str(source),
            "command": f"{args.compiler} -std={args.std} {flags} -I{ROOT / 'src'} -c {source}".replace("  ", " "),
        })
    out = ROOT / "build" / "compile_commands.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    print(f"Wrote {out} with {len(entries)} translation units.")


if __name__ == "__main__":
    main()
