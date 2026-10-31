# Obligation format — known limits

## Guarantee boundary

The core guarantees record consistency, not observation truth or writer admission. It checks that
recorded lifecycle events agree with each other and that bindings recompute. It does not prove that
an evaluation, deadline crossing, context change, or actor claim is true.

Actors are not authenticated. Identity rules R1–R3 compare strings. A deployment must decide who
may append each act and how writers are authenticated; it may compose an authority layer around the
core.

## Missing components

- There is no evaluator. The tool never reads a live clock, runs a resolver, resolves evidence, or
  computes a context hash.
- There is no storage or atomic-append protocol, signature scheme, or input JSON Schema.
  The fold report has an output schema.
- References such as `source`, `target`, and evidence refs are opaque strings.
- The boundary's `input_snapshot_digest` is required but not recomputed.
- `occurrence_rule` is required free text and is not interpreted.

Firing is owner-only because the present grammar lets a firing choose `target_revision` and a
recurring occurrence's deadline. Opening firing to another writer requires a mechanically checkable
trigger, deadline and target-binding rule that this version does not have.

## Meaning of results

`PASS` means the recorded core rules are satisfied. In particular, `PASS` on a dropped duty means
“no longer required,” not “work done.” A coherent but false evaluation can pass if a deployment
admits it.

## Reference implementation choices

- A dated deadline crosses at the next midnight in `Europe/Zagreb`.
- `seq`, `event_count`, and frontier values are signed 64-bit integers.
- Per-duty sequence starts at 1 with no gaps.
- A duplicate `(id, seq)` keeps the first line and records a warning.
- One invalid event rejects the whole duty.
- Every boundary requires a fresh evaluation for every occurrence that is neither superseded nor
  dropped.
- Events split on LF; CRLF is accepted and a bare CR is rejected.
- JSON decoding precedes the 64-level container-depth check. If decoding succeeds, deeper input
  gets the explicit depth-limit diagnostic. If the decoder raises `RecursionError` first, the
  tool reports the generic `invalid JSON or duplicate keys` diagnostic with input error 65 and
  blocks the result. That message does not identify excessive nesting; the decoder's recursion
  ceiling depends on the runtime. There is no depth check before decoding.
- The boundary schema is `obligation-boundary/v0`.

The specification is candidate text. The synthetic fixtures show agreement between the validator
and its declared rules, not production fitness. No routine live gate is included.
