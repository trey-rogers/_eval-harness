#!/usr/bin/env python3
"""Run behavioral evals across models and skills using an LLM CLI.

Default backend is **Cursor** (`agent -p`) when `agent` is on PATH; otherwise
**Claude Code** (`claude -p`) if `claude` is on PATH. Override with `--backend` or
`EVAL_MATRIX_BACKEND=cursor|claude`.

Writes the directory layout expected by `_eval-harness/scripts/aggregate_benchmark.py` and
`_eval-harness/scripts/matrix_scorecard.py`, then aggregates per (model, skill).

Requirements:
  - Cursor: `agent` on PATH (`agent login`, optional `CURSOR_API_KEY` for CI).
  - Claude Code: `claude` on PATH (standard Claude Code CLI authentication).
  - Model ids must match the chosen backend (`agent --list-models` vs Anthropic ids).

Examples (add skills repo bin/ to PATH — see _eval-harness/README.md §4):
  ev --list                                                  # auto-discovered skills + live model list
  ev --list-cli-models                                       # raw model slugs from the backend CLI
  ev --backend cursor -m composer-2-fast -s submit-pr        # one model × one skill, default config (with_skill)
  ev -m composer-2-fast -s submit-pr --dry-run               # preview planned runs, no LLM calls
  ev -m composer-2-fast -s add-action-logs --baseline        # A/B: skill on vs skill off (atrophy check)
  ev -m composer-2-fast -s add-action-logs --configs without_skill   # baseline only (no skill in prompt)
  ev -m composer-2-fast,gpt-5.2-codex-high -s submit-pr,review-pr --scorecard   # cross-model scorecard
  ev -m composer-2-fast -s submit-pr --eval-ids 1,2,6        # subset of eval ids
  ev -m composer-2-fast -s submit-pr --no-grade              # smoke test executor only

  # Note: a shell command named "eval" is a bash/zsh builtin; use `ev` or `skilleval`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from shutil import which
from datetime import datetime, timezone
from pathlib import Path


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def discover_skills(repo_root: Path) -> list[dict]:
    """Walk repo_root for SKILL.md and return one entry per discovered skill.

    Skips directories whose path contains a component starting with "_" or "." or
    equal to "node_modules". Each entry includes whether `evals/evals.json` is
    present (i.e. the skill is runnable through this harness).
    """
    skills: list[dict] = []
    for skill_md in repo_root.rglob("SKILL.md"):
        rel = skill_md.relative_to(repo_root)
        parts = rel.parts[:-1]  # exclude the SKILL.md filename
        if any(p.startswith("_") or p.startswith(".") or p == "node_modules" for p in parts):
            continue
        skill_dir = skill_md.parent
        rel_dir = str(skill_dir.relative_to(repo_root))
        skills.append(
            {
                "name": skill_dir.name,
                "path": rel_dir,
                "abs_path": skill_dir,
                "has_evals": (skill_dir / "evals" / "evals.json").is_file(),
            }
        )
    return sorted(skills, key=lambda s: s["path"])


def resolve_skill(arg: str, discovered: list[dict]) -> dict:
    """Resolve a `-s` argument to a discovered skill.

    Accepts either:
      - a relative path from the skills repo root (e.g. `superpowers/skills/foo`), or
      - a short directory leaf name (e.g. `foo`) when unambiguous.

    Raises ValueError with a user-facing message on miss / ambiguity.
    """
    arg_norm = arg.strip().strip("/")
    by_path = {s["path"]: s for s in discovered}
    if arg_norm in by_path:
        return by_path[arg_norm]
    leaf_matches = [s for s in discovered if s["name"] == arg_norm]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    if len(leaf_matches) > 1:
        paths = ", ".join(s["path"] for s in leaf_matches)
        raise ValueError(
            f"Ambiguous skill {arg!r}: multiple matches found. Use a full relative "
            f"path. Candidates: {paths}"
        )
    runnable_names = sorted({s["name"] for s in discovered if s["has_evals"]})
    raise ValueError(
        f"Unknown skill {arg!r}. Runnable skills (have evals/evals.json): "
        f"{', '.join(runnable_names) or '<none>'}"
    )


def split_csv(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]


def model_dir_name(model_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", model_id)
    return safe or "model"


def resolve_backend(choice: str) -> str:
    """Return 'cursor' or 'claude', or '' if neither can be used."""
    choice = (choice or "auto").strip().lower()
    env = os.environ.get("EVAL_MATRIX_BACKEND", "").strip().lower()
    if choice == "auto" and env in ("cursor", "claude"):
        return env
    if choice in ("cursor", "claude"):
        return choice
    if which("agent"):
        return "cursor"
    if which("claude"):
        return "claude"
    return ""


def peek_backend_from_argv(argv: list[str]) -> str:
    """Best-effort --backend value before full argparse (for help / model listing)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--backend" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--backend="):
            return a.split("=", 1)[1].strip().lower()
        i += 1
    return "auto"


