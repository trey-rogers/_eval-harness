#!/usr/bin/env python3
"""Convert evals/prompts.jsonl to evals/evals.json (see _eval-harness/references/schemas.md).

Each JSONL line is one object. Required: id (int), prompt (str), expectations (list[str]).
Optional: name, expected_output, files (list[str]), negative_control (bool).

Usage:
  python jsonl_to_evals.py --skill-name submit-pr path/to/evals/prompts.jsonl
  python jsonl_to_evals.py path/to/evals/prompts.jsonl   # skill name inferred from parent dir
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def line_to_eval(obj: dict, default_name: str) -> dict:
    eid = obj.get("id")
    if eid is None:
        raise ValueError("Each line must include 'id'")
    prompt = obj.get("prompt")
    if not prompt:
        raise ValueError(f"Eval id={eid} missing 'prompt'")
    expectations = obj.get("expectations")
    if not isinstance(expectations, list) or not expectations:
        raise ValueError(f"Eval id={eid} must have non-empty 'expectations' list")

    expected_output = obj.get("expected_output")
    if not expected_output:
        suffix = " (negative control)" if obj.get("negative_control") else ""
        expected_output = (obj.get("name") or f"eval-{eid}") + suffix

    files = obj.get("files") or []
    if not isinstance(files, list):
        raise ValueError(f"Eval id={eid}: 'files' must be a list")

    return {
        "id": int(eid),
        "prompt": prompt,
        "expected_output": str(expected_output),
        "files": files,
        "expectations": [str(x) for x in expectations],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "prompts_jsonl",
        type=Path,
        help="Path to prompts.jsonl",
    )
    parser.add_argument(
        "--skill-name",
        default="",
        help="skill_name field in evals.json (default: parent of evals/)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output evals.json (default: <prompts_dir>/evals.json)",
    )
    args = parser.parse_args()

    path: Path = args.prompts_jsonl
    if not path.is_file():
        print(f"Not found: {path}", file=sys.stderr)
        sys.exit(1)

    evals_dir = path.parent
    skill_name = args.skill_name or evals_dir.parent.name
    out = args.output or (evals_dir / "evals.json")

    evals: list[dict] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"{path}:{lineno}: invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        evals.append(line_to_eval(obj, skill_name))

    evals.sort(key=lambda e: e["id"])
    payload = {"skill_name": skill_name, "evals": evals}
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {out} ({len(evals)} evals)")


if __name__ == "__main__":
    main()
