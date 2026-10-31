# Format principles

*Status: a proposed set of principles for new formats in this family. They are not frozen. Where
a program checks a principle, the principle says so; the rest are stated only.*

These rules exist to stop one failure: the same thing invented twice, one level down. Early formats
in this family had two event vocabularies, two path grammars, and a new identifier scheme in each
new format. The rules below prevent that.

## Layers

| layer | what it is | rule |
|---|---|---|
| basic | an irreducible typed object | owns its vocabulary; defined once |
| profile | a basic format narrowed for one use | narrows its parent, never widens it |
| composite | several basic formats combined into one record | names every constituent |
| protocol | a flow: documents, a state table, and who acts when | owns only its sequence and states |
| projection | a derived view over records of another format | stores nothing new |
| adapter | a mapping from a format of this family to a foreign contract | changes neither side |

A composite does not call itself basic.

## The principles

**1. Derive before you invent.** Before a new field, value, grammar or identifier scheme, look for
an existing one. If a basic format owns it, reference it. A new basic format needs a written argument
that it cannot be a profile or a composite.

**2. One owner per shared vocabulary.** A vocabulary used by more than one format has exactly one
owning format. The others reference it and may only narrow it. Example: the trigger format owns the
kinds of events; the relation format owns the kinds of dependency edges and, until an addressing
format exists, the grammar of references. Closed value lists shared by many formats belong in one
shared annex. No such annex is in this export.

*Current exception.* Obligation's `trigger.event` is a free string chosen per obligation.
The [evaluator-provenance fixture](../format_obligation/fixtures/fold-cases/r3-evaluator-provenance-pass.events.jsonl)
uses `trigger/review`, outside trigger's closed event vocabulary. The formats do not yet share
this vocabulary; principle 2 is a proposed design rule, not an enforced integration.

**3. A profile narrows, never widens.** What a profile admits is a subset of what its parent
admits. Its checks may only add errors. It may extend a blocklist, never shrink it.

**4. Declare the derivation.** A derived format lists its constituents. Each field maps to a
constituent, or is marked as new, with a reason. *Checked* by a layout check that is not in this
export: it reads the declared constituents and verifies that each one exists.

**5. Closed by default; extension by rule.** Vocabularies are closed. An open set uses a registry.
For extensions, the rule is: an unknown critical extension is rejected; an unknown non-critical
extension is ignored and preserved.

**6. Fail closed. Unknown is never OK.** If a tool cannot establish an input, the result is
"unknown" or "indeterminate". It is never a pass and never an empty result.

**7. Check or state.** A rule is either checked by a program or written down as an assumption,
in a file of stated assumptions. The obligation self-test includes fixture cases built to fail,
and checks that each is rejected as expected. The acceptance format states its own rule on this in
[its README](https://github.com/ivmat/acceptance-format#why-a-green-check-is-not-enough-the-check-must-be-able-to-fail).

**8. Deterministic tools.** A tool is a pure function of its inputs. Time is an explicit argument.
There is no network access. Output is sorted. Tools use the standard library of one language only.

**9. One ladder of maturity.** The status words are defined in [MATURITY.md](../MATURITY.md#status-words). A format
enters real use in stages that can be reversed, never in one step.

**10. A flow is not done until a protocol shows it.** Formats are types. A protocol is the program
that uses them. Each flow gets a document with a state table, the actor at each step (code, model or
person), and a worked example.

**11. Neutral surfaces.** Every surface is a file format or a command with JSON in and JSON out. No
format depends on one model provider. An adapter to a provider is thin.

**12. Judged fields do not pass as checked.** See the anti-masquerade rule in
[What a program can decide](02-what-a-program-can-decide.md). This rule is proposed for all formats;
it is enforced only in the acceptance format.

## Conventions: when in doubt, follow acceptance

The acceptance format is the most mature member of the family. Where a convention is not settled,
copy acceptance.

| topic | convention |
|---|---|
| serialization | authored files in TOML; messages and reports in JSON; a Markdown twin kept in parity |
| identifiers | a published identifier is never reused; a replacement gets a new identifier that names the old one |
| versions | major version in the identifier; minor changes only add; `0.x` is unstable |
| hashes | SHA-512 over a domain prefix and the exact bytes; no canonicalization; one registry of domains |
| time | ISO-8601 UTC timestamps and ISO-8601 durations (`P180D`) |
| amendments | add, modify, drop, tighten, relax; a floor only rises, unless a relaxation names its reason |
| tool exit codes | `0` pass, `1` validation failure, `2` indeterminate or usage error, `99` self-test mismatch |
| test cases | one failing fixture per rule, named by the rule |

**Known debt.** Most tools in this family, including the obligation tool, follow an older scheme
in part: distinct exit codes per class of error, a `cases.json` list of fixtures, SHA-256
identifiers of occurrences. The rule is: a format moves to
the conventions above the next time it changes in substance. New tools follow the conventions from
the start.

## Example

A new format for release checklists needs a "when" field. Principle 1 sends the author to the
trigger format, which owns the kinds of events. The author references the existing event
`first-release` instead of inventing `release-tagged`. The checklist becomes a profile: it narrows the trigger's
vocabulary and adds no rule the trigger does not have.
