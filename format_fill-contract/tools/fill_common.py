"""fill_common.py — shared, stdlib-only building blocks for the three `fill_*` tools
(`fill_validate.py`, `fill_observe.py`, `fill_evaluate.py`). Not a CLI itself.

Owns the ONE closed vocabulary (so the three tools cannot silently drift apart):

  - FILLED_PREDICATES — the closed predicate registry for `filled_when` trees; DUE_PREDICATES — the
    closed predicate registry for `due_when` trees. Each maps a predicate name to a
    {param_name: (required: bool, kind: "str"|"int")} spec.
  - parse_duration / DURATION_RE — the day-only `<int>d` duration grammar, bounded to
    [DURATION_MIN_DAYS, DURATION_MAX_DAYS]. Every observed fact this format reasons about (a git
    commit date, a stamp date, "days since instantiated") is day-granular, so an hour unit would be
    false precision; day-only also keeps every duration well inside `timedelta`'s own representable
    range, so no duration-driven date arithmetic can ever overflow.
  - validate_path_grammar / validate_glob_grammar — the file-path and glob grammars a contract's
    `file` entries and an observer's src/test/spec/manifest glob arguments must both satisfy.
  - The strict JSON-input contract (unique_pairs / reject_constant / find_json_input_defect /
    load_json_strict) every JSON reader in this format funnels a foreign document through before
    trusting it — used by `fill_evaluate.py` to read an OBSERVATIONS document.
  - validate_contract(...) — full contract-shape + closed-vocabulary validation, used by
    `fill_validate.py` directly and, in fail-closed mode, by `fill_observe.py`/`fill_evaluate.py`
    before either trusts a contract at all.
  - validate_observations(...) — full shape validation of an OBSERVATIONS document (whether it came
    from `fill_observe.py` or any other source) before `fill_evaluate.py` trusts any of it: every
    field `fill_evaluate.py` reads is checked for the type it expects, so a malformed value never
    reaches Python's own arithmetic/date-parsing and crashes the tool — it becomes one entry in a
    normal error report instead.
  - walk_leaves(...) — a predicate-tree walker, used by `fill_observe.py` to learn which
    not-contains/stamp/rule facts a given file's `filled_when` tree needs gathered.
  - Kleene three-valued evaluation (TRUE/FALSE/UNKNOWN, eval_any/eval_all/eval_not) — used only by
    `fill_evaluate.py`; kept here so the truth table itself is pinned in one place.

Every predicate-expression tree walker (validate_expr, walk_leaves, expr_has_predicate) is
ITERATIVE (an explicit stack, never a function calling itself once per level of the tree's own
nesting) and enforces MAX_EXPR_DEPTH: a contract is authored by hand, but nothing stops it from
nesting `any`/`all`/`not` far deeper than any real contract would — an unbounded recursive walker
would let such an input crash the tool with a Python RecursionError before any of this format's own
error codes ever fire.

Stdlib only: json, re, tomllib (Python >= 3.11), datetime, pathlib.

Stdout environment contract: for report-producing commands, stdout may be a real file descriptor
of any number or a text stream with ``write``/``flush``. The tool writes its one report completely
and exits with its documented code, or exits 74 having written nothing further. Any other stdout
object, or a stdout operation failing with OSError, ValueError, TypeError, OverflowError,
AttributeError, or io.UnsupportedOperation, means stdout unavailable and exit 74; it never produces
exit 120 or a traceback. Argparse ``--help``/usage text is argparse's own output, not a report, and
is not routed through this boundary. External signals (including SIGINT, SIGPIPE reset to
SIG_DFL, and SIGKILL) remain subject to the OS/Python signal behavior. There are no other scope
exclusions.
"""
from __future__ import annotations

import itertools
import io
import json
import math
import os
import re
import select
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPORT_SCHEMA = None
REPORT_SCHEMA_REASON = "library; report contracts are declared by the calling tools"


