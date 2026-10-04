# Obligation core format

The words **enforced** and **stated** distinguish executable checks from claims a deployment must
supply. The reference validator checks consistency only.

## 1. Encoding and common types

Inputs are strict UTF-8 JSON. The event log is JSONL split on LF; CRLF is accepted, bare CR is not.
Duplicate JSON keys, non-finite numbers, lone surrogates and nesting deeper than 64 are input errors.
All record grammars below are closed.

The core fold self-test reports unknown-key rejection coverage as normalized object paths. The
roots are `event` and `boundary`; an event payload is named `payload(<kind>)`; array elements and
data-keyed map values use `[]` (for example,
`boundary.observations_manifest[][][]`). The closure gate compares that machine-readable list
against its own static inventory derived from the object levels documented here; it does not
derive its expectation from the mutation generator.

A non-blank string contains a non-whitespace character. A digest is
`sha256:` plus the lowercase hexadecimal SHA-256 of canonical JSON (keys sorted, no insignificant
space, UTF-8). RFC3339 timestamps require an offset and at most six fractional digits.

## 2. Duty record: 19 fields

The `minted.duty` object contains exactly these fields.

| field | rule |
|---|---|
| `id` | non-blank stable duty id; equals the event row id |
| `natural_key` | digest of the identity operands in section 5 |
| `scope` | non-blank deployment scope |
| `source` | non-blank source object id, without revision |
| `source_revision` | non-blank immutable revision currently bound |
| `trigger` | exactly `{event, condition}`, both non-blank |
| `action` | non-blank required act |
| `target` | non-blank target object |
| `retrigger` | `once` or `every-event` |
| `duty_kind` | `one-time` or `continuing` |
| `occurrence_rule` | non-blank description; not interpreted |
| `required_by` | ISO date or non-blank `boundary:<id>` |
| `discharge_predicate` | exactly `{resolver, operands, resolved}`; strings non-blank and `resolved` true |
| `created_by` | non-blank specification owner |
| `created_at` | RFC3339 timestamp with offset |
| `occurrences` | exactly `{}` at mint |
| `status` | exactly `open` at mint |
| `discharge_evidence` | exactly `[]` at mint |
| `supersedes` | always present; `null` for a root or a different duty id |

## 3. Event envelope and payloads

Every event contains exactly `{id, seq, kind, recorded_at, actor, batch_id, payload}`. `seq` is a
positive signed-64-bit integer, starts at 1 per duty and has no gaps. `recorded_at` is RFC3339.
`actor` and `batch_id` are non-blank. A repeated `(id, seq)` keeps the first record with a warning.

The kinds and exact payload fields are:

| kind | payload |
|---|---|
| `minted` | `{duty}` |
| `fired` | `{occurrence_id, trigger_event_id, trigger, required_by, target_revision}` |
| `predicate_evaluated` | `{occurrence_id, result, bound_revision, evaluated_at, predicate_digest, evidence_digest, context_hash, boundary_id}` |
| `deadline_crossed` | `{occurrence_id, at, bound_revision, boundary_id}` |
| `stale_detected` | `{occurrence_ids, context_hash_old, context_hash_new, reason, bound_revision, boundary_id}` |
| `superseded` | `{by}` |
| `discharged` | `{occurrence_ids, evidence_refs}` |
| `source_revised` | `{from_revision, to_revision}` |
| `dropped` | `{reason}`; any non-blank string, with no input length limit |

Diagnostic truncation applies only when text is rendered in a report; it never changes whether a
`dropped.reason` is accepted.

## 4. Identity

These rules compare recorded strings; they do not authenticate actors.

- **R1:** the `minted.actor` equals `created_by`.
- **R2:** `fired`, `superseded`, `source_revised`, and `dropped` have actor equal to
  `created_by`.
- **R3:** evaluator acts (`predicate_evaluated`, `deadline_crossed`, `stale_detected`) and
  `discharged` require a non-blank actor but impose no actor identity.

## 5. Natural and occurrence identity

The natural key is the digest of:

```json
{"v":1,"scope":S,"source":SOURCE,"trigger_event":E,"trigger_condition":C,"action":A,"target":T}
```

The source revision is excluded. Duties may share a natural key only when all of them lie on one
valid reciprocal supersession chain. An invalid intermediate breaks the exemption. A duty invalid
for any reason still claims its natural key. When a mint envelope fails admission but its duty
payload independently yields valid `id`, `natural_key`, `supersedes`, and `created_by` metadata,
that metadata remains in the fixed point even though the duty itself is invalid.

Reciprocity, branching, cycle and natural-key checks are one fixed-point validation. After any
duty becomes invalid, the validator rebuilds the graph from the remaining valid duties and repeats
all four checks. It stops only when a complete pass adds no invalid duty. The natural-key exemption
is therefore computed on the final valid graph, never on adjacency retained from an earlier pass.

An occurrence id is the digest of
`{"v":1,"duty":DUTY_ID,"trigger_event_id":TRIGGER_EVENT_ID}`. Redelivery must preserve the
trigger id, deadline and target revision. A `once` duty admits only one distinct occurrence.

A dated firing may tighten but not loosen its applicable rule. Each new occurrence of an
`every-event` duty binds its own deadline; redelivery cannot extend it.

## 6. Evaluation, discharge, deadlines and staleness

An evaluation's predicate digest equals the digest of the duty predicate. Its bound revision is the
digest of source revision, predicate digest, target revision, evidence digest and context hash. A
discharge names occurrences and a non-empty unique evidence list; each occurrence needs a current
true evaluation whose evidence digest equals the digest of that list.

A dated deadline crosses at the next midnight in `Europe/Zagreb`. A named boundary crosses at that
boundary. A crossing binds the current source revision and boundary id.