def fetch_cli_model_list_text(
    backend: str,
    *,
    timeout_sec: float = 15.0,
    max_lines: int | None = 120,
) -> tuple[str, str]:
    """Return (text, error_message). text is stdout or empty; error_message if probe failed."""
    if backend == "cursor":
        if which("agent") is None:
            return "", "`agent` not on PATH; install Cursor CLI (see https://cursor.com/docs/cli)."
        try:
            proc = subprocess.run(
                ["agent", "--list-models"],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return "", f"Could not run `agent --list-models`: {e}"
        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            return "", f"`agent --list-models` exited {proc.returncode}" + (f": {err}" if err else "")
        if not out:
            return "", "`agent --list-models` returned no output."
        if max_lines is not None:
            lines = out.splitlines()
            if len(lines) > max_lines:
                extra = len(lines) - max_lines
                out = "\n".join(lines[:max_lines]) + f"\n… ({extra} more lines; run `agent --list-models` for the full list)"
        return out, ""

    if backend == "claude":
        if which("claude") is None:
            return "", "`claude` not on PATH; install Claude Code."
        for cmd in (["claude", "--list-models"], ["claude", "models"]):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                return "", f"Could not run {' '.join(cmd)!r}: {e}"
            out = (proc.stdout or "").strip()
            if proc.returncode == 0 and out:
                if max_lines is not None:
                    lines = out.splitlines()
                    if len(lines) > max_lines:
                        extra = len(lines) - max_lines
                        out = "\n".join(lines[:max_lines]) + f"\n… ({extra} more lines)"
                return out, ""
        return (
            "",
            "Could not list Claude models (tried `claude --list-models` and `claude models`). "
            "Use Anthropic-style model ids documented for Claude Code.",
        )

    return "", f"Unknown backend {backend!r} for model listing."


def help_epilog_for_models(argv: list[str]) -> str:
    """Extra help text for -h/--help: models from the CLI when available."""
    lines = [
        "Model ids for -m / --model must match your --backend CLI.",
        "",
    ]
    choice = peek_backend_from_argv(argv)
    backend = resolve_backend(choice)
    if not backend:
        lines.append(
            "No LLM CLI detected on PATH for auto backend. Install `agent` (Cursor) or `claude` "
            "(Claude Code), or pass --backend cursor|claude after installing the matching binary."
        )
        lines.append("")
        lines.append("After install, run:  <this command> --list-cli-models")
        return "\n".join(lines)

    text, err = fetch_cli_model_list_text(backend, max_lines=40)
    lines.append(f"Backend resolved for this help: {backend}")
    lines.append("")
    if text:
        lines.append("Available models (from CLI; truncated in --help):")
        lines.append(text)
        lines.append("")
        lines.append("Full list: run this command with --list-cli-models")
    else:
        lines.append(f"Could not query CLI for models: {err}")
    return "\n".join(lines)


def log_eval_cli_failure(
    *,
    phase: str,
    model_id: str,
    skill_name: str,
    eval_id: int,
    config: str,
    exit_code: int,
    stderr: str,
    stdout: str,
    run_dir: Path,
) -> None:
    """Print CLI failures to stderr so users see quota/API errors immediately."""
    parts = [
        f"[eval-harness] {phase} failed: model={model_id!r} skill={skill_name!r} "
        f"eval={eval_id} config={config!r} exit={exit_code}",
        f"artifacts: {run_dir}",
    ]
    err_t = (stderr or "").strip()
    out_t = (stdout or "").strip()
    if err_t:
        cap = 12000
        excerpt = err_t if len(err_t) <= cap else err_t[:cap] + "\n… (stderr truncated)"
        parts.append("--- stderr ---")
        parts.append(excerpt)
    if exit_code != 0 and out_t:
        cap = 8000
        # Usage and policy errors sometimes land on stdout for headless CLIs.
        excerpt = out_t if len(out_t) <= cap else out_t[:cap] + "\n… (stdout truncated)"
        parts.append("--- stdout (excerpt; non-zero exit) ---")
        parts.append(excerpt)
    print("\n".join(parts), file=sys.stderr)


def call_agent_llm(
    backend: str,
    prompt: str,
    model: str | None,
    cwd: Path,
    timeout: int,
) -> tuple[str, str, int]:
    """Run one headless LLM invocation; prompt on stdin. Returns stdout, stderr, exit code."""
    cwd = cwd.resolve()
    if backend == "cursor":
        cmd = [
            "agent",
            "-p",
            "--output-format",
            "text",
            "--trust",
            "--workspace",
            str(cwd),
        ]
        if model:
            cmd.extend(["--model", model])
        env = dict(os.environ)
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
            timeout=timeout,
        )
        out = result.stdout or ""
        err = result.stderr or ""
        return out, err, result.returncode

    if backend == "claude":
        cmd = ["claude", "-p", "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
            timeout=timeout,
        )
        out = result.stdout or ""
        err = result.stderr or ""
        return out, err, result.returncode

    raise ValueError(f"Unknown backend: {backend!r}")


