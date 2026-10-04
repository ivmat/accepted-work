# Impact-rule format

**Status: first cut.** A draft JSON Schema, two reference tools, and self-tests with 88 + 66
cases that pass. See [Status](#status).

## What it records

An impact rule maps a changed path, or a named event in a repository, to one or more updates that
must follow. A rule names who can notice the need for the update and who can make it, how
important the update is, and how urgent it is. A rules file holds the rules of one repository.

## A small example

This is the test case `fixtures/validate/00-valid-minimal.toml`. It is valid.

```toml
# Invented, independently-designed rules file (not a record of any real repository).
impact_version = "0.1.0"
repo = "alpha-service"

[[rules]]
id = "pricing-schema-freeze"
relation = "governs"
importance = "critical"
urgency = "before-commit"

[rules.when]
changed = ["schema/**", "fixtures/**"]

[rules.decided_by]
detect = "program"
fix = "human"

[[rules.must_update]]
target = "SCHEMA-FREEZE.md"
action = "re-verify"
check = "python3 tools/schema_digest.py --check"
note = "schema freeze digest must still match SCHEMA-FREEZE.md"
```

This rule says: when a file under `schema/` or `fixtures/` changes, `SCHEMA-FREEZE.md` must be
re-verified by running the given check command. A program can notice the need; a human must make
the fix. It is critical, and due before the commit lands.

## The fields

| field | type | required | meaning |
|---|---|---|---|
| `impact_version` | string | required | version of the file shape |
| `repo` | string, `<repo>` id grammar | required | the repository the file describes |
| `inherit` | array of strings | optional | carried, never dereferenced (see below) |
| `derive` | array | optional | derivers; accepted for shape only, never run (see below) |
| `rules` | array | required | the rules; may be empty |
| `rules[].id` | string, no control character | required | unique within the file, and across every `--rules` file in one invocation |
| `rules[].when` | table | required | exactly one of `changed` or `event` |
| `rules[].when.changed` | array of globs | one of `changed`/`event` | the rule fires if a changed path matches one of these globs |
| `rules[].when.event` | one of 9 event kinds | one of `changed`/`event` | the rule fires if this event is present; for example `new-dependency`, `first-test`, `public` — the closed list is in the schema |
| `rules[].relation` | one of 7 closed relations | required | `depends-on`, `template-of`, `pins`, `traces-to`, `records`, `governs`, `documents` |
| `rules[].must_update` | array | required, non-empty | the updates this rule requires |
| `rules[].must_update[].target` | string, no control character | required | the file, or other thing, that must be updated |
| `rules[].must_update[].action` | one of 4 closed actions | required | `re-verify`, `re-render`, `update`, `attend` |
| `rules[].must_update[].check` | string | optional | a shell command; exit 0 means the update is discharged. No tool here runs it |
| `rules[].must_update[].note` | string | optional | free text |
| `rules[].decided_by` | table | required | who can notice the need and who can make the fix (see below) |
| `rules[].importance` | one of 4 closed levels | required | `critical`, `high`, `normal`, `low`; used to sort a report's items |
| `rules[].urgency` | closed literal or `within:<duration>` | required | `before-commit`, `same-session`, `next-maturity-event`, or `within:<N>d`/`<N>h`/`<N>m` (a positive integer, no more than a 36500-day/100-year equivalent) |
| `rules[].retrigger` | `once` or `every-event` | optional, default `every-event` | fire one occurrence per rule, or one per matching event |

`decided_by.detect` and `decided_by.fix` are each one of `program`, `model`, `human` — a closed set
of actor-kind tags. They record which kind of actor can notice the need for an update, and which
kind can make it.

## Repo ids, globs, and changed paths

A `<repo>` id — the top-level `repo` field, and any `<repo>:` cross-repo prefix inside a glob or a
changed path — matches `^[a-z0-9][a-z0-9_-]*$`, 1 to 64 characters, with no control character. This
is the same grammar the relation format uses for its own repo ids. A mistyped `repo` field (any
non-string value, an oversized integer included) is reported with a bounded diagnostic — never the
literal value formatted directly, which could itself raise for a sufficiently large integer (Python
bounds integer-to-decimal-string conversion); the same bounded formatter covers every foreign value
this tool's error messages interpolate, scalar or nested inside a list/table, to any depth.

**The documented diagnostic bound:** no single reported issue's `reason` either tool ever emits is
longer than 2,000 characters (`impact_rules.DIAGNOSTIC_REASON_MAX_CHARS`), for any input, including
an adversarial one — a 10,000-character `--rules`/`--changed-file` path argument, an unreadable-file
exception, or a value so large that formatting it naively would itself be unbounded. This is checked
once more, unconditionally, by each tool's own report constructor, so it holds even for a `reason`
built by a call site that forgot to self-bound. Report fields that echo an input value as given
(`repo`, `impact_version`, `files[]`) are a different thing and are NOT size-capped — a report always
names the exact file(s)/`repo`/`impact_version` it was given, even an invalid one, and only the
diagnostic `reason` text itself is bounded.

A `when.changed[]` glob is a repo-relative POSIX path pattern. It must not start with `/`, and it
must not contain a `.` or `..` segment, an empty segment, or a control character. A segment that is
exactly `**` matches zero or more whole path segments. Any other segment matches literal characters
plus `*` (`*` matches any run of characters, but never crosses a `/`). The allowed characters in a
non-`**` segment are letters, digits, `_`, `.`, `-`, `*`. A glob may carry a cross-repo prefix,
`<repo>:<glob>`; it is matched only against a changed-path entry carrying the identical `<repo>:`
prefix. Size limits, checked at validation time, not only when matching runs: at most 1024
characters, 64 `/`-separated segments, and 16 `*` characters in any one segment.

A `--changed`/`--changed-file` entry (a changed path) is checked against the same grammar, at a
larger size ceiling, since it is a real reported value, not a small authored pattern: at most 4096
characters and 256 segments. A `*` inside a changed path is a literal character, never a wildcard.
A changed path may carry a cross-repo prefix too. An entry that fails this grammar is reported as
an input error — never a crash, and never a silent non-match.

### The changed-file line contract

`--changed-file` names a text file, one changed path per line. UTF-8 only. Lines split on LF only.
A single trailing CR per line (CRLF) is accepted and stripped; a bare CR is rejected as a record
separator. A blank line, or a line that starts with `#` after trimming leading space/tab, is a
comment or blank line, and is skipped. Any OTHER line carrying leading or trailing space or tab is
rejected, not trimmed — a changed-path entry is used exactly as given, since a real file path may
legally begin or end with a space. At most 100,000 lines.

## What the programs check, and what they do not

`impact_validate.py` checks one or more `--rules` files as a single merged set: known keys,
required keys, every closed enum, the glob grammar (including the size limits above), the repo-id
grammar, a rule id and `must_update[].target` with no control character, and rule-id uniqueness
within one file and across every `--rules` file passed together — a later file may not redefine an
earlier file's rule id.

`inherit` is carried and shape-checked (an array of non-blank strings), but never resolved to a
file. The only way one rules file builds on another, in this first cut, is passing multiple
`--rules FILE` arguments to the same invocation, in order.

`derive` entries are checked for shape and closed name only: `erlang-xref`, `pinned-sha`,
`declared-coverage`, `git-cochange`, either as a bare name or as a single-key table,
`{ <name> = { ...params } }`. No deriver ever runs. `impact_evaluate.py` reports every validated
`derive` entry as `not-evaluated`; a `derive` entry never contributes a fired entry.

`impact_evaluate.py` is a pure function. It reads the same merged rules set — gated on that set
validating clean; an invalid rules set reports `status: "unknown"`, with `fired` and
`not_evaluated_derivers` both `null`, never an empty list — plus a set of changed paths and/or
event names, and reports every rule whose `when` matches: a `changed` glob against at least one
changed path, or an `event` against the supplied event names. It sorts the result by importance,
then urgency, then rule id, then target. A no-match evaluation is success (`fired: []`), never an
error.

With `--obligations`, each fired entry gains a REPORT-ONLY SUMMARY: `trigger`, `action`, `target`,
`required_by`, `discharge_predicate`, `retrigger`, `priority`. This summary does **not** conform to
this family's obligation-record format and is not itself an obligation record — it carries none of
that format's own required identity or provenance fields. A caller that wants a real obligation
record must build one from this summary; the summary itself needs conversion first, and is not a
shortcut around that.

`--now` must match one strict form exactly — `YYYY-MM-DDTHH:MM:SS` followed by `Z` or an explicit
`[+-]HH:MM` offset, checked before parsing, never trimmed first. BOTH the clock time's own
hour/minute/second AND the offset's hour/minute are bounded to a real clock value (`00`-`23` for
every hour field, `00`-`59` for every minute/second field), enforced IN the grammar itself, never
left to `datetime.fromisoformat`'s own parsing — that call otherwise accepts `T24:00:00` as a second,
non-canonical spelling of the next day's midnight (silently rolling the date over) and normalizes an
out-of-range offset such as `+00:60` or `+12:99` instead of rejecting it, so both are caught before
parsing ever runs. Its year must not exceed 9000; converting it to UTC must not overflow or underflow the
representable date range either (an early date with a positive offset, or a late date with a
negative offset, can do this). All of this is a usage error, never a runtime failure. The year
bound, together with `urgency`'s own 36500-day maximum above, guarantees `--obligations`' `now +
<duration>` arithmetic can never overflow — an out-of-range `urgency` is always a validation error,
never a runtime failure either. A `within:<duration>`'s digit count is itself bounded (15 digits)
and checked before the digits are ever converted to a number, so an absurdly long numeral is
rejected as malformed rather than exhausting the conversion itself.

