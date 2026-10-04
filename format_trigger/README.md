# Trigger format

**Status: first cut.** A draft JSON Schema, two reference tools, and self-tests with 67 + 70
cases that pass. See [Status](#status).

## What it records

A trigger is a rule that fires when a named event happens in a repository: "when a file under
`spec/` changes", "when the commit count reaches 12". A trigger file holds the triggers of one
repository. The format owns the vocabulary of event kinds; other formats in this family use that
vocabulary or narrow it.

## A small example

This is the test case `fixtures/validate/00-valid-minimal.toml`. It is valid.

```toml
trigger_version = "0.1.0"
repo = "acme"

[[triggers]]
id = "spec-change-alert"
retrigger = "every-event"

[triggers.when]
source = "acme:spec/"
event = "path-changed"

[triggers.when.params]
paths = ["spec/**"]
```

## The fields

| field | type | required | meaning |
|---|---|---|---|
| `trigger_version` | string | required | version of the file shape |
| `repo` | string | required | the repository that the file describes |
| `triggers` | array | required | the triggers |
| `triggers[].id` | string, `^[a-z0-9][a-z0-9-]*$` | required | unique within the file |
| `triggers[].retrigger` | `once` or `every-event` | required | fire on the first matching event only, or on each one |
| `when.source` | entity-ref | required | the scope. An event fires the trigger only if its source is inside this scope |
| `when.event` | one of 16 event kinds | required | the closed list is in the schema, for example `path-changed`, `first-release`, `commits-at-least`, `cadence` |
| `when.params` | table | depends on the event kind | `paths` (globs), `n` (an integer of at least 1), `every` (a duration such as `10d`) or `src_glob` |

An event record, one JSON object per line, has `event_id`, `event`, `source`, `at` and `params`.

A glob (a trigger's own `paths[]`/`src_glob`) is at most 1024 characters, at most 64 `/`-separated
segments, and at most 16 `*` characters in any one segment. A reported event path (an EVENT
record's own `params.paths`, a literal value, never a pattern) is at most 4096 characters and 256
segments. An entity-ref (`when.source`, an EVENT record's own `source`) is at most 4096 characters.
A trigger `id` is at most 256 characters (on top of its own `^[a-z0-9][a-z0-9-]*$` grammar). An
event's `n` parameter (`commits-at-least`, `days-since-setup`) is an integer from 1 to
9223372036854775807 (a signed 64-bit range). All of these are checked at validation time, not only
when a trigger is fired against an event.

### TOML and JSON input

Every TOML trigger file is checked for a non-finite number (`nan`, `inf`, `-inf`, or a float
expression that overflows to one of them) anywhere in the document, at any depth, before any other
rule runs — TOML's float grammar allows all three, but this format rejects them as a plain input
error rather than carrying one through to a report. A TOML integer literal too large for Python's
own integer-to-decimal-string conversion (around 4,300 digits) is reported the same way, never as
an uncaught failure.

Every EVENT and history record `trigger_fire.py` reads (one JSON object per line) is held to one
contract, checked at load time, before any other rule: no duplicate keys in one object (the parse
rejects a repeated key outright, not silently keeping the last one); no `NaN`, `Infinity` or
`-Infinity`, and no finite-looking literal that overflows to one of them (e.g. `1e400`), at any
depth — not valid JSON, even though a permissive parser may otherwise accept them; every string,
key or value, must encode as UTF-8 (a lone UTF-16 surrogate does not); and nesting is at most 64
levels deep, checked by walking the parsed value without recursion, so that a
pathologically deep-but-otherwise-valid input is rejected cleanly rather than exhausting the call
stack. Any violation is reported the same way an unparseable line already is: as the tool's
conforming input error, never a crash.

**The documented diagnostic bound:** no single reported issue's `reason` either tool ever emits is
longer than 2,000 characters, for any input, including an adversarial one — a 10,000-character
entity-ref or trigger-id argument, or a value so large that formatting it naively would itself be
unbounded (an oversized integer is never converted to a decimal string to report it; a digit-count
estimate is used instead). This is checked once more, unconditionally, by each tool's own report
constructor, so it holds even for a message built by a call site that forgot to self-bound.

## What the program checks, and what it does not

`trigger_validate.py` checks one trigger file: known keys, the event kind, the parameters that
each kind requires or forbids, the glob and duration grammar (including the size limits above),
the entity-ref grammar, and unique ids.

`trigger_fire.py` is a pure function. It reads a trigger file, a stream of events and an optional
history of earlier firings, and reports what fires. A trigger fires when the kind is equal, the
source is inside the scope, and the parameters match. Each firing is one **occurrence**, with an
id derived from the trigger id and the event id. A redelivered event is suppressed. A `once`
trigger fires at most one occurrence. If the trigger file is invalid, the result is "unknown",
not an empty list. Every non-success firing report, including usage and input/internal failures,
has `status: "unknown"`; its `now` is the validated caller timestamp, or `null` only if no valid
`--now` was parsed.

No program here produces events. Whether an event record is true is outside the check.

## Input-schema differential

The validator and firing self-tests generate property additions, removals, and allowed-type
witnesses from every positive trigger-file and event-record fixture, compare schema verdicts with
the real CLIs, and permit only one documented semantic rule: the required and allowed `params`
keys depend on `event` (unexported specification §§2, 3.7, and 4; schema
`$defs.when_params.description` and `x-semantic-only-rules[per-event-params-closed-set]`).

## Files

| path | content |
|---|---|
| [`schema/trigger-0.1.0-draft.schema.json`](schema/trigger-0.1.0-draft.schema.json) | draft JSON Schema for the trigger file, one event record, and the two tools' reports; shape and closed-vocabulary rules only |
| [`tools/trigger_validate.py`](tools/trigger_validate.py) | reference validator for one trigger file, with its self-test |
| [`tools/trigger_fire.py`](tools/trigger_fire.py) | the pure firing evaluator, with its self-test |
| [`tools/schema_conformance.py`](tools/schema_conformance.py) | stdlib-only fail-loud report-schema subset checker shared byte-for-byte across the five formats |
| [`tools/trigger_vocab.py`](tools/trigger_vocab.py) | the event vocabulary, the entity-ref grammar, the glob grammar, and the firing rule the two tools import; not run on its own |
| [`fixtures/validate/`](fixtures/validate/) | 76 files: 75 fixture inputs plus `cases.json`, which lists 41 validator cases and 37 firing cases with their expected results |
| [`fixtures/example/`](fixtures/example/) | one invented, illustrative trigger file and five invented change events, not a record of a real repository |

There is no `spec/` directory and no `KNOWN-LIMITS.md` in this export.

## Status

**First cut**: a specification (not exported as prose; the schema and the two tools carry the
rules), a validator, a firing evaluator, and self-tests that pass 67 + 70 cases. Here: 1 draft
schema, 4 tools, 82 test-case files, 67 + 70 self-test cases.

What is missing: the specification text as a standalone document; a program that produces event
records from a live repository — nothing in this export watches a repository or emits events; the
`fixtures/example/` events were written by hand for this publication.

Do not treat the list of event kinds as final. Do not treat this format as in routine use: no
check in this export runs it against real records.

The fixtures are synthetic; the example under `fixtures/example/` is invented for this
publication, not a record of any real repository.

## How to check it yourself

From the root of this tree:

```sh
python3 -B format_trigger/tools/trigger_fire.py --selftest
```

The last line of its output was:

```
SELFTEST PASS: 70 cases
```

`trigger_validate.py --selftest` ends with `SELFTEST PASS: 67 cases`.

Inventory: 88 files total, 4 tools, 82 test-case files, 67 + 70 self-test cases.

The stdout environment contract applies to report-producing commands: a tool writes its one report
completely to stdout and exits with its documented code, or exits 74 having written nothing
further; it never exits 120 or emits a traceback because of stdout. Stdout may be any object. A
real file descriptor of any number, or a text stream with `write`/`flush`, is fully supported. Any
other object, or any stdout operation failing with `OSError`, `ValueError`, `TypeError`,
`OverflowError`, `AttributeError`, or `io.UnsupportedOperation`, means stdout unavailable and exit
74. Exceptions from the tool's own logic still propagate. The only exclusions are argparse
`--help`/usage text (argparse's own output, not a report, and not routed through this boundary) and
external signals (`SIGINT`, `SIGPIPE` reset to `SIG_DFL`, `SIGKILL`), which terminate as the
OS/Python decide. There are no other scope exclusions.

No dependency beyond the Python 3.11 floor this export already requires.