def extract_json_object(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def build_executor_prompt(
    *,
    with_skill: bool,
    skill_body: str,
    skill_name: str,
    user_prompt: str,
) -> str:
    if with_skill:
        return (
            "You are evaluating workflow adherence. Apply the following skill when "
            f"deciding what to recommend. Skill name: {skill_name}\n\n"
            "--- SKILL BEGIN ---\n"
            f"{skill_body}\n"
            "--- SKILL END ---\n\n"
            "User request:\n"
            f"{user_prompt}\n\n"
            "Respond with a clear, structured answer. When the skill names scripts, "
            "commands, or repository paths, preserve them accurately."
        )
    return (
        "Answer using general software engineering practices for Android and GitHub workflows. "
        "Do not claim you followed a private team skill document.\n\n"
        "User request:\n"
        f"{user_prompt}\n\n"
        "Respond with a clear, structured answer."
    )


def build_grader_prompt(*, expectations: list[str], assistant_response: str) -> str:
    exp_json = json.dumps(expectations, indent=2)
    return f"""You are an automatic grader. The text below is the full assistant response (treat it as the transcript).

Expectations to judge (each PASS or FAIL based only on evidence in the assistant response):
{exp_json}

Assistant response:
---
{assistant_response}
---

Rules:
- For each expectation, copy the exact "text" string into your output and set passed true/false with concrete evidence quoted or paraphrased from the assistant response.
- If evidence is missing, failed must be true.
- Output a single JSON object only (no markdown fences). Shape:
{{"expectations":[{{"text":"...","passed":true,"evidence":"..."}}],"summary":{{"passed":N,"failed":M,"total":T,"pass_rate":0.0}}}}
where T is the number of expectations, pass_rate = passed/T (float)."""


def normalize_grading(raw: dict, expectations: list[str]) -> dict:
    """Ensure grading dict uses canonical expectation keys for aggregation/scorecards."""
    ex_out = []
    raw_list = raw.get("expectations") if isinstance(raw, dict) else None
    if not isinstance(raw_list, list):
        raw_list = []
    by_text = {str(e.get("text", "")): e for e in raw_list if isinstance(e, dict)}
    for text in expectations:
        row = by_text.get(text)
        if row and "passed" in row and "evidence" in row:
            ex_out.append(
                {"text": text, "passed": bool(row["passed"]), "evidence": str(row["evidence"])}
            )
        else:
            ex_out.append(
                {
                    "text": text,
                    "passed": False,
                    "evidence": "Grader did not return a verdict for this expectation.",
                }
            )
    passed = sum(1 for e in ex_out if e["passed"])
    total = len(ex_out)
    failed = total - passed
    pr = (passed / total) if total else 0.0
    summary = raw.get("summary") if isinstance(raw, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    summary = {
        "passed": passed,
        "failed": failed,
        "total": total,
        "pass_rate": float(summary.get("pass_rate", pr)),
    }
    summary["pass_rate"] = float(passed / total) if total else 0.0
    return {
        "expectations": ex_out,
        "summary": summary,
        "execution_metrics": raw.get("execution_metrics")
        if isinstance(raw.get("execution_metrics"), dict)
        else {
            "tool_calls": {},
            "total_tool_calls": 0,
            "total_steps": 1,
            "errors_encountered": 0,
            "output_chars": 0,
            "transcript_chars": 0,
        },
        "timing": raw.get("timing") if isinstance(raw.get("timing"), dict) else {},
        "claims": raw.get("claims") if isinstance(raw.get("claims"), list) else [],
        "user_notes_summary": raw.get("user_notes_summary")
        if isinstance(raw.get("user_notes_summary"), dict)
        else {"uncertainties": [], "needs_review": [], "workarounds": []},
    }


def run_one(
    *,
    repo_root: Path,
    backend: str,
    model_id: str,
    skill_name: str,
    skill_path: Path,
    skill_body: str,
    eval_obj: dict,
    configs: list[str],
    run_number: int,
    bench_skill_dir: Path,
    cwd: Path,
    timeout_executor: int,
    timeout_grader: int,
    grade: bool,
    grader_model: str | None,
    dry_run: bool,
) -> None:
    eid = int(eval_obj["id"])
    prompt = str(eval_obj["prompt"])
    expectations = [str(x) for x in eval_obj.get("expectations", [])]

    for config in configs:
        with_skill = config == "with_skill"
        run_dir = bench_skill_dir / f"eval-{eid}" / config / f"run-{run_number}"
        outputs_dir = run_dir / "outputs"
        if dry_run:
            print(f"  would run: {model_id} {skill_name} eval-{eid} {config} -> {run_dir}")
            continue

        outputs_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "eval_id": eid,
            "prompt": prompt,
            "skill_name": skill_name,
            "configuration": config,
            "model": model_id,
            "llm_backend": backend,
        }
        (run_dir / "eval_metadata.json").write_text(json.dumps(meta, indent=2) + "\n")

        ex_prompt = build_executor_prompt(
            with_skill=with_skill,
            skill_body=skill_body,
            skill_name=skill_name,
            user_prompt=prompt,
        )
        t0 = time.time()
        stdout, stderr, code = call_agent_llm(
            backend, ex_prompt, model_id, cwd, timeout_executor
        )
        elapsed = time.time() - t0

        if code != 0:
            log_eval_cli_failure(
                phase="executor",
                model_id=model_id,
                skill_name=skill_name,
                eval_id=eid,
                config=config,
                exit_code=code,
                stderr=stderr,
                stdout=stdout,
                run_dir=run_dir,
            )

        (outputs_dir / "response.md").write_text(stdout)
        if stderr.strip():
            (outputs_dir / "executor_stderr.txt").write_text(stderr)
        transcript = (
            "## Eval Prompt\n\n"
            f"{prompt}\n\n"
            "## Configuration\n\n"
            f"{config} (with_skill={with_skill})\n\n"
            "## Assistant\n\n"
            f"{stdout}\n"
        )
        (run_dir / "transcript.md").write_text(transcript)

        out_chars = len(stdout)
        (outputs_dir / "metrics.json").write_text(
            json.dumps(
                {
                    "tool_calls": {},
                    "total_tool_calls": 0,
                    "total_steps": 1,
                    "files_created": ["response.md"],
                    "errors_encountered": 1 if code != 0 else 0,
                    "output_chars": out_chars,
                    "transcript_chars": len(transcript),
                },
                indent=2,
            )
            + "\n"
        )
        (run_dir / "timing.json").write_text(
            json.dumps(
                {
                    "total_duration_seconds": round(elapsed, 2),
                    "executor_duration_seconds": round(elapsed, 2),
                    "executor_exit_code": code,
                },
                indent=2,
            )
            + "\n"
        )

        grading: dict
        if not grade:
            failed = len(expectations)
            grading = {
                "expectations": [
                    {
                        "text": t,
                        "passed": False,
                        "evidence": "Grading skipped (--no-grade).",
                    }
                    for t in expectations
                ],
                "summary": {
                    "passed": 0,
                    "failed": failed,
                    "total": len(expectations),
                    "pass_rate": 0.0,
                },
                "execution_metrics": json.loads((outputs_dir / "metrics.json").read_text()),
                "timing": {
                    "executor_duration_seconds": round(elapsed, 2),
                    "total_duration_seconds": round(elapsed, 2),
                },
                "claims": [],
                "user_notes_summary": {
                    "uncertainties": (
                        [] if code == 0 else [f"executor CLI exited with code {code}"]
                    ),
                    "needs_review": [],
                    "workarounds": [],
                },
            }
        else:
            g_prompt = build_grader_prompt(
                expectations=expectations, assistant_response=stdout
            )
            g0 = time.time()
            g_out, g_err, g_code = call_agent_llm(
                backend, g_prompt, grader_model or model_id, cwd, timeout_grader
            )
            g_elapsed = time.time() - g0
            if g_code != 0:
                log_eval_cli_failure(
                    phase="grader",
                    model_id=grader_model or model_id,
                    skill_name=skill_name,
                    eval_id=eid,
                    config=config,
                    exit_code=g_code,
                    stderr=g_err,
                    stdout=g_out,
                    run_dir=run_dir,
                )
            parsed = extract_json_object(g_out)
            if parsed is None:
                print(
                    f"[eval-harness] grader output was not valid JSON: skill={skill_name!r} "
                    f"eval={eid} config={config!r} grader_exit={g_code} -> {run_dir / 'grader_raw.txt'}",
                    file=sys.stderr,
                )
                grading = normalize_grading(
                    {
                        "expectations": [
                            {
                                "text": t,
                                "passed": False,
                                "evidence": f"Grader JSON parse failed. Raw exit {g_code}.",
                            }
                            for t in expectations
                        ]
                    },
                    expectations,
                )
                (run_dir / "grader_raw.txt").write_text(g_out + "\n\n" + g_err)
            else:
                grading = normalize_grading(parsed, expectations)
            timing = grading.setdefault("timing", {})
            if isinstance(timing, dict):
                timing.setdefault("executor_duration_seconds", round(elapsed, 2))
                timing.setdefault("grader_duration_seconds", round(g_elapsed, 2))
                timing["total_duration_seconds"] = round(elapsed + g_elapsed, 2)

        (run_dir / "grading.json").write_text(json.dumps(grading, indent=2) + "\n")


def aggregate(repo_root: Path, bench_skill_dir: Path, skill_name: str, skill_abs: Path) -> None:
    _ = repo_root  # kept for call-site compatibility
    agg = Path(__file__).resolve().parent / "aggregate_benchmark.py"
    if not agg.is_file():
        print(f"Warning: aggregate script missing: {agg}", file=sys.stderr)
        return
    subprocess.run(
        [
            sys.executable,
            str(agg),
            str(bench_skill_dir),
            "--skill-name",
            skill_name,
            "--skill-path",
            str(skill_abs),
        ],
        check=False,
    )
    bench_json = bench_skill_dir / "benchmark.json"
    if bench_json.is_file():
        data = json.loads(bench_json.read_text())
        meta = data.setdefault("metadata", {})
        # model_id stored in parent folder name is lossy; caller patches below
        bench_json.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-m",
        "--models",
        default="",
        help="Comma-separated model ids (match backend). Use --list-cli-models or -h to see CLI slugs.",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "cursor", "claude"),
        default="auto",
        help="LLM CLI: cursor = Cursor `agent -p`, claude = Claude Code `claude -p`. "
        "auto = env EVAL_MATRIX_BACKEND if set, else prefer `agent` on PATH else `claude`.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="ID",
        help="Add one model id (repeatable).",
    )
    parser.add_argument(
        "-s",
        "--skills",
        default="",
        help="Comma-separated skill identifiers. Each is either a short directory leaf "
        "(e.g. submit-pr) or a relative path from --repo-root (e.g. "
        "superpowers/skills/systematic-debugging). Skills are auto-discovered as any "
        "directory containing SKILL.md and evals/evals.json.",
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="ID",
        help="Add one skill identifier (short leaf or relative path); repeatable.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Skills repository root (default: parent of _eval-harness).",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Working directory for the LLM CLI (Cursor: agent --workspace; Claude: cwd). "
        "Default: --repo-root.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Run output directory (default: _eval-harness/results/<UTC timestamp>).",
    )
    configs_group = parser.add_mutually_exclusive_group()
    configs_group.add_argument(
        "--configs",
        default=None,
        help="Comma-separated configurations to run per eval. "
        "with_skill (default) injects the SKILL.md into the prompt; "
        "without_skill is a baseline with no skill. "
        "Pass 'with_skill,without_skill' to A/B test whether the skill still "
        "helps — useful for spotting skills that have atrophied and could be "
        "removed. See --baseline for a shorthand.",
    )
    configs_group.add_argument(
        "--baseline",
        action="store_true",
        help="Shorthand for --configs with_skill,without_skill. Runs each eval "
        "twice (skill injected vs. skill omitted) so you can compare and decide "
        "if the skill is still pulling its weight.",
    )
    parser.add_argument(
        "--runs-per-eval",
        type=int,
        default=1,
        help="Run number suffix only run-1..N (default 1).",
    )
    parser.add_argument(
        "--eval-ids",
        default="",
        help="Optional comma-separated eval ids to include (default: all).",
    )
    parser.add_argument(
        "--timeout-executor",
        type=int,
        default=600,
        help="Timeout seconds per executor LLM call.",
    )
    parser.add_argument(
        "--timeout-grader",
        type=int,
        default=300,
        help="Timeout seconds per grader LLM call.",
    )
    parser.add_argument(
        "--grader-model",
        default=None,
        help="Model for grading (default: same as executor model per run).",
    )
    parser.add_argument(
        "--no-grade",
        action="store_true",
        help="Skip grader; write failing placeholder grading (for dry executor tests).",
    )
    parser.add_argument(
        "--scorecard",
        action="store_true",
        help="After runs, build cross-model scorecard for this output directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned runs only.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print runnable skills (auto-discovered from filesystem) and the model "
        "list from your --backend CLI (equivalent to --list-cli-models for models). "
        "Exits without running anything.",
    )
    parser.add_argument(
        "--list-cli-models",
        action="store_true",
        help="Print models reported by the backend CLI (agent --list-models or claude) and exit.",
    )

    argv_rest = sys.argv[1:]
    if "-h" in argv_rest or "--help" in argv_rest:
        parser.epilog = help_epilog_for_models(argv_rest)

    args = parser.parse_args()

    if args.list_cli_models:
        backend_lm = resolve_backend(args.backend)
        if not backend_lm:
            print(
                "No LLM CLI on PATH. Install `agent` (Cursor) or `claude` (Claude Code), "
                "or set EVAL_MATRIX_BACKEND and ensure that binary exists.",
                file=sys.stderr,
            )
            sys.exit(127)
        text, err = fetch_cli_model_list_text(backend_lm, max_lines=None)
        if text:
            print(text)
            return
        print(err, file=sys.stderr)
        sys.exit(1)

    repo_root = Path(args.repo_root or repo_root_from_script()).resolve()
    discovered = discover_skills(repo_root)

    if args.list:
        backend_lm = resolve_backend(args.backend)
        runnable = [s for s in discovered if s["has_evals"]]
        print("Skills (auto-discovered; runnable = has evals/evals.json):")
        if runnable:
            for s in runnable:
                # Show leaf name + relative path; identical when at repo root.
                if s["name"] == s["path"]:
                    print(f"  {s['name']}")
                else:
                    print(f"  {s['name']}\t({s['path']})")
        else:
            print("  <no runnable skills found under repo root>")
        print()
        if backend_lm:
            text, err = fetch_cli_model_list_text(backend_lm, max_lines=None)
            print(f"Models (live from `{backend_lm}` CLI):")
            if text:
                print(text)
            else:
                print(f"  (could not query CLI: {err})")
        else:
            print(
                "Models: no LLM CLI on PATH for the resolved backend. "
                "Install Cursor `agent` (https://cursor.com/docs/cli) or Claude Code "
                "`claude`, or pass --backend cursor|claude after installing the matching "
                "binary, then re-run --list."
            )
        return

    models = split_csv(args.models) + list(args.model)
    if not models:
        print("Provide --models or repeat --model.", file=sys.stderr)
        sys.exit(2)

    skills_arg = split_csv(args.skills) + list(args.skill)
    if not skills_arg:
        print("Provide --skills or repeat --skill.", file=sys.stderr)
        sys.exit(2)

    resolved: list[tuple[str, Path]] = []
    for arg in skills_arg:
        try:
            skill = resolve_skill(arg, discovered)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        if not skill["has_evals"]:
            print(
                f"Skill {arg!r} resolved to {skill['path']!r} but is missing "
                f"evals/evals.json; nothing to run.",
                file=sys.stderr,
            )
            sys.exit(2)
        resolved.append((skill["name"], skill["abs_path"]))

    if args.baseline:
        configs_arg = "with_skill,without_skill"
    else:
        configs_arg = args.configs or "with_skill"
    configs = [c.strip() for c in configs_arg.split(",") if c.strip()]
    for c in configs:
        if c not in ("with_skill", "without_skill"):
            print(f"Invalid config {c!r}; use with_skill or without_skill.", file=sys.stderr)
            sys.exit(2)

    eval_id_filter: set[int] | None = None
    if args.eval_ids.strip():
        eval_id_filter = {int(x.strip()) for x in args.eval_ids.split(",") if x.strip()}

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = Path(args.out or repo_root / "_eval-harness" / "results" / ts).resolve()
    cwd = Path(args.cwd or repo_root).resolve()

    backend = resolve_backend(args.backend)
    if not args.dry_run:
        if backend == "":
            print(
                "No LLM CLI found. Install Cursor Agent (`agent`, see https://cursor.com/docs/cli) "
                "or Claude Code (`claude`), or set EVAL_MATRIX_BACKEND=cursor|claude and ensure that "
                "binary is on PATH.",
                file=sys.stderr,
            )
            sys.exit(127)
        if backend == "cursor" and which("agent") is None:
            print("Backend is cursor but `agent` not on PATH.", file=sys.stderr)
            sys.exit(127)
        if backend == "claude" and which("claude") is None:
            print("Backend is claude but `claude` not on PATH.", file=sys.stderr)
            sys.exit(127)

    print(f"Output directory: {out_dir}")
    if backend:
        print(f"LLM backend: {backend} — workspace: {cwd}")
    else:
        print(f"LLM backend: (unresolved; dry-run only) — workspace: {cwd}")
    if args.dry_run:
        print("(dry-run)")

    grade = not args.no_grade

    for model_id in models:
        mdir = model_dir_name(model_id)
        for skill_name, skill_abs in resolved:
            skill_body = (skill_abs / "SKILL.md").read_text()
            evals_data = json.loads((skill_abs / "evals" / "evals.json").read_text())
            evals_list = evals_data.get("evals", [])
            if eval_id_filter is not None:
                evals_list = [e for e in evals_list if int(e["id"]) in eval_id_filter]
            bench_skill_dir = out_dir / mdir / skill_name
            print(f"== {model_id} / {skill_name} ({len(evals_list)} evals) ==")
            for ev in evals_list:
                for run_n in range(1, args.runs_per_eval + 1):
                    run_one(
                        repo_root=repo_root,
                        backend=backend,
                        model_id=model_id,
                        skill_name=skill_name,
                        skill_path=skill_abs,
                        skill_body=skill_body,
                        eval_obj=ev,
                        configs=configs,
                        run_number=run_n,
                        bench_skill_dir=bench_skill_dir,
                        cwd=cwd,
                        timeout_executor=args.timeout_executor,
                        timeout_grader=args.timeout_grader,
                        grade=grade,
                        grader_model=args.grader_model,
                        dry_run=args.dry_run,
                    )
            if not args.dry_run:
                aggregate(repo_root, bench_skill_dir, skill_name, skill_abs)
                bpath = bench_skill_dir / "benchmark.json"
                if bpath.is_file():
                    data = json.loads(bpath.read_text())
                    meta = data.setdefault("metadata", {})
                    meta["executor_model"] = model_id
                    meta["llm_backend"] = backend
                    bpath.write_text(json.dumps(data, indent=2) + "\n")

    if args.scorecard and not args.dry_run:
        sc = repo_root / "_eval-harness" / "scripts" / "matrix_scorecard.py"
        subprocess.run(
            [sys.executable, str(sc), str(out_dir), "--title", f"Eval matrix {ts}"],
            check=False,
        )
        print(f"Scorecard: {out_dir / 'scorecard.md'}")
        detail = out_dir / "scorecard-detail.md"
        if detail.is_file():
            print(f"Scorecard detail: {detail}")


if __name__ == "__main__":
    main()
