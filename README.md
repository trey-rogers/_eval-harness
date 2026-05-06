# Skill eval harness (M2)

This directory defines **behavioral evals** for Cursor skills within a repository, **cross-model benchmarking** against any model your installed CLI accepts (`agent --list-models` for Cursor, Anthropic ids for Claude Code), and scripts to compile prompt packs into `evals/evals.json` (schema: [`references/schemas.md`](references/schemas.md)).

**New here?** Start with [GETTING_STARTED.md](GETTING_STARTED.md) (PATH, auth, first `ev` run, compiling `prompts.jsonl`).

Skills are **auto-discovered** from the filesystem: any directory under the skills repo root that contains both `SKILL.md` and `evals/evals.json` is runnable through the harness — there is no central registry to maintain.

## 1. Goals

- **Repeatable cases**: Same prompts and expectations across iterations and models.
- **Discriminative expectations**: Prefer checks that fail when the agent skips key workflow steps.
- **Negative control**: At least one case that should *not* trigger inappropriate automation or over-confident completion claims.

## 2. Layout


| Path                          | Purpose                                                                                                   |
| ----------------------------- | --------------------------------------------------------------------------------------------------------- |
| `template/prompts.jsonl`      | Minimal JSONL template (copy per skill).                                                                  |
| `template/rubric.example.md`  | Optional qualitative rubric for judges.                                                                   |
| `scripts/jsonl_to_evals.py`   | Compile `evals/prompts.jsonl` → `evals/evals.json`.                                                       |
| `scripts/matrix_scorecard.py` | Build `scorecard.md`, `scorecard-detail.md`, and `scorecard.json` from per-model `benchmark.json` files.  |
| `scripts/aggregate_benchmark.py` | Roll up per-eval `grading.json` files into `benchmark.json` / `benchmark.md` under each model × skill run. |
| `scripts/run_matrix.py`       | Run executor + grader via Cursor `agent -p` or Claude `claude -p`; aggregate + optional scorecard.        |
| `references/schemas.md`       | JSON shapes for `evals.json`, `grading.json`, `benchmark.json`.                                         |
| `bin/skilleval`               | Thin wrapper around `run_matrix.py` — **use this CLI** for matrix runs (see §4).                          |
| `bin/ev`                      | Symlink to `skilleval` (short command).                                                                   |
| `results/`                    | Gitignored run outputs (create as needed).                                                                |


Use `**ev` / `skilleval`** for running the eval matrix (discovery, dry-run, benchmarks, `--scorecard`). Other helpers (`jsonl_to_evals.py`, `matrix_scorecard.py`) are invoked with `python3` from the repo root as documented below.

Each skill that participates ships:

```
<skill-root>/evals/
├── prompts.jsonl    # Source of truth for cases (edit here)
├── evals.json       # Generated; do not hand-edit unless you know the sync story
└── rubric.md        # Optional; copy from template if you want weighted criteria
```

