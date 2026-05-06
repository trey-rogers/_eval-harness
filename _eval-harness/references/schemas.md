# JSON schemas (skill eval harness)

This document describes the JSON shapes used by `_eval-harness`: `evals/evals.json` per skill, per-run `grading.json`, optional `timing.json`, and aggregated `benchmark.json` consumed by `aggregate_benchmark.py` and `matrix_scorecard.py`.

---

## evals.json

Defines eval cases for a skill. Located at `<skill-root>/evals/evals.json` (usually generated from `evals/prompts.jsonl` via `scripts/jsonl_to_evals.py`).

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's example prompt",
      "expected_output": "Description of expected result",
      "files": ["evals/files/sample1.pdf"],
      "expectations": [
        "The output includes X",
        "The skill used script Y"
      ]
    }
  ]
}
```

**Fields:**

- `skill_name`: Name matching the skill directory / frontmatter
- `evals[].id`: Unique integer identifier
- `evals[].prompt`: The task to execute
- `evals[].expected_output`: Human-readable description of success
- `evals[].files`: Optional list of input file paths (relative to skill root)
- `evals[].expectations`: List of verifiable statements the grader checks

---

## grading.json

Written by the harness grader under each `run-*` directory.

```json
{
  "expectations": [
    {
      "text": "The output includes the name 'John Smith'",
      "passed": true,
      "evidence": "Found in the assistant response: 'John Smith'"
    }
  ],
  "summary": {
    "passed": 2,
    "failed": 1,
    "total": 3,
    "pass_rate": 0.67
  },
  "timing": {
    "executor_duration_seconds": 10.0,
    "grader_duration_seconds": 2.0,
    "total_duration_seconds": 12.0
  }
}
```

**Expectations:** Each item must use the keys `text`, `passed`, and `evidence`. `aggregate_benchmark.py` and scorecards assume this shape.

---

## timing.json

Optional per-run timing written alongside `grading.json`.

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3
}
```

---

## benchmark.json

Produced by `_eval-harness/scripts/aggregate_benchmark.py` under each `(model, skill)` directory. `matrix_scorecard.py` reads these files; field names and nesting should match this shape.

```json
{
  "metadata": {
    "skill_name": "submit-pr",
    "skill_path": "/path/to/submit-pr",
    "executor_model": "composer-2-fast",
    "analyzer_model": "<model-name>",
    "timestamp": "2026-01-15T10:30:00Z",
    "evals_run": [1, 2, 3],
    "runs_per_configuration": 1
  },
  "runs": [
    {
      "eval_id": 1,
      "configuration": "with_skill",
      "run_number": 1,
      "result": {
        "pass_rate": 0.85,
        "passed": 6,
        "failed": 1,
        "total": 7,
        "time_seconds": 42.5,
        "tokens": 3800,
        "tool_calls": 18,
        "errors": 0
      },
      "expectations": [
        {"text": "...", "passed": true, "evidence": "..."}
      ],
      "notes": []
    }
  ],
  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.85, "stddev": 0.0, "min": 0.85, "max": 0.85},
      "time_seconds": {"mean": 45.0, "stddev": 0.0, "min": 45.0, "max": 45.0},
      "tokens": {"mean": 3800, "stddev": 0, "min": 3800, "max": 3800}
    },
    "without_skill": {
      "pass_rate": {"mean": 0.35, "stddev": 0.0, "min": 0.35, "max": 0.35},
      "time_seconds": {"mean": 32.0, "stddev": 0.0, "min": 32.0, "max": 32.0},
      "tokens": {"mean": 2100, "stddev": 0, "min": 2100, "max": 2100}
    },
    "delta": {
      "pass_rate": "+0.50",
      "time_seconds": "+13.0",
      "tokens": "+1700"
    }
  },
  "notes": []
}
```

**Important:** Use `configuration` (not `config`), and keep numeric metrics under each run’s `result` object as shown. Altering names or nesting will break aggregation or scorecards.
