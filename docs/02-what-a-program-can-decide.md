# What a program can decide

*Status: an idea under investigation. No format in this repository carries the annotation described
here yet, and no check enforces it. The question it answers is common to every format.*

## The question

A typed field is not always a checked field. Some fields a program can decide completely. For
others, a program can check the shape, but a model or a person must judge whether the content is
right. For some, a program can check only that something is there.

Each format answers this question for its own fields, in its own words. This note proposes one
shared way to state the answer, per field.

## One annotation per field

Each field, or each position inside a field, gets a checkability class: how much a program can
decide about the field today.

| class | a program can decide | example |
|---|---|---|
| `typed-valued` | all of it. The type is closed and the value is decidable, or a deterministic predicate decides it. | a deadline, compared with the clock |
| `typed-shell` | the shape: the field is present, well formed, from a closed list, bound to its record. Whether the content is adequate or true needs judgment. | a list of constraints: each is a string, but whether it was honoured is judged |
| `free` | only that the field is present and not empty. | a prose rationale |

## The class states what is true today

The class is computed against the checkers that exist now. It is never computed against checkers
that are planned.

Take a spending cap: `max_spend_eur: 40`. The type is closed and the value is a number. It looks
fully mechanical. But a program can decide "the spend stayed under the cap" only if a program
measures the spend. If no such meter exists, the field is `typed-shell` today, and the spend is
judged. The field becomes `typed-valued` on the day the meter exists, not on the day the meter is
planned.

A plan to build a checker can be recorded. It is recorded apart from the annotation, as a plan. It
is never written into the class.

The same holds for references. A field that points at another object is decidable only if the
reference can be resolved. In this family, a shared addressing format for named things is still
planned. Until it exists, some reference fields are shape-checked only.

## The anti-masquerade rule

The annotation exists to protect one property:

> A field that needs judgment must never be presented so that it reads as mechanically checked.
> Where nothing is stated, the default is "not checked", never "checked".

A missing check is visible. A judged value that looks checked is invisible, and people cite it.

The acceptance format already has this rule for its own records: it never vouches by silence, and a
parity check keeps its two representations in step. This note proposes the rule for every format.

Two limits, stated plainly:

- The rule is scoped to presentation: how a record is shown to a reader. Whether a stored or
  transmitted value can also masquerade as checked is an open question.
- The "silence means not checked" half is cheap to enforce: one default-deny rule on the
  annotation. The "never reads as checked" half needs a rendering check per format. Outside
  acceptance, no such check exists yet. There, that half is stated, not enforced.

## Not the same as the strength of evidence

The acceptance format grades evidence records by the kind of method behind them, from a
kernel-checked proof to a human review. That grades a piece of evidence. The checkability class
grades a field. A field can be `typed-valued` with no evidence record near it. An evidence record
can be strong about a claim whose field is `free`. The two must be separate keys.

## Example

A task record for a translation job:

| field | value | class |
|---|---|---|
| `due` | `2026-10-05T12:00:00Z` | `typed-valued` |
| `target_language` | `de` (from a closed list) | `typed-valued` |
| `max_spend_eur` | `40` | `typed-shell` (no spend meter exists) |
| `constraints` | `["keep product names untranslated"]` | `typed-shell` |
| `objective` | "a German version a customer can follow" | `free` |

## Open questions

1. Where the annotation lives: a shared annex, a column in each schema, or a view generated from
   field types.
2. Per field, or per position inside a field.
3. Does validity over time, such as "decidable until revoked", need its own marker?
4. Can the rendering half of the rule be checked in one generic way, or only per format?

A related, separate question: whether a check blocks an action or only reports a finding. That is a
second axis, independent of the checkability class.