A continuing-duty stale event names a non-empty unique occurrence list, changed non-blank context
hashes, `reason: "reevaluation_pending"`, the current source revision and boundary id. It
invalidates the old evaluation and makes a discharged occurrence open/pending. Revision migration
must have later same-batch stale coverage for every invalidated evaluation.

## 7. Drop and linked replacement

Drop is absorbing. It changes every occurrence to `dropped`; a later event is invalid. Dropped
occurrences need no observations. The duty's manifest entry is `{}`, including when it previously
had occurrences. A batch whose in-scope duties are all dropped or superseded may contain zero
evaluator events.

Supersession is one graph validated in both directions:

- A's `superseded{by: B}` and B's `supersedes: A` must both exist.
- A and B are valid and in scope and have the same owner.
- One predecessor has at most one successor; cycles are invalid.
- A link asserted by only one side is invalid. Invalid or absent replacements fail closed.

These rules iterate together with natural-key uniqueness as the fixed point in section 5. An
invalidation at any position propagates through reciprocal links in either direction; every pass
rebuilds claimant sets, cycle walks and adjacency before the next pass.

## 8. Boundary

A boundary contains exactly
`{schema, boundary_id, recorded_cutoff, in_scope_duty_ids, evaluator_batch,
observations_manifest, event_frontier}`.

- `schema` is `obligation-boundary/v0`.
- Scope ids are a non-empty unique list and each is present in the log.
- The committed evaluator batch contains
  `{batch_id, input_snapshot_digest, committed, event_count, event_ids}`.
- `event_ids` contains exactly three lists: `predicate_evaluated`, `deadline_crossed`, and
  `stale_detected`. They exactly enumerate this batch's in-scope evaluator events.
- The manifest maps each duty and occurrence to exact entries
  `{kind, event_id, bound_revision}` required at this boundary.
- The frontier maps every duty to its highest consumed sequence.

Every ordinary occurrence needs a fresh predicate evaluation. A due occurrence also needs a
crossing; a pending occurrence also needs its stale observation. Superseded and dropped occurrences
need none. A dropped duty's manifest is an empty map.

## 9. Projection

| condition | result | blocks |
|---|---|---|
| invalid record, boundary or batch | `FAIL` | yes |
| superseded with valid present in-scope replacement | `PASS` | no |
| superseded without one | `FAIL` | yes |
| dropped | `PASS` | no |
| expired | `FAIL` | yes |
| open and pending reevaluation | `PENDING` | yes |
| due and open | `FAIL` | yes |
| open and not due | `WARN-recorded` | no |
| discharged with current matching true evaluation/evidence | `PASS` | no |

Worst-of order is `FAIL, PENDING, WARN-recorded, PASS`. Open and expired occurrences create a
required action `{kind: "obligation", duty_id, occurrence_id, state}`. A normal duty with no
occurrence is open/WARN and gets the same action with `occurrence_id: null`. A dropped duty with no
occurrence is dropped/PASS and gets none.

## 10. Authority-extension pre-pass

After successful parsing and before validation, exit 20 reports every occurrence of exactly these
structures:

1. top-level `authority` or `waiver` in a `minted.duty`;
2. event kind `waived` or `waiver_lapsed`;
3. event-envelope key `authority_ref`;
4. `source_revised.payload.authority_ref`;
5. any core payload key `waiver_id`, `expires`, `next_review_cutoff`, or
   `later_date_authority_ref`;
6. `waiver_lapsed` in the batch event map; a manifest entry whose `kind` is
   `waiver_lapsed`; or `lapse_satisfaction` at boundary or evaluator-batch top level.

Identifier positions and id values are never inspected. Shared core keys such as `by`,
`occurrence_ids`, `at`, `bound_revision`, and `boundary_id` are not detections; if misplaced they
receive an ordinary schema error.

After JSON parsing, the core performs one typing pass over every event and the boundary. Only
unreadable input, malformed JSON, or a non-object event/boundary is exit 65. Envelope, kind, field,
payload and boundary type/shape defects are collected validation errors under their documented
codes; every later phase reads only the typed records. The outer unexpected-error handler remains
a crash guard, but no JSON-object input is in its exit-65 domain.

Validation has no boundary-error early return. Event de-duplication, envelope and payload checks,
duty admission, sequence checking and lifecycle folding always run. In-scope presence and the
supersession/natural-key fixed point additionally require a usable non-empty unique string scope.
Observation/batch validation and projection additionally require a non-blank boundary id and a
valid RFC3339 cutoff; only those scope/boundary-dependent phases are skipped when their prerequisite
is unusable. All errors from runnable phases are retained.

Precedence is usage 64, malformed/unreadable/non-object input 65, extension 20, ordinary validation
(the numerically lowest collected code 10–19), then block 1 or success 0. Thus boundary code 18
never masks a collected event code 10–17. Stdout failure 74 applies to every path.

## 11. Exit codes

| code | meaning |
|---:|---|
| 0 | valid and unblocked |
| 1 | valid and blocked |
| 10 | event kind or envelope error |
| 11 | missing or invalid required field |
| 13 | duty, natural-key, occurrence, owner or supersession identity error |
| 14 | unknown occurrence |
| 15 | illegal transition |
| 16 | per-duty sequence gap |
| 17 | predicate, evidence, revision, migration or staleness binding error |
| 18 | boundary schema/scope/cutoff error |
| 19 | incomplete or inconsistent evaluator batch |
| 20 | authority extension structure detected |
| 64 | usage error |
| 65 | unreadable, malformed or non-object input; unexpected-error crash guard |
| 74 | stdout unavailable |
| 99 | self-test failure |
