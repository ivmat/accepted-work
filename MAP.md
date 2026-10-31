# Map of the family

Acceptance is at the centre. It records a claim about an artifact, the evidence for it, its
assumptions and its open gaps. It is a separate work, published at
[github.com/ivmat/acceptance-format](https://github.com/ivmat/acceptance-format).

This export holds five further formats: [obligation](format_obligation/),
[relation](format_relation/), [trigger](format_trigger/), [impact rule](format_impact-rule/) and
[fill contract](format_fill-contract/).

Obligation is a pure duty core: it records a scheduled judgment, its deadline rule, occurrence
identity, evidence-bound evaluations, drop and linked replacement. It guarantees consistency of
the record, not observation truth or writer admission; deployments may compose their own authority
layer. The link between
obligation and acceptance is at the level of the format. An acceptance decision can be given with
conditions. A condition can leave a duty. The acceptance format refers to the obligation format
for such duties, but it does not include it.

The obligation core outputs, for each occurrence of a duty, one result (`PASS`,
`WARN-recorded`, `PENDING` or `FAIL`), a block bit and a list of required actions. See
[format_obligation/README.md](format_obligation/README.md).

Relation records a typed dependency edge between two things, with a query report shape for what a
change affects. See [format_relation/README.md](format_relation/README.md). Trigger records a rule
that fires when a named event happens, and owns the vocabulary of event kinds; other formats use
that vocabulary or narrow it. See [format_trigger/README.md](format_trigger/README.md). Neither is
wired to obligation or to acceptance yet. Obligation uses its own free-form trigger event names;
see [Format principles](docs/04-format-principles.md#the-principles), principle 2, for an existing fixture.

Impact rule records a per-repository rule: when these paths change, or this event happens, these
targets must be updated, and how important and how urgent that is. It draws its event kinds from
trigger's vocabulary and its relation kinds from relation's. See [format_impact-rule/README.md](format_impact-rule/README.md). Fill
contract records, for a file a template creates, what "filled" means and when filling becomes due.
It draws its maturity event kinds from trigger's vocabulary too. See
[format_fill-contract/README.md](format_fill-contract/README.md). Neither is wired to obligation or
to acceptance.

Four further formats are being prepared for a later export: gate manifest, mechanism, grant and
intent. See [MATURITY.md](MATURITY.md) for the status of each part.
