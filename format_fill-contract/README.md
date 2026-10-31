# Fill-contract format

**Status: first cut.** A draft JSON Schema, three reference tools, and self-tests with
79 + 81 + 77 planned self-test cases. See [Status](#status).

## What it records

A fill-contract file names, for one or more tracked files in a repository, what "filled" means
and when filling becomes due. "Filled" is a fact about the file's own content (a placeholder
marker is gone, a table has grown past its template size, a dated stamp is present). "Due" is a
fact about the repository's own maturity (the first commit touching `src/`, a commit count, days
since the repository was set up).

Three tools do the work, kept deliberately separate:

- `fill_validate.py` checks that one contract file is well-formed.
- `fill_observe.py` reads a repository (read-only) and a contract, and reports the raw facts the
  contract's own rules need — never a filled/due decision.
- `fill_evaluate.py` reads a contract and an observation report and decides, for each tracked
  file, one of five states: `not-due`, `due`, `overdue`, `filled`, `unknown`.

## A small example

This is one entry of `fixtures/validate/01-valid.toml`, an invented example (not a record of any
real repository or template) — the fixture has four entries, each with its own shape.

```toml
[[contracts]]
file = "ONBOARDING.md"
importance = "critical"
urgency_after_due = "3d"
template_rows = 1
filled_when = { all = [ { predicate = "no-marker" }, { predicate = "min-lines", n = 10 }, { predicate = "digest-present" } ] }
due_when = { any = [ { predicate = "first-dependency" }, { predicate = "commits-at-least", n = 12 } ] }
adequacy = { decided_by = "model", when = "on-filled" }
```

`ONBOARDING.md` is filled once it has no leftover marker, at least 10 lines, and a frontmatter
digest — all three, together. It becomes due once the repository's first commit adding a
dependency manifest lands, or once the repository reaches 12 commits — and is then `overdue` if it
is still not filled 3 days after that. The fixture's other three entries use a bare predicate with
no combinator at all, a `not`-wrapped predicate, and a nested `any`-of-`all`; the full file is the
fixture itself.

## The fields

| field | type | required | meaning |
|---|---|---|---|
| `fill_version` | string | required | version of the file shape |
| `template` | string | required | free-text label for the template this contract goes with |
| `contracts` | array | required | one entry per tracked file; may not be empty |
| `contracts[].file` | string, path grammar | required | the tracked file's path, relative to the repository root |
| `contracts[].importance` | `critical`, `high`, `normal` or `low` | required | used only to sort a report's items |
| `contracts[].urgency_after_due` | duration (`<int>d`, 1..36500) | required | how long after `due_when` fires before the state becomes `overdue` |
| `contracts[].template_rows` | integer, 0..100000 | required when `filled_when` uses `rows-beyond-template`, otherwise optional (but still type-checked if present) and unused | the template's own row count, subtracted from the observed count |
| `contracts[].filled_when` | predicate-expression tree | required | when the file counts as filled |
| `contracts[].due_when` | predicate-expression tree | required | when the file becomes due |
| `contracts[].retrigger` | table (`on` = event list, and/or `every` = duration) | optional | carried, not evaluated (see below) |
| `contracts[].adequacy` | table (`decided_by`, `when`) | optional | carried, not evaluated (see below) |

A predicate-expression tree is one leaf, `{ predicate = "<name>", ...params }`, or one combinator,
`{ any = [...] }`, `{ all = [...] }` or `{ not = <tree> }`. A leaf never also carries a combinator
key, and a combinator node never carries any key other than its own (`any`/`all`/`not`) — exactly
one of the four shapes, always, with no stray sibling keys either way. `not` is allowed only inside
`filled_when`; a maturity event has no well-defined due date to negate. A tree nesting deeper than
64 levels is rejected, not silently truncated. Every object this format's tools parse — the
contract's own top level, each entry, every combinator node, `retrigger`, `adequacy`, and every
level of an observations document — is closed: an unrecognized key is always a validation error.

`filled_when` uses one of 9 closed predicates: `no-marker`, `not-contains`, `rows-beyond-template`,
`min-lines`, `stamp`, `fresh`, `digest-present`, `declared-rules`, `command`. `due_when` uses one of
11: `instantiated`, `first-src-commit`, `first-test`, `first-spec`, `first-dependency`,
`first-release`, `commits-at-least`, `days-since-setup`, `public`, `session-closed`,
`new-dependency`. `command`, `public`, `session-closed` and `new-dependency` are recognized names
that this first cut never resolves to true or false — a predicate using one always evaluates to
`unknown`. A predicate's own integer parameter (`min`, `n`) must be between 1 and 100000 — "at
least N" has no meaningful N=0 reading.

