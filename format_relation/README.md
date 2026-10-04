# Relation format

**Status: first cut.** A draft JSON Schema, a reference validator, and a self-test with 72
cases that pass. See [Status](#status).

## What it records

A relation is a typed dependency between two things: "the API guide documents the source folder",
"this module pins that library file". A relations file lists such edges for one repository. Each
kind of relation says in which direction a change spreads, so a query can later answer "what does
this change affect?".

## A small example

This is the test case `fixtures/validate/00-valid-minimal.toml`. It is valid.

```toml
relation_version = "0.1.0"
repo = "alpha-service"

[[edges]]
from = "alpha-service:docs/setup.md"
relation = "documents"
to = "alpha-service:scripts/install.sh"
note = "baseline valid edge: a known relation, no propagates, no directed override"

[[edges]]
from = "alpha-service:CHANGELOG.md"
relation = "records"
to = "alpha-service:src/"
```

A change to `scripts/install.sh` affects `docs/setup.md`, and a change anywhere under `src/`
affects `CHANGELOG.md`: both `documents` and `records` spread from `to` to `from` (see the fields
table).

## The fields

| field | type | required | meaning |
|---|---|---|---|
| `relation_version` | string | required | version of the file shape |
| `repo` | string | required | the repository that the file describes |
| `edges` | array of edges | required | the dependencies; may be empty |
| `edges[].from`, `edges[].to` | entity-ref | required | the two things. An entity-ref names a whole repository, a file, a folder (trailing `/`) or a named resource |
| `edges[].relation` | string | required | one of nine closed kinds (`depends-on`, `pins`, `traces-to`, `records`, `documents`, `governs`, `template-of`, `supersedes`, `owns`), or a custom `x-` token |
| `edges[].propagates` | `to-from`, `from-to` or `none` | optional | direction of spread; allowed only on an `x-` token |
| `edges[].directed` | boolean | optional | carried; it does not change the direction of spread |
| `edges[].note` | string | optional | free text |
| `edges[].source` | entity-ref | optional | where the edge came from |

## What the program checks, and what it does not

`relation_validate.py` checks one or more files as one set. It rejects unknown or missing keys, a
malformed entity-ref, an unknown relation, `propagates` on a closed kind, an edge from a thing to
itself, and two identical edges, also across files. A `@self` reference is rejected unless the
caller names the repository with `--self` — checked on `from`, `to` and the optional `source`
field alike.

It does not check that a named file exists. It cannot decide whether an edge is true or whether
the file lists every real dependency. The entity-ref grammar is temporary: it is not a separate
format yet, and every format that needs one repeats it or references this one.

### TOML input and limits

Every relations file is checked for a non-finite number (`nan`, `inf`, `-inf`, or a float
expression that overflows to one of them) anywhere in the document, at any depth, before any other
rule runs — TOML's float grammar allows all three, but this format rejects them as a plain input
error rather than carrying one through to a report. A TOML integer literal too large for Python's
own integer-to-decimal-string conversion (around 4,300 digits) is reported the same way, never as
an uncaught failure, wherever it appears in the document. An entity-ref (`from`, `to`, `source`) is
at most 4096 characters.

**The documented diagnostic bound:** no single reported issue's `reason` the validator ever emits
is longer than 2,000 characters, for any input, including an adversarial one — a 10,000-character
entity-ref argument, or a value so large that formatting it naively would itself be unbounded (an
oversized integer is never converted to a decimal string to report it; a digit-count estimate is
used instead). This is checked once more, unconditionally, by the tool's own report constructor,
so it holds even for a message built by a call site that forgot to self-bound.

The draft schema also describes the shape of a query report (`impacted`/`depends`: what changing
one entity affects, or what one entity depends on). The tool that produces that report is not
included in this export.

## Input-schema differential

The validator self-test generates property additions, removals, and allowed-type witnesses from
every positive relations fixture, compares the schema verdict with the real CLI, and permits only
one documented semantic rule: `propagates` is forbidden on a closed relation and optional only on
an `x-` relation (unexported specification §§2.1 and 3; schema `$defs.edge.description` and
`x-semantic-only-rules[propagates-only-on-x-token]`).

## Files

| path | content |
|---|---|
| [`schema/relation-0.1.0-draft.schema.json`](schema/relation-0.1.0-draft.schema.json) | draft JSON Schema for the edges file and the query report; shape and closed-vocabulary rules only |
| [`tools/relation_validate.py`](tools/relation_validate.py) | reference validator, with its self-test (Python 3 standard library only) |
| [`tools/relation_vocab.py`](tools/relation_vocab.py) | the entity-ref grammar, the relation vocabulary, and the traversal logic the validator imports; not run on its own |
| [`tools/schema_conformance.py`](tools/schema_conformance.py) | stdlib-only fail-loud checker used by the report boundary |
| [`fixtures/validate/`](fixtures/validate/) | 43 files: 42 fixture inputs plus `cases.json`, which lists 46 cases with their expected results |
| [`fixtures/example/`](fixtures/example/) | one invented, illustrative edges file, not a record of a real repository |

There is no `spec/` directory and no `KNOWN-LIMITS.md` in this export.

## Status

**First cut**: a specification (not exported as prose; the schema and the validator carry the
rules), a validator, and a self-test that passes 72 cases. Here: 1 draft schema, 3 tools,
44 test-case files, 72 self-test cases.

What is missing: the specification text as a standalone document; the query tool
(`impacted`/`depends`); a real address format for the things that edges connect — the entity-ref
grammar here is a placeholder for one.

Do not treat this format as in routine use: no check in this export runs it against real records,
and no other exported format depends on it yet.

The fixtures are synthetic; the example under `fixtures/example/` is invented for this
publication, not a record of any real repository.

## How to check it yourself

From the root of this tree:

```sh
python3 -B format_relation/tools/relation_validate.py --selftest
```

The last line of its output was:

```
SELFTEST PASS: 72 cases
```

Inventory: 49 files total, 3 tools, 44 test-case files, 72 self-test cases.

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
