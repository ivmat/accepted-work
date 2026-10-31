# accepted-work

Record formats for work you did not watch being done.

> **Status: work in progress.** Formats can change without notice. Comments are welcome;
> contributions are not accepted yet. See [MATURITY.md](MATURITY.md) for current limits.

## What this is

A family of file formats and tools for recording duties, dependencies and checks around an
acceptance decision. The [acceptance format](https://github.com/ivmat/acceptance-format), which
records claims, evidence, assumptions and open gaps, is published separately.

## What is in it, and how far each part is

| format | what it records |
|---|---|
| [Obligation](format_obligation/) | a duty, its deadline and the recorded judgments about it |
| [Relation](format_relation/) | a typed dependency edge |
| [Trigger](format_trigger/) | a rule that fires on a named event |
| [Impact rule](format_impact-rule/) | which updates a change requires |
| [Fill contract](format_fill-contract/) | what makes a generated file filled, and when filling is due |

[MATURITY.md](MATURITY.md) owns the status definitions, inventories and missing features.
[MAP.md](MAP.md) explains the relationships between formats. [Concept notes](docs/) explain the
shared ideas; [How to read a format](docs/06-how-to-read-a-format.md) gives a reading and usage guide.

## How to check something yourself

```sh
bash gates/run_checks.sh
```

Requires bash and Python 3.11 or later. Obligation also needs a time-zone database (on some
systems, the Python package `tzdata`); fill contract's observer self-test needs `git` on `PATH`.
The runner checks inventories and runs the exported tools' self-tests. It reports justified
platform skips separately from passed cases. A green run means the applicable checks passed, not that the tools are correct in general. A validator checks records, not whether
their contents are true.

Audited independent suites run with two workers; other suites run alone. `GATE_JOBS=1` forces
serial execution; values up to 4 are capped by CPU count. Every tool has a 900-second timeout,
and results are reported in roster order. No result is cached. Run without `-O` or
`PYTHONOPTIMIZE`: optimized Python disables assertion controls and is refused.

## The one rule behind it

**Reduce assumptions, never eliminate them, and know what is left.** A person or model proposes;
a program decides only what it can check. Undecidable results remain unknown. Assumptions and
gaps stay visible in the record. Self-tests include inputs that must be rejected; the
[format principles](docs/04-format-principles.md) explain the rules and their current limits.

## How the work is produced

Selected parts of a separate repository are exported here for readers without that context.
The source, working notes, trials and reviews stay private; this history records exports. Language
models wrote most text and code under my direction. I read most of each export and am responsible
for its claims. Export checks cover schemas, self-tests, validator/schema agreement and private
names; each check provides only the assurance its scope supports.

## What is deliberately not here

Real-use records stay private; public fixtures are synthetic. Some family members are not yet
exported; [MATURITY.md](MATURITY.md#other-parts) lists them. Acceptance is linked, not copied.

## Who made it

Ivo Matijašević.

## Licence

Dual licensed under Apache-2.0 or MIT; see [LICENSE-APACHE](LICENSE-APACHE) and
[LICENSE-MIT](LICENSE-MIT). Contributions are not accepted yet. The separate
[acceptance repository](https://github.com/ivmat/acceptance-format) states its own licence.

## How to reply

Comments about overlap, missing boundaries or useful experiments are welcome through
[my GitHub profile](https://github.com/ivmat). See [CHANGELOG.md](CHANGELOG.md) for export history.
