# Glossary

*Plain definitions of the terms this repository uses. Where a note explains a term in depth, the
entry points there.*

## The frame

- **Call**: one unit of work with a typed input and a typed output. It may have effects. See
  [Work as typed calls](01-work-as-typed-calls.md).
- **Signature**: the types of a call's input and output, and the effects it may have. It does not
  say what implements the call.
- **Deterministic implementation**: a program. The same input gives the same output.
- **Nondeterministic implementation**: a language model or a person. A repeat call can give a
  different output.
- **Mechanical**: a task a program can already do correctly. Mechanical work belongs to a program.
- **Format**: the type of a record: its fields, their values, and the rules a checker applies. A
  format types one side of a call.
- **Pipeline**: a composition of typed calls.
- **Runtime**: in this design, the program that schedules the calls, checks inputs and outputs
  against their formats, and records what happened. Not part of this repository.

## Layers of work

- **Class**: a definition of a piece of work. It fixes the shape only. See
  [Class, instance, run](03-class-instance-run.md).
- **Instance**: a class bound to concrete targets and permissions. It stays at rest until it is
  called.
- **Run**: one execution of one instance.
- **Non-widening**: a lower layer, or a delegated permission, may narrow what its parent allows, never
  widen it.
- **Completion condition**: the statement that says when a run is done.

## Deciding and checking

- **Checkability class**: how much a program can decide about a field today: `typed-valued`,
  `typed-shell` or `free`. See [What a program can decide](02-what-a-program-can-decide.md).
- **`typed-valued`**: a program decides the field completely.
- **`typed-shell`**: a program checks the shape; judgment decides whether the content is adequate.
- **`free`**: a program checks only that the field is present and not empty.
- **Anti-masquerade rule**: a judged field must never be presented as mechanically checked. Silence
  means "not checked".
- **Acceptance**: the check of a result against the contract of its call, and the record of that
  check. The acceptance format is published in its own repository.
- **Evidence**: a record that supports a claim, for example the output of a test run.
- **Check** (also **gate**): a test that can fail. A blocking check stops an action when it fails.
- **Negative control**: a case that must fail. The obligation self-test includes cases built to
  fail on purpose, and checks that each is rejected as expected. The acceptance format's own rule
  on negative controls is at
  [its README](https://github.com/ivmat/acceptance-format#why-a-green-check-is-not-enough-the-check-must-be-able-to-fail).
- **Fail closed**: when a tool cannot establish an input, it reports "unknown", never "valid".
- **Fold**: turning a stream of events into the current state, by applying them in order. The
  events are never edited.
- **Occurrence**: one firing of a standing duty or trigger. Each occurrence is checked and recorded
  on its own.

## Kinds of format

- **Basic format**: an irreducible typed object. It owns its vocabulary.
- **Profile**: a basic format narrowed for one use. It admits a subset of what its parent admits.
- **Composite**: a format that combines several formats into one record.
- **Protocol**: a flow: documents, a state table, and who acts at each step.
- **Projection**: a view derived from records of another format.
- **Adapter**: a mapping from a format of this family to a foreign contract.
- **Vocabulary**: a closed list of allowed values, such as the kinds of events. Each shared
  vocabulary has one owning format.

## Maturity

- **Status words** (planned, first cut, candidate, in use): defined in [MATURITY.md](../MATURITY.md#status-words).

## The formats

- **Obligation**: a duty with a trigger, a deadline rule and a mechanical test for "done". Its state
  is folded from events.
- **Relation**: a typed dependency edge between two things, with a query for what a change affects.
- **Trigger**: a rule that fires when a named event happens. It owns the vocabulary of events.
- **Entity-ref**: a string that names a whole repository, a file, a folder or a named resource.
  Relation and trigger both use this grammar; neither owns it as a separate format yet.
- **Impact rule**: a per-repository rule of the form "when X changes, Y must be updated".
- **Fill contract**: a rule that says when a file generated from a template counts as filled in.
- **Gate manifest**: a typed list of the checks a repository runs.
- **Mechanism**: a description of a deterministic capability: inputs, outputs, effects, and what it
  decides and does not decide.
- **Intent**: a statement of direction: an objective and its constraints.
- **Grant**: delegated authority or resources that can narrow and never widen.
- **Observation**: the record of one observed event, as an append-only log line.
- **Claim** (planned): a general statement: an assumption, an invariant, a requirement, a thing known
  to be false.
- **Work surface** (planned): what one run may read, write and call, and with what budget.
- **Change impact** (planned): the impact of one change, as one record.

## Tools

- **Consumer**: a program that reads records of a format and checks or decides something about them.
- **Self-test**: a tool's own run over all its fixtures. It prints the number of cases that passed.
- **Fixture**: a test input with its expected result. Fixtures in this repository are invented.
