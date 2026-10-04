# How to read a format

*Status: describes the layout of a format directory in this export, and how to run its tool.*

## The parts of a format directory

This export has five format directories: [`format_obligation/`](../format_obligation/),
[`format_relation/`](../format_relation/), [`format_trigger/`](../format_trigger/),
[`format_impact-rule/`](../format_impact-rule/) and
[`format_fill-contract/`](../format_fill-contract/). A format directory holds some of these parts:

| part | what it holds | obligation | relation | trigger | impact rule | fill contract |
|---|---|---|---|---|---|---|
| `README.md` | a status line, what the format is, an example, how to run its tools | yes | yes | yes | yes | yes |
| `spec/` | the specification: fields, events, rules, and the lifecycle | yes | — | — | — | — |
| `KNOWN-LIMITS.md` | what the format does not do, and what is still open | yes | — | — | — | — |
| `schema/` | a draft JSON Schema; the tool checks what the schema does not encode | — | yes | yes | yes | yes |
| `tools/` | the reference tools, each with its self-test | yes | yes | yes | yes | yes |
| `fixtures/` | test inputs; `cases.json` lists each case with its expected result | yes | yes | yes | yes | yes |

The obligation format has no JSON Schema; it is typed by its specification and its program. For
relation, trigger, impact rule and fill contract, the README carries the field definitions and the
limits, and the schema's descriptions name what only the tool checks.

Read in this order: the README, then the specification and known limits where the format has them, then the schema.

## How to run a format's tool

```bash
python3 format_obligation/tools/obligation_fold.py --selftest    # prints: SELFTEST PASS: N fixture cases
python3 format_obligation/tools/obligation_fold.py fold --events EVENTS.jsonl --boundary BOUNDARY.json
```

- The tools need Python 3 with its standard library; the obligation tool also needs a time-zone
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

You want to know whether the release-notes duty in the obligation README blocks a release. You run
the self-test, then `fold` with the event log and a boundary file. The JSON report gives each
occurrence a result and sets the block bit. You read `KNOWN-LIMITS.md` before you rely on the
result.
