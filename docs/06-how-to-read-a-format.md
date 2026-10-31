# How to read a format

*Status: describes the layout of a format directory in this export, and how to run its tool.*

## The parts of a format directory

This export has five format directories: [`format_obligation/`](../format_obligation/),
[`format_relation/`](../format_relation/), [`format_trigger/`](../format_trigger/),
[`format_impact-rule/`](../format_impact-rule/) and
[`format_fill-contract/`](../format_fill-contract/). A format directory holds some of these parts:

| part | what it holds | obligation | relation | trigger | impact rule | fill contract |
|---|---|---|---|---|---|---|
| `README.md` | status, purpose, links to examples or fixtures, tool commands | yes | yes | yes | yes | yes |
| `spec/` | the specification: fields, events, rules, and the lifecycle | yes | — | — | — | — |
| `KNOWN-LIMITS.md` | what the format does not do, and what is still open | yes | — | — | — | — |
| `schema/` | JSON Schema contracts; tools also check rules beyond schema validation | output only | yes | yes | yes | yes |
| `tools/` | the reference tools, each with its self-test | yes | yes | yes | yes | yes |
| `fixtures/` | test inputs; `cases.json` lists each case with its expected result | yes | yes | yes | yes | yes |

Obligation inputs are defined by its specification and validator; there is no input JSON Schema.
Its [fold-report schema](../format_obligation/schema/obligation-fold-0.1.0-draft.schema.json) checks
outputs. For the other four formats, the README describes the fields and limits, and schema
descriptions name rules checked only by the tools.

Read in this order: the README, then the specification and known limits where the format has them, then the schema.

## How to run a format's tool

```bash
python3 format_obligation/tools/obligation_fold.py --selftest    # prints: SELFTEST PASS: N cases
python3 format_obligation/tools/obligation_fold.py fold --events EVENTS.jsonl --boundary BOUNDARY.json
```

- The tools need Python 3.11 or later with its standard library; the obligation tool also needs a time-zone
  database, and one tool, fill contract's `fill_observe.py`, needs `git` on `PATH`. The runner
  `gates/run_checks.sh` requires Python 3.11 or later, and runs every tool's self-test in this
  export; `SKIP_GIT_SELFTESTS=1` skips the one that needs git.
- Run `--selftest` first. A pass means that the reported self-test cases passed. It does not show
  that the tool is correct in general.
- The tool does not read the clock. The boundary file carries the recorded cutoff time.
- The tool does not use the network and does not write files. The `fold` command prints one JSON report.
- Exit code `0` means valid and not blocked. The format's README lists the other codes.

A tool checks a record. It does not check whether the record is true. What a tool can and cannot
decide is the subject of [What a program can decide](02-what-a-program-can-decide.md).

## Example

The obligation [evaluator-provenance fixture](../format_obligation/fixtures/fold-cases/r3-evaluator-provenance-pass.events.jsonl)
contains a duty and its recorded events. Its matching boundary and expected outcome are listed in
[the case index](../format_obligation/fixtures/fold-cases/cases.json). Run `fold` with those two
inputs to inspect the occurrence results and block bit. Read
[KNOWN-LIMITS.md](../format_obligation/KNOWN-LIMITS.md) before relying on the result.
