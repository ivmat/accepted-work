# Changelog

Each entry is one export from the separate working repository, which is not public. The dates are export dates, not
the dates of the work.

## Unreleased — licence

- The format family is now dual licensed under Apache-2.0 or MIT.
- LICENSE-APACHE and LICENSE-MIT are in the repository root.
- README.md and NOTICE.md state the licence. The "not yet open" status note is removed.

## 2026-10-04 — fourth export, the first public one

This is the first export published on GitHub. The entries below it were local exports that were not
published; they are kept so that the history of the formats reads in one place.

**Obligation**

- Obligation is now the pure duty core. Writer authority, authority references, waivers and waiver
  lapse are outside the public grammar; deployments may compose those concerns separately.
- The core adds owner identity rules, an absorbing owner-recorded `dropped` event, and a reciprocal
  `supersedes` link. Natural-key reuse is allowed only along one valid linked chain; a duty that is
  invalid for any reason still claims its natural key.
- Authority-extension structures are rejected structurally with exit 20 before ordinary
  validation. Identifier values and shared core keys are not mistaken for extension markers.
- The tool is split into policy-free primitives and the core fold, and the fold report has a closed
  JSON schema. The private compatibility validator and its golden-output gate are not exported.
- The format's tool and fixtures are now derived from the working repository by the same checked
  script as the other four formats, not hand-copied.

**All five formats**

- Closed grammars: the schemas of relation, trigger, impact rule and fill contract and the obligation
  fold report admit no undeclared keys, including inside report items and `errors[]`. Impact rule's
  deriver parameters are a typed registry (`git-cochange` takes `min_support`, `min_confidence`,
  `advisory`; the other derivers take none).
- Each validator now agrees with its published schema on inputs: integer fields accept integral
  numbers such as `1.0`, as the schemas already did; fill-contract observation reason fields follow
  one rule in schema and tool (optional by file state, `null` or a string with a non-whitespace
  character).
- Every tool validates each report against its declared schema before writing it; a report that
  does not conform becomes an internal-error report (exit 65). Every non-ok report has
  `status: "unknown"` and the common `{code, reason}` error items.
- One byte-identical, stdlib schema-subset checker (`tools/schema_conformance.py`) is shipped with
  each format. It supports a stated subset of JSON Schema and rejects anything outside it; patterns
  follow ECMAScript semantics for the allowed regex subset.
- Stricter input handling: a number that is not finite is an input error at any depth; an integer
  too long to read is an input error, not a crash; bounded integer fields stay within the signed
  64-bit range; each diagnostic reason is at most 2,000 characters.
- Reports are plain UTF-8 without a byte-order mark; text that cannot encode as UTF-8 is shown
  escaped. If stdout cannot receive a report, a tool writes nothing further and exits 74.
- Self-tests run every fixture through the real command line, include cases built to fail on
  purpose, and compare each validator with its schema on generated variants of every fixture.
  Self-test counts: obligation 130, relation 72, trigger 67 + 70, impact rule 88 + 66, fill contract
  79 + 81 + 77.

## 2026-10-02 (unpublished) — status note

- A status note at the top of [README.md](README.md): deep work in progress, not licensed for use
  yet. The licence plan in the README and [NOTICE.md](NOTICE.md) now names when a format opens:
  once it has shown that it is useful in the author's own systems. No format changed.

## 2026-10-01 (unpublished) — third export

- Two further formats, [impact rule](format_impact-rule/) and [fill contract](format_fill-contract/),
  both first cut: a draft schema, their validators (and, for fill contract, an observer and an
  evaluator), and their fixtures. Their files are derived from the working repository by a checked
  script, not hand-copied.
- The status table in [README.md](README.md), the family map in [MAP.md](MAP.md) and the rows in
  [MATURITY.md](MATURITY.md) updated for both. The remaining four formats stay not in this export.
- Five more pinned self-tests in `gates/run_checks.sh`. One of them, fill contract's observer, needs
  `git` on `PATH` and can be skipped with `SKIP_GIT_SELFTESTS=1`.

## 2026-09-29 (unpublished) — second export

- Two further formats, [relation](format_relation/) and [trigger](format_trigger/), both first
  cut: a draft schema, their validators (and, for trigger, a firing evaluator), and their fixtures.
  Their files are derived from the working repository by a checked script, not hand-copied.
- The status table in [README.md](README.md) and the rows in [MATURITY.md](MATURITY.md) updated
  for both. The remaining six formats stay not in this export.
- A stated exception to [Format principles](docs/04-format-principles.md) principle 2: the
  obligation format's worked example does not draw its trigger event name from the trigger
  format's closed vocabulary.
- Three more pinned self-tests in `gates/run_checks.sh`.

## 2026-09-29 (unpublished) — first export

- The entry page, the family map, the status table and the notice.
- The concept notes, in `docs/`.
- The obligation format.
- The names and status of the other formats, in [README.md](README.md) and
  [MATURITY.md](MATURITY.md). Their files are not in this export.
- A public check runner: `gates/run_checks.sh`.
- The acceptance format is linked, not copied. It is published separately under its own licence.
