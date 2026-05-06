# Getting started (skill eval harness)

Path from zero to a working eval run. Authoring conventions, scorecard behavior, and advanced CLI flags live in [README.md](README.md).

## Prerequisites

- **Python 3** — used by compile helpers (`jsonl_to_evals.py`, etc.).
- **An LLM CLI on `PATH`** — the matrix runner shells out to one of these backends:


| Backend                                  | Binary   | Typical auth                                                                  |
| ---------------------------------------- | -------- | ----------------------------------------------------------------------------- |
| **Cursor** (default when `agent` exists) | `agent`  | `agent login` or `CURSOR_API_KEY` ([Cursor CLI](https://cursor.com/docs/cli)) |
| **Claude Code**                          | `claude` | Claude Code session / standard `claude` CLI authentication                    |


## 1. Put the CLI on your PATH

From the **skills repository root** (the directory that contains `bin/` and `_eval-harness/`):

```bash
export PATH="/absolute/path/to/skills/bin:$PATH"
```

Add that line to your shell profile if you use `ev` / `skilleval` often.

**Why `ev` and not `eval`?** In bash and zsh, `eval` is a **shell builtin** (it executes arguments as shell code). A program named `eval` on `PATH` is not run when you type `eval --list` — the shell runs the builtin instead. Use `**ev`**, `**skilleval`**, or an alias, e.g. `alias sk-eval='/absolute/path/to/skills/bin/skilleval'`.

## 2. Verify discovery (no API calls)

```bash
ev --list                                                  # auto-discovered skills + live model list
ev --list-cli-models                                       # raw model slugs from the backend CLI
```

Runnable skills are auto-discovered: each needs both `SKILL.md` and `evals/evals.json`.

Preview a run without calling any LLM:

```bash
ev -m composer-2-fast -s submit-pr --dry-run
```

(`--dry-run` does not require `agent` / `claude` on `PATH`.)

## 3. Run one skill × one model

Use a model id your backend accepts (see `ev --list` / `ev --list-cli-models` / `ev -h`):

```bash
ev --backend cursor -m composer-2-fast -s submit-pr
```

### Without `bin` on your PATH

Call the wrapper by **absolute path**; flags are the same as above.

```bash
/absolute/path/to/skills/bin/ev --list
/absolute/path/to/skills/bin/ev --backend cursor -m composer-2-fast -s submit-pr
```

`ev` and `skilleval` are thin wrappers around `_eval-harness/scripts/run_matrix.py` if you need to read or debug the implementation.

## Outputs

Runs write under `_eval-harness/results/<UTC>/` (or `--out`). Each cell includes transcripts, `grading.json`, and aggregated `benchmark.json`. With `--scorecard`, the run directory also gets `scorecard.md`, `scorecard-detail.md`, and `scorecard.json`. See [README.md](README.md) §4 (Outputs, Notes, and advanced flags).

## Editing eval prompts

Cases live in `<skill>/evals/prompts.jsonl`. After edits, regenerate `evals.json` from the **skills repo root**:

```bash
python3 _eval-harness/scripts/jsonl_to_evals.py submit-pr/evals/prompts.jsonl --skill-name submit-pr
```

Replace `submit-pr` with your skill directory name. Full field list and workflow: [README.md §3](README.md#3-authoring-behavioral-evals-promptsjsonl).