After changing `prompts.jsonl`, regenerate `evals.json` with `python3 scripts/jsonl_to_evals.py` — see [GETTING_STARTED.md § Editing eval prompts](GETTING_STARTED.md#editing-eval-prompts).

## 3. Authoring behavioral evals (prompts.jsonl)

**Workflow**

1. Copy `template/prompts.jsonl` into `<skill>/evals/prompts.jsonl`.
2. Replace template rows with **3–6** realistic cases (4 is a good default).
3. Include **at least one** row with `"negative_control": true` — typically a near-miss where the skill must not run its full automation, must ask for scope, or must refuse misleading success.
4. Each line is one JSON object with:
  - `**id`** (int, unique): stable identifier for the eval.
  - `**prompt`** (string): the user message to execute against.
  - `**expectations`** (array of strings): objectively checkable statements (transcript, commands proposed, file paths, headings present, etc.).
  - `**expected_output`** (string, optional): short human description; defaults from `name` if omitted.
  - `**files**` (array, optional): fixture paths relative to the skill root (usually empty for these workflow skills).
  - `**name**` (string, optional): short label for viewers and benchmarks.
  - `**negative_control**` (bool, optional): annotate the negative-control case; does not change schema in `evals.json` but documents intent for authors and graders.
5. Optionally add `evals/rubric.md` for human or LLM-judge scoring when binary expectations are insufficient.
6. Run `python3 scripts/jsonl_to_evals.py` (see [GETTING_STARTED.md](GETTING_STARTED.md#editing-eval-prompts)) to refresh `evals/evals.json`.
7. Either run `**ev` / `skilleval**` (see §4), or produce the same artifacts by hand: lay out per-eval directories with `grading.json` under each config (see [`references/schemas.md`](references/schemas.md)), then run `python3 scripts/aggregate_benchmark.py <that-skill-run-dir> --skill-name <name> --skill-path <path>` so `benchmark.json` lands next to the eval tree (for example under `results/<run>/<model_id>/<skill_name>/`).

**Cross-model scorecard**

After you have one `benchmark.json` per model per skill under a single run directory (standalone scorecard from existing outputs; when you run the matrix with `--scorecard`, `ev` / `skilleval` invokes this for you):

```bash
python3 scripts/matrix_scorecard.py results/<run_timestamp>
```

This writes three artifacts under the run directory:

- `scorecard.md` — headline pass-rate matrix (model × skill) with a spread column.
- `scorecard.json` — machine-readable version of the matrix.
- `scorecard-detail.md` — per-skill drill-down for two questions: *which skills might be drifting* and *which model produces the best output for each skill*. Each skill section lists the recommended model (highest pass rate; ties broken by lower stddev, then mean wall time), a per-model summary table, every failed expectation with the grader's evidence, an explicit negative-control verdict, and a "drift watch" block that flags pass-rate stddev > 15 pp, evals that flip between runs of the same configuration, missing baseline data, and negative-control failures. Pass `--no-detail` to skip it, or `--detail-output-md` / `--detail-title` to override the path or title.

**Model IDs**

Pass any model id your installed CLI accepts to `-m`. Folder names under `results/<run>/` are derived from those ids (slashes are sanitized) so scorecards line up automatically.

To see what the installed CLI accepts: run `ev --list` (auto-discovered runnable skills + live model list from your `--backend` CLI), `ev --list-cli-models` (raw CLI output, no skills), or `ev -h` (truncated model list in the help epilog).

## 4. CLI: run models × skills (`ev` / `skilleval` → `run_matrix.py`)

PATH, backend auth, first discovery/run, `ev` vs shell `eval`, absolute-path invocation, and the `jsonl_to_evals.py` compile command: **[GETTING_STARTED.md](GETTING_STARTED.md)**.

Invocation is via `**ev`** or `**skilleval*`* in `bin/` (wrappers around `scripts/run_matrix.py`). Running real evals requires an LLM CLI on **PATH** (Cursor `agent` or Claude Code `claude`).

`**--backend`**: `auto` (default) picks `EVAL_MATRIX_BACKEND` if set to `cursor` or `claude`, else `agent` if on PATH, otherwise `claude`. Use `--backend cursor` or `--backend claude` to force.

**Model ids** passed to `-m` must match the backend: use `**--list-cli-models`** or `**-h` / `--help`** (truncated) for Cursor slugs from `agent --list-models`, or Claude Code’s listing / Anthropic-style ids for `claude`.

**Runtime errors:** when an executor or grader CLI call exits non-zero, the harness prints `**[eval-harness] … failed`** to **stderr** with exit code, stderr/stdout excerpts, and the run directory (for example quota or usage limits). If the grader returns non-JSON, stderr includes a pointer to `grader_raw.txt`.

**Skill identifiers** for `-s` / `--skill` are auto-discovered from the filesystem. Pass either:

- A short directory leaf (e.g. `submit-pr`, `systematic-debugging`) when it’s unambiguous, or
- A relative path from the skills repo root (e.g. `superpowers/skills/systematic-debugging`) when two skills share the same leaf name.

A directory only counts as a runnable skill when it contains both `SKILL.md` and `evals/evals.json`. Run `ev --list` to see the current set.

### Examples (advanced)

```bash
# A/B baseline: same eval run twice, with and without the skill injected
# Useful for deciding whether a skill has atrophied and could be removed.
ev -m composer-2-fast -s add-action-logs --baseline

# Equivalent to --baseline, but written explicitly:
ev -m composer-2-fast -s add-action-logs --configs with_skill,without_skill

# Baseline only (no skill at all in the prompt)
ev -m composer-2-fast -s add-action-logs --configs without_skill

# Cross-model scorecard
skilleval -m composer-2-fast,gpt-5.2-codex-high -s submit-pr,review-pr --scorecard

# Claude Code backend (model ids must match `claude`)
ev --backend claude -m claude-sonnet-4-20250514 -s submit-pr

# Smoke executor only (placeholder grades)
ev --backend cursor -m composer-2-fast -s submit-pr --no-grade

# Subset of eval ids; app repo as workspace (--cwd); skills repo stays on --repo-root
ev --backend cursor -m composer-2-fast -s submit-pr \
  --eval-ids 1,2,6 --cwd /path/to/cmpt-android --repo-root /path/to/.cursor/skills
```

**Outputs** (under `--out` or `results/<UTC>/`):

- `<model_id>/<skill_name>/eval-<id>/<with_skill|without_skill>/run-1/` — `eval_metadata.json`, `transcript.md`, `outputs/response.md`, `grading.json`, `timing.json`
- `<model_id>/<skill_name>/benchmark.json` — from `scripts/aggregate_benchmark.py` with `metadata.executor_model` and `metadata.llm_backend` set
- `scorecard.md` / `scorecard-detail.md` / `scorecard.json` — when `--scorecard` is passed

**Notes**

- Each eval runs **executor** then **grader** (second headless LLM call) unless `--no-grade`.
- `--grader-model` overrides the grader model; executor uses `-m` for that column.
- `--dry-run` does not call CLIs (no `agent` / `claude` required on PATH).
