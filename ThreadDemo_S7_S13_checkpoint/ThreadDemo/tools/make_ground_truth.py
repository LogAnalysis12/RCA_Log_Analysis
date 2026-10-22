"""Build ground_truth.json from the '// @GT' markers in src/ plus tools/scenarios_meta.json.

Marker syntax (put it on the line ABOVE the statement it describes):
    // @GT <scenario> <role> <qualified function> <KIND> <target> [key=value ...]
Roles: victim, culprit, culprit_aux, alloc, spawn, lock, missing_lock
Kinds: READ, WRITE, DELETE, CLEAR, NEW, SPAWN, LOCK, NO_LOCK, RETURN_EMPTY

Usage:  python tools/make_ground_truth.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARK = re.compile(r"//\s*@GT\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)$")


def collect_markers():
    edges = []
    for path in sorted(list((ROOT / "src").rglob("*.cpp")) + list((ROOT / "src").rglob("*.h"))):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            match = MARK.search(line)
            if not match:
                continue
            scenario, role, function, kind, rest = match.groups()
            tokens = rest.split()
            target = tokens[0] if tokens else ""
            extras = {}
            for token in tokens[1:]:
                if "=" in token:
                    key, value = token.split("=", 1)
                    extras[key] = value
            target_line = index + 1
            while target_line < len(lines) and (
                not lines[target_line].strip() or "@GT" in lines[target_line]
            ):
                target_line += 1
            edges.append({
                "scenario": scenario,
                "role": role,
                "function": function,
                "kind": kind,
                "target": target,
                "extra": extras,
                "file": path.relative_to(ROOT).as_posix(),
                "line": target_line + 1,
            })
    return edges


def main() -> int:
    meta = json.loads((ROOT / "tools" / "scenarios_meta.json").read_text(encoding="utf-8"))
    edges = collect_markers()
    problems = []
    scenarios = []
    for scenario in meta["scenarios"]:
        own = [e for e in edges if e["scenario"] == scenario["id"]]
        roles = {e["role"] for e in own}
        for needed in scenario["must_have_roles"]:
            if needed not in roles:
                problems.append(f"{scenario['id']}: missing marker role '{needed}'")
        scenarios.append({**scenario, "edges": own})
    output = {
        "schema_version": meta["schema_version"],
        "log_window_ms": meta["log_window_ms"],
        "scenarios": scenarios,
    }
    target = ROOT / "ground_truth.json"
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {target} with {len(edges)} marked edges for {len(scenarios)} scenarios.")
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