def is_schema_integer(value) -> bool:
    """JSON Schema integer semantics: finite integral numbers, excluding booleans."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, (float, Decimal)) and math.isfinite(value) and value == int(value)


def normalize_schema_numbers(value):
    """Normalize integral TOML/JSON numbers at admission, before integer-only operations."""
    if is_schema_integer(value):
        return int(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: normalize_schema_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_schema_numbers(item) for item in value]
    return value

# ---------------------------------------------------------------------------
# Kleene three-valued logic
# ---------------------------------------------------------------------------
TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"
E_USAGE = 64
E_OUTPUT = 74


def eval_any(values: list) -> str:
    if TRUE in values:
        return TRUE
    if UNKNOWN in values:
        return UNKNOWN
    return FALSE


def eval_all(values: list) -> str:
    if FALSE in values:
        return FALSE
    if UNKNOWN in values:
        return UNKNOWN
    return TRUE


def eval_not(value: str) -> str:
    if value == TRUE:
        return FALSE
    if value == FALSE:
        return TRUE
    return UNKNOWN


# ---------------------------------------------------------------------------
# Closed predicate registries
# ---------------------------------------------------------------------------
# name -> {param: (required: bool, kind: "str" | "int")}
FILLED_PREDICATES = {
    "no-marker": {},
    "not-contains": {"text": (True, "str")},
    "rows-beyond-template": {"min": (True, "int")},
    "min-lines": {"n": (True, "int")},
    "stamp": {"key": (True, "str")},
    "fresh": {"max_age": (True, "str"), "key": (False, "str")},
    "digest-present": {},
    "declared-rules": {"min": (True, "int")},
    "command": {"shell": (True, "str")},
}

# Not observed at all in this first cut — still a CLOSED, recognized predicate name (so a contract
# using it validates), but fill_observe.py never resolves a fact for it and fill_evaluate.py always
# resolves it to UNKNOWN.
DUE_PREDICATES = {
    "instantiated": {},
    "first-src-commit": {},
    "first-test": {},
    "first-spec": {},
    "first-dependency": {},
    "first-release": {},
    "commits-at-least": {"n": (True, "int")},
    "days-since-setup": {"n": (True, "int")},
    "public": {},
    "session-closed": {},
    "new-dependency": {},
}

# Predicates this first cut never resolves to true/false — always UNKNOWN by construction.
UNOBSERVED_DUE_PREDICATES = {"public", "session-closed", "new-dependency"}
UNOBSERVED_FILLED_PREDICATES = {"command"}

IMPORTANCE_ENUM = ("critical", "high", "normal", "low")
IMPORTANCE_RANK = {name: i for i, name in enumerate(IMPORTANCE_ENUM)}
ADEQUACY_WHEN_ENUM = ("on-filled",)

# .fullmatch, not .match: without MULTILINE, Python's trailing `$` in a pattern matches just BEFORE
# a final '\n' in the subject string, so `DURATION_RE.match("5d\n")` would wrongly succeed (a TOML
# string CAN carry an embedded '\n' via an escape, e.g. `urgency_after_due = "5d\n"`). No `.strip()`
# is applied to the candidate string ANYWHERE this grammar is checked, either — every other grammar
# check in this module (path, glob, predicate names) already rejects leading/trailing whitespace by
# construction (it is simply not in the allowed character set); a duration must hold to the exact
# same full-string contract, not a laxer one.
#
# Day-only: this format's own observed facts (a git commit date, a stamp date, "days since
# instantiated") are all day-granular, so an `h` unit would be false precision without ever changing
# a real decision — see DURATION_MIN_DAYS/DURATION_MAX_DAYS below for the bound this also lets every
# duration stay well inside.
# Ignore leading zeros, then check decimal length before converting to int.
DURATION_RE = re.compile(r"([0-9]+)d")
DURATION_MIN_DAYS = 1
DURATION_MAX_DAYS = 36_500  # ~100 years; date arithmetic still checks year-9999 overflow.

# ---------------------------------------------------------------------------
# Numeric bounds for predicate-expression integer parameters and OBSERVATIONS counts. Both exist so
# a syntactically well-typed but absurd number (a duration or count near Python's/timedelta's own
# limits) is a normal validation error, never a runtime OverflowError/MemoryError reaching a caller.
# ---------------------------------------------------------------------------
# `min`/`n` params on a predicate leaf (rows-beyond-template, min-lines, stamp/fresh have none,
# declared-rules, commits-at-least, days-since-setup): all represent "at least this many" counts, so
# 0 is not a meaningful lower bound (an "at least 0" predicate would always be true) — 1 is. The
# upper bound limits arithmetic cost; dates near year 9999 can still overflow, in which case
# the evaluator reports unknown.
PREDICATE_INT_MIN = 1
PREDICATE_INT_MAX = 100_000

# `template_rows` is a row COUNT (may legitimately be 0 — an empty template), not an "at least"
# threshold, so its own minimum differs from PREDICATE_INT_MIN.
TEMPLATE_ROWS_MIN = 0
TEMPLATE_ROWS_MAX = 100_000

# Observation-side counts (table_rows_actual, line_count, declared_rules_count, commits_count,
# days_since_setup/days_since_setup): generous upper bounds on what a real scan of a
# `MAX_SCAN_BYTES`-capped file or a real git history could ever report.
OBS_COUNT_MIN = 0
OBS_COUNT_MAX = 10_000_000

# ---------------------------------------------------------------------------
# Predicate-expression tree size/depth bound (shared by validate_expr, walk_leaves,
# expr_has_predicate below)
# ---------------------------------------------------------------------------
# A contract is hand-authored, but TOML happily nests inline tables/arrays as deep as an author (or
# an attacker with write access to a contract file) cares to type. 64 is generous for anything a
# real filled_when/due_when tree needs (the deepest real examples in this format's own fixtures
# nest 2-3 levels) while still bounding every walker below to O(depth) stack frames, never O(input
# nesting) Python call frames.
MAX_EXPR_DEPTH = 64


_SAFE_REPR_MAX_CHARS = 1000
_SAFE_REPR_MAX_ITEMS = 500

# The DOCUMENTED diagnostic bound: the README promises that no single (code, reason) entry this
# format's tools ever report is longer than this many characters, for ANY input, including an
# adversarial one (a 1,000,000-character field value, an attacker-chosen key name, ...). This is
# comfortably above `_SAFE_REPR_MAX_CHARS` (the cap on one `safe_repr_for_error` call's own output)
# to leave headroom for the fixed surrounding text every error message also carries (a path prefix,
# a closed-vocabulary listing, a code number) — every reason this format's validators build embeds
# AT MOST one bounded `safe_repr_for_error` call per interpolated input-derived value, so this
# ceiling is never approached by accumulation, only checked here as the end-to-end guarantee a
# caller (and this format's own self-tests) can rely on without re-deriving it from
# `_SAFE_REPR_MAX_CHARS` plus "however much fixed text a message happens to add".
DIAGNOSTIC_REASON_MAX_CHARS = 2000

# A `str` value longer than this is truncated to THIS MANY CHARACTERS BEFORE `repr()` ever sees it
# — never after. `repr()`/`str()` on an arbitrarily large string is itself `O(len(v))` allocation
# and quote/backslash-escaping work, the same class of unbounded cost `_safe_repr_scalar`'s integer
# guard (below) already exists to avoid for an oversized int — a hostile million-character string
# value must never be hand to `repr()` in full even once, whether it is the top-level value or one
# element nested (at any depth) inside a container the walk in `safe_repr_for_error` also bounds by
# item count. 200 is generous for any real value this format's error messages ever need to show
# (an enum value, a glob, a path, an id) while keeping the per-element cost of the container walk
# fixed regardless of how long any one element claims to be.
_SAFE_REPR_STR_PREVIEW_CHARS = 200


_ESCAPE_SEQUENCE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")


def _escape_unicode_safe(s: str) -> str:
    """Replaces every character in `s` that cannot itself re-encode as UTF-8 (a lone UTF-16
    surrogate, U+D800-U+DFFF — the shape a CLI argument takes once `surrogateescape` has decoded an
    OS byte with no valid UTF-8 interpretation) with a visible backslash escape. Idempotent on a
    string that is already UTF-8-safe."""
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


def _backoff_escape_boundary(s: str, keep: int) -> int:
    """If slicing `s` at `keep` would land strictly inside a `\\uXXXX` escape sequence, backs `keep`
    off to the index just before that escape begins, so a capped string never ends with an
    unterminated half-escape. A `\\uXXXX` escape is always exactly 6 characters."""
    start = max(0, keep - 5)
    end = min(len(s), keep + 6)
    for m in _ESCAPE_SEQUENCE_RE.finditer(s, start, end):
        if m.start() < keep < m.end():
            return m.start()
    return keep


def _cap_with_marker(s: str, max_chars: int, marker: str = "...>") -> str:
    """Truncates `s` so the RETURNED string, INCLUDING `marker`, is never longer than `max_chars` —
    the one place every bounded-text helper below (`_safe_repr_scalar`, `safe_repr_for_error`,
    `bound_reason`, `bound_path_preview`) enforces its own cap, so the marker's own length can never
    itself push a result past the cap it exists to enforce (the defect this fold fixes: appending a
    4-character marker AFTER already slicing to the full cap used to return a 1,004-character result
    for a 1,000-character cap). `max_chars` is assumed to be comfortably larger than `len(marker)`,
    true for every caller in this module (the smallest cap used here is `_SAFE_REPR_MAX_CHARS`,
    1000). The cut point is also backed off (`_backoff_escape_boundary`) so it never splits a
    `\\uXXXX` escape sequence in half."""
    keep = max(max_chars - len(marker), 0)
    keep = _backoff_escape_boundary(s, keep)
    return s[:keep] + marker


def _safe_repr_is_container(v) -> bool:
    return isinstance(v, (list, tuple, dict, set, frozenset))


def _safe_repr_scalar(v) -> str:
    """`repr(v)` for any non-container value, bounded on two independent axes, NEITHER of which
    ever calls `repr()`/`str()` on more of `v` than a fixed prefix:

    - An oversized Python `int` (`bit_length() > 64`) would make `repr()`/`str()` ITSELF raise:
      CPython bounds integer-to-string conversion (`sys.get_int_max_str_digits()`, 4300 digits by
      default) to stop an adversarially huge int from becoming a denial-of-service via string
      formatting. `bit_length() > 64` (not the much larger 4300-digit guard itself) is the chosen
      threshold: simple, uniform, and comfortably below where the real guard could ever fire. The
      digit count itself is an ESTIMATE from the bit length (`bit_length * log10(2)`, never by
      stringifying `v`) — exact enough for an error message, and safe to compute for a value of any
      size.
    - A `str` longer than `_SAFE_REPR_STR_PREVIEW_CHARS` is sliced to that many characters BEFORE
      `repr()` is called on it — never repr'd in full first and truncated after. The returned text
      is then, like every other case below, hard-capped to `_SAFE_REPR_MAX_CHARS` characters (plus
      a trailing `...>` marker) — so a SCALAR return (the top-level value, or one value nested
      inside a container) is bounded by the exact same fixed constant the container-walking
      `safe_repr_for_error` itself is, never only by the container-walk path."""
    if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > 64:
        digits = int(v.bit_length() * math.log10(2)) + 1
        return f"<{digits}-digit integer>"
    if isinstance(v, str) and len(v) > _SAFE_REPR_STR_PREVIEW_CHARS:
        s = repr(v[:_SAFE_REPR_STR_PREVIEW_CHARS]) + "...>"
    else:
        s = repr(v)
    if len(s) > _SAFE_REPR_MAX_CHARS:
        s = _cap_with_marker(s, _SAFE_REPR_MAX_CHARS)
    return s


def safe_repr_for_error(value) -> str:
    """A bounded stand-in for `repr(value)`, safe to interpolate into any error message for ANY
    foreign value this format's tools read — a TOML/JSON scalar, or a CONTAINER (list/tuple/dict/
    set) nesting one, to any depth, holding one. Two independent, adversary-proof bounds, neither
    of which depends on trusting the input's own declared size:

    - An oversized `int` (`_safe_repr_scalar`, above) is replaced by a cheap digit-count estimate
      wherever it appears — at the top level, or nested inside a container at any depth — never by
      calling `repr()`/`str()` on it, which is the one call CPython itself can make raise
      (`ValueError: Exceeds the limit ... for integer string conversion`) or run unboundedly slow.
    - A container is walked with an EXPLICIT, heap-allocated work stack — never Python function
      recursion — so an adversarially DEEP container (a list nested inside a list nested inside a
      list, ...) cannot exhaust the interpreter's own recursion limit the way a recursive
      `repr()`/`str()` implementation could. Each container's own elements are read through
      `itertools.islice`, never materialized in full first, so an adversarially WIDE container (a
      list with millions of entries) costs only the remaining item budget to inspect, never O(its
      declared length). Both axes are capped together: at most `_SAFE_REPR_MAX_ITEMS` values (of
      any kind, at any depth) are ever rendered, and the assembled text is hard-truncated to
      `_SAFE_REPR_MAX_CHARS` characters (plus a trailing `...>` marker) before this function
      returns — so the TOTAL work done and the TOTAL string length are both bounded by fixed
      constants, never by anything read from the value itself."""
    if not _safe_repr_is_container(value):
        return _safe_repr_scalar(value)

    parts = []
    total_len = 0
    items_seen = 0
    # Each stack entry is ("tok", literal text to emit as-is) or ("val", a value still to render).
    stack = [("val", value)]

    while stack:
        if total_len >= _SAFE_REPR_MAX_CHARS or items_seen >= _SAFE_REPR_MAX_ITEMS:
            break
        kind, payload = stack.pop()
        if kind == "tok":
            parts.append(payload)
            total_len += len(payload)
            continue
        v = payload
        items_seen += 1
        if not _safe_repr_is_container(v):
            s = _safe_repr_scalar(v)
            parts.append(s)
            total_len += len(s)
            continue

        if isinstance(v, dict):
            open_b, close_b, source = "{", "}", v.items()
        elif isinstance(v, tuple):
            open_b, close_b, source = "(", ")", v
        elif isinstance(v, list):
            open_b, close_b, source = "[", "]", v
        else:  # set, frozenset
            open_b, close_b, source = "{", "}", v

        total_n = len(v)
        budget = max(_SAFE_REPR_MAX_ITEMS - items_seen, 0)
        take = min(budget, total_n)
        entries = list(itertools.islice(source, take))
        has_more = total_n > take

        sequence = [("tok", open_b)]
        for i, item in enumerate(entries):
            if i:
                sequence.append(("tok", ", "))
            if isinstance(v, dict):
                k, val = item
                sequence.append(("val", k))
                sequence.append(("tok", ": "))
                sequence.append(("val", val))
            else:
                sequence.append(("val", item))
        if has_more:
            if entries:
                sequence.append(("tok", ", "))
            sequence.append(("tok", f"...({total_n - take} more)"))
        sequence.append(("tok", close_b))
        stack.extend(reversed(sequence))

    text = "".join(parts)
    if len(text) > _SAFE_REPR_MAX_CHARS or len(stack) > 0:
        # Either branch needs the trailing marker — len(text) already over cap, OR the item/char
        # budget stopped the walk before every frame was emitted — and either way the marker itself
        # must never push the result past the cap (`_cap_with_marker`, not a bare slice-then-append).
        text = _cap_with_marker(text, _SAFE_REPR_MAX_CHARS)
    return text


_REASON_TRUNCATION_MARKER = "...<truncated>"


def bound_reason(text) -> str:
    """The FINAL, STRUCTURAL enforcement point for the documented diagnostic bound: returns `text`
    (coerced to `str` first if it is not already one) truncated, if necessary, so the RETURNED
    string is NEVER longer than `DIAGNOSTIC_REASON_MAX_CHARS` characters, including the truncation
    marker it appends — for ANY input, of any length, built through ANY path. This is deliberately
    NOT the only bound in this module (`safe_repr_for_error` already bounds one interpolated value
    at a time, and `bound_path_preview` below bounds a path argument at its own call site, so a
    well-formed message rarely if ever reaches this cap) — it is the one place EVERY `reason` string
    this format's tools ever report is checked again, unconditionally, immediately before it leaves
    a report constructor. That is what makes the bound hold even for a call site that forgot to
    self-bound (a raw `OSError`/exception message, an aggregated multi-error join, or a future call
    site added without rereading this module's own rules) — the report constructor itself, not every
    call site separately, is what a caller (and this format's own self-tests) can rely on."""
    if not isinstance(text, str):
        text = str(text)
    # Escaped BEFORE the length check/cap, not after: a usage-error reason built
    # straight from argparse's own message (which embeds the raw, `surrogateescape`-decoded argv
    # text) must never carry a lone surrogate past this, the final, unconditional backstop.
    text = _escape_unicode_safe(text)
    if len(text) <= DIAGNOSTIC_REASON_MAX_CHARS:
        return text
    return _cap_with_marker(text, DIAGNOSTIC_REASON_MAX_CHARS, marker=_REASON_TRUNCATION_MARKER)


def bound_path_preview(path) -> str:
    """Returns a short, BOUNDED preview of a filesystem path (or any other path-like value: a
    `pathlib.Path`, or a raw CLI string) safe to interpolate DIRECTLY into an error message — for
    ANY input, including an adversarially long `--contract`/`--observations` argument (a
    10,000-character string). Truncated to `_SAFE_REPR_STR_PREVIEW_CHARS` characters (plus a
    trailing marker if truncated) BEFORE any further formatting, the same bound (and the same
    reasoning) `_safe_repr_scalar` already applies to an oversized string VALUE — this is the
    counterpart for a path argument, which every call site below interpolates RAW (never through
    `safe_repr_for_error`, which would add quoting a plain path display does not want). Call this at
    every site that interpolates a `--rules`/`--contract`/`--observations`/`--repo`/`--changed-file`
    path value directly, and in place of an exception's own `str(exc)` wherever that exception is
    known to embed the same (or a derived) path in full — use the exception's CLASS name alongside
    this bounded preview instead, never the exception's unbounded `str()`. A CLI argument is decoded
    by Python from the OS's raw bytes using `surrogateescape` (e.g. a POSIX filename byte `\\xff`
    with no valid UTF-8 interpretation becomes the Python string `"\\udcff"`, a LONE UTF-16
    surrogate) — `str(path)` on such a value is still a `str`, but one that cannot itself encode as
    UTF-8, so interpolating it RAW into a message would put a string this format's own reports can
    never actually serialize into (this is a SEPARATE, structural concern from `encodes_as_utf8`
    above, which only gates CONTENT values this format validates, not every path-like argument an
    exception message may embed). Every such character is replaced with a visible, UTF-8-safe
    backslash escape here, BEFORE the length cap below, so the cap still holds on the ESCAPED text
    (escaping can only lengthen a string, never shorten it — capping first and escaping after could
    let a result exceed the cap)."""
    s = str(path)
    s = _escape_unicode_safe(s)
    if len(s) <= _SAFE_REPR_STR_PREVIEW_CHARS:
        return s
    return _cap_with_marker(s, _SAFE_REPR_STR_PREVIEW_CHARS)


JOIN_ERROR_LIST_MAX_ITEMS = 10

# Each item is bounded to THIS many characters (plus a trailing marker if truncated) BEFORE it is
# joined — not just the (possibly nonexistent) bounding a caller already applied. `join_error_list`
# does NOT assume its items are already individually bounded: a caller may be joining raw, foreign
# strings (e.g. unknown `files[]` entries read straight from an `--observations` document) of up to
# this format's own length limits, not something already run through `safe_repr_for_error` first.
# `JOIN_ERROR_LIST_MAX_ITEMS` (10) of these, each capped to this many characters, plus the fixed
# `"; ... and <N> more"` suffix (at most ~40 characters for any realistic count), is always
# comfortably inside `DIAGNOSTIC_REASON_MAX_CHARS` (2000) even before any caller-added prefix text —
# which is what makes the omitted-count suffix survive `bound_reason`'s own final cap: the defect
# this bound fixes is a handful of very long items alone consuming the ENTIRE 2,000-character budget,
# pushing the "; ... and N more" suffix (the only place a caller learns anything was left out) past
# the cut.
JOIN_ERROR_LIST_ITEM_MAX_CHARS = 100


def _bound_join_item(item) -> str:
    s = item if isinstance(item, str) else safe_repr_for_error(item)
    if len(s) <= JOIN_ERROR_LIST_ITEM_MAX_CHARS:
        return s
    return _cap_with_marker(s, JOIN_ERROR_LIST_ITEM_MAX_CHARS)


def join_error_list(errors: list, limit: int = JOIN_ERROR_LIST_MAX_ITEMS) -> str:
    """Joins a LIST of error strings into one `"; "`-separated summary, bounded on TWO independent
    axes: an adversarial input (e.g. an `--observations` document with hundreds of malformed
    `files[]` entries) can make `len(errors)` itself unbounded (bounded here by `limit`, defaulting
    to `JOIN_ERROR_LIST_MAX_ITEMS`), AND any one entry can itself be arbitrarily long (bounded here by
    `JOIN_ERROR_LIST_ITEM_MAX_CHARS`, applied to EACH of the `limit` shown entries individually,
    BEFORE they are joined — never relying on a caller to have already bounded them). At most `limit`
    entries are ever shown, each at most `JOIN_ERROR_LIST_ITEM_MAX_CHARS` characters; a
    `"; ... and N more"` suffix (computed from the FULL, original entry count — never silently
    dropped, and never itself truncated by the per-item cap) names exactly how many were left out of
    the `limit` shown. Bounding each entry's OWN length here, rather than only bounding the limit on
    entry COUNT, is what keeps the omitted-count suffix from being squeezed out by a handful of very
    long entries alone exhausting the final report-wide character cap (`DIAGNOSTIC_REASON_MAX_CHARS`)
    before the suffix is ever reached. This is bounded at the SOURCE, before `bound_reason`'s own
    final truncation ever runs, so a caller's message keeps every one of the first `limit` errors
    intact (if short) rather than losing the tail ones to an arbitrary mid-string cut."""
    shown = errors[:limit]
    text = "; ".join(_bound_join_item(item) for item in shown)
    remaining = len(errors) - len(shown)
    if remaining > 0:
        text += f"; ... and {remaining} more"
    return text


class FillError(Exception):
    """Raised with a (code, reason) pair by any fill_common check; callers translate to their own
    tool's exit-code numbering (each tool's module docstring is the authoritative map)."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


def parse_duration(text) -> timedelta | None:
    """Returns a timedelta for a valid, IN-BOUNDS `<int>d` string, else None (never raises — callers
    decide whether a bad duration is a validation error). No stripping: a string with leading/
    trailing whitespace (or any other character) does not match, full stop — the same full-string
    contract every other grammar check in this module holds to. Bounded to
    [DURATION_MIN_DAYS, DURATION_MAX_DAYS]: a value outside that range returns None exactly like a
    malformed string, rather than reaching `timedelta(days=n)` with a number large enough to risk an
    `OverflowError` (or a value later added to a `date` that would overflow `date`'s own
    year-1..9999 range) — checked here, once, so every caller gets the same bound for free."""
    if not isinstance(text, str):
        return None
    m = DURATION_RE.fullmatch(text)
    if not m:
        return None
    digits = m.group(1).lstrip("0") or "0"
    if len(digits) > len(str(DURATION_MAX_DAYS)):
        return None
    n = int(digits)
    if not (DURATION_MIN_DAYS <= n <= DURATION_MAX_DAYS):
        return None
    return timedelta(days=n)


def is_nonblank_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


_ECMASCRIPT_WHITESPACE = frozenset(
    "\u0009\u000b\u000c\u0020\u00a0\ufeff\u000a\u000d\u2028\u2029"
    "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u202f\u205f\u3000"
)


def is_nonblank_reason(value) -> bool:
    """Match JSON Schema's ECMAScript ``\\S`` semantics exactly.

    Python ``str.strip()`` additionally treats U+001C..U+001F as whitespace, so it is not the
    executable definition for the two reason fields constrained by schema pattern ``\\S``.
    """
    return isinstance(value, str) and any(ch not in _ECMASCRIPT_WHITESPACE for ch in value)


def encodes_as_utf8(value: str) -> bool:
    """False for a Python str that cannot encode as UTF-8 (it holds a lone UTF-16 surrogate,
    U+D800-U+DFFF). Checked directly against this repo's own `tomllib` (Python 3.11+): a `\\uD800`-
    style escape in a TOML basic string is correctly REJECTED at parse time
    (`tomllib.TOMLDecodeError: Escaped character is not a Unicode scalar value`), and a *raw*,
    unescaped lone surrogate cannot occur in valid UTF-8 bytes at all — so a `file` value read via
    `load_toml`'s `path.read_text(encoding="utf-8")` + `tomllib.loads(...)` can never actually carry
    one; this call site's own use of `encodes_as_utf8` is defense in depth, not a remedy for a
    live TOML defect. `json.loads`, by contrast, DOES accept a `\\uD800`-style escape and happily
    produces a lone-surrogate `str` (verified directly: `json.loads('{"x": "\\\\ud800"}')`
    succeeds) — `fill_common.find_json_input_defect` (the OBSERVATIONS-JSON reader's own input
    contract) is where this check catches a real, reachable input."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def load_toml(path: Path) -> dict:
    """Raises FillError(65, ...) on any read/parse failure — the one shared 'malformed input' path
    every tool funnels through before doing its own numbered error classes. Also catches the bare
    `ValueError` `tomllib` itself raises (NOT wrapped in `tomllib.TOMLDecodeError`) for an integer
    literal too long for CPython's own integer-string-conversion guard (~4300 digits) — this failure
    happens INSIDE the parse, before any dict exists to attribute it to one field, so it is reported
    the same way as any other unparseable TOML: exit 65, not a runtime crash reaching a caller."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # `str(exc)` on an `OSError` embeds its own `filename` argument IN FULL (the same value
        # `path` already names) — the exception's CLASS name plus one bounded path preview, never
        # `str(exc)` itself, is what keeps this message's length independent of how long a
        # `--contract` argument happens to be.
        raise FillError(65, f"cannot read contract file {bound_path_preview(path)}: "
                             f"{type(exc).__name__}")
    except UnicodeError as exc:
        raise FillError(65, f"cannot read contract file {bound_path_preview(path)}: {exc}")
    try:
        return tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise FillError(65, f"invalid TOML in {bound_path_preview(path)}: {exc}")
    except ValueError as exc:
        raise FillError(65, f"invalid TOML in {bound_path_preview(path)} (integer literal too "
                             f"large): {exc}")


# ---------------------------------------------------------------------------
# Path and glob grammar (a contract's own `file` entries; an observer's --src-glob/--test-glob/
# --spec-glob/--manifest arguments, which fill_observe.py turns into `:(glob)<glob>` git pathspecs)
# ---------------------------------------------------------------------------
# Size limits, checked at validation time (not only when a glob/path is actually used): a contract's
# own `file` value and an observer's glob arguments are necessarily small (hand-authored), so these
# ceilings exist to bound the cost of checking them, not because a real one comes close.
PATH_MAX_LENGTH = 4096
PATH_MAX_SEGMENTS = 256
GLOB_MAX_LENGTH = 1024
GLOB_MAX_SEGMENTS = 64
GLOB_MAX_STARS_PER_SEGMENT = 16

_CONTROL_CHARS = {chr(c) for c in range(0x20)} | {chr(0x7F)}
_GLOB_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.*-]+$")


def validate_path_grammar(path) -> str | None:
    """Returns None if `path` is a well-formed, repo-relative file path per this format's grammar,
    else a reason string. Rejects: a non-string value; a string that cannot encode as UTF-8 (see
    `encodes_as_utf8`); an empty string; longer than PATH_MAX_LENGTH; a leading '/' (absolute path);
    a leading ':' (git pathspec magic — see below); a backslash; a control character; more than
    PATH_MAX_SEGMENTS '/'-separated segments; an empty segment (a double slash or a trailing slash);
    a '.' or '..' segment (no path may name a location outside the directory it is written relative
    to). The leading-':' rule exists because `fill_observe.py` passes a contract entry's own `file`
    value to git (as a `:(literal)<path>` pathspec, so an ORDINARY leading ':' in the path itself
    would otherwise be read as a SECOND, nested pathspec-magic prefix by git, not a literal
    character) — the same reason `validate_glob_grammar` already forbids it for a `--src-glob`/
    `--test-glob`/`--spec-glob`/`--manifest` argument."""
    if not isinstance(path, str):
        return f"must be a string, got {type(path).__name__}"
    if not encodes_as_utf8(path):
        return "contains a character that cannot encode as UTF-8 (a lone UTF-16 surrogate)"
    if path == "":
        return "must not be empty"
    if len(path) > PATH_MAX_LENGTH:
        return f"exceeds the {PATH_MAX_LENGTH}-character limit ({len(path)} characters)"
    if path.startswith("/"):
        return "absolute path not allowed"
    if path.startswith(":"):
        return "leading ':' not allowed (would be read as git pathspec magic, not a literal path)"
    if "\\" in path:
        return "backslash not allowed"
    if any(ch in _CONTROL_CHARS for ch in path):
        return "control character not allowed"
    segments = path.split("/")
    if len(segments) > PATH_MAX_SEGMENTS:
        return f"exceeds the {PATH_MAX_SEGMENTS}-segment limit ({len(segments)} segments)"
    for seg in segments:
        if seg == "":
            return "empty path segment (double slash or trailing slash)"
        if seg in (".", ".."):
            return f"'{seg}' path segment not allowed"
    return None


def validate_glob_grammar(glob) -> str | None:
    """Returns None if `glob` is a well-formed glob per this format's grammar (used both for a
    `--src-glob`/`--test-glob`/`--spec-glob`/`--manifest` observer argument and for the derived
    `**/<manifest>` form), else a reason string. Same grammar and size ceilings as
    `validate_path_grammar`, plus: '*' is allowed inside a segment (up to
    GLOB_MAX_STARS_PER_SEGMENT), and '**' is allowed only as a whole segment on its own (never mixed
    with other characters in the same segment) — the shape `fill_observe.py` turns into a git
    `:(glob)<glob>` pathspec, so no leading ':' or other pathspec-magic character is allowed either
    (checked here as a plain, if redundant, extra: this grammar already forbids every character a
    pathspec magic word would need except ':' itself, which is covered by the segment charset
    below)."""
    if not isinstance(glob, str):
        return f"must be a string, got {type(glob).__name__}"
    if not encodes_as_utf8(glob):
        return "contains a character that cannot encode as UTF-8 (a lone UTF-16 surrogate)"
    if glob == "":
        return "must not be empty"
    if len(glob) > GLOB_MAX_LENGTH:
        return f"exceeds the {GLOB_MAX_LENGTH}-character limit ({len(glob)} characters)"
    if glob.startswith("/"):
        return "absolute path not allowed"
    if glob.startswith(":"):
        return "leading ':' not allowed (would be read as git pathspec magic, not a literal glob)"
    if "\\" in glob:
        return "backslash not allowed"
    if any(ch in _CONTROL_CHARS for ch in glob):
        return "control character not allowed"
    segments = glob.split("/")
    if len(segments) > GLOB_MAX_SEGMENTS:
        return f"exceeds the {GLOB_MAX_SEGMENTS}-segment limit ({len(segments)} segments)"
    for seg in segments:
        if seg == "":
            return "empty path segment (double slash or trailing slash)"
        if seg == "..":
            return "'..' segment not allowed"
        if seg == "**":
            continue
        if "**" in seg:
            return (f"'**' must be its own path segment, not mixed with other characters: "
                    f"{safe_repr_for_error(seg)}")
        if seg.count("*") > GLOB_MAX_STARS_PER_SEGMENT:
            return (f"segment {safe_repr_for_error(seg)} has more than "
                    f"{GLOB_MAX_STARS_PER_SEGMENT} '*' characters")
        if not _GLOB_SEGMENT_RE.fullmatch(seg):
            return (f"segment {safe_repr_for_error(seg)} has a disallowed character (allowed: letters, digits, "
                     f"'_', '.', '-', '*')")
    return None


# ---------------------------------------------------------------------------
# The strict JSON-input contract: every foreign JSON document this format reads (fill_evaluate.py's
# --observations file) is checked once, at load time, the same way.
# ---------------------------------------------------------------------------
MAX_JSON_DEPTH = 64


def unique_pairs(pairs: list) -> dict:
    """`object_pairs_hook` for `json.loads`: rejects a duplicate key instead of silently keeping
    only the last value for it (`json.loads`'s own default `dict(pairs)` behavior)."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {safe_repr_for_error(key)}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    """`parse_constant` for `json.loads`: JSON's grammar has no NaN/Infinity/-Infinity, but Python's
    decoder accepts them anyway (`json.loads("NaN")` succeeds) unless this hook is set."""
    raise ValueError(f"non-JSON numeric constant: {value}")


def find_json_input_defect(obj, max_depth: int = MAX_JSON_DEPTH):
    """ITERATIVE walk (an explicit stack, not a function calling itself once per level of the
    input's own nesting) over one parsed JSON value. Returns a reason string for the first defect
    found, or None if the whole structure is clean: a string (a dict KEY or a VALUE, at any nesting
    depth) that cannot encode as UTF-8 (see `encodes_as_utf8`), or nesting deeper than `max_depth`.
    The top-level value is depth 1; each dict/list nested one level inside another adds 1."""
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            if not encodes_as_utf8(value):
                return (f"a string cannot encode as UTF-8 (contains a lone UTF-16 surrogate): "
                        f"{safe_repr_for_error(value)}")
        elif isinstance(value, (float, Decimal)) and not math.isfinite(value):
            return "non-finite number is not a JSON value"
        elif isinstance(value, dict):
            if depth > max_depth:
                return f"nesting is deeper than {max_depth} levels"
            for k, v in value.items():
                stack.append((k, depth))
                stack.append((v, depth + 1))
        elif isinstance(value, list):
            if depth > max_depth:
                return f"nesting is deeper than {max_depth} levels"
            for item in value:
                stack.append((item, depth + 1))
    return None


def _escape_report_value(value):
    """Recursively applies `_escape_unicode_safe` to every string in `value` (a dict key or value,
    a list/tuple element, at any nesting depth) — see `emit_report`."""
    if isinstance(value, str):
        return _escape_unicode_safe(value)
    if isinstance(value, dict):
        return {(_escape_unicode_safe(k) if isinstance(k, str) else k): _escape_report_value(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_escape_report_value(v) for v in value]
    return value


_STDOUT_FAILURES = (OSError, ValueError, TypeError, OverflowError, AttributeError,
                    io.UnsupportedOperation)
try:
    _MAX_DESCRIPTOR = min(max(int(os.sysconf("SC_OPEN_MAX")) - 1, 0), 2 ** 31 - 1)
except (OSError, ValueError, TypeError, OverflowError, AttributeError):
    _MAX_DESCRIPTOR = 2 ** 31 - 1
_SELECT_FD_SETSIZE = getattr(select, "FD_SETSIZE", 1024)


class _DescriptorFreeStdout:
    """Shutdown-safe stdout replacement that never allocates or touches a descriptor."""

    closed = False
    encoding = "utf-8"

    def write(self, text):
        return len(text)

    def flush(self):
        return None


_DESCRIPTOR_FREE_STDOUT = _DescriptorFreeStdout()


def _stdout_attr(stream, name, default=None):
    try:
        return getattr(stream, name, default)
    except _STDOUT_FAILURES as exc:
        raise StdoutUnavailable from exc


def _stdout_flush(stream) -> None:
    flush = _stdout_attr(stream, "flush")
    if not callable(flush):
        raise StdoutUnavailable
    try:
        flush()
    except _STDOUT_FAILURES as exc:
        raise StdoutUnavailable from exc


def _valid_descriptor(candidate) -> bool:
    return (isinstance(candidate, int) and not isinstance(candidate, bool)
            and 0 <= candidate <= _MAX_DESCRIPTOR)


def _wait_stdout_writable(fd: int) -> None:
    try:
        poll_factory = getattr(select, "poll", None)
        if poll_factory is not None:
            poller = poll_factory()
            poller.register(fd, select.POLLOUT)
            events = poller.poll()
            if not events:
                raise StdoutUnavailable
            return
        if fd < _SELECT_FD_SETSIZE:
            select.select([], [fd], [])
            return
    except _STDOUT_FAILURES as exc:
        raise StdoutUnavailable from exc
    raise StdoutUnavailable


def force_utf8_stdout() -> None:
    """Pins `sys.stdout`'s encoding to plain, BOM-free UTF-8, strict error mode, regardless of what
    `PYTHONIOENCODING` (or the platform locale) would otherwise select. `emit_report()` already
    guarantees the TEXT it returns is safe for this format's own strict JSON reader, but writing
    it re-encodes that text using `sys.stdout`'s encoding — `PYTHONIOENCODING=utf-8-sig`
    prepends a byte-order mark and `PYTHONIOENCODING=utf-16` changes the byte encoding entirely,
    either of which makes the printed report fail this tool's own strict reader even though
    `emit_report()` did its job. Every CLI's `run_cli()` boundary calls this FIRST, before any report is
    printed. `errors="strict"` matches every report's existing guarantee: every string in a report
    has already passed through `_escape_unicode_safe`, so it is always encodable as plain UTF-8 —
    nothing here should ever need a replace/ignore fallback. A no-op when `sys.stdout` has no
    `reconfigure` method — a non-file stream (e.g. an `io.StringIO` a selftest redirects stdout to
    while calling `main()` in-process) has no encoding of its own to pin."""
    stdout = sys.stdout
    if stdout is None:
        raise StdoutUnavailable
    reconfigure = _stdout_attr(stdout, "reconfigure", None)
    if reconfigure is not None:
        if not callable(reconfigure):
            raise StdoutUnavailable
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except _STDOUT_FAILURES as exc:
            raise StdoutUnavailable from exc


class StdoutUnavailable(Exception):
    """The report could not be written completely to stdout."""


_REPORT_SCHEMA_CACHE = {}
_REPORT_BOUNDARY_FAILED = False


def register_report_schema(module_file: str, declaration: tuple[str, str]) -> None:
    from schema_conformance import audit
    schema_rel, def_name = declaration
    format_root = Path(module_file).resolve().parent.parent
    key = (str(format_root), schema_rel, def_name)
    if key not in _REPORT_SCHEMA_CACHE:
        document = json.loads((format_root / schema_rel).read_text(encoding="utf-8"))
        audit(document)
        if def_name not in document.get("$defs", {}):
            raise ValueError(f"missing $defs.{def_name}")
        _REPORT_SCHEMA_CACHE[key] = document


def _declared_report_contract():
    frame = sys._getframe(1)
    while frame is not None:
        namespace = frame.f_globals
        declaration = namespace.get("REPORT_SCHEMA")
        module_file = namespace.get("__file__")
        if isinstance(declaration, tuple) and len(declaration) == 2 and module_file:
            return declaration, Path(module_file).resolve().parent.parent
        frame = frame.f_back
    return None


def _internal_error_report(def_name: str) -> dict:
    error = {"code": 65, "reason": "report failed its schema"}
    return {"status": "unknown", "exit_code": 65, "errors": [error]}


def _schema_checked_report_text(text: str) -> str:
    global _REPORT_BOUNDARY_FAILED
    contract = _declared_report_contract()
    if contract is None:
        return text
    (schema_rel, def_name), format_root = contract
    key = (str(format_root), schema_rel, def_name)
    try:
        document = _REPORT_SCHEMA_CACHE.get(key)
        if document is None:
            from schema_conformance import audit, conformance_errors
            document = json.loads((format_root / schema_rel).read_text(encoding="utf-8"))
            audit(document)
            if def_name not in document.get("$defs", {}):
                raise ValueError(f"missing $defs.{def_name}")
            _REPORT_SCHEMA_CACHE[key] = document
        else:
            from schema_conformance import conformance_errors
        report = json.loads(text)
        errors = conformance_errors(report, document, f"#/$defs/{def_name}", "report", audited=True)
    except Exception:
        errors = ["report contract could not be checked"]
    if not errors:
        return text
    _REPORT_BOUNDARY_FAILED = True
    fallback = _internal_error_report(def_name)
    if conformance_errors(fallback, document, f"#/$defs/{def_name}", "internal_error_report",
                          audited=True):
        raise RuntimeError("internal-error report failed its schema")
    return json.dumps(fallback, indent=2, sort_keys=True, allow_nan=False)


def write_report(text: str) -> None:
    """Write one complete UTF-8 report, translating only stdout failures to a boundary exception.

    A real stdout descriptor uses ``os.write`` so Python's text wrappers cannot conceal a short
    write. A descriptor-less in-process stream keeps the text protocol used by callers that
    redirect ``main()`` to ``io.StringIO``.
    """
    text = _schema_checked_report_text(text)
    payload_text = text + "\n"
    payload = payload_text.encode("utf-8", "strict")
    stdout = sys.stdout
    if stdout is None or _stdout_attr(stdout, "closed", False):
        raise StdoutUnavailable

    _stdout_flush(stdout)
    buffer = _stdout_attr(stdout, "buffer", None)
    if buffer is not None and buffer is not stdout:
        _stdout_flush(buffer)

    fileno = _stdout_attr(stdout, "fileno", None)
    fd = None
    if fileno is not None:
        if not callable(fileno):
            raise StdoutUnavailable
        try:
            candidate = fileno()
        except io.UnsupportedOperation as exc:
            if isinstance(stdout, io.TextIOBase):
                candidate = None
            else:
                raise StdoutUnavailable from exc
        except _STDOUT_FAILURES as exc:
            raise StdoutUnavailable from exc
        if candidate is not None:
            if not _valid_descriptor(candidate):
                raise StdoutUnavailable
            fd = candidate

    if fd is None:
        write = _stdout_attr(stdout, "write")
        if not callable(write):
            raise StdoutUnavailable
        try:
            written = write(payload_text)
        except _STDOUT_FAILURES as exc:
            raise StdoutUnavailable from exc
        if written != len(payload_text):
            raise StdoutUnavailable
        _stdout_flush(stdout)
        return

    offset = 0
    try:
        while offset < len(payload):
            try:
                written = os.write(fd, payload[offset:])
            except BlockingIOError:
                _wait_stdout_writable(fd)
                continue
            if written == 0:
                raise StdoutUnavailable
            offset += written
    except _STDOUT_FAILURES as exc:
        raise StdoutUnavailable from exc


def _quarantine_stdout_fd(failed_stdout=None) -> None:
    """Best-effort neutralization of the failed stream and fd 1, including shutdown flushes."""
    sys.stdout = _DESCRIPTOR_FREE_STDOUT
    failed_fd = None
    try:
        candidate = failed_stdout.fileno() if failed_stdout is not None else None
        if _valid_descriptor(candidate):
            failed_fd = candidate
    except Exception:
        pass

    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        for target in dict.fromkeys(fd for fd in (failed_fd, 1) if fd is not None):
            if target == devnull_fd:
                continue
            try:
                os.dup2(devnull_fd, target)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if devnull_fd is not None and devnull_fd not in (failed_fd, 1):
            try:
                os.close(devnull_fd)
            except Exception:
                pass


def run_cli(main, argv=None) -> int:
    """Run one Fill-contract CLI at the stdout byte boundary.

    A CLI either writes its one strict-valid report and flushes it explicitly, or, when stdout
    cannot take that report, writes nothing further to stdout and returns ``E_OUTPUT``. No fallback
    report is attempted on the failed stream.
    """
    global _REPORT_BOUNDARY_FAILED
    _REPORT_BOUNDARY_FAILED = False
    stdout = sys.stdout
    try:
        unavailable = stdout is None or _stdout_attr(stdout, "closed", False)
    except StdoutUnavailable:
        unavailable = True
    if unavailable:
        _quarantine_stdout_fd(stdout)
        return E_OUTPUT
    try:
        force_utf8_stdout()
    except StdoutUnavailable:
        _quarantine_stdout_fd(stdout)
        return E_OUTPUT
    try:
        code = main(argv)
    except StdoutUnavailable:
        _quarantine_stdout_fd(sys.stdout)
        return E_OUTPUT
    try:
        stdout = sys.stdout
        if stdout is None:
            raise StdoutUnavailable
        _stdout_flush(stdout)
    except StdoutUnavailable:
        _quarantine_stdout_fd(stdout)
        return E_OUTPUT
    return 65 if _REPORT_BOUNDARY_FAILED else code


def output_boundary_selftest(script_path: Path, valid_argv: list[str],
                             usage_argv: list[str]) -> list[tuple[str, bool, str]]:
    """Subprocess matrix for complete-report-or-74 behavior, including interpreter shutdown."""
    import subprocess
    import threading

    checks = []
    env = os.environ.copy()
    env.pop("PYTHONUNBUFFERED", None)

    def command(argv, unbuffered=False):
        result = [sys.executable, "-B"]
        if unbuffered:
            result.append("-u")
        return [*result, str(script_path), *argv]

    def clean_stderr(stderr: bytes) -> bool:
        decoded = stderr.decode("utf-8", "replace")
        return not any(marker in decoded for marker in
                       ("Traceback", "Exception ignored", "BrokenPipeError"))

    normal = {}
    for kind, argv in (("valid", valid_argv), ("usage", usage_argv)):
        proc = subprocess.run(command(argv), capture_output=True, env=env)
        wanted_code = 0 if kind == "valid" else E_USAGE
        baseline_ok = (proc.returncode == wanted_code and bool(proc.stdout)
                       and clean_stderr(proc.stderr))
        normal[kind] = (proc.returncode, proc.stdout, baseline_ok)

    for unbuffered in (False, True):
        for kind, argv in (("valid", valid_argv), ("usage", usage_argv)):
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            try:
                proc = subprocess.run(command(argv, unbuffered), stdout=write_fd,
                                      stderr=subprocess.PIPE, env=env)
            finally:
                os.close(write_fd)
            ok = proc.returncode == E_OUTPUT and clean_stderr(proc.stderr)
            mode = "unbuffered" if unbuffered else "buffered"
            checks.append((f"closed-pipe-{mode}-{kind}", ok,
                           f"exit={proc.returncode} clean_stderr={clean_stderr(proc.stderr)}"))

    def fill_pipe(write_fd: int) -> int:
        os.set_blocking(write_fd, False)
        total = 0
        block = b"x" * 65536
        while True:
            try:
                total += os.write(write_fd, block)
            except BlockingIOError:
                return total

    for kind, argv in (("valid", valid_argv), ("usage", usage_argv)):
        read_fd, write_fd = os.pipe()
        filler_len = fill_pipe(write_fd)
        received = []
        reader_error = []

        def drain():
            try:
                while True:
                    chunk = os.read(read_fd, 65536)
                    if not chunk:
                        break
                    received.append(chunk)
            except OSError as exc:
                reader_error.append(type(exc).__name__)
            finally:
                os.close(read_fd)

        proc = subprocess.Popen(command(argv, True), stdout=write_fd,
                                stderr=subprocess.PIPE, env=env)
        os.close(write_fd)
        reader = threading.Thread(target=drain)
        reader.start()
        stderr = proc.communicate()[1]
        reader.join()
        stream = b"".join(received)
        expected_code, expected_report, baseline_ok = normal[kind]
        report = stream[filler_len:]
        ok = (baseline_ok and not reader_error and proc.returncode == expected_code
              and report == expected_report and clean_stderr(stderr))
        checks.append((f"full-nonblocking-drained-{kind}", ok,
                       f"exit={proc.returncode}/{expected_code} bytes={len(report)}/"
                       f"{len(expected_report)} baseline_ok={baseline_ok} "
                       f"reader_error={reader_error or 'none'}"))

    for kind, argv in (("valid", valid_argv), ("usage", usage_argv)):
        read_fd, write_fd = os.pipe()
        fill_pipe(write_fd)
        os.close(read_fd)
        try:
            proc = subprocess.run(command(argv, True), stdout=write_fd,
                                  stderr=subprocess.PIPE, env=env)
        finally:
            os.close(write_fd)
        ok = proc.returncode == E_OUTPUT and clean_stderr(proc.stderr)
        checks.append((f"full-nonblocking-closed-{kind}", ok,
                       f"exit={proc.returncode} clean_stderr={clean_stderr(proc.stderr)}"))

    probe = r'''
import fcntl, importlib.util, io, json, os, resource, sys
script, argv_json, mode = sys.argv[1:4]
sys.path.insert(0, os.path.dirname(script))
spec = importlib.util.spec_from_file_location("output_boundary_target", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if mode == "text-wrapper":
    sys.stdout = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"))
elif mode == "high-fd":
    high_fd = fcntl.fcntl(1, fcntl.F_DUPFD, 2048)
    sys.stdout = io.TextIOWrapper(io.FileIO(high_fd, "wb", closefd=True), encoding="utf-8")
elif mode == "raising":
    class Stream:
        closed = False
        def write(self, _text): raise OSError("write unavailable")
        def flush(self): raise OSError("flush unavailable")
    sys.stdout = Stream()
elif mode == "exhausted-raising":
    class Stream:
        closed = False
        def write(self, _text): raise OSError("write unavailable")
        def flush(self): raise OSError("flush unavailable")
    sys.stdout = Stream()
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(128, soft), hard))
    held = []
    while True:
        try: held.append(os.open(os.devnull, os.O_RDONLY))
        except OSError: break
elif mode == "bytes":
    sys.stdout = io.BytesIO()
elif mode == "huge-fileno":
    class Stream:
        closed = False
        def fileno(self): return 2 ** 80
        def write(self, _text): raise AssertionError("invalid descriptor reached write")
        def flush(self): pass
    sys.stdout = Stream()
elif mode == "short":
    class Stream:
        closed = False
        def write(self, text): return min(2, len(text))
        def flush(self): pass
    sys.stdout = Stream()
elif mode == "closed":
    sys.stdout.close()
raise SystemExit(module.main(json.loads(argv_json)))
'''

    def probe_command(mode: str, argv):
        return [sys.executable, "-B", "-c", probe, str(script_path),
                json.dumps(argv, allow_nan=False), mode]

    def run_probe(mode: str, argv, closed_pipe=False):
        command_line = probe_command(mode, argv)
        if not closed_pipe:
            return subprocess.run(command_line, capture_output=True, env=env)
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            return subprocess.run(command_line, stdout=write_fd,
                                  stderr=subprocess.PIPE, env=env)
        finally:
            os.close(write_fd)

    read_fd, write_fd = os.pipe()
    filler_len = fill_pipe(write_fd)
    received = []
    reader_error = []

    def drain_high_fd():
        try:
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                received.append(chunk)
        except OSError as exc:
            reader_error.append(type(exc).__name__)
        finally:
            os.close(read_fd)

    proc = subprocess.Popen(probe_command("high-fd", valid_argv), stdout=write_fd,
                            stderr=subprocess.PIPE, env=env)
    os.close(write_fd)
    reader = threading.Thread(target=drain_high_fd)
    reader.start()
    stderr = proc.communicate()[1]
    reader.join()
    expected_code, expected_report, baseline_ok = normal["valid"]
    report = b"".join(received)[filler_len:]
    ok = (baseline_ok and not reader_error and proc.returncode == expected_code
          and report == expected_report and clean_stderr(stderr))
    checks.append(("high-fd-full-nonblocking-drained", ok,
                   f"exit={proc.returncode}/{expected_code} bytes={len(report)}/"
                   f"{len(expected_report)} baseline_ok={baseline_ok} "
                   f"reader_error={reader_error or 'none'}"))

    read_fd, write_fd = os.pipe()
    fill_pipe(write_fd)
    os.close(read_fd)
    try:
        proc = subprocess.run(probe_command("high-fd", valid_argv), stdout=write_fd,
                              stderr=subprocess.PIPE, env=env)
    finally:
        os.close(write_fd)
    ok = proc.returncode == E_OUTPUT and clean_stderr(proc.stderr)
    checks.append(("high-fd-full-nonblocking-closed", ok,
                   f"exit={proc.returncode} clean_stderr={clean_stderr(proc.stderr)}"))

    for mode in ("exhausted-raising", "bytes", "huge-fileno"):
        proc = run_probe(mode, valid_argv)
        ok = proc.returncode == E_OUTPUT and not proc.stdout and clean_stderr(proc.stderr)
        checks.append((f"replacement-{mode}-valid", ok,
                       f"exit={proc.returncode} stdout_bytes={len(proc.stdout)} "
                       f"clean_stderr={clean_stderr(proc.stderr)}"))

    for mode, closed_pipe in (("text-wrapper", True), ("raising", False), ("short", False)):
        for kind, argv in (("valid", valid_argv), ("usage", usage_argv)):
            proc = run_probe(mode, argv, closed_pipe)
            stdout = getattr(proc, "stdout", b"") or b""
            ok = proc.returncode == E_OUTPUT and not stdout and clean_stderr(proc.stderr)
            checks.append((f"replacement-{mode}-{kind}", ok,
                           f"exit={proc.returncode} stdout_bytes={len(stdout)} "
                           f"clean_stderr={clean_stderr(proc.stderr)}"))

    proc = run_probe("closed", [])
    ok = proc.returncode == E_OUTPUT and not proc.stdout and clean_stderr(proc.stderr)
    checks.append(("public-main-closed-stdout", ok,
                   f"exit={proc.returncode} stdout_bytes={len(proc.stdout)} "
                   f"clean_stderr={clean_stderr(proc.stderr)}"))

    def raise_non_stdout_oserror(_argv=None):
        raise OSError("not stdout")

    try:
        run_cli(raise_non_stdout_oserror)
        propagated = False
        detail = "returned instead of raising"
    except OSError as exc:
        propagated = str(exc) == "not stdout"
        detail = f"raised={type(exc).__name__}: {exc}"
    checks.append(("non-stdout-oserror-propagates", propagated, detail))
    return checks


def emit_report(report, **dumps_kwargs) -> str:
    """The ONE funnel every report this format's CLIs print to stdout is routed through before it
    reaches `write_report()`: recursively escapes every string in `report` the same way `bound_path_preview`
    already escapes one path, then serializes with `json.dumps(..., allow_nan=False)` — `allow_nan`
    is forced here regardless of what a caller passes. This is the BACKSTOP, not a substitute for
    bounding/escaping each field at the point it is built: every report constructor in this format's
    tools still runs every `reason` through `bound_reason` and every path through
    `bound_path_preview` at the source — this funnel exists so a report field that reaches `write_report()`
    without having gone through either (an argparse usage message built straight from the raw,
    `surrogateescape`-decoded argv text, or a future call site not yet written) still cannot make
    this format emit a report its own strict reader would reject."""
    dumps_kwargs["allow_nan"] = False
    return json.dumps(_escape_report_value(report), **dumps_kwargs)


def load_json_strict(text: str) -> tuple:
    """Returns (obj, None) on success or (None, reason) on any JSON-input-contract violation: bad
    JSON syntax, a duplicate object key, a NaN/Infinity/-Infinity numeric constant, a string (key or
    value, at any depth) that cannot encode as UTF-8, nesting deeper than MAX_JSON_DEPTH, or a
    parser RecursionError on sufficiently deep (if unusual) input — all reported the same way, never
    left to surface as a crash. Callers decide the exit code; this function never raises for a
    malformed INPUT (a caller bug — e.g. passing something other than a str — still raises
    normally)."""
    try:
        obj = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant,
                         parse_float=Decimal)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    except (ValueError, RecursionError) as exc:
        return None, f"invalid JSON input ({exc})"
    defect = find_json_input_defect(obj)
    if defect is not None:
        return None, defect
    return obj, None


# ---------------------------------------------------------------------------
# Predicate-expression tree validation
# ---------------------------------------------------------------------------
def _validate_predicate_leaf(node: dict, mode: str, path: str) -> list:
    """The leaf-shape checks for one already-confirmed `{"predicate": ...}` node (no combinator
    key present — the caller already enforced 'exactly one of predicate/any/all/not')."""
    errors = []
    name = node["predicate"]
    registry = FILLED_PREDICATES if mode == "filled" else DUE_PREDICATES
    if not isinstance(name, str) or name not in registry:
        return [(13, f"{path}: unknown {mode} predicate {safe_repr_for_error(name)} (closed "
                     f"vocabulary: {sorted(registry)})")]
    spec = registry[name]
    extra = set(node.keys()) - {"predicate"} - set(spec.keys())
    if extra:
        errors.append((13, f"{path}: predicate {name!r} has unknown parameter(s) "
                            f"{safe_repr_for_error(sorted(extra))}"))
    for pname, (required, kind_) in spec.items():
        if pname not in node:
            if required:
                errors.append((13, f"{path}: predicate {name!r} missing required parameter {pname!r}"))
            continue
        value = node[pname]
        if kind_ == "str" and not isinstance(value, str):
            errors.append((13, f"{path}: predicate {name!r} parameter {pname!r} must be a string"))
        elif kind_ == "int" and not is_schema_integer(value):
            errors.append((13, f"{path}: predicate {name!r} parameter {pname!r} must be an integer"))
        elif kind_ == "int" and not (PREDICATE_INT_MIN <= value <= PREDICATE_INT_MAX):
            errors.append((13, f"{path}: predicate {name!r} parameter {pname!r} must be between "
                                f"{PREDICATE_INT_MIN} and {PREDICATE_INT_MAX}, got "
                                f"{safe_repr_for_error(value)}"))
        elif kind_ == "int":
            node[pname] = int(value)
        elif pname == "max_age" and isinstance(value, str) and parse_duration(value) is None:
            errors.append((13, f"{path}: predicate {name!r} max_age {safe_repr_for_error(value)} "
                                f"is not a valid, in-bounds duration (<int>d, "
                                f"{DURATION_MIN_DAYS}..{DURATION_MAX_DAYS})"))
    return errors


def validate_expr(expr, mode: str, path: str) -> list:
    """mode is 'filled' or 'due'. Returns a list of (code, reason) tuples; [] means expr is a
    well-formed predicate-expression tree for that mode. Fail-closed: a node that is not a dict, or
    a dict with zero or more-than-one of {predicate, any, all, not}, is always an error — never
    guessed into one shape or the other. ITERATIVE (an explicit stack): see the module docstring and
    MAX_EXPR_DEPTH for why."""
    errors = []
    stack = [(expr, mode, path, 1)]
    while stack:
        node, node_mode, node_path, depth = stack.pop()
        if depth > MAX_EXPR_DEPTH:
            errors.append((13, f"{node_path}: predicate-expression tree nests deeper than "
                                f"{MAX_EXPR_DEPTH} levels"))
            continue
        if not isinstance(node, dict):
            errors.append((13, f"{node_path}: predicate-expression node must be a table, got "
                                f"{type(node).__name__}"))
            continue
        keys = {"predicate", "any", "all", "not"} & set(node.keys())
        if len(keys) != 1:
            errors.append((13, f"{node_path}: node must have exactly one of predicate/any/all/not, "
                                f"found {sorted(keys) or 'none'}"))
            continue
        kind = next(iter(keys))

        if kind == "predicate":
            errors.extend(_validate_predicate_leaf(node, node_mode, node_path))
            continue

        # A combinator node's key set is EXACTLY {kind} — unlike a leaf, which legitimately carries
        # predicate-specific params alongside "predicate", a combinator carries nothing else at all
        # (the schema's own `additionalProperties: false` on every combinator branch already says
        # this; this is the Python validator agreeing with it exactly, closing a gap where a node
        # such as `{"any": [...], "extra": true}` used to pass because `len(keys) == 1` alone did
        # not notice the sibling key).
        combinator_extra = set(node.keys()) - {kind}
        if combinator_extra:
            errors.append((13, f"{node_path}: {kind!r} node has unknown extra key(s) "
                                f"{safe_repr_for_error(sorted(combinator_extra))}"))

        if kind == "not":
            if node_mode == "due":
                errors.append((14, f"{node_path}: 'not' is not allowed inside due_when predicates "
                                    f"(negating a maturity event has no well-defined due date)"))
                continue
            stack.append((node["not"], node_mode, node_path + ".not", depth + 1))
            continue

        # any / all
        items = node[kind]
        if not isinstance(items, list) or not items:
            errors.append((13, f"{node_path}.{kind}: must be a non-empty array"))
            continue
        # Push children in REVERSE so the leftmost item pops (and so is fully processed, including
        # its own descendants) first — reproducing left-to-right depth-first order without
        # recursion.
        for i in range(len(items) - 1, -1, -1):
            stack.append((items[i], node_mode, f"{node_path}.{kind}[{i}]", depth + 1))
    return errors


def expr_has_predicate(expr, name: str) -> bool:
    """True iff a leaf predicate named `name` occurs anywhere in the tree (any mode), within
    MAX_EXPR_DEPTH. Used to decide whether template_rows is required, and by fill_observe.py to know
    a file needs a command_result/declared-rules fact gathered. ITERATIVE; a tree deeper than
    MAX_EXPR_DEPTH is unreachable here in normal operation (validate_expr already rejects it before
    any tool trusts the contract) — the depth cap is defense in depth, not a documented behavior
    change for a validated tree."""
    stack = [(expr, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_EXPR_DEPTH or not isinstance(node, dict):
            continue
        if "predicate" in node:
            if node.get("predicate") == name:
                return True
            continue
        for kind in ("any", "all"):
            if kind in node and isinstance(node[kind], list):
                for item in node[kind]:
                    stack.append((item, depth + 1))
        if "not" in node:
            stack.append((node["not"], depth + 1))
    return False


def walk_leaves(expr, out: list) -> None:
    """Appends every leaf predicate table (the {'predicate': ..., ...params} dicts) found anywhere
    in the tree, in a deterministic left-to-right order, within MAX_EXPR_DEPTH. Malformed nodes are
    skipped (a validate_expr call is expected to have already rejected them; this walker never
    raises). ITERATIVE; see expr_has_predicate's docstring for why the depth cap is unreachable in
    normal (already-validated) operation."""
    stack = [(expr, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_EXPR_DEPTH or not isinstance(node, dict):
            continue
        if "predicate" in node:
            out.append(node)
            continue
        to_push = []
        for kind in ("any", "all"):
            if kind in node and isinstance(node[kind], list):
                to_push.extend(node[kind])
        if "not" in node:
            to_push.append(node["not"])
        # Push in reverse so `to_push[0]` (the first `any`/`all` item, or `not`'s own child if
        # there were no any/all) is on TOP of the stack and so pops — and is fully walked,
        # including its own descendants — before `to_push[1]`, matching the original recursive
        # left-to-right, any/all-then-not order.
        for item in reversed(to_push):
            stack.append((item, depth + 1))


def validate_retrigger(retrigger, path: str) -> list:
    if not isinstance(retrigger, dict):
        return [(16, f"{path}: retrigger must be a table")]
    has_on = "on" in retrigger
    has_every = "every" in retrigger
    if not has_on and not has_every:
        return [(16, f"{path}: retrigger must declare 'on' (a list of events) or 'every' (a duration)")]
    errors = []
    if has_on:
        events = retrigger["on"]
        if not isinstance(events, list) or not events:
            errors.append((16, f"{path}.on: must be a non-empty array of event names"))
        else:
            for i, ev in enumerate(events):
                if not isinstance(ev, str) or ev not in DUE_PREDICATES:
                    errors.append((16, f"{path}.on[{i}]: {safe_repr_for_error(ev)} is not a closed "
                                        f"maturity-event name"))
    if has_every:
        if parse_duration(retrigger["every"]) is None:
            errors.append((16, f"{path}.every: {safe_repr_for_error(retrigger['every'])} is not a "
                                f"valid duration"))
    extra = set(retrigger.keys()) - {"on", "every"}
    if extra:
        errors.append((16, f"{path}: unknown key(s) {safe_repr_for_error(sorted(extra))}"))
    return errors


def validate_adequacy(adequacy, path: str) -> list:
    if not isinstance(adequacy, dict):
        return [(16, f"{path}: adequacy must be a table")]
    errors = []
    if not is_nonblank_str(adequacy.get("decided_by")):
        errors.append((16, f"{path}.decided_by: required, must be a non-blank string"))
    when = adequacy.get("when")
    if when not in ADEQUACY_WHEN_ENUM:
        errors.append((16, f"{path}.when: must be one of {ADEQUACY_WHEN_ENUM}, got "
                            f"{safe_repr_for_error(when)}"))
    extra = set(adequacy.keys()) - {"decided_by", "when"}
    if extra:
        errors.append((16, f"{path}: unknown key(s) {safe_repr_for_error(sorted(extra))}"))
    return errors


TOP_LEVEL_ALLOWED = {"fill_version", "template", "contracts"}
CONTRACT_ENTRY_ALLOWED = {
    "file", "importance", "urgency_after_due", "template_rows", "filled_when", "due_when",
    "retrigger", "adequacy",
}


def validate_contract(contract) -> list:
    """Full contract-shape + closed-vocabulary validation. Returns a list of (code, reason) tuples;
    [] means valid. Never raises on malformed data — a malformed field is always reported as an
    error entry, never a Python exception. Every object this function walks is CLOSED: an unknown
    key is always an error, matching the schema's own `additionalProperties: false` at every one of
    these levels exactly (predicate leaves are the one deliberate exception — the schema and
    `_validate_predicate_leaf` both say so — because their own parameter set is itself semantic)."""
    errors = []
    if not isinstance(contract, dict):
        return [(10, "top level must be a table")]

    top_extra = set(contract.keys()) - TOP_LEVEL_ALLOWED
    if top_extra:
        errors.append((10, f"top level: unknown key(s) {safe_repr_for_error(sorted(top_extra))}"))

    if not is_nonblank_str(contract.get("fill_version")):
        errors.append((10, "fill_version: required, must be a non-blank string"))
    if not is_nonblank_str(contract.get("template")):
        errors.append((10, "template: required, must be a non-blank string"))

    entries = contract.get("contracts")
    if not isinstance(entries, list) or not entries:
        errors.append((11, "contracts: required, must be a non-empty array of tables"))
        return errors

    seen_files = set()
    for i, entry in enumerate(entries):
        prefix = f"contracts[{i}]"
        if not isinstance(entry, dict):
            errors.append((11, f"{prefix}: must be a table"))
            continue

        entry_extra = set(entry.keys()) - CONTRACT_ENTRY_ALLOWED
        if entry_extra:
            errors.append((12, f"{prefix}: unknown key(s) {safe_repr_for_error(sorted(entry_extra))}"))

        file_ = entry.get("file")
        path_reason = validate_path_grammar(file_)
        if path_reason is not None:
            errors.append((12, f"{prefix}.file: {path_reason}"))
        elif file_ in seen_files:
            errors.append((12, f"{prefix}.file: duplicate file entry {safe_repr_for_error(file_)}"))
        else:
            seen_files.add(file_)

        importance = entry.get("importance")
        if importance not in IMPORTANCE_ENUM:
            errors.append((12, f"{prefix}.importance: must be one of {IMPORTANCE_ENUM}, got "
                                f"{safe_repr_for_error(importance)}"))

        urgency = entry.get("urgency_after_due")
        if parse_duration(urgency) is None:
            errors.append((12, f"{prefix}.urgency_after_due: {safe_repr_for_error(urgency)} is not "
                                f"a valid duration"))

        filled_when = entry.get("filled_when")
        if filled_when is None:
            errors.append((12, f"{prefix}.filled_when: required"))
        else:
            errors.extend(validate_expr(filled_when, "filled", f"{prefix}.filled_when"))

        due_when = entry.get("due_when")
        if due_when is None:
            errors.append((12, f"{prefix}.due_when: required"))
        else:
            errors.extend(validate_expr(due_when, "due", f"{prefix}.due_when"))

        # Type/bounds are checked whenever template_rows is PRESENT, regardless of whether
        # filled_when actually needs it (a one-way rule, not an iff: an entry MAY carry
        # template_rows without using rows-beyond-template — it is simply unused then — but if
        # present, it is still a real field with a real type, not a free-form value the tool
        # ignores). "Required because rows-beyond-template is used" is a SEPARATE check below.
        if "template_rows" in entry:
            tr = entry["template_rows"]
            if (not is_schema_integer(tr)
                    or not (TEMPLATE_ROWS_MIN <= tr <= TEMPLATE_ROWS_MAX)):
                errors.append((12, f"{prefix}.template_rows: must be an integer between "
                                    f"{TEMPLATE_ROWS_MIN} and {TEMPLATE_ROWS_MAX}, got "
                                    f"{safe_repr_for_error(tr)}"))
            else:
                entry["template_rows"] = int(tr)

        needs_template_rows = isinstance(filled_when, dict) and expr_has_predicate(
            filled_when, "rows-beyond-template")
        if needs_template_rows and "template_rows" not in entry:
            errors.append((15, f"{prefix}.template_rows: required because filled_when uses "
                                f"rows-beyond-template; NOT otherwise restricted — a one-way rule, "
                                f"not an iff (an entry MAY carry template_rows without using "
                                f"rows-beyond-template; it is simply unused then)"))

        if "retrigger" in entry:
            errors.extend(validate_retrigger(entry["retrigger"], f"{prefix}.retrigger"))
        if "adequacy" in entry:
            errors.extend(validate_adequacy(entry["adequacy"], f"{prefix}.adequacy"))

    return errors


# ---------------------------------------------------------------------------
# OBSERVATIONS document shape validation (used by fill_evaluate.py before trusting a foreign
# --observations file; guarantees every field eval_filled_leaf/eval_due_leaf reads below is the
# type they expect, so a malformed value is always a reported error, never a crash)
# ---------------------------------------------------------------------------
# One exact calendar-date grammar for every input date.
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The TIME portion's own hour/minute/second are each bounded to a REAL clock value
# ([01][0-9]|2[0-3] for the hour, [0-5][0-9] for minute/second — exactly the same bound already
# applied to the offset below): `datetime.fromisoformat` otherwise accepts `T24:00:00` as a second,
# non-canonical spelling of the next day's midnight and silently rolls the date over, which breaks
# this format's "one canonical grammar, one spelling per instant" stance (a plain `[0-9]{2}` hour
# field would let it through the regex only for `fromisoformat` to then do the rollover).
_NOW_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?)?"
)


def parse_now(value: str) -> datetime:
    """Canonical date or timestamp, interpreted in UTC if no offset is supplied.
    Offset conversion must also fit datetime's year 1..9999 range. No trimming.
    """
    if (not isinstance(value, str) or not _NOW_RE.fullmatch(value)
            or parse_iso_date(value[:10]) is None):
        raise FillError(64, f"--now must be a canonical date or timestamp: "
                             f"{safe_repr_for_error(value)}")
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = datetime.combine(dt.date(), dt.time(), timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise FillError(64, f"--now is not a representable date or timestamp: "
                             f"{safe_repr_for_error(value)}") from exc


def parse_iso_date(value) -> date | None:
    """Returns a `date` object for a canonical 'YYYY-MM-DD' string, else None (never raises).
    Callers that need to COMPARE two dates chronologically must compare the returned `date` objects
    — never the original strings, even when both look 'ISO-ish' — this function's return value is
    what makes that possible without a caller re-implementing the grammar check."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_iso_date_or_none(value) -> bool:
    if value is None:
        return True
    return parse_iso_date(value) is not None


def _is_nonneg_int(value) -> bool:
    return is_schema_integer(value) and value >= 0


def _is_bounded_obs_count(value) -> bool:
    """A non-negative integer within OBS_COUNT_MIN..OBS_COUNT_MAX — used for every count field an
    OBSERVATIONS document may carry (table_rows_actual, line_count, declared_rules_count,
    commits_count), so an absurd value is a normal shape error here, never a later runtime failure
    in comparisons/arithmetic against it."""
    return is_schema_integer(value) and OBS_COUNT_MIN <= value <= OBS_COUNT_MAX


_FILE_OBS_EXISTS_TRUE_FIELDS = {
    "markers_present", "contains_text", "table_rows_actual", "line_count", "stamps",
    "digest_present", "declared_rules_count", "last_modified_date", "command_result",
    "scan_skipped_reason",
}
_FILE_OBS_EXISTS_FALSE_FIELDS = {"unreadable_reason"}


def validate_file_observation(obs, path: str) -> list:
    """Shape-checks one observations.files[] entry. `exists: false` implies every field OTHER than
    `file`/`exists`/`unreadable_reason` is absent — the observer never guesses a content fact about
    a file it did not read; `unreadable_reason` (optional, non-blank string or null) MAY be present
    to say why (a symlink escaping --repo, a permission error, ...). This is a ONE-WAY rule:
    `exists: true` does NOT imply every other field is present (e.g. `command_result` appears only
    when the entry's filled_when tree declares a `command` predicate; `scan_skipped_reason` appears
    only when the file was too large to scan)."""
    if not isinstance(obs, dict):
        return [f"{path}: must be an object"]
    errors = []
    if not is_nonblank_str(obs.get("file")):
        errors.append(f"{path}.file: required, must be a non-blank string")
    exists = obs.get("exists")
    if not isinstance(exists, bool):
        errors.append(f"{path}.exists: required, must be a boolean")
        return errors  # cannot check field-set rules below without knowing exists' truth value

    if exists:
        allowed = {"file", "exists"} | _FILE_OBS_EXISTS_TRUE_FIELDS
        if "markers_present" in obs:
            v = obs["markers_present"]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                errors.append(f"{path}.markers_present must be an array of strings")
        if "contains_text" in obs:
            v = obs["contains_text"]
            if not isinstance(v, dict) or not all(isinstance(val, bool) for val in v.values()):
                errors.append(f"{path}.contains_text must be an object mapping strings to booleans")
        for count_field in ("table_rows_actual", "line_count", "declared_rules_count"):
            if count_field not in obs:
                continue
            if not _is_bounded_obs_count(obs[count_field]):
                errors.append(f"{path}.{count_field} must be an integer in "
                              f"{OBS_COUNT_MIN}..{OBS_COUNT_MAX}")
            else:
                obs[count_field] = int(obs[count_field])
        if "stamps" in obs:
            v = obs["stamps"]
            if not isinstance(v, dict) or not all(_is_iso_date_or_none(val) for val in v.values()):
                errors.append(f"{path}.stamps must be an object mapping strings to an ISO-8601 date "
                               f"string or null")
        if "digest_present" in obs and not isinstance(obs["digest_present"], bool):
            errors.append(f"{path}.digest_present must be a boolean")
        if "last_modified_date" in obs and not _is_iso_date_or_none(obs["last_modified_date"]):
            errors.append(f"{path}.last_modified_date must be a canonical YYYY-MM-DD calendar date string or null")
        if "command_result" in obs and obs["command_result"] != "unknown":
            errors.append(f"{path}.command_result must be the literal string 'unknown'")
        if "scan_skipped_reason" in obs and not (
            obs["scan_skipped_reason"] is None or is_nonblank_reason(obs["scan_skipped_reason"])
        ):
            errors.append(f"{path}.scan_skipped_reason must be a non-blank string or null")
    else:
        allowed = {"file", "exists"} | _FILE_OBS_EXISTS_FALSE_FIELDS
        if "unreadable_reason" in obs and not (
            obs["unreadable_reason"] is None or is_nonblank_reason(obs["unreadable_reason"])
        ):
            errors.append(f"{path}.unreadable_reason must be a non-blank string or null")
        forbidden = set(obs) - allowed
        if forbidden:
            errors.append(f"{path}: exists is false but carries content-fact field(s) "
                           f"{safe_repr_for_error(sorted(forbidden))} (the observer never guesses "
                           f"facts about a file it cannot read)")

    unknown = set(obs) - allowed
    if unknown:
        errors.append(f"{path}: unknown field(s) {safe_repr_for_error(sorted(unknown))}")
    return errors


_MATURITY_DATE_FIELDS = (
    "instantiated_date", "first_src_commit_date", "first_test_date", "first_spec_date",
    "first_dependency_date", "first_release_date",
)


def validate_maturity(m, path: str) -> list:
    if not isinstance(m, dict):
        return [f"{path}: must be an object"]
    errors = []
    for f in _MATURITY_DATE_FIELDS:
        if f in m and not _is_iso_date_or_none(m[f]):
            errors.append(f"{path}.{f} must be a canonical YYYY-MM-DD calendar date string or null")
    if "commits_count" in m:
        if not _is_bounded_obs_count(m["commits_count"]):
            errors.append(f"{path}.commits_count must be an integer in {OBS_COUNT_MIN}..{OBS_COUNT_MAX}")
        else:
            m["commits_count"] = int(m["commits_count"])
    if "commit_dates" in m:
        v = m["commit_dates"]
        if not isinstance(v, list) or not all(isinstance(x, str) and _is_iso_date_or_none(x) for x in v):
            errors.append(f"{path}.commit_dates must be an array of canonical YYYY-MM-DD calendar date strings")
        else:
            if len(v) > OBS_COUNT_MAX:
                errors.append(f"{path}.commit_dates: more than {OBS_COUNT_MAX} entries")
            # `commits-at-least`'s own due-date lookup (fill_evaluate.py's eval_due_leaf) indexes
            # commit_dates[n-1] assuming OLDEST FIRST, exactly as fill_observe.py's own
            # `--reverse` git log always produces it; a document that is not actually sorted that
            # way (hand-written, or from a third-party observer) would silently hand back the WRONG
            # commit's date for a given n, not an error — checked here, once, so every evaluator
            # caller can trust the order without re-checking it itself.
            parsed = [parse_iso_date(x) for x in v]
            if any(a > b for a, b in zip(parsed, parsed[1:])):
                errors.append(f"{path}.commit_dates: must be sorted oldest-first (non-decreasing)")
            # fill_observe.py's own commit_dates() never caps the list independently of
            # commits_count (one entry per commit reachable from HEAD, same as commits_count's own
            # count) — so when both fields are present, a length mismatch is itself a shape defect,
            # not merely a style choice a caller is free to make.
            if ("commits_count" in m and _is_bounded_obs_count(m["commits_count"])
                    and len(v) != m["commits_count"]):
                errors.append(f"{path}.commit_dates: length ({len(v)}) does not match "
                               f"commits_count ({m['commits_count']})")
    if "days_since_setup" in m:
        v = m["days_since_setup"]
        if not (v is None or (is_schema_integer(v)
                              and OBS_COUNT_MIN <= v <= OBS_COUNT_MAX)):
            errors.append(f"{path}.days_since_setup must be null, or an integer in "
                           f"{OBS_COUNT_MIN}..{OBS_COUNT_MAX}")
        elif v is not None:
            m["days_since_setup"] = int(v)
    allowed = set(_MATURITY_DATE_FIELDS) | {"commits_count", "commit_dates", "days_since_setup"}
    unknown = set(m) - allowed
    if unknown:
        errors.append(f"{path}: unknown field(s) {safe_repr_for_error(sorted(unknown))}")
    return errors


OBSERVATIONS_ALLOWED = {"repo", "contract", "now", "observer_version", "maturity", "files"}
_OBSERVATIONS_STRING_FIELDS = ("repo", "contract", "now", "observer_version")


def validate_observations(obs) -> list:
    """Full shape validation of an OBSERVATIONS document (fill_observe.py's own output, or any
    object shaped like it) BEFORE fill_evaluate.py trusts any of it. Returns a list of reason
    strings; [] means every field fill_evaluate.py reads is guaranteed to be the type it expects.
    Closed at the top level too (unknown keys rejected) and every metadata field
    (repo/contract/now/observer_version) is type-checked when present, matching
    $defs.observations's own `additionalProperties: false` exactly — these are carried, informative
    fields (none of them feeds a decision), but "carried" does not mean "untyped"."""
    if not isinstance(obs, dict):
        return ["observations: top level must be an object"]
    errors = []
    top_extra = set(obs.keys()) - OBSERVATIONS_ALLOWED
    if top_extra:
        errors.append(f"observations: unknown field(s) {safe_repr_for_error(sorted(top_extra))}")
    for key in _OBSERVATIONS_STRING_FIELDS:
        if key in obs and not isinstance(obs[key], str):
            errors.append(f"observations.{key} must be a string")
    if "now" in obs:
        try:
            parse_now(obs["now"])
        except FillError:
            errors.append("observations.now must be a canonical date or timestamp")
    files = obs.get("files")
    if not isinstance(files, list):
        errors.append("observations.files: must be an array")
    else:
        # A duplicate `files[].file` is rejected here, the same shape as the contract-side
        # duplicate-`file` check in `validate_contract`: `fill_evaluate.py`'s own `obs_by_file`
        # lookup (run_evaluate) is a plain dict keyed by `file`, so two entries naming the same file
        # would otherwise silently let "whichever one is LAST in the array" decide that file's
        # state — the SAME two observations in the other order would then decide a DIFFERENT state,
        # with no error reported either way.
        seen_files = set()
        for i, f in enumerate(files):
            errors.extend(validate_file_observation(f, f"observations.files[{i}]"))
            if isinstance(f, dict):
                file_val = f.get("file")
                if isinstance(file_val, str):
                    if file_val in seen_files:
                        errors.append(f"observations.files[{i}].file: duplicate file entry")
                    else:
                        seen_files.add(file_val)
    errors.extend(validate_maturity(obs.get("maturity"), "observations.maturity"))
    return errors


# ---------------------------------------------------------------------------
# Frontmatter / marker / stamp text helpers (used by fill_observe.py)
# ---------------------------------------------------------------------------
# STAMP_RE stays a regex: its character classes are all tightly constrained (a bare key charset, a
# fixed-width date), so an ordinary `finditer` over it does a bounded amount of work per starting
# position — nothing here backtracks over an unbounded `.*?`. Marker scanning (FILL:/RULE:) does
# NOT use a `.*?`-DOTALL regex at all (see _iter_html_comments below): that pattern's own lazy
# wildcard, on an adversarial file with many `<!-- FILL:` openers and no matching `-->` anywhere,
# makes each failed match attempt scan to the end of the remaining text before `finditer` advances
# to try the next starting position — O(openers x remaining length), quadratic in the number of
# unclosed openers. A 64 KiB (or even 1 MiB) size cap on top of that only shrinks the constant, it
# does not change the O(n^2) shape; the fix is the scanner, not the cap.
STAMP_RE = re.compile(r"<!--\s*STAMP:\s*([A-Za-z0-9_-]+)\s+(\d{4}-\d{2}-\d{2})\s*-->")


def _iter_html_comments(text: str):
    """Yields the raw CONTENT (the text between '<!--' and '-->') of every well-formed HTML
    comment in `text`, left to right, via a single forward-only LINEAR scan: `str.find` is called
    at most twice per comment found (once for '<!--', once for the matching '-->'), and the search
    position only ever advances — it never re-scans a region already covered. The first '<!--' with
    no matching '-->' anywhere in the rest of the text ends the scan entirely (that one search is
    the last O(remaining-length) unit of work done; there is no second, third, ... unclosed opener
    each separately re-paying that cost, which is what made the old `.*?`-DOTALL regex approach
    quadratic on exactly this adversarial shape). This does mean a comment AFTER an unclosed opener
    is not found — an intentional, documented trade-off for a hard linear bound, not an oversight."""
    pos = 0
    while True:
        start = text.find("<!--", pos)
        if start == -1:
            return
        end = text.find("-->", start + 4)
        if end == -1:
            return  # an unclosed opener: stop scanning (see docstring)
        yield text[start + 4:end]
        pos = end + 3


def _marker_id(content: str) -> str:
    """A marker's id is everything before the first em-dash ' — ' separator (or the whole comment
    body if there is no em-dash), trimmed."""
    return content.split("—", 1)[0].strip()


def find_markers(text: str) -> list:
    ids = set()
    for content in _iter_html_comments(text):
        stripped = content.lstrip()  # '<!--\s*FILL:' — only LEADING whitespace before 'FILL:'
        if stripped.startswith("FILL:"):
            marker_id = _marker_id(stripped[len("FILL:"):])
            if marker_id:
                ids.add(marker_id)
    return sorted(ids)


def find_rule_count(text: str) -> int:
    count = 0
    for content in _iter_html_comments(text):
        stripped = content.lstrip()
        if stripped.startswith("RULE:"):
            marker_id = _marker_id(stripped[len("RULE:"):])
            if marker_id:
                count += 1
    return count


def find_stamps(text: str, keys: set) -> dict:
    """Latest date per requested stamp key; null (None) if a key is never stamped. STAMP_RE's own
    `(\\d{4}-\\d{2}-\\d{2})` group only checks digit SHAPE, not real calendar validity — a line
    reading `<!-- STAMP: k 2026-02-30 -->` matches the regex but names a day that does not exist. A
    stamp whose date fails `parse_iso_date` (not a real calendar date) is treated exactly like a
    malformed stamp line (bad key, no date at all): silently absent, never retained. This is the
    one guarantee that keeps the observer and the evaluator in agreement — a successful
    `fill_observe.py` run must never produce a `stamps` value `fill_common.validate_file_observation`
    would itself reject. Dates are compared as PARSED `date` objects (never as strings) to pick the
    latest, even though canonical 'YYYY-MM-DD' strings happen to also sort correctly as strings —
    parsing first is the one rule this format applies uniformly, not an accident this call site
    relies on."""
    latest = {k: None for k in keys}
    latest_parsed = {}
    for m in STAMP_RE.finditer(text):
        key, date_str = m.group(1), m.group(2)
        if key not in latest:
            continue
        parsed = parse_iso_date(date_str)
        if parsed is None:
            continue
        if key not in latest_parsed or parsed > latest_parsed[key]:
            latest_parsed[key] = parsed
            latest[key] = date_str
    return latest


def count_table_data_rows(text: str) -> int:
    """Counts markdown table DATA rows: consecutive '|'-prefixed lines are one table block; if the
    block's 2nd line is a separator (---/:---/etc.), rows = len(block) - 2 (header + separator
    dropped); otherwise every line in the block counts (no separator recognized -> no header
    assumed)."""
    lines = text.splitlines()
    total = 0
    block = []

    def flush(block):
        if not block:
            return 0
        if len(block) >= 2 and re.match(r"^\|[\s:|-]+\|$", block[1].strip()):
            return max(0, len(block) - 2)
        return len(block)

    for line in lines:
        if line.strip().startswith("|") and line.strip().endswith("|"):
            block.append(line)
        else:
            total += flush(block)
            block = []
    total += flush(block)
    return total


def frontmatter_digest_present(text: str) -> bool:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        m = re.match(r"^digest:\s*(.*)$", line)
        if m and m.group(1).strip().strip('"').strip("'"):
            return True
    return False