A `derive[]` entry's params must contain only values JSON itself can represent, AND every integer
inside them, however deeply nested in a table or an array, must fit `-(2**63)..2**63-1` (a signed
64-bit range). Three TOML-only cases are rejected at validation time, never normalized: a date, a
datetime, or a time (three scalar types JSON does not have); a non-finite float (`nan`, `inf`,
`-inf`, or any float expression that overflows to one of them, such as a huge exponent); and an
integer outside that 64-bit range — not because JSON itself cannot represent a larger integer
(JSON integers are unbounded), but because Python's own JSON encoder converts an integer to decimal
text to serialize it, and refuses a conversion whose digit count crosses its own, independent
limit. A param table is carried, unevaluated, straight into the evaluate report's
`not_evaluated_derivers[].params`, and this format does not guess what normalized form an unrun
deriver would have wanted. As a final safeguard, both tools serialize every report with JSON's own
strict number grammar (no bare `NaN`/`Infinity` tokens), so a non-finite value cannot reach a
report by any path.

No program here writes to a repository, or mints anything.

### Exit codes

`impact_validate.py` — `0` valid; `10` a required key is missing; `11` an unknown key; `12` a wrong
shape (including an invalid `<repo>`/rule id/target); `13` a value outside a closed enum; `14` a
glob fails grammar or a size limit; `15` `when` carries both `changed` and `event`, or neither; `16`
a duplicate rule id; `17` a malformed `urgency`, including one past the 36500-day maximum or with
more than 15 duration digits; `18` a `derive` entry outside the closed deriver list, malformed, with
a param value JSON cannot represent (a date/datetime/time, or a non-finite float), or with a param
integer outside `-(2**63)..2**63-1`; `64` usage error; `65` an unreadable or malformed `--rules`
file; `74` stdout unavailable, so no report can be written; `99` self-test mismatch.