A duration is `<int>d` only (day-granular; no `h` unit — every fact this format observes is itself
day-granular), between 1 and 36500 days, matched as an exact string (no leading/trailing whitespace).
Only ASCII decimal digits are allowed. Leading zeros are accepted; the significant digit count is
checked before integer conversion, including for arbitrarily long input. Out-of-bounds urgency,
freshness, and retrigger durations use validation codes 12, 13, and 16 respectively. Observation
counts are integers in 0..10000000. A
`file` path is at most 4096 characters and 256 `/`-separated segments, with no `..` segment, no
absolute path, no leading `:`, and no control character. A glob (an observer's own `--src-glob`/
`--test-glob`/`--spec-glob`/`--manifest` argument, turned into a git pathspec) is at most 1024
characters, 64 segments, and 16 `*` characters in any one segment, with the same `..`/absolute/
control-character rules, plus no leading `:`. Both grammars forbid a leading `:` for the same
reason: it is git pathspec magic, not a literal path/glob character — a `file`/glob value is always
passed to git as a literal (`:(literal)<path>`) or glob (`:(glob)<glob>`) pathspec, and an ordinary
leading `:` inside the value itself would otherwise be read as a second, nested pathspec prefix. An
observer's `--src-glob`/`--test-glob`/`--spec-glob`/`--manifest` argument given but split to an
empty list (e.g. `--src-glob ""`, or `--src-glob ","`) is a usage error (15), not a silent fallback
to the defaults. All of these are checked at validation time, as an exact-string match — never after
trimming whitespace first.

