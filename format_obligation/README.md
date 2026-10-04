# Obligation format

Status: **candidate**. This publication contains the pure duty core: a deterministic record-consistency
validator and its fixtures. It is a format and reference implementation, not an enforcement service.

## What an obligation is

An obligation is a proposition about a target that must be judged true by a time, judged again when
its trigger fires again, and discharged by a judgment bound to evidence. Each firing creates an
occurrence with its own immutable trigger identity and deadline binding.

The core guarantees consistency, not truth. It checks the closed record and event shapes, lifecycle
transitions, identities, digests, evaluation/evidence bindings, deadline crossings, staleness and
boundary completeness. It does not run resolvers, resolve references, authenticate actors, or prove
that an admitted observation reflects reality.

Observation truth and writer admission belong to the deployment. A deployment may compose an
authority layer around this core. The core itself excuses a duty only when its owner records a
`dropped` event or replaces it through a reciprocal `supersedes` link.

## Records and events

A log is JSONL, one event per line. The first event for a duty is `minted`; later events fire
occurrences, record evaluations and deadline crossings, detect staleness, discharge occurrences,
revise a source, supersede the duty, or drop it. Replay order is `(id, seq)`, never timestamp.

A duty has 19 closed fields. `created_by` is its specification owner. `supersedes` is always
present: `null` for a root, otherwise the predecessor duty id. Event `actor` records provenance.
The core compares actor strings for the owner-only acts but does not authenticate them.

A linked replacement is valid only when both directions agree: A records `superseded{by: B}` and
B records `supersedes: A`. The chain must stay in scope, preserve the owner, have no branches and
have no cycles. Duties may share a natural key only along one wholly valid reciprocal chain.

A `dropped{reason}` event is owner-only and absorbing. It makes every occurrence `dropped`; a duty
may also be dropped before its first firing. A dropped duty contributes `PASS` because it is no
longer required, not because the work was completed.

## Boundary result

A boundary names the duties in scope and one committed evaluator batch. The batch has exactly three
event lists: `predicate_evaluated`, `deadline_crossed`, and `stale_detected`. Every non-dropped,
non-superseded occurrence has the observations required by its state and deadline.

Each occurrence contributes one of `FAIL`, `PENDING`, `WARN-recorded`, or `PASS`. The duty and
aggregate use that worst-to-best order. `FAIL` and `PENDING` set the block bit. Open or expired
occurrences produce required actions of kind `obligation`.

## Closed core and deployment extensions

The core rejects authority/waiver extension structures with exit 20 before ordinary validation.
Detection is structural and limited to the schema positions listed in
[spec/format.md](spec/format.md); identifier values and free text are never inspected. A private or
third-party deployment can validate a larger composed grammar separately.

## Files

| path | purpose |
|---|---|
| [spec/format.md](spec/format.md) | closed fields, events, identity, boundary and exit rules |
| [spec/lifecycle.md](spec/lifecycle.md) | states and legal transitions |
| [KNOWN-LIMITS.md](KNOWN-LIMITS.md) | explicit non-guarantees and implementation choices |
| [tools/obligation_primitives.py](tools/obligation_primitives.py) | policy-free parsing, binding, deadline, chain and stdout primitives |
| [tools/obligation_fold.py](tools/obligation_fold.py) | core validator and fold |
| [tools/schema_conformance.py](tools/schema_conformance.py) | stdlib-only fail-loud checker used by the report boundary |
| [schema/obligation-fold-0.1.0-draft.schema.json](schema/obligation-fold-0.1.0-draft.schema.json) | closed output contract for fold reports |
| [fixtures/fold-cases/](fixtures/fold-cases/) | core fixtures |

## Run

```sh
python3 tools/obligation_fold.py --selftest
python3 tools/obligation_fold.py fold --events EVENTS.jsonl --boundary BOUNDARY.json
```

Python 3.11 or newer is required. The implementation uses only the standard library and the system
time-zone database. The stdout environment contract applies to report-producing commands: the tool
writes its one strict UTF-8 JSON report completely and exits with its documented code, or exits 74
having written nothing further; it never exits 120 or emits a traceback because of stdout. Stdout
may be any object. A real file descriptor of any number, or a text stream with `write`/`flush`, is
fully supported. Any other object, or any stdout operation failing with `OSError`, `ValueError`,
`TypeError`, `OverflowError`, `AttributeError`, or `io.UnsupportedOperation`, means stdout
unavailable and exit 74. Exceptions from the tool's own logic still propagate. The only exclusions
are argparse `--help`/usage text (argparse's own output, not a report, and not routed through this
boundary) and external signals (`SIGINT`, `SIGPIPE` reset to `SIG_DFL`, `SIGKILL`), which terminate
as the OS/Python decide. There are no other scope exclusions. The self-test currently reports
`SELFTEST PASS: 130 cases`.

Inventory: 194 files total, 3 tools, 186 test-case files, 130 self-test cases.
