# accepted-work

Record formats for work you did not watch being done.

> **Status: work in progress.**
> Every format can change without notice. Comments are welcome. Contributions are not accepted yet.

## What this is

A family of record formats and the programs that check them. A record format is a fixed file
shape for one kind of fact about work: a duty, a dependency, a check, a finding. With a record,
the person who accepts a piece of work can check it with tools that the producer does not control,
whether a person or a language model did the work.

The main format of the family is the **acceptance format**: the record of a claim about a piece of
work, its evidence, its assumptions and its open gaps. It is published separately, at
[github.com/ivmat/acceptance-format](https://github.com/ivmat/acceptance-format). The formats in
this repository describe the work around an acceptance decision. The name of this repository says
what the family is for: work that was accepted, with a record of why.

The formats are open under Apache-2.0 or MIT. They are still in development.

## Who made it

Ivo Matijašević.

## The one rule behind it

**Reduce assumptions, never eliminate them, and know what is left.** A record writes down "what is
left", in a form that a stranger can check. So:

1. A person or a model proposes. A deterministic program decides only what it is able to decide.
2. In this export, the obligation self-test includes cases built to fail on purpose, and checks
   that each one is rejected as expected. The acceptance format's own rule on this is at
   [its README](https://github.com/ivmat/acceptance-format#why-a-green-check-is-not-enough-the-check-must-be-able-to-fail).
3. If a program cannot decide, the result is "unknown", never "pass".
4. Assumptions and open gaps are fields of the record, not footnotes.

## What is in it, and how far each part is

- **planned** — a design note only. No schema, no code.
- **first cut** — a specification, a validator and test cases; a draft schema where one exists. No file outside
  the format's own directory is written to it, and no tool outside it reads it.
- **candidate** — a first cut that is also on trial in my private work: files there are written
  to the format, or a tool there reads it. The trial blocks nothing yet.
- **in use** — real work depends on it, and a routine check enforces it. No part of this
  repository is in use.

Details: [MATURITY.md](MATURITY.md). Relations: [MAP.md](MAP.md).

| part | what it records | status |
|---|---|---|
| [Concept notes](docs/) | how a format is made; what a program can decide | not a format |
| [Obligation](format_obligation/) | a duty: trigger, deadline, a mechanical test for "done" | candidate: one pilot; no routine check runs it |
| [Relation](format_relation/) | a typed dependency edge, with a query report shape for what a change affects | first cut: a draft schema and a validator; not wired to anything else here |
| [Trigger](format_trigger/) | a rule that fires when a named event happens; owns the vocabulary of event kinds | first cut: a draft schema, a validator and a firing evaluator; not wired to anything else here |
| [Impact rule](format_impact-rule/) | a per-repository rule: when these paths change or this event happens, these targets must be updated, how important, how urgent | first cut: a draft schema, a validator and an evaluator; not wired to anything else here |
| [Fill contract](format_fill-contract/) | what "filled" means for a file a template creates, and when filling becomes due | first cut: a draft schema, a validator, an observer and an evaluator; not wired to anything else here |
| Gate manifest, mechanism, grant, intent | the checks a repository runs; what a program decides; delegated authority; a requirement | validators and test cases exist; being prepared for a later export; not in this export |
| Claim, work surface, change impact | a general statement; one worker's allowed files and tools; change impact as one record | planned; not exported |
| [Acceptance](https://github.com/ivmat/acceptance-format) | a claim, its evidence, assumptions and open gaps | separate work; see "Licence" |

## How the work is produced

The work is done in a separate working repository, which is not public. Selected parts are exported
here, with their text rewritten for a reader without context. The history here is the history of the exports, not
of the work. The working history, the trials and the reviews stay private. Language
models wrote most of the text and code, under my direction. I read most of each export, and I
am responsible for every claim on this page.

What I do not read line by line is held by hard gates: checks that a program runs and that must pass
before anything is exported. They include the schemas and their checkers, self-tests with cases built
to fail on purpose, a comparison of the validators with their schemas, and a scan for private names. A
gate is only as good as what it checks, so the gates are meant to become stricter with every export:
each weakness that a review finds becomes a new check, not a one-off fix.

## How to check something yourself

```sh
bash gates/run_checks.sh
```

It needs bash and Python 3.11 or later. The obligation tool also needs a time-zone database (on
some systems, the Python package `tzdata`). A tool in a `tools/` folder also accepts `--selftest`,
which runs it on its own test cases, including inputs that it must reject. A green run means that
all selected self-tests passed. It does not show that a tool is correct in general. A validator
checks the shape of a record, not the truth of its content.

## What is deliberately not here

- **Records of real use.** The test cases here were made for testing. Records from my own work
  stay private.
- **Some members of the family.** Their absence is a decision, not an oversight.
- **A copy of the acceptance format.** It is linked, so that one text has one licence.

## Licence

Dual licensed under Apache-2.0 or MIT; see [LICENSE-APACHE](LICENSE-APACHE) and
[LICENSE-MIT](LICENSE-MIT). Contributions are not accepted yet.

The acceptance format and its protocol are a separate work, only referenced here. They are
published at [github.com/ivmat/acceptance-format](https://github.com/ivmat/acceptance-format). Its
repository states its licence as: "Dual licensed under Apache-2.0 or MIT".

## How to reply

The most useful reply is one line: "this duplicates X", "the boundary you are missing is Y", or
"the smallest experiment that would break this is Z". Comments are welcome; you can reach me
through my GitHub profile, [github.com/ivmat](https://github.com/ivmat).

Changes: [CHANGELOG.md](CHANGELOG.md).