`impact_evaluate.py` — `0` evaluation ran (zero matches is success, not an error; `status: "ok"`,
`fired`/`not_evaluated_derivers` both arrays); `10`-`18` the rules did not validate (same classes as
above; `status: "unknown"`, `fired`/`not_evaluated_derivers` are `null`); `64` usage error (no
`--rules`; both `--changed` and `--changed-file` given; an `--event` outside the closed vocabulary;
a malformed `--now` — wrong grammar, an out-of-range offset hour/minute, an impossible calendar
date/time, a year past the year-9000 bound, or a timezone conversion that overflows/underflows);
`65` an unreadable/malformed `--rules` file or `--changed-file`, a `--changed`/`--changed-file`
entry failing the changed-path grammar or size limits, or an unexpected exception reaching the
catch-all; `74` stdout unavailable, so no report can be written; `99` self-test mismatch. Every
non-`0` exit other than 74 reports `status: "unknown"` — the schema's
own `evaluate_report.status` enum has only `ok`/`unknown`, so a usage error or an unreadable file is
reported the same way every other non-ok exit is, never a third status value.

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

## Input-schema differential

The validator self-test generates property additions, removals, and allowed-type witnesses from
every positive rules-file fixture, compares schema verdicts with the real CLI, and permits only one
documented semantic rule: `rule.when` carries exactly one of `changed` and `event` (unexported
specification §2.3; schema `$defs.when.description` and
`x-semantic-only-rules[when-exactly-one-of-changed-or-event]`).

