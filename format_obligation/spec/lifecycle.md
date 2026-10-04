# Obligation lifecycle

State belongs to each occurrence. The duty also has a summary state.

## States

| state | meaning |
|---|---|
| `open` | fired and not discharged; it may be not due, due, or pending reevaluation |
| `discharged` | a discharge event cites a current true evaluation and matching evidence |
| `expired` | its deadline crossed without discharge |
| `superseded` | a reciprocally linked replacement took over; absorbing |
| `dropped` | the owner recorded that the duty is no longer required; absorbing |

With occurrences, summary priority is `expired > open > discharged`. A superseded or dropped duty
reports that terminal state. Before the first firing a normal duty is `open`; a duty dropped before
its first firing is `dropped`.

## Transitions

| from | event and guard | to |
|---|---|---|
| absent | `minted`; full 19-field duty; actor equals `created_by` | open duty, no occurrences |
| live duty | `fired`; owner actor; derived occurrence id; trigger/deadline bindings valid | new open occurrence |
| live duty | same valid firing redelivered | unchanged |
| open/discharged/expired | `predicate_evaluated`; valid bindings | store current result; clear pending; false after crossing expires |
| open | `deadline_crossed`; recorded deadline reached | expired, unless pending reevaluation |
| discharged/expired | valid `deadline_crossed` | unchanged, history retained |
| open/expired/discharged | `discharged`; current true evaluation and evidence digest match | discharged |
| discharged | `stale_detected`; continuing duty and hashes bind | open and pending |
| open/expired | `stale_detected`; continuing duty and hashes bind | invalidate evaluation; open stays pending |
| any live occurrence | `stale_detected`; one-time duty | unchanged history |
| live duty | `source_revised`; owner actor; revision and stale coverage bind | same duty at new revision |
| live duty | reciprocal `superseded{by}`; owner actor; valid chain | all occurrences superseded |
| live duty | `dropped{reason}`; owner actor; non-blank reason | all occurrences dropped |

A second distinct firing on a `once` duty, an event after supersession or drop, a self-link, a cycle,
a branch, or an unreciprocated link is invalid.

## Examples

Drop: a duty may be minted and then dropped before any trigger fires. Its boundary manifest entry is
`{}`, its result is `PASS`, and it has no required action.

Relax by replacement: A records `superseded{by: B}`; B records `supersedes: A`; both share the same
owner and are in scope. B may state a looser deadline or predicate and is projected independently.
