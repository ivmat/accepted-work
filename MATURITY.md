# Maturity of each part

This page says what exists in this tree for each part, what is missing, and what you should not
rely on. The status words are defined in [README.md](README.md).

**How the counts were made.** The counts are counts of this tree, on the day of the export. "Tools"
counts the program files. "Test-case files" counts the files under `fixtures/`. "Self-test cases"
is the number that the tool's `--selftest` reported when `gates/run_checks.sh` ran on this tree.
The working repository holds more than this tree: test cases that were built from real records,
and working notes, are not exported.

**For every part:** nothing here is frozen. Field names, error codes and file layouts can change
without notice. There is no versioned release. A validator checks the shape and the consistency of
a record. It does not check that the content is true.

## Formats

| part | status | what exists in this tree | what is missing | do not rely on |
|---|---|---|---|---|
| Obligation | candidate | a pure-core specification in 2 files and a page on known limits; a closed JSON schema for fold reports; 3 tools (policy-free primitives, validator, and schema checker); 186 test-case files; self-test cases 130 | an input JSON schema; observation truth, writer admission and routine enforcement on real records. One earlier pilot exists in my own work. | consistency checks as proof that observations are true or actors are authenticated |
| Relation | first cut | a draft JSON schema; no specification text: the format is typed by the schema and by the validator; 3 tools; 44 test-case files; self-test cases 72 | the specification text; the query tool (`impacted`/`depends`); a real address format for what edges connect | this format as wired to anything else in this export, or as in trial anywhere |
| Trigger | first cut | a draft JSON schema; no specification text: the format is typed by the schema and by the two tools; 4 tools; 82 test-case files; self-test cases 67 + 70 | the specification text; a program that produces events from a live repository | the list of event kinds as final, or this format as in trial anywhere |
| Impact rule | first cut | a draft JSON schema; no specification text: the format is typed by the schema and by the two tools that run; 4 tools; 72 test-case files; self-test cases 88 + 66 | the specification text; the deriver kinds (`derive:`), which are shape-checked but never run; the `inherit` list, which is carried but never dereferenced | this format as wired to anything else in this export, or as in trial anywhere |
| Fill contract | first cut | a draft JSON schema; no specification text: the format is typed by the schema and by the three tools; 5 tools; 44 test-case files; self-test cases 79 + 81 + 77 | the specification text; the `command` predicate, which is never executed; the `public`, `session-closed` and `new-dependency` events, which are never observed; `retrigger` and `adequacy`, which are carried but never evaluated | this format as wired to anything else in this export, or as in trial anywhere. `fill_observe.py`'s self-test needs `git` on `PATH`; `SKIP_GIT_SELFTESTS=1` skips it |

"Candidate" and "first cut" are defined in [README.md](README.md). The trial of the obligation
format is in my own work and is not part of this tree.

## Other parts

| part | status | what exists | what is missing | do not rely on |
|---|---|---|---|---|
| Concept notes (`docs/`) | not a format | six notes on how a format is made and what a program can decide | a frozen vocabulary | any note as a specification; they state ideas, not rules that a program enforces |
| Public check runner (`gates/run_checks.sh`) | not a format | one script that runs the self-tests that are in this tree | the full check suite of the working repository, which is not exported | a green run as more than "all selected self-tests passed" |
| Four further formats (gate manifest, mechanism, grant, intent) | not in this export | nothing in this tree; they have validators and test cases in the working repository | their text for a public reader | anything about them until they are exported |
| Planned formats: claim, work surface, change impact | planned | nothing in this tree | a schema, a validator, test cases | — |
| Acceptance | separate work | published at [github.com/ivmat/acceptance-format](https://github.com/ivmat/acceptance-format) | — | this page for its status; its own README states it |
