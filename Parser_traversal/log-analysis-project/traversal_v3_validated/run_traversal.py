from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from traversal.backend_factory import make_backend_factory
from traversal.investigator import run_investigation
from traversal.neptune_tools import NeptuneTraversalTools

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="./config.json")
    parser.add_argument("--issues", default="./output/task31_anchors/resolved_issues.json")
    parser.add_argument("--output", default="./output/investigation_result.json")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    root = config_path.parent
    config = load(config_path)
    issues_path = (root / args.issues).resolve() if not Path(args.issues).is_absolute() else Path(args.issues)
    issues = load(issues_path)
    issue = next(i for i in issues if i.get("anchor_resolution", {}).get("status") == "RESOLVED")
    n = config["neptune"]
    t = config.get("traversal", {})
    tools = NeptuneTraversalTools(
        endpoint=n["endpoint"], region=n.get("region", "us-east-1"),
        port=n.get("port", 8182), profile=n.get("profile") or None,
        source_root=config["source_root"], ignored_fields=t.get("ignored_fields", []),
        ignored_project_callees=t.get("ignored_project_callees", []),
        ignored_external_prefixes=t.get("ignored_external_prefixes", []),
    )
    result = run_investigation(
        issue, tools, make_backend_factory(config["llm"]),
        max_steps=t.get("max_steps", 18),
        max_critic_rounds=t.get("max_critic_rounds", 2),
    )
    output = (root / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"status": result.get("status"), "output": str(output)}, indent=2))
    return 0 if result.get("status") == "ACCEPTED" else 2

if __name__ == "__main__":
    raise SystemExit(main())