Every date uses exactly `YYYY-MM-DD` (ASCII digits and hyphens), checked with `fullmatch` and
calendar validation. Basic ISO dates, week dates, impossible days, and extra whitespace fail.
This applies to maturity dates, commit dates, stamps, and last-modified dates. Observation `now`
and CLI `--now` accept that date alone or `YYYY-MM-DDTHH:MM:SS` (hours 00–23, minutes and seconds
00–59 — `T24:00:00` is not accepted as a second spelling of the next day's midnight), optionally
followed by a decimal point and 1–6 fractional-second digits, then optional `Z` or an offset
`±HH:MM` (hours 00–23, minutes 00–59). No trimming occurs. A missing offset means UTC; supplied
offsets are converted to UTC before evaluation. Invalid timestamps and UTC conversion overflow are
CLI usage errors (64); invalid observation `now` is an observation shape error (13). All
chronological comparisons use parsed dates. A **commit date** is likewise put on its UTC calendar
day (the author's own offset is converted to UTC before the date is taken) before any comparison
against `--now` (also UTC) — the two sides of every such subtraction are always on the same
calendar. `maturity.commit_dates` is the list of commit author days SORTED oldest-first — not the
history order a plain `git log --reverse` produces (a cherry-pick, a rebase, an amend of an old
commit, or a contributor with a skewed clock can make a later-in-history commit carry an earlier
author date) — and, when present, must be non-decreasing and, when `commits_count` is also present,
the same length as it — `commits-at-least`'s own due-date lookup indexes this array positionally and
trusts both.
An invalid stamp is not retained; a requested key with no valid stamp has value `null`.

### JSON input

`fill_evaluate.py`'s `--observations` file is foreign input, held to one contract, checked at load
time, before any field of it is read: no duplicate keys in one JSON object (a repeated key is
rejected outright, not silently kept as the last value); no `NaN`, `Infinity` or `-Infinity`; every
string, key or value, must encode as UTF-8 (a lone UTF-16 surrogate does not); nesting is at most
64 levels deep, checked by walking the parsed value without recursion. Past that, every field is
checked against its expected shape (a count is an integer, a date is a real calendar date, a stamp
table maps strings to dates or null, and so on) before `fill_evaluate.py` decides a single state.
A file failing either check is reported the tool's own conforming input error — never a crash.
Exponent overflow such as JSON `1e999` is also rejected. TOML `nan`, `inf`, `-inf`, and `1e999`
are rejected by field validation, including in carried `retrigger` and `adequacy` fields. Printed
JSON reports use `allow_nan=False` as a final safeguard.

## Observing a repository

`fill_observe.py` needs a `git` binary on `PATH`. It runs `git log`, `git tag` and `git rev-parse`
against `--repo`, read-only — never a command that writes to the repository. Its own self-test
builds temporary git repositories under a temp directory, with a hermetic environment (every
inherited `GIT_*` variable stripped, an isolated `HOME`, no hooks, no signing, a fixed commit
identity and date) so it never touches the caller's real git state. `SKIP_GIT_SELFTESTS=1 bash
gates/run_checks.sh` skips this tool's self-test; a skip is reported as a skip, not counted as a
pass.

`--repo` must be a git repository's own top level, never a subdirectory of one — checked by directory
IDENTITY (`os.path.samefile` against `git rev-parse --show-toplevel`, i.e. inode/device, not a
string comparison of resolved paths, which an alias spelling such as a case-variant path on a
case-insensitive filesystem would wrongly fail): a subdirectory would otherwise get repository-wide
facts (commit count, first-commit dates) describing the WHOLE repository while per-file reads stayed
relative to the subdirectory — a mismatch, not a usable observation. A **shallow clone** (for
example `actions/checkout`'s own default of `fetch-depth: 1`) is detected once
(`git rev-parse --is-shallow-repository`) and reported honestly: every maturity fact that depends on
finding the OLDEST matching commit (the repository's own first commit, the first commit touching
`src/**`/`test/**`/`spec/**`/a manifest glob, the total commit count, every commit's date, days
since instantiated) is left out of the observation entirely rather than computed from a visible
window that is truncated at the grafted boundary — a shallow clone's own history only goes back so
far, so reporting any of those facts anyway would be a confidently WRONG date, not merely a missing
one. `fill_evaluate.py` resolves a `due_when` predicate that needs one of those facts to `unknown`,
the same way it already does for a hand-written observations document that never set the key. Fetch
full history (`fetch-depth: 0` in CI) to get real facts. The per-file fact
(`last_modified_date`) is ALSO left out of the observation for every file in this case: the clone's
one grafted boundary commit looks like a genuine change to EVERY file present in its tree (git's diff
machinery treats a shallow graft as a root commit with no parent, so every tracked file appears
"added" there), not only to files actually touched inside the visible window — a shallow clone
yields no history-derived facts at all, per-file or repository-wide.

Before reading a tracked file, `fill_observe.py` resolves both `--repo` and the file's path
(following symlinks) and checks that the file stays inside `--repo`. A path that escapes — a
symlink pointing outside the repository — is never read; the observation reports `exists: false`
with an `unreadable_reason` explaining why. The same shape (`exists: false` +
`unreadable_reason`) is used when a tracked file exists but cannot actually be READ (a permission
error, or a race where the file is removed between the check and the read): one unreadable file
never aborts the rest of the observation.

A file's content (markers, stamps, table rows, frontmatter digest, rule count) is scanned only up
to 1 MiB; a larger file is not scanned at all, and the observation carries `scan_skipped_reason`
instead. Marker scanning is linear in the file's size: it finds each `<!--`, finds the next `-->`
once, and treats the text between them as one comment; if no `-->` is found, scanning stops there
for the rest of the file. A non-UTF-8 file is read with invalid sequences rendered as U+FFFD, never a crash; a
marker or stamp that straddles an invalid sequence is simply not matched, the same as an ordinary
absent one.

`exists: false` means every field other than `file`/`exists`/`unreadable_reason` is absent — the
observer never guesses a fact about a file it did not read. This is one-way: `exists: true` does
not mean every other field is present (`command_result` appears only when the contract's
`filled_when` uses `command`; `scan_skipped_reason` appears only when the file was too large to
scan).

Every `git log` call `fill_observe.py` makes runs with `--no-show-signature` and
`-c log.showSignature=false`, so a caller's own global `log.showSignature = true` (an ordinary
setting for someone who signs commits) cannot make git print signature-verification lines ahead of
the one date line this tool expects to parse. That `-c` override touches only this one config key —
it does not disable the caller's config wholesale (`safe.directory`, in particular, keeps working).

## What the programs check, and what they do not

`fill_validate.py` checks one contract: the top-level fields, every entry's fields, every
predicate-expression tree against the closed vocabularies above, and the path/duration grammar.

`fill_observe.py` gathers facts. It never decides a state.

`fill_evaluate.py` is a pure function: it reads only the contract and the observation file plus
`--now`, no filesystem walk beyond those two reads, no git, no wall clock. It combines predicate
results with three-valued (Kleene) logic — an `unknown` branch is masked only by a deciding
sibling (a true under `any`, a false under `all`), never silently outvoted. A due-date fact that is
simply ABSENT from the observations document (the key is not there — a hand-written or
third-party observations document need not set every optional field the way `fill_observe.py`
always does) resolves `unknown`, never `false`: absent evidence is not negative evidence. Only a
key that IS present with an explicit `null` resolves `false` (the observer looked, and the event
has not happened). This "explicit `null` is a confirmed negative" rule is written for due-DATE
facts; `days_since_setup` is the one exception: a present `days_since_setup: null` means "no
evidence either way" (clock skew, a future-dated commit, or a `--now` earlier than the repository's
first commit), not a confirmed "zero days", and resolves `unknown`, the same as an absent key.
A true `all` uses the latest child date; a true `any` uses the earliest date
among true children. Conservatively, if any true child contributing to either result lacks a
date, the combined date is unknown. This propagates through nested expressions. An unfilled
item with a true due predicate but no computable date has state `unknown`, with no guessed
deadline. Date arithmetic outside years 1–9999 also produces state `unknown`. The state machine is
monotone in `--now`: when the computed `due_at` is LATER than `--now`'s own date, the state is
`not-due` (not `due`) — `--now` has not reached the moment that makes the item due yet, even though
the observations document already knows it eventually will.
Two `observations.files[]` entries naming the same file are a shape error (not "whichever one
comes last in the array wins"), the same rule a contract's own duplicate `file` entries already
follow.
With `--obligations`,
it additionally prints, per file in state `due` or `overdue`, a report-only summary shaped loosely
after a duty record from this family's obligation format. That summary does **not** conform to the
obligation format (different trigger/discharge shape, several fields explicitly null) and would
need conversion before it could be used as an obligation record — it is a report, not a duty.

`retrigger` and `adequacy` are shape-checked but never semantically evaluated by any tool here: no
model call, no verdict, no adjudicated/re-opened state. A filled file stays filled regardless of a
later retrigger event.

No program here writes to a repository, mints an obligation, or judges whether a fact is true —
only whether the shapes and the arithmetic are right.

### Exit codes

`fill_validate.py` — `0` valid; `10` bad or unknown top-level field; `11` `contracts` malformed;
`12` an entry field missing, mistyped, out of bounds, an unknown entry key, or a `file` failing the
path grammar; `13` a malformed predicate-expression node (including a leaf carrying a combinator
key, a combinator node carrying a stray key, an out-of-bounds predicate parameter, or a tree past
the depth bound); `14` `not` used inside `due_when`; `15` `template_rows` missing where required;
`16` a malformed `retrigger`/`adequacy`; `64` usage error; `65` unreadable file or invalid TOML;
`74` stdout unavailable, so no report can be written; `99` self-test mismatch or infrastructure
failure.

`fill_observe.py` — `0` observation completed; `10` `--repo` missing or not a directory; `11`
`--repo` not a usable git repository, or is a subdirectory of one rather than its own top level;
`12` `--contract` unreadable or invalid TOML; `13` `--contract` fails shape validation; `14` a git
call timed out, could not start, returned an invalid date, or FAILED with a nonzero exit after
`--repo` was accepted as the repository's own top level (an infrastructure failure — a failed
history/tag/shallow-status query is never silently read as success-with-empty-output and never
becomes a `null` fact; an unborn repository's own genuinely empty history is detected explicitly and
is not an error); `15` a glob argument fails the glob grammar, or is explicitly empty; `64` usage
error; `65` other unreadable input; `74` stdout unavailable, so no report can be written; `99`
self-test mismatch or infrastructure failure.

`fill_evaluate.py` — `0` report computed (an item's own `unknown` state is a valid decided value,
not an error); `10` `--contract` unreadable or invalid TOML; `11` `--contract` fails shape
validation; `12` `--observations` unreadable, invalid JSON, or fails the JSON-input contract; `13`
`--observations` fails shape validation; `64` usage error; `65` other unreadable input; `74` stdout
unavailable, so no report can be written; `99` self-test mismatch or infrastructure failure.

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

A self-test that cannot create its temporary directory exits 99 and prints
`selftest infrastructure failure` with the exception type and reason.

### Bounded diagnostics

No single reported issue's `reason` any of the three tools ever emits is longer than 2,000
characters (`fill_common.DIAGNOSTIC_REASON_MAX_CHARS`), for any input, including an adversarial one
(a 10,000-character `--contract`/`--observations` path, an unreadable-file exception, or hundreds of
aggregated `--observations` shape errors). Checked once more, unconditionally, by each tool's own
report constructor. Report fields that echo an input value as given (`fill_observe.py`'s `repo`/
`contract`, and each file's own `file` path) are a different thing and are NOT size-capped.

## Input-schema differential

The validator and evaluator self-tests generate property additions, removals, and allowed-type
witnesses from every positive contract and observations fixture and compare schema verdicts with
the real CLIs. The documented semantic rules are: `template_rows` becomes required when
`filled_when` uses `rows-beyond-template` (unexported specification §§9 and 12.1; schema
`$defs.contract_entry.properties.template_rows.description` and
`x-semantic-only-rules[template-rows-required]`), and maturity dates must be canonical real
`YYYY-MM-DD` dates (unexported specification §§13.1 and 14.1; schema
`x-semantic-only-rules[canonical-calendar-dates-and-finite-values]`).

The observations schema itself encodes the `exists`-conditioned reason-field sets, accepts `null`
for either reason where that field is applicable, and rejects blank reason strings.

## Files

| path | content |
|---|---|
| [`schema/fill-contract-0.1.0-draft.schema.json`](schema/fill-contract-0.1.0-draft.schema.json) | draft JSON Schema for the contract file, the observations report, and the evaluation report (including the obligation-summary shape); shape and closed-vocabulary rules only |
| [`tools/fill_validate.py`](tools/fill_validate.py) | reference validator for one contract file, with its self-test |
| [`tools/fill_observe.py`](tools/fill_observe.py) | the read-only fact-gatherer, with its self-test (needs `git` on `PATH`) |
| [`tools/fill_evaluate.py`](tools/fill_evaluate.py) | the pure state-decision function, with its self-test |
| [`tools/fill_common.py`](tools/fill_common.py) | the closed vocabularies, the grammar checks, the JSON-input contract, and the Kleene logic the three tools import; not run on its own |
| [`fixtures/validate/`](fixtures/validate/) | 44 files: 42 fixture inputs (29 contract `.toml` files, 13 observation `.json` files) plus `cases.json` and `evaluate_cases.json`, which list the validator and evaluator cases with their expected results |

`fill_observe.py`'s own self-test does not read any file under `fixtures/`: it builds its
repositories and contracts in a temporary directory at run time.

There is no `spec/` directory and no `KNOWN-LIMITS.md` in this export.

## Status

**First cut**: a specification (not exported as prose; the schema and the three tools carry the
rules), a validator, an observer, an evaluator, and self-tests with 79 + 81 + 77
planned cases. Here: 51 files total (this README, 1 draft schema, 5 tools, 44 test-case files), 79 + 81 + 77 self-test cases.

What is missing: the specification text as a standalone document; the `command` predicate is never
executed; `public`, `session-closed` and `new-dependency` are never observed; `retrigger` and
`adequacy` are carried but never evaluated — no re-opened state exists.

Do not treat this format as in routine use: no check in this export runs it against a real
repository or a real observation history.

The fixtures are synthetic; the example under `fixtures/validate/01-valid.toml` is invented for
this publication, not a record of any real repository or template.

## How to check it yourself

From the root of this tree:

```sh
python3 -B format_fill-contract/tools/fill_validate.py --selftest
python3 -B format_fill-contract/tools/fill_observe.py --selftest
python3 -B format_fill-contract/tools/fill_evaluate.py --selftest
```

The validator and evaluator report `SELFTEST PASS: 79 cases` and `SELFTEST PASS: 77 cases`.
The observer reports its actual executed count plus a `SELFTEST SUMMARY` with passed, failed and
named skipped cases. Its roster stays at 81: a skipped platform probe is not counted as passed.
The runner independently checks temporary-filesystem capabilities and effective-user constraints
before accepting a skip (case aliases, permission probes under root, or unavailable symlinks).
Missing cases, unknown skips and unexpected infrastructure failures fail the check.

Inventory: 51 files total, 5 tools, 44 test-case files, 79 + 81 + 77 self-test cases.

`fill_observe.py --selftest` needs `git` on `PATH`; the other two need nothing beyond the Python
3.11 floor this export already requires.
