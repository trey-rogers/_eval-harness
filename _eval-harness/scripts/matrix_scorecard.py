#!/usr/bin/env python3
"""Build a side-by-side scorecard from per-model benchmark.json files.

Expected layout (under a single run directory):

  <run_dir>/
    <model_id>/
      <skill_name>/benchmark.json

model_id matches the slug accepted by your backend CLI (e.g. `agent --list-models`
for Cursor, or Anthropic-style ids for Claude Code). Folder names may sanitize slashes.

Two artifacts are produced (alongside scorecard.json):

- ``scorecard.md``         — punchy headline matrix (model × skill pass rates).
- ``scorecard-detail.md``  — per-skill drill-down with failures, negative-control
                             results, recommended model, and drift signals.

Usage:
  python matrix_scorecard.py /path/to/_eval-harness/results/2026-04-30-run1
  python matrix_scorecard.py /path/to/run --output-md /path/to/scorecard.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PRIMARY_CONFIG = "with_skill"
DRIFT_STDDEV_THRESHOLD = 0.15  # 15 percentage points


def load_benchmark(p: Path) -> dict | None:
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def pass_rate_for_config(bench: dict, prefer: str = PRIMARY_CONFIG) -> float | None:
    rs = bench.get("run_summary") or {}
    if prefer in rs and "pass_rate" in rs[prefer]:
        return float(rs[prefer]["pass_rate"].get("mean", 0.0))
    # Single-config or alternate naming: first non-delta key
    for k, v in rs.items():
        if k == "delta":
            continue
        if isinstance(v, dict) and "pass_rate" in v:
            return float(v["pass_rate"].get("mean", 0.0))
    return None


def discover_grid(run_dir: Path) -> dict[str, dict[str, dict]]:
    """Return grid[model_id][skill_name] = { benchmark path, pass_rate, meta }."""
    grid: dict[str, dict[str, dict]] = {}
    if not run_dir.is_dir():
        return grid

    for model_dir in sorted(run_dir.iterdir()):
        if not model_dir.is_dir() or model_dir.name.startswith("."):
            continue
        model_id = model_dir.name
        grid[model_id] = {}
        for skill_dir in sorted(model_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            bpath = skill_dir / "benchmark.json"
            bench = load_benchmark(bpath)
            if not bench:
                continue
            pr = pass_rate_for_config(bench)
            meta = bench.get("metadata") or {}
            grid[model_id][skill_dir.name] = {
                "path": str(bpath),
                "pass_rate": pr,
                "executor_model": meta.get("executor_model", model_id),
                "skill_name": meta.get("skill_name", skill_dir.name),
            }
    return grid


def render_markdown(grid: dict[str, dict[str, dict]], title: str) -> str:
    models = sorted(grid.keys())
    skills = sorted({s for m in grid.values() for s in m})

    header = ["Skill"] + models + ["spread"]
    rows = []
    for skill in skills:
        cells = [skill]
        values: list[float] = []
        for model in models:
            cell = grid.get(model, {}).get(skill)
            if not cell or cell.get("pass_rate") is None:
                cells.append("—")
            else:
                v = float(cell["pass_rate"])
                values.append(v)
                cells.append(f"{v * 100:.0f}%")
        if len(values) >= 2:
            spread = max(values) - min(values)
            cells.append(f"{spread * 100:.0f} pp")
        elif len(values) == 1:
            cells.append("—")
        else:
            cells.append("—")
        rows.append(cells)

    # Markdown table
    lines = [f"# {title}", "", "| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("Pass rates use `run_summary.with_skill.pass_rate.mean` when present; otherwise the first configuration block in `run_summary`.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detail scorecard (scorecard-detail.md)
# ---------------------------------------------------------------------------


def load_prompts_meta(skill_path: Path | None) -> dict[int, dict]:
    """Read prompts.jsonl from a skill directory.

    Returns ``{eval_id: {"name": str, "negative_control": bool}}``. Missing or
    unreadable files yield an empty dict so the scorecard degrades gracefully.
    """
    if not skill_path:
        return {}
    p = Path(skill_path) / "evals" / "prompts.jsonl"
    if not p.is_file():
        return {}
    by_id: dict[int, dict] = {}
    try:
        text = p.read_text()
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        eid = obj.get("id")
        if eid is None:
            continue
        by_id[int(eid)] = {
            "name": obj.get("name") or f"eval-{eid}",
            "negative_control": bool(obj.get("negative_control")),
        }
    return by_id


def collect_eval_details(bench: dict, config: str = PRIMARY_CONFIG) -> dict[int, dict]:
    """Aggregate per-eval pass/fail data across all runs of one configuration."""
    by_eval: dict[int, dict] = {}
    for run in bench.get("runs") or []:
        if run.get("configuration") != config:
            continue
        eid = run.get("eval_id")
        if eid is None:
            continue
        slot = by_eval.setdefault(int(eid), {
            "run_pass_rates": [],
            "failed_expectations": [],  # list of {text, evidence, run_number}
        })
        result = run.get("result") or {}
        pr = result.get("pass_rate")
        if pr is not None:
            slot["run_pass_rates"].append(float(pr))
        for exp in run.get("expectations") or []:
            if not exp.get("passed"):
                slot["failed_expectations"].append({
                    "text": exp.get("text") or "",
                    "evidence": exp.get("evidence") or "",
                    "run_number": run.get("run_number"),
                })

    for slot in by_eval.values():
        rates = slot["run_pass_rates"]
        slot["pass_rate"] = sum(rates) / len(rates) if rates else None
        # Eval "flips" when not all runs of the same config share the same pass rate.
        slot["flipped"] = len(set(rates)) > 1 if len(rates) > 1 else False
    return by_eval


def has_baseline_runs(bench: dict) -> bool:
    """True if the benchmark contains any non-primary configuration data."""
    for run in bench.get("runs") or []:
        cfg = run.get("configuration")
        if cfg and cfg != PRIMARY_CONFIG:
            return True
    # Some pipelines populate only ``run_summary`` (no per-run rows); honor that too.
    for k, v in (bench.get("run_summary") or {}).items():
        if k in {PRIMARY_CONFIG, "delta"}:
            continue
        if isinstance(v, dict) and "pass_rate" in v:
            return True
    return False


def discover_detail_grid(run_dir: Path) -> dict[str, dict]:
    """Return ``{skill_name: {"prompts": ..., "models": {model_id: cell}}}``.

    Each model cell has pass_rate / pass_rate_stddev / mean_time / has_baseline /
    eval_details (from ``collect_eval_details``).
    """
    skills: dict[str, dict] = {}
    if not run_dir.is_dir():
        return skills

    for model_dir in sorted(run_dir.iterdir()):
        if not model_dir.is_dir() or model_dir.name.startswith("."):
            continue
        model_id = model_dir.name
        for skill_dir in sorted(model_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            bpath = skill_dir / "benchmark.json"
            bench = load_benchmark(bpath)
            if not bench:
                continue
            skill_name = skill_dir.name
            entry = skills.setdefault(skill_name, {"prompts": {}, "models": {}})
            if not entry["prompts"]:
                meta = bench.get("metadata") or {}
                skill_path = meta.get("skill_path")
                if skill_path:
                    entry["prompts"] = load_prompts_meta(Path(skill_path))

            run_summary = bench.get("run_summary") or {}
            primary = run_summary.get(PRIMARY_CONFIG) or {}
            pr = primary.get("pass_rate") or {}
            ts = primary.get("time_seconds") or {}
            entry["models"][model_id] = {
                "pass_rate": pr.get("mean"),
                "pass_rate_stddev": pr.get("stddev"),
                "mean_time": ts.get("mean"),
                "has_baseline": has_baseline_runs(bench),
                "eval_details": collect_eval_details(bench, PRIMARY_CONFIG),
            }
    return skills


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _pp(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f} pp"


def _time(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}s"


def recommend_model(models: dict) -> str | None:
    """Highest pass rate; ties broken by lower stddev, then lower mean time."""
    candidates = [(mid, m) for mid, m in models.items() if m.get("pass_rate") is not None]
    if not candidates:
        return None

    def key(item: tuple[str, dict]) -> tuple[float, float, float]:
        _, m = item
        return (
            -float(m["pass_rate"] or 0.0),
            float(m.get("pass_rate_stddev") or 0.0),
            float(m.get("mean_time") or 0.0),
        )

    candidates.sort(key=key)
    return candidates[0][0]


def _negative_control_ids(prompts: dict[int, dict]) -> set[int]:
    return {eid for eid, m in (prompts or {}).items() if m.get("negative_control")}


def negative_control_summary(skill_entry: dict) -> str:
    neg_ids = sorted(_negative_control_ids(skill_entry.get("prompts") or {}))
    if not neg_ids:
        return "n/a"
    failures: list[str] = []
    for eid in neg_ids:
        for mid, m in skill_entry["models"].items():
            ed = (m.get("eval_details") or {}).get(eid)
            if ed is not None and (ed.get("pass_rate") or 0.0) < 1.0:
                failures.append(f"⚠ eval-{eid} failed on `{mid}`")
    if not failures:
        eval_label = ", ".join(f"eval-{e}" for e in neg_ids)
        scope = "all models passed" if len(skill_entry["models"]) > 1 else "passed"
        return f"✓ {scope} {eval_label}"
    return "; ".join(failures)


def drift_signal(skill_entry: dict) -> str:
    """Compose a drift signal string for a skill across all its models."""
    models = skill_entry["models"]
    neg_ids = _negative_control_ids(skill_entry.get("prompts") or {})

    high_stddev: list[str] = []
    flipped_evals: set[int] = set()
    neg_control_fails: set[str] = set()

    for mid, m in models.items():
        sd = m.get("pass_rate_stddev")
        if sd is not None and sd > DRIFT_STDDEV_THRESHOLD:
            high_stddev.append(_pp(sd))
        for eid, ed in (m.get("eval_details") or {}).items():
            if ed.get("flipped"):
                flipped_evals.add(eid)
            if eid in neg_ids and (ed.get("pass_rate") or 0.0) < 1.0:
                neg_control_fails.add(mid)

    flags: list[str] = []
    if high_stddev:
        unique = sorted(set(high_stddev), key=lambda s: int(s.split()[0]))
        flags.append(f"⚠ stddev {'–'.join(unique)}")
    if flipped_evals:
        flags.append("⚠ flipped: " + ", ".join(f"eval-{e}" for e in sorted(flipped_evals)))
    if neg_control_fails:
        flags.append("⚠ neg-control failed on " + ", ".join(f"`{m}`" for m in sorted(neg_control_fails)))
    return "; ".join(flags) if flags else "—"


def render_at_a_glance(skills: dict) -> str:
    lines = [
        "| Skill | Recommended model | Pass rates | Drift signal | Negative control |",
        "| --- | --- | --- | --- | --- |",
    ]
    for skill in sorted(skills.keys()):
        entry = skills[skill]
        rec = recommend_model(entry["models"])
        if rec and len(entry["models"]) > 1:
            rec_cell = f"**`{rec}`**"
        elif rec:
            rec_cell = f"`{rec}`"
        else:
            rec_cell = "—"
        pr_bits = " · ".join(f"{mid} {_pct(m.get('pass_rate'))}" for mid, m in sorted(entry["models"].items()))
        lines.append(f"| {skill} | {rec_cell} | {pr_bits} | {drift_signal(entry)} | {negative_control_summary(entry)} |")
    return "\n".join(lines)


def render_skill_section(skill_name: str, entry: dict) -> str:
    models = entry["models"]
    prompts = entry.get("prompts") or {}
    neg_ids = _negative_control_ids(prompts)

    lines: list[str] = [f"## {skill_name}", ""]

    def _pass_phrase(m: dict) -> str:
        base = f"{_pct(m.get('pass_rate'))} pass"
        t = m.get("mean_time")
        return f"{base} at {_time(t)} mean wall time" if t is not None else base

    # Recommendation sentence
    rec = recommend_model(models)
    if rec and len(models) > 1:
        rm = models[rec]
        others = [
            f"`{mid}`: {_pass_phrase(m)}"
            for mid, m in sorted(models.items()) if mid != rec
        ]
        sentence = f"**Recommended model: `{rec}`** — {_pass_phrase(rm)}."
        if others:
            sentence += " " + "; ".join(others) + "."
        lines.append(sentence)
    elif rec:
        rm = models[rec]
        lines.append(f"Only model with data: `{rec}` — {_pass_phrase(rm)}.")
    lines.append("")

    # Per-model summary table
    lines.append("| Model | Pass | Stddev | Mean time | Negative control | Failing evals |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for mid in sorted(models.keys()):
        m = models[mid]
        nc_states: list[str] = []
        for eid in sorted(neg_ids):
            ed = (m.get("eval_details") or {}).get(eid)
            if ed is None:
                nc_states.append(f"— eval-{eid}")
            elif (ed.get("pass_rate") or 0.0) >= 1.0:
                nc_states.append(f"✓ eval-{eid}")
            else:
                nc_states.append(f"⚠ eval-{eid}")
        nc_cell = ", ".join(nc_states) if nc_states else "n/a"

        # "Failing evals" excludes negative-control evals; those have their own column.
        failing_ids = sorted(
            eid for eid, ed in (m.get("eval_details") or {}).items()
            if (ed.get("pass_rate") or 0.0) < 1.0 and eid not in neg_ids
        )
        failing_cell = ", ".join(f"eval-{e}" for e in failing_ids) if failing_ids else "—"

        lines.append(
            f"| {mid} | {_pct(m.get('pass_rate'))} | {_pp(m.get('pass_rate_stddev'))} | "
            f"{_time(m.get('mean_time'))} | {nc_cell} | {failing_cell} |"
        )
    lines.append("")

    # Failures section: cross-model, grouped by eval, then by failed expectation text
    failing_evals: set[int] = set()
    for m in models.values():
        for eid, ed in (m.get("eval_details") or {}).items():
            if eid in neg_ids:
                continue
            if (ed.get("pass_rate") or 0.0) < 1.0 and ed.get("failed_expectations"):
                failing_evals.add(eid)

    if failing_evals:
        lines.append("### Failures")
        lines.append("")
        for eid in sorted(failing_evals):
            name = (prompts.get(eid) or {}).get("name") or f"eval-{eid}"
            failing_models = [
                mid for mid in sorted(models.keys())
                if (models[mid].get("eval_details") or {}).get(eid, {}).get("failed_expectations")
            ]
            if len(failing_models) == len(models) and len(models) > 1:
                who = "both models" if len(models) == 2 else "all models"
            else:
                who = ", ".join(f"`{m}`" for m in failing_models)
            lines.append(f"**eval-{eid} — \"{name}\" — {who}**")
            lines.append("")

            # Group failed expectations by their text so shared failures collapse.
            by_exp: dict[str, dict[str, str]] = {}
            order: list[str] = []
            for mid in failing_models:
                ed = models[mid]["eval_details"][eid]
                for fe in ed["failed_expectations"]:
                    text = fe["text"]
                    if text not in by_exp:
                        by_exp[text] = {}
                        order.append(text)
                    by_exp[text][mid] = fe["evidence"]
            for text in order:
                lines.append(f"> Expectation: *\"{text}\"*")
                lines.append("")
                for mid in failing_models:
                    if mid in by_exp[text]:
                        lines.append(f"- `{mid}`: {by_exp[text][mid]}")
                lines.append("")

    # Negative control section
    if neg_ids:
        lines.append("### Negative control")
        lines.append("")
        for eid in sorted(neg_ids):
            name = (prompts.get(eid) or {}).get("name") or f"eval-{eid}"
            lines.append(f"**eval-{eid} — \"{name}\" (negative_control: true)**")
            lines.append("")
            for mid in sorted(models.keys()):
                ed = (models[mid].get("eval_details") or {}).get(eid)
                if ed is None:
                    lines.append(f"- `{mid}`: not run")
                    continue
                pr = ed.get("pass_rate") or 0.0
                if pr >= 1.0:
                    lines.append(f"- `{mid}`: ✓ skill correctly suppressed")
                else:
                    fes = ed.get("failed_expectations") or []
                    if fes:
                        for fe in fes:
                            lines.append(f"- `{mid}`: ⚠ failed — {fe['evidence']}")
                    else:
                        lines.append(f"- `{mid}`: ⚠ partial pass ({_pct(pr)})")
            lines.append("")

    # Drift watch
    drift_bullets: list[str] = []

    high_stddev_models = [
        (mid, m) for mid, m in models.items()
        if (m.get("pass_rate_stddev") or 0.0) > DRIFT_STDDEV_THRESHOLD
    ]
    if high_stddev_models:
        # Surface the lowest-pass-rate eval(s) per model — usually what's driving the variance.
        worst_evals: dict[int, list[str]] = {}
        for mid, m in high_stddev_models:
            ed = m.get("eval_details") or {}
            if not ed:
                continue
            min_pr = min((e.get("pass_rate") or 1.0) for e in ed.values())
            if min_pr >= 1.0:
                continue
            for eid, e in ed.items():
                if (e.get("pass_rate") or 1.0) == min_pr:
                    worst_evals.setdefault(eid, []).append(mid)
        sd_bits = ", ".join(f"`{mid}` {_pp(m.get('pass_rate_stddev'))}" for mid, m in high_stddev_models)
        threshold_pp = int(DRIFT_STDDEV_THRESHOLD * 100)
        if worst_evals:
            ev_bits = "; ".join(
                f"eval-{eid} on {', '.join(f'`{m}`' for m in sorted(set(mids)))}"
                for eid, mids in sorted(worst_evals.items())
            )
            drift_bullets.append(
                f"⚠ Pass-rate stddev exceeds {threshold_pp} pp ({sd_bits}). Driven by {ev_bits}."
            )
        else:
            drift_bullets.append(f"⚠ Pass-rate stddev exceeds {threshold_pp} pp ({sd_bits}).")

    flipped_evals: set[int] = set()
    for m in models.values():
        for eid, ed in (m.get("eval_details") or {}).items():
            if ed.get("flipped"):
                flipped_evals.add(eid)
    if flipped_evals:
        drift_bullets.append(
            "⚠ Eval(s) flipped between runs of the same configuration: "
            + ", ".join(f"eval-{e}" for e in sorted(flipped_evals))
            + "."
        )

    if all(not m.get("has_baseline") for m in models.values()):
        drift_bullets.append(
            "No `without_skill` baseline data was collected, so skill-vs-no-skill drift "
            "can't be measured. Re-run with `--baseline` (or "
            "`--configs with_skill,without_skill`) to populate it."
        )

    if drift_bullets:
        lines.append("### Drift watch")
        lines.append("")
        for b in drift_bullets:
            lines.append(f"- {b}")
        lines.append("")

    return "\n".join(lines)


def _run_meta(run_dir: Path) -> tuple[int | None, str | None]:
    """Return (runs_per_configuration, llm_backend) from any benchmark.json under run_dir."""
    if not run_dir.is_dir():
        return None, None
    for model_dir in sorted(run_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        for skill_dir in sorted(model_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            bench = load_benchmark(skill_dir / "benchmark.json")
            if bench:
                meta = bench.get("metadata") or {}
                return meta.get("runs_per_configuration"), meta.get("llm_backend")
    return None, None


def render_detail_markdown(skills: dict, run_dir: Path, title: str) -> str:
    n_skills = len(skills)
    all_models: set[str] = set()
    eval_count = 0
    for entry in skills.values():
        all_models.update(entry["models"].keys())
        for m in entry["models"].values():
            eval_count = max(eval_count, len(m.get("eval_details") or {}))
    runs_per_config, backend = _run_meta(run_dir)

    lines: list[str] = [f"# {title}", ""]

    bits = [
        f"{n_skills} skill" + ("s" if n_skills != 1 else ""),
        f"{len(all_models)} model" + ("s" if len(all_models) != 1 else ""),
        f"{eval_count} eval" + ("s" if eval_count != 1 else ""),
    ]
    summary = "Run: " + " × ".join(bits)
    if runs_per_config is not None:
        summary += f" · {runs_per_config} runs/config"
    if backend:
        summary += f" · backend: {backend}"
    lines.append(summary)
    lines.append("Headline matrix: [`scorecard.md`](scorecard.md)")
    lines.append("")

    lines.append("## At a glance")
    lines.append("")
    lines.append(render_at_a_glance(skills))
    lines.append("")
    lines.append("**How to read this**")
    lines.append("")
    lines.append("- **Recommended model**: highest pass rate; ties broken by stability (lower stddev), then mean wall time.")
    threshold_pp = int(DRIFT_STDDEV_THRESHOLD * 100)
    lines.append(
        f"- **Drift signal**: flagged when pass-rate stddev > {threshold_pp} pp, when an eval flips between "
        "runs of the same config, or when the negative control fails. Cross-run drift requires a prior run "
        "to compare against."
    )
    lines.append(
        "- **Negative control**: the eval intentionally designed to make the skill *not* fire its full "
        "automation. A failure here is more serious than a normal miss."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for skill in sorted(skills.keys()):
        lines.append(render_skill_section(skill, skills[skill]))
        lines.append("---")
        lines.append("")

    lines.append("*Generated from `<run_dir>/<model>/<skill>/benchmark.json`.*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Directory containing <model>/<skill>/benchmark.json")
    parser.add_argument("--title", default="Cross-model benchmark scorecard")
    parser.add_argument("--detail-title", default=None,
                        help="Title for scorecard-detail.md (defaults to 'Scorecard detail — <run_dir name>').")
    parser.add_argument("-o", "--output-md", type=Path, default=None)
    parser.add_argument("-j", "--output-json", type=Path, default=None)
    parser.add_argument("--detail-output-md", type=Path, default=None,
                        help="Path for scorecard-detail.md (default: <run_dir>/scorecard-detail.md).")
    parser.add_argument("--no-detail", action="store_true", help="Skip writing scorecard-detail.md.")
    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir.is_dir():
        print(f"Not a directory: {run_dir}", file=sys.stderr)
        sys.exit(1)

    grid = discover_grid(run_dir)
    if not grid:
        print(f"No benchmark.json files found under {run_dir}", file=sys.stderr)
        sys.exit(1)

    md = render_markdown(grid, args.title)
    out_md = args.output_md or (run_dir / "scorecard.md")
    out_md.write_text(md)
    print(f"Wrote {out_md}")

    serial = {
        "run_dir": str(run_dir),
        "models": list(grid.keys()),
        "skills": sorted({s for m in grid.values() for s in m}),
        "grid": {
            model: {skill: {"pass_rate": cell.get("pass_rate"), "path": cell.get("path")} for skill, cell in skills.items()}
            for model, skills in grid.items()
        },
    }
    out_json = args.output_json or (run_dir / "scorecard.json")
    out_json.write_text(json.dumps(serial, indent=2) + "\n")
    print(f"Wrote {out_json}")

    if args.no_detail:
        return

    skills = discover_detail_grid(run_dir)
    if not skills:
        return
    detail_title = args.detail_title or f"Scorecard detail — {run_dir.name}"
    detail_md = render_detail_markdown(skills, run_dir, detail_title)
    out_detail = args.detail_output_md or (run_dir / "scorecard-detail.md")
    out_detail.write_text(detail_md)
    print(f"Wrote {out_detail}")


if __name__ == "__main__":
    main()