## Files

| path | content |
|---|---|
| [`schema/impact-rule-0.1.0-draft.schema.json`](schema/impact-rule-0.1.0-draft.schema.json) | draft JSON Schema for the rules file and the evaluate report; shape and closed-vocabulary rules only |
| [`tools/impact_validate.py`](tools/impact_validate.py) | reference validator for one or more `--rules` files, with its self-test |
| [`tools/impact_evaluate.py`](tools/impact_evaluate.py) | the pure evaluator, with its self-test |
| [`tools/impact_rules.py`](tools/impact_rules.py) | the closed vocabularies, the grammar checks, and the evaluator the two tools import; not run on its own |
| [`fixtures/validate/`](fixtures/validate/) | 66 files: 61 rules-file fixtures (`.toml`), 4 changed-path list fixtures (`.txt`), plus `cases.json`, which lists 61 validator cases and 39 evaluator cases with their expected results |
| [`fixtures/example/`](fixtures/example/) | one invented, illustrative rules file and five invented changed-path lists, not a record of a real repository |

There is no `spec/` directory and no `KNOWN-LIMITS.md` in this export.

## Status

**First cut**: a specification (not exported as prose; the schema and the two tools carry the
rules), a validator, a pure evaluator, and self-tests that pass 88 + 66 cases. Here: 1 draft
schema, 4 tools, 72 test-case files, 88 + 66 self-test cases.

What is missing: the specification text as a standalone document; the four `derive` entries never
run; `inherit` is never dereferenced to a file; a program that turns an `--obligations` summary
into a real obligation record.

Do not treat the list of event kinds, relations, or derivers as final. Do not treat this format as
in routine use: no check in this export runs it against a real repository.

The fixtures are synthetic; the example under `fixtures/example/` is invented for this publication,
not a record of any real repository.

## How to check it yourself

From the root of this tree:

```sh
python3 -B format_impact-rule/tools/impact_validate.py --selftest
python3 -B format_impact-rule/tools/impact_evaluate.py --selftest
```

The last lines of their output were:

```
SELFTEST PASS: 88 cases
SELFTEST PASS: 66 cases
```

Inventory: 78 files total, 4 tools, 72 test-case files, 88 + 66 self-test cases.

No dependency beyond the Python 3.11 floor this export already requires. Neither tool uses `git`.
