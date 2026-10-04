"""impact_rules.py — shared, stdlib-only core for `impact_validate.py` and `impact_evaluate.py`.

Implements:
  - TOML loading of one rules file (tomllib; stdlib, Python 3.11+).
  - Shape/enum/glob-grammar validation of a MERGED set of rules files.
  - Rule-id uniqueness within and across files.
  - Repo-id grammar (own repo, and any `<repo>:` cross-repo prefix in a glob or a changed path).
  - Glob-grammar checking (with documented size limits, enforced at validation) and exact,
    iterative glob-to-path matching (no recursion, no backtracking regex — see `glob_match_path`).
  - Grammar/size-limit checking of a literal changed-path entry (`--changed`/`--changed-file`),
    the pure evaluator `impact()`, and the `not_evaluated_derivers` projection.
  - `load_changed_file`: the strict encoding/line contract for a `--changed-file` text file.
  - `project_obligation`: a REPORT-ONLY summary shaped like a subset of a `format_obligation` duty
    record (see its own docstring — this never mints or IS an obligation record).

This module raises nothing on its own for a caller's convenience — `validate_merged()` and
`validate_changed_paths()` return a list of issues (possibly empty); callers (`impact_validate.py`
/ `impact_evaluate.py`) decide exit codes and JSON shape. `load_toml_file()` and
`load_changed_file()` DO raise (`RulesInputError`) for I/O/decode/parse failures.

Stdlib only: tomllib, datetime, pathlib, re.

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
from datetime import date, datetime, time, timedelta, timezone
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
    if is_schema_integer(value):
        return int(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: normalize_schema_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_schema_numbers(item) for item in value]
    return value

IMPACT_RULES_VERSION = "impact-rule/first-cut-0.1.0-draft"

# ---- exit-code classes (shared numbering between validate and evaluate) ----
E_MISSING_KEY = 10
E_UNKNOWN_KEY = 11
E_BAD_TYPE = 12
E_BAD_ENUM = 13
E_BAD_GLOB = 14
E_BAD_WHEN = 15
E_DUP_RULE_ID = 16
E_BAD_URGENCY = 17
E_BAD_DERIVE = 18
E_USAGE = 64
E_INPUT = 65
E_OUTPUT = 74
E_SELFTEST = 99

# ---- closed vocabularies ----
EVENT_VOCAB = {
    "new-dependency", "template-tag-moved", "first-src-commit", "first-test", "first-spec",
    "first-release", "public", "session-closed", "rule-added",
}
RELATION_VOCAB = {"depends-on", "template-of", "pins", "traces-to", "records", "governs", "documents"}
ACTION_VOCAB = {"re-verify", "re-render", "update", "attend"}
# One tag each for `decided_by.detect`/`decided_by.fix`: which kind of actor can notice the need
# for an update, and which kind can make it. A closed set of actor-kind tags.
DECIDER_VOCAB = {"program", "model", "human"}
IMPORTANCE_VOCAB = {"critical", "high", "normal", "low"}
RETRIGGER_VOCAB = {"once", "every-event"}
DERIVER_VOCAB = {"erlang-xref", "pinned-sha", "declared-coverage", "git-cochange"}

IMPORTANCE_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_DURATION_RE = re.compile(r"^[1-9][0-9]*[dhm]$")
_DURATION_SECONDS = {"d": 86400, "h": 3600, "m": 60}

# `--now`'s own grammar: ONE strict, fully-documented ISO-8601 UTC form, checked with `.fullmatch`
# before any parsing runs — not `datetime.fromisoformat`'s own (version-dependent, considerably
# wider) acceptance, which accepts many undocumented variants this format never promised to parse.
# `YYYY-MM-DDTHH:MM:SS` plus either `Z` or an explicit `[+-]HH:MM` offset; no fractional seconds,
# no other separator, no surrounding whitespace (a leading/trailing space or newline is a grammar
# failure here, not silently trimmed). BOTH the clock time's own hour/minute/second AND the offset's
# hour/minute are bounded to a REAL clock value IN THE GRAMMAR itself (`[01][0-9]|2[0-3]` for every
# hour field, `[0-5][0-9]` for every minute/second field) — `datetime.fromisoformat` otherwise
# accepts `T24:00:00` as a second, non-canonical spelling of the next day's midnight (silently
# rolling the date over) and normalizes an out-of-range offset like `+00:60`/`+12:99` instead of
# rejecting it, so both bounds must be enforced here, before that call ever runs
# (format_fill-contract's own `_NOW_RE` uses the same bound on every hour/minute/second field).
# `[0-9]`, not `\d`, throughout: `\d` also matches non-ASCII decimal digits (e.g. U+0660
# ARABIC-INDIC DIGIT ZERO), which `int()`/`datetime.fromisoformat` either reject inconsistently or
# accept in ways this grammar never promised to parse.
_NOW_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)

# Repo-id grammar: byte-identical to format_relation's owner pattern (the repo-id charset inside
# the entity-ref/repo-prefix family this format shares), plus the same 1-64 length bound.
_REPO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.*-]+$")
_CONTROL_CHARS = {chr(c) for c in range(0x20)} | {chr(0x7F)}

TOP_REQUIRED = ("impact_version", "repo", "rules")
TOP_OPTIONAL = ("inherit", "derive")
TOP_ALLOWED = set(TOP_REQUIRED) | set(TOP_OPTIONAL)

RULE_REQUIRED = ("id", "when", "relation", "must_update", "decided_by", "importance", "urgency")
RULE_OPTIONAL = ("retrigger",)
RULE_ALLOWED = set(RULE_REQUIRED) | set(RULE_OPTIONAL)

WHEN_ALLOWED = {"changed", "event"}
DECIDED_BY_REQUIRED = ("detect", "fix")
MUST_UPDATE_REQUIRED = ("target", "action")
MUST_UPDATE_OPTIONAL = ("check", "note")
MUST_UPDATE_ALLOWED = set(MUST_UPDATE_REQUIRED) | set(MUST_UPDATE_OPTIONAL)

# Size limits, checked at VALIDATION time (not only when matching runs): an authored glob pattern
# is necessarily small (hand-written), while a reported/supplied changed-path entry is a real value
# from outside and gets the larger ceiling. Both exist so neither the segment-count DP in
# `glob_match_path` nor the per-segment '*'-count in its chunk search can be driven arbitrarily
# large by an ordinary-looking but oversized input.
GLOB_MAX_LENGTH = 1024
GLOB_MAX_SEGMENTS = 64
GLOB_MAX_STARS_PER_SEGMENT = 16
CHANGED_PATH_MAX_LENGTH = 4096
CHANGED_PATH_MAX_SEGMENTS = 256
CHANGED_FILE_MAX_LINES = 100_000

# A `within:<N>d|h|m` duration longer than this is MALFORMED (E_BAD_URGENCY, exit 17), not merely
# large: an unbounded N (the grammar `[1-9][0-9]*` alone allows arbitrarily many digits) turns
# `--obligations`' own `now + timedelta(...)` arithmetic (`project_obligation`) into an
# OverflowError — a runtime crash the caller can only see as a generic input failure, never the
# documented validation class. 36500 days (100 years) is comfortably past any real urgency and
# comfortably under `timedelta`'s own ~2.7-billion-day representable range, so the bound is never
# the thing that is actually limiting — a legitimate duration will never approach it.
MAX_DURATION_DAYS = 36500
MAX_DURATION_SECONDS = MAX_DURATION_DAYS * 86400

# Checked BEFORE `int(...)` ever runs on the duration's digit string: `_DURATION_RE`'s grammar
# (`[1-9][0-9]*`) bounds the CHARACTER SET but not the LENGTH — an adversarial 5000-digit numeral is
# still grammar-valid, and Python's own `int()` refuses (as of 3.11+, `ValueError`) a decimal string
# longer than `sys.get_int_max_str_digits()` (4300 by default) specifically because integer-string
# conversion is quadratic in digit count — a large-enough string is a denial-of-service vector, not
# merely slow. A duration needing this many digits is already nonsensical (MAX_DURATION_DAYS itself
# is 5 digits; even converted to minutes it is 8), so this check rejects it as a malformed/
# oversized `urgency` (E_BAD_URGENCY) — the documented validation class — rather than ever calling
# `int()` and letting Python's own conversion limit turn it into an uncaught `ValueError`.
MAX_DURATION_DIGITS = 15

# `--now`'s year is bounded too, defense-in-depth alongside MAX_DURATION_DAYS: with both bounds in
# place, `now + timedelta(days=<=MAX_DURATION_DAYS)` can NEVER reach datetime's own year-9999
# ceiling (9000 + 100 years of headroom is nowhere near it), so `project_obligation`'s addition is
# provably safe by construction, not by catching whatever OverflowError happens to surface.
MAX_NOW_YEAR = 9000


class RulesInputError(Exception):
    """I/O, decode, or TOML-syntax failure reading one --rules file, OR an I/O/UTF-8/line-contract
    failure reading a --changed-file (exit 65 either way — both are "a file this tool was told to
    read could not be read as the shape it must have")."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.code = E_INPUT
        self.reason = reason


class UsageError(Exception):
    """A CLI-argument problem (exit 64)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.code = E_USAGE
        self.reason = reason


def issue(code: int, reason: str) -> dict:
    return {"code": code, "reason": reason}


_SAFE_REPR_MAX_CHARS = 1000
_SAFE_REPR_MAX_ITEMS = 500

# The DOCUMENTED diagnostic bound: the README promises that no single issue entry's `reason` this
# format's tools ever report is longer than this many characters, for ANY input, including an
# adversarial one (a 1,000,000-character field value, an attacker-chosen key name, ...). Byte-for-
# byte the same bound, for the same reason, as `format_fill-contract`'s own
# `fill_common.DIAGNOSTIC_REASON_MAX_CHARS` — comfortably above `_SAFE_REPR_MAX_CHARS` (the cap on
# one `safe_repr_for_error` call's own output) to leave headroom for the fixed surrounding text
# every error message also carries, with every message embedding at most one bounded
# `safe_repr_for_error` call per interpolated input-derived value.
DIAGNOSTIC_REASON_MAX_CHARS = 2000

# A `str` value longer than this is truncated to THIS MANY CHARACTERS BEFORE `repr()` ever sees it
# — never after. Byte-for-byte the same bound as `format_fill-contract`'s own
# `fill_common._SAFE_REPR_STR_PREVIEW_CHARS` (see its comment for the full rationale): `repr()` on
# an arbitrarily large string is itself `O(len(v))` work, the same class of unbounded cost the
# oversized-int guard below already exists to avoid, and a hostile million-character string value
# must never reach `repr()` in full, whether at the top level or nested inside a container.
_SAFE_REPR_STR_PREVIEW_CHARS = 200


_ESCAPE_SEQUENCE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")


def _escape_unicode_safe(s: str) -> str:
    """Replaces every character in `s` that cannot itself re-encode as UTF-8 (a lone UTF-16
    surrogate, U+D800-U+DFFF — the shape a CLI argument takes once `surrogateescape` has decoded an
    OS byte with no valid UTF-8 interpretation) with a visible backslash escape. Idempotent on a
    string that is already UTF-8-safe: encoding with `backslashreplace` and decoding back is a
    no-op round trip when there is nothing to substitute."""
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


def _backoff_escape_boundary(s: str, keep: int) -> int:
    """If slicing `s` at `keep` would land strictly inside a `\\uXXXX` escape sequence (the shape
    `_escape_unicode_safe` and `repr()` both produce for an unencodable or non-printable character),
    backs `keep` off to the index just before that escape begins — so a capped string never ends
    with an unterminated half-escape (e.g. `\\udc` immediately followed by a truncation marker,
    which reads as a DIFFERENT, shorter escape once re-parsed). A `\\uXXXX` escape is always exactly
    6 characters, so it is enough to look at the small window immediately around `keep`."""
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
    for a 1,000-character cap). The cut point is also backed off (`_backoff_escape_boundary`) so it
    never splits a `\\uXXXX` escape sequence in half. Byte-for-byte the same helper (and the same
    reasoning) as `format_fill-contract`'s own `fill_common._cap_with_marker`."""
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
      formatting. `bit_length() > 64` is the chosen threshold (comfortably below where the real
      4300-digit guard could ever fire). The digit count itself is an ESTIMATE from the bit length
      (`bit_length * log10(2)`, never by stringifying `v`).
    - A `str` longer than `_SAFE_REPR_STR_PREVIEW_CHARS` is sliced to that many characters BEFORE
      `repr()` is called on it — never repr'd in full first and truncated after. The returned text
      is then, like every other case below, hard-capped to `_SAFE_REPR_MAX_CHARS` characters (plus
      a trailing `...>` marker) — so a SCALAR return (top-level, or nested one level inside a
      container) is bounded by the same fixed constant the container-walking
      `safe_repr_for_error` itself is."""
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
    foreign value this format's tools read — a TOML scalar (a rules document is user-authored TOML,
    so `repo`/a `derive[]` param/any other field can be mistyped to any TOML type, an oversized
    integer included), or a CONTAINER (list/tuple/dict/set) nesting one, to any depth. Byte-for-byte
    the same algorithm as `format_fill-contract`'s own `fill_common.safe_repr_for_error` (see its
    docstring for the full rationale): an explicit, heap-allocated work stack (never Python function
    recursion, so an adversarially DEEP container cannot exhaust the interpreter's own recursion
    limit) walking each container's elements through `itertools.islice` (never materializing a
    container in full first, so an adversarially WIDE container costs only the remaining item
    budget). At most `_SAFE_REPR_MAX_ITEMS` values (any kind, any depth) are rendered, and the
    assembled text is hard-truncated to `_SAFE_REPR_MAX_CHARS` characters (plus a trailing `...>`
    marker) before this function returns."""
    if not _safe_repr_is_container(value):
        return _safe_repr_scalar(value)

    parts = []
    total_len = 0
    items_seen = 0
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
    well-formed message rarely if ever reaches this cap) — it is the one place EVERY issue `reason`
    this format's tools ever report is checked again, unconditionally, immediately before it leaves
    a report constructor. That is what makes the bound hold even for a call site that forgot to
    self-bound (a raw `OSError`/exception message, or a future call site added without rereading
    this module's own rules) — the report constructor itself, not every call site separately, is
    what a caller (and this format's own self-tests) can rely on. Byte-for-byte the same helper (and
    the same reasoning) as `format_fill-contract`'s own `fill_common.bound_reason`."""
    if not isinstance(text, str):
        text = str(text)
    # Escaped BEFORE the length check/cap, not after: a `reason` built from an unvalidated
    # argv/exception value — every usage-error reason this format's tools emit
    # for a `surrogateescape`-decoded hostile argv value) must never carry a lone UTF-16 surrogate
    # past this, the final, unconditional backstop, even when the call site that built the message
    # forgot to escape it itself.
    text = _escape_unicode_safe(text)
    if len(text) <= DIAGNOSTIC_REASON_MAX_CHARS:
        return text
    return _cap_with_marker(text, DIAGNOSTIC_REASON_MAX_CHARS, marker=_REASON_TRUNCATION_MARKER)


def bound_path_preview(path) -> str:
    """Returns a short, BOUNDED preview of a filesystem path (or any other path-like value: a
    `pathlib.Path`, or a raw CLI string) safe to interpolate DIRECTLY into an error message — for
    ANY input, including an adversarially long `--rules`/`--changed-file` argument (a
    10,000-character string). Truncated to `_SAFE_REPR_STR_PREVIEW_CHARS` characters (plus a
    trailing marker if truncated) BEFORE any further formatting, the same bound (and the same
    reasoning) `_safe_repr_scalar` already applies to an oversized string VALUE — this is the
    counterpart for a path argument, which every call site below interpolates RAW (never through
    `safe_repr_for_error`, which would add quoting a plain path display does not want). Call this at
    every site that interpolates a `--rules`/`--changed-file` path value directly, and in place of
    an exception's own `str(exc)` wherever that exception is known to embed the same (or a derived)
    path in full — use the exception's CLASS name alongside this bounded preview instead, never the
    exception's unbounded `str()`. Byte-for-byte the same helper (and the same reasoning) as
    `format_fill-contract`'s own `fill_common.bound_path_preview`, PLUS one more guarantee: a CLI
    argument is decoded by Python from the OS's raw bytes using `surrogateescape` (e.g. a POSIX
    filename byte `\\xff` with no valid UTF-8 interpretation becomes the Python string `"\\udcff"`,
    a LONE UTF-16 surrogate) — `str(path)` on such a value is still a `str`, but one that cannot
    itself encode as UTF-8, so interpolating it RAW into a message would put a string this format's
    own reports can never actually serialize into. Every such character is replaced with a visible,
    UTF-8-safe backslash escape here, BEFORE the length cap below, so the cap still holds on the
    ESCAPED text (escaping can only lengthen a string, never shorten it — capping first and escaping
    after could let a result exceed the cap)."""
    s = str(path)
    s = _escape_unicode_safe(s)
    if len(s) <= _SAFE_REPR_STR_PREVIEW_CHARS:
        return s
    return _cap_with_marker(s, _SAFE_REPR_STR_PREVIEW_CHARS)


# ------------------------------------------------------------------------------------------------
# The JSON input contract every JSON value this format's tools read OR PRINT must satisfy: (a) no
# duplicate object keys; (b) no NaN/Infinity/-Infinity; (c) every string, key or value, must encode
# as UTF-8; (d) nesting no deeper than MAX_JSON_DEPTH. This format's own `--rules`/`--changed-file`
# inputs are TOML/text, not JSON, but every report this format's CLIs print IS JSON, and a consumer
# reading it is entitled to apply this same strict contract — `find_json_input_defect` is what this
# format's own self-tests use to prove a REAL printed report satisfies it (H7: producer against
# rule), not only a hand-built fixture. Byte-for-byte the same helpers (and the same reasoning) as
# `format_trigger`'s own `trigger_vocab.py` (which also uses them to validate real JSON/JSONL input
# files, a use this format has no need for).
# ------------------------------------------------------------------------------------------------
MAX_JSON_DEPTH = 64


def unique_pairs(pairs: list) -> dict:
    """`object_pairs_hook` for `json.loads`: rejects a duplicate key instead of silently keeping
    only the last value for it (`json.loads`'s own default `dict(pairs)` behavior)."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    """`parse_constant` for `json.loads`: JSON's grammar has no NaN/Infinity/-Infinity, but
    Python's decoder accepts them anyway (`json.loads("NaN")` succeeds) unless this hook is set —
    reject them, rather than let a non-JSON numeric constant into what should be a strictly-standard
    JSON parse."""
    raise ValueError(f"non-JSON numeric constant: {value}")


def find_json_input_defect(obj, max_depth: int = MAX_JSON_DEPTH):
    """ITERATIVE walk (an explicit stack, never recursion over input size) over one parsed JSON
    value. Returns a reason string for the first defect found, or None if the whole structure is
    clean: a string (a dict KEY or a VALUE, at any nesting depth) that cannot encode as UTF-8, a
    non-finite float, or nesting deeper than `max_depth`. The top-level value is depth 1."""
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return (f"a string cannot encode as UTF-8 (contains a lone UTF-16 surrogate): "
                        f"{safe_repr_for_error(value)}")
        elif isinstance(value, float):
            if not math.isfinite(value):
                return f"contains a non-finite number (not allowed): {value!r}"
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


def _report_schema_entries(declaration):
    if isinstance(declaration, tuple) and len(declaration) == 2:
        return ((None, declaration),)
    if isinstance(declaration, dict) and declaration:
        return tuple(declaration.items())
    raise ValueError("REPORT_SCHEMA must be a pair or a nonempty command mapping")


def register_report_schema(module_file: str, declaration) -> None:
    from schema_conformance import audit
    format_root = Path(module_file).resolve().parent.parent
    for command, entry in _report_schema_entries(declaration):
        if command is not None and (not isinstance(command, str) or not command):
            raise ValueError("REPORT_SCHEMA command keys must be nonempty strings")
        if not (isinstance(entry, tuple) and len(entry) == 2 and
                all(isinstance(item, str) and item for item in entry)):
            raise ValueError("REPORT_SCHEMA entries must be (schema-relative-path, defs-name)")
        schema_rel, def_name = entry
        key = (str(format_root), schema_rel, def_name)
        if key not in _REPORT_SCHEMA_CACHE:
            document = json.loads((format_root / schema_rel).read_text(encoding="utf-8"))
            audit(document)
            if def_name not in document.get("$defs", {}):
                raise ValueError(f"missing $defs.{def_name}")
            _REPORT_SCHEMA_CACHE[key] = document


def _declared_report_contract(report_command=None):
    frame = sys._getframe(1)
    while frame is not None:
        namespace = frame.f_globals
        declaration = namespace.get("REPORT_SCHEMA")
        module_file = namespace.get("__file__")
        if isinstance(declaration, tuple) and len(declaration) == 2 and module_file:
            return declaration, Path(module_file).resolve().parent.parent
        if isinstance(declaration, dict) and module_file:
            entry = declaration.get(report_command)
            if entry is None:
                raise ValueError(f"no report schema declared for command {report_command!r}")
            return entry, Path(module_file).resolve().parent.parent
        frame = frame.f_back
    return None


def _internal_error_report(def_name: str) -> dict:
    error = {"code": E_INPUT, "reason": "report failed its schema"}
    if def_name == "run_case_report":
        return {"code": E_INPUT,
                "report": {"status": "unknown", "exit_code": E_INPUT, "errors": [error],
                           "fired": None, "not_evaluated_derivers": None,
                           "impact_rules_version": IMPACT_RULES_VERSION}}
    return {"status": "unknown", "exit_code": E_INPUT, "errors": [error]}


def _schema_checked_report_text(text: str, report_command=None) -> str:
    global _REPORT_BOUNDARY_FAILED
    contract = _declared_report_contract(report_command)
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


def write_report(text: str, *, report_command=None) -> None:
    """Write one complete UTF-8 report, translating only stdout failures to a boundary exception.

    A real stdout descriptor uses ``os.write`` so Python's text wrappers cannot conceal a short
    write. A descriptor-less in-process stream keeps the text protocol used by callers that
    redirect ``main()`` to ``io.StringIO``.
    """
    text = _schema_checked_report_text(text, report_command)
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
    """Run one Impact-rule CLI at the stdout byte boundary.

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
    return E_INPUT if _REPORT_BOUNDARY_FAILED else code


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
    already escapes one path (`_escape_report_value`), then serializes with
    `json.dumps(..., allow_nan=False)` — `allow_nan` is forced here regardless of what a caller
    passes, so a call site cannot opt back out of it by omission. This is the BACKSTOP, not a
    substitute for bounding/escaping each field at the point it is built: every report constructor
    in this format's tools still runs every `reason` through `bound_reason` and every path through
    `bound_path_preview` at the source — this funnel exists so a report field that reaches `write_report()`
    without having gone through either (an argparse usage message built straight from the raw,
    `surrogateescape`-decoded argv text, or a future call site not yet written) still cannot make
    this format emit a report its own strict reader would reject."""
    dumps_kwargs["allow_nan"] = False
    return json.dumps(_escape_report_value(report), **dumps_kwargs)


def is_nonblank_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_clean_str(value) -> bool:
    """Non-blank string with no control character (U+0000-U+001F, U+007F) — used for id/path/
    repo-id-shaped fields (a rule id, a must_update target) that are not full glob-grammar
    checked but must still never carry a control character."""
    return is_nonblank_str(value) and not any(ch in _CONTROL_CHARS for ch in value)


def _valid_repo_id(repo) -> bool:
    # .fullmatch, not .match: Python's trailing `$` matches just BEFORE a final '\n', so
    # .match("acme\n") on a `...$` pattern wrongly succeeds. .fullmatch requires the whole string,
    # including any trailing newline, to be consumed by the pattern.
    return isinstance(repo, str) and 1 <= len(repo) <= 64 and bool(_REPO_ID_RE.fullmatch(repo))


def load_toml_file(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # `str(exc)` on an `OSError` embeds its own `filename` argument IN FULL (the same value
        # `path` already names) — the exception's CLASS name plus one bounded path preview, never
        # `str(exc)` itself, keeps this message's length independent of how long a `--rules`
        # argument happens to be.
        raise RulesInputError(f"cannot read {bound_path_preview(path)}: {type(exc).__name__}")
    try:
        doc = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except UnicodeDecodeError as exc:
        raise RulesInputError(f"not valid UTF-8 ({bound_path_preview(path)}): {exc}")
    except tomllib.TOMLDecodeError as exc:
        raise RulesInputError(f"TOML syntax error ({bound_path_preview(path)}): {exc}")
    if not isinstance(doc, dict):
        raise RulesInputError(f"top level of {bound_path_preview(path)} must be a TOML table")
    return doc


def load_changed_file(path: Path) -> list:
    """Reads a `--changed-file` text file: one changed-path entry per non-blank, non-`#`-prefixed
    line. Split on LF ONLY (never on a Unicode line separator such as U+2028/U+2029, which
    `str.splitlines()` would wrongly treat as a record boundary). A single trailing CR per line
    (CRLF) is accepted and stripped; a bare CR is rejected as a record separator. A line that is
    ENTIRELY space/tab (or empty) is a blank line, skipped; a line that, after trimming
    space/tab, starts with '#' is a comment, skipped (the comment marker itself may be indented).
    Any OTHER line carrying leading or trailing ASCII space/tab is REJECTED (exit 65) rather than
    silently trimmed — a changed-path entry is used exactly as given, never altered, because a
    real file path may legally begin or end with a space, and silently trimming one would turn a
    real path into a different string without saying so. Raises `RulesInputError` (exit 65) on any
    I/O failure, invalid UTF-8, a bare CR, a line with untrimmed leading/trailing whitespace, or a
    file with more than `CHANGED_FILE_MAX_LINES` lines. Each returned entry still goes through
    `validate_changed_paths` before it is used — this function only enforces the FILE's own
    encoding/line contract, not each path's grammar."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # `str(exc)` on an `OSError` embeds its own `filename` argument IN FULL (the same value
        # `path` already names) — the exception's CLASS name plus one bounded path preview, never
        # `str(exc)` itself, keeps this message's length independent of how long a `--changed-file`
        # argument happens to be.
        raise RulesInputError(f"cannot read --changed-file {bound_path_preview(path)}: "
                               f"{type(exc).__name__}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesInputError(f"--changed-file {bound_path_preview(path)} is not valid UTF-8: {exc}")
    lines = text.split("\n")
    # `str.split("\n")` always yields one MORE element than there are LF characters — the text
    # AFTER the final one, which is "" whenever the file ends with a trailing LF (the normal case;
    # every line, including the last, is LF-terminated). That trailing "" is a split SENTINEL, not
    # a record: it is never appended to `out` below (it is always the blank-line case), so counting
    # it here would reject a file of EXACTLY CHANGED_FILE_MAX_LINES well-formed, LF-terminated
    # lines — one line short of the documented limit, not at it.
    record_count = len(lines) - 1 if text.endswith("\n") else len(lines)
    if record_count > CHANGED_FILE_MAX_LINES:
        raise RulesInputError(f"--changed-file {bound_path_preview(path)} exceeds the "
                               f"{CHANGED_FILE_MAX_LINES}-line limit ({record_count} lines)")
    out = []
    for lineno, line in enumerate(lines, start=1):
        if lineno < len(lines) and line.endswith("\r"):
            line = line[:-1]
        if "\r" in line:
            raise RulesInputError(f"--changed-file {bound_path_preview(path)}:{lineno}: a bare CR "
                                   f"is not a line separator (only a single trailing CR forming "
                                   f"CRLF is accepted)")
        trimmed = line.strip(" \t")
        if trimmed == "" or trimmed.startswith("#"):
            continue
        if line != trimmed:
            raise RulesInputError(f"--changed-file {bound_path_preview(path)}:{lineno}: "
                                   f"leading/trailing space or tab is not trimmed, it is rejected "
                                   f"(a changed-path entry is "
                                   f"used exactly as given): {safe_repr_for_error(line)}")
        out.append(line)
    return out


# ------------------------------------------------------------------------------------------------
# Repo-prefix / glob / changed-path grammar (shared by when.changed[] and --changed/--changed-file)
# ------------------------------------------------------------------------------------------------

def _split_cross_repo(text: str):
    """Returns (repo_or_None, rest) for one glob or changed-path entry. The cross-repo form is
    `<repo>:<rest>` where `<repo>` contains no '/' — this is a SPLIT only; the caller validates
    `repo` (`_valid_repo_id`) and `rest` (path grammar) separately."""
    if ":" in text:
        prefix, _, rest = text.partition(":")
        if "/" not in prefix:
            return prefix, rest
    return None, text


def validate_glob_grammar(text: str, *, is_changed_path: bool = False):
    """Returns None if `text` is grammar-valid, else a reason string. Handles an optional
    `<repo>:` cross-repo prefix. `is_changed_path=True` validates a literal, reported changed-path
    entry against the larger CHANGED_PATH_* size ceiling and skips the per-segment '*'-count limit
    (a changed path is a literal value, not an authored pattern — a literal '*' character in it is
    never a wildcard; see `glob_match_path`'s `seg_match`)."""
    if not isinstance(text, str) or text == "":
        return "must be a non-empty string"
    if any(ch in _CONTROL_CHARS for ch in text):
        return "must not contain a control character"
    max_length = CHANGED_PATH_MAX_LENGTH if is_changed_path else GLOB_MAX_LENGTH
    if len(text) > max_length:
        return (f"exceeds the {max_length}-character limit ({len(text)} characters): "
                f"{safe_repr_for_error(text)}")

    repo, path_part = _split_cross_repo(text)
    if repo is not None:
        if not _valid_repo_id(repo):
            return (f"malformed cross-repo prefix (not a valid <repo> id): "
                    f"{safe_repr_for_error(repo)}")
        if path_part == "":
            return "empty glob after '<repo>:'"

    if path_part.startswith("/"):
        return "absolute path not allowed"
    if path_part == "":
        return "empty glob"

    segments = path_part.split("/")
    max_segments = CHANGED_PATH_MAX_SEGMENTS if is_changed_path else GLOB_MAX_SEGMENTS
    if len(segments) > max_segments:
        return f"exceeds the {max_segments}-segment limit ({len(segments)} segments)"

    for seg in segments:
        if seg == "":
            return "empty path segment"
        if seg in (".", ".."):
            return f"'{seg}' segment not allowed"
        if seg == "**":
            continue
        if "**" in seg:
            return (f"'**' must be its own path segment, not mixed with other characters "
                    f"({safe_repr_for_error(seg)})")
        if not is_changed_path and seg.count("*") > GLOB_MAX_STARS_PER_SEGMENT:
            return (f"segment {safe_repr_for_error(seg)} has more than "
                    f"{GLOB_MAX_STARS_PER_SEGMENT} '*' characters")
        if not _SEGMENT_RE.fullmatch(seg):  # .fullmatch: see _valid_repo_id's comment on .match
            return (f"segment {safe_repr_for_error(seg)} has a disallowed character (allowed: "
                    f"letters, digits, '_', '.', '-', '*')")
    return None


def _validate_glob_segment_grammar(glob: str, label: str) -> list:
    """One `when.changed[]` entry, checked at VALIDATION time. Returns a list of E_BAD_GLOB issues
    (empty iff grammar-valid, including the length/segment/star-count size limits)."""
    reason = validate_glob_grammar(glob, is_changed_path=False)
    if reason:
        return [issue(E_BAD_GLOB, f"{label}: {reason}: {safe_repr_for_error(glob)}")]
    return []


def validate_changed_paths(changed_paths: list) -> list:
    """Returns a list of reason strings (empty iff every entry is clean) for a full
    `--changed`/`--changed-file` list: each entry must be a non-empty string satisfying the same
    grammar as an authored glob (no leading '/', no '.'/'..' segment, no control character), sized
    against the larger CHANGED_PATH_* ceiling. A violating entry is reported here — as an ordinary
    input-shape issue in the normal report — rather than left to crash or silently fail to match
    anything downstream."""
    issues = []
    for i, p in enumerate(changed_paths):
        if not isinstance(p, str):
            issues.append(f"--changed[{i}] must be a string, got {type(p).__name__}")
            continue
        reason = validate_glob_grammar(p, is_changed_path=True)
        if reason:
            issues.append(f"--changed[{i}] {safe_repr_for_error(p)}: {reason}")
    return issues


# ------------------------------------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------------------------------------

def _validate_when(when, rule_label: str) -> list:
    errors = []
    if not isinstance(when, dict):
        return [issue(E_BAD_TYPE, f"{rule_label}.when must be a table")]
    unknown = set(when) - WHEN_ALLOWED
    for key in sorted(unknown):
        errors.append(issue(E_UNKNOWN_KEY,
                             f"{rule_label}.when has unknown key: {safe_repr_for_error(key)}"))
    has_changed = "changed" in when
    has_event = "event" in when
    if has_changed and has_event:
        errors.append(issue(E_BAD_WHEN, f"{rule_label}.when has BOTH 'changed' and 'event' "
                                          f"(exactly one is required)"))
    elif not has_changed and not has_event:
        errors.append(issue(E_BAD_WHEN, f"{rule_label}.when has NEITHER 'changed' nor 'event' "
                                          f"(exactly one is required)"))
    if has_changed:
        changed = when["changed"]
        if not isinstance(changed, list) or not changed:
            errors.append(issue(E_BAD_TYPE, f"{rule_label}.when.changed must be a non-empty array"))
        else:
            for i, glob in enumerate(changed):
                if not is_nonblank_str(glob):
                    errors.append(issue(E_BAD_TYPE,
                                         f"{rule_label}.when.changed[{i}] must be a non-blank string"))
                else:
                    errors.extend(_validate_glob_segment_grammar(glob, f"{rule_label}.when.changed[{i}]"))
    if has_event:
        event = when["event"]
        if not is_nonblank_str(event):
            errors.append(issue(E_BAD_TYPE, f"{rule_label}.when.event must be a non-blank string"))
        elif event not in EVENT_VOCAB:
            errors.append(issue(E_BAD_ENUM,
                                 f"{rule_label}.when.event not in {sorted(EVENT_VOCAB)}: "
                                 f"{safe_repr_for_error(event)}"))
    return errors


def _validate_decided_by(decided_by, rule_label: str) -> list:
    errors = []
    if not isinstance(decided_by, dict):
        return [issue(E_BAD_TYPE, f"{rule_label}.decided_by must be a table")]
    for field in DECIDED_BY_REQUIRED:
        if field not in decided_by:
            errors.append(issue(E_MISSING_KEY, f"{rule_label}.decided_by missing: {field}"))
    unknown = set(decided_by) - set(DECIDED_BY_REQUIRED)
    for key in sorted(unknown):
        errors.append(issue(E_UNKNOWN_KEY,
                             f"{rule_label}.decided_by has unknown key: {safe_repr_for_error(key)}"))
    for field in DECIDED_BY_REQUIRED:
        if field in decided_by:
            value = decided_by[field]
            if not is_nonblank_str(value):
                errors.append(issue(E_BAD_TYPE, f"{rule_label}.decided_by.{field} must be a string"))
            elif value not in DECIDER_VOCAB:
                errors.append(issue(E_BAD_ENUM,
                                     f"{rule_label}.decided_by.{field} not in {sorted(DECIDER_VOCAB)}: "
                                     f"{safe_repr_for_error(value)}"))
    return errors


def _validate_must_update(must_update, rule_label: str) -> list:
    errors = []
    if not isinstance(must_update, list) or not must_update:
        return [issue(E_BAD_TYPE, f"{rule_label}.must_update must be a non-empty array")]
    for i, entry in enumerate(must_update):
        label = f"{rule_label}.must_update[{i}]"
        if not isinstance(entry, dict):
            errors.append(issue(E_BAD_TYPE, f"{label} must be a table"))
            continue
        for field in MUST_UPDATE_REQUIRED:
            if field not in entry:
                errors.append(issue(E_MISSING_KEY, f"{label} missing: {field}"))
        unknown = set(entry) - MUST_UPDATE_ALLOWED
        for key in sorted(unknown):
            errors.append(issue(E_UNKNOWN_KEY, f"{label} has unknown key: {safe_repr_for_error(key)}"))
        if "target" in entry and not is_clean_str(entry["target"]):
            errors.append(issue(E_BAD_TYPE, f"{label}.target must be a non-blank string with no "
                                              f"control character"))
        if "action" in entry:
            action = entry["action"]
            if not is_nonblank_str(action):
                errors.append(issue(E_BAD_TYPE, f"{label}.action must be a string"))
            elif action not in ACTION_VOCAB:
                errors.append(issue(E_BAD_ENUM,
                                     f"{label}.action not in {sorted(ACTION_VOCAB)}: "
                                     f"{safe_repr_for_error(action)}"))
        if "check" in entry and not is_nonblank_str(entry["check"]):
            errors.append(issue(E_BAD_TYPE, f"{label}.check must be a non-blank string"))
        if "note" in entry and not isinstance(entry["note"], str):
            errors.append(issue(E_BAD_TYPE, f"{label}.note must be a string"))
    return errors


def _validate_urgency(urgency, rule_label: str) -> list:
    if not is_nonblank_str(urgency):
        return [issue(E_BAD_TYPE, f"{rule_label}.urgency must be a string")]
    if urgency in ("before-commit", "same-session", "next-maturity-event"):
        return []
    if urgency.startswith("within:"):
        duration = urgency[len("within:"):]
        if not _DURATION_RE.fullmatch(duration):  # .fullmatch: see _valid_repo_id's comment on .match
            return [issue(E_BAD_URGENCY,
                           f"{rule_label}.urgency {safe_repr_for_error(urgency)} is not a valid "
                           f"duration (expected a positive integer followed by d|h|m, e.g. '7d')")]
        unit = duration[-1]
        digits = duration[:-1]
        if len(digits) > MAX_DURATION_DIGITS:
            # Length checked BEFORE int(): see MAX_DURATION_DIGITS's own comment — this branch
            # never calls int() on the oversized string at all. The offending urgency string
            # (potentially carrying an adversarially long digit run) is routed through
            # `safe_repr_for_error`, never interpolated raw, for the same reason.
            return [issue(E_BAD_URGENCY,
                           f"{rule_label}.urgency {safe_repr_for_error(urgency)} exceeds the "
                           f"{MAX_DURATION_DAYS}-day maximum duration")]
        n = int(digits)
        seconds = n * _DURATION_SECONDS[unit]
        if seconds > MAX_DURATION_SECONDS:
            return [issue(E_BAD_URGENCY,
                           f"{rule_label}.urgency {safe_repr_for_error(urgency)} exceeds the "
                           f"{MAX_DURATION_DAYS}-day maximum duration")]
        return []
    return [issue(E_BAD_URGENCY,
                   f"{rule_label}.urgency {safe_repr_for_error(urgency)} is not a closed literal "
                   f"and does not start with 'within:'")]


# A TOML integer is unbounded in principle, but Python's own `json` encoder converts an `int` to
# DECIMAL TEXT to serialize it — and CPython refuses (`ValueError`) a decimal-string conversion
# longer than `sys.get_int_max_str_digits()` (4300 by default) for an integer above this magnitude,
# because that conversion is quadratic in digit count (the same denial-of-service concern
# `_validate_urgency`'s own `MAX_DURATION_DIGITS` guards against — see its comment). A validated-
# clean `derive[]` params table must never let a later `json.dumps` (in the evaluate report's
# `not_evaluated_derivers[].params`) hit that limit and crash to a generic runtime failure (exit
# 65) instead of the documented `E_BAD_DERIVE` validation class (exit 18) — so the bound is
# enforced HERE, at validation time, against the SAME 64-bit signed range `format_fill-contract`'s
# own integer fields use (`-(2**63) <= v <= 2**63-1`; i.e. a signed 64-bit two's-complement range),
# comparing the int directly (`<`/`>`, pure binary comparison) and never converting it to a string
# first — not even to report WHICH value failed, since that would risk the exact decimal-conversion
# failure this check exists to prevent for an adversarially large magnitude; `int.bit_length()` is
# used instead, which never converts to decimal.
MAX_DERIVE_INT = 2 ** 63 - 1
MIN_DERIVE_INT = -(2 ** 63)

DERIVER_PARAM_SPECS = {
    "erlang-xref": {},
    "pinned-sha": {},
    "declared-coverage": {},
    "git-cochange": {
        "min_support": "integer>=1",
        "min_confidence": "number[0,1]",
        "advisory": "boolean",
    },
}

# Requiredness is part of the executable registry, even though every first-cut deriver parameter
# is currently optional.  Keeping it explicit lets the schema/Python drift check fail if either
# side later makes (for example) git-cochange.advisory required without changing the other.
DERIVER_REQUIRED_PARAMS = {name: set() for name in DERIVER_PARAM_SPECS}


def _find_non_json_value(value):
    """ITERATIVE walk (an explicit stack, not a function calling itself once per level of the
    input's own nesting — no recursion over input size) over one already-parsed TOML value.
    Returns a reason-fragment string (meant to read naturally after "params ") for the first value
    found that this format rejects, or None if the whole structure is clean. Three independent
    reasons a TOML value can fail this:
      - TOML has three scalar types JSON does not — `date`, `datetime`, `time` (`tomllib` returns
        real `datetime.date`/`datetime.datetime`/`datetime.time` objects for them) — the stdlib
        `json` encoder raises `TypeError` on any of the three, not a silent stringification.
      - TOML floats include `nan`/`inf`/`-inf` (and any float expression that overflows to one of
        them, e.g. `1e999`) — JSON's own grammar has no token for a non-finite number; the stdlib
        encoder's DEFAULT behaviour (`allow_nan=True`) is to print the bare, non-standard words
        `NaN`/`Infinity`/`-Infinity` into what is supposed to be strictly-standard JSON output —
        every caller of `json.dumps` in this format's tools also passes `allow_nan=False` as a
        second, independent safeguard (see `impact_validate.py`/`impact_evaluate.py`), but the
        right place to catch this is here, so it is a documented validation error, not a
        `json.dumps`-time `ValueError` surfacing at the last possible moment.
      - a TOML integer outside `MIN_DERIVE_INT..MAX_DERIVE_INT` — see that pair's own comment: this
        one is NOT about JSON representability (JSON integers are unbounded) but about Python's own
        decimal-conversion limit when this value is later serialized.
    This is DECIDED policy, not an oversight: this format REJECTS a TOML-only, non-finite, or
    oversized-integer value here, at validation time, rather than normalizing it (e.g. to an
    ISO-8601 string, or clamping a non-finite float or an oversized integer)."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, (date, datetime, time)):
            return (f"contain a {type(v).__name__} value — TOML allows it, but JSON (the report "
                    f"format) does not represent it, and this format does not normalize it; use "
                    f"a string")
        elif isinstance(v, (float, Decimal)) and not math.isfinite(v):
            return (f"contain a non-finite float ({v!r}) value — TOML allows it, but JSON (the "
                    f"report format) does not represent it, and this format does not normalize "
                    f"it; use a string")
        elif isinstance(v, bool):
            continue  # a TOML/JSON boolean, not an int — never subject to the magnitude bound
        elif is_schema_integer(v) and (v < MIN_DERIVE_INT or v > MAX_DERIVE_INT):
            return (f"contain an integer outside the supported "
                    f"{MIN_DERIVE_INT}..{MAX_DERIVE_INT} magnitude (a signed 64-bit range; this "
                    f"value's bit length is {int(v).bit_length()} bits) — Python's own JSON encoder "
                    f"cannot safely convert a decimal string this long, and this format does not "
                    f"normalize it; use a smaller integer or a string")
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return None


def _validate_derive_entry(entry, index: int) -> list:
    """Validate the closed deriver-name and per-deriver parameter registry.

    JSON representability and signed-64-bit integer checks run before the scalar registry checks,
    preserving their independent validation-time guarantee for values nested inside a malformed
    real parameter as well as for ordinary scalar values.
    """
    label = f"derive[{index}]"
    if isinstance(entry, str):
        if entry not in DERIVER_VOCAB:
            return [issue(E_BAD_DERIVE,
                           f"{label} {safe_repr_for_error(entry)} not in {sorted(DERIVER_VOCAB)}")]
        return []
    if isinstance(entry, dict):
        if len(entry) != 1:
            return [issue(E_BAD_DERIVE,
                           f"{label} table form must have exactly one key (the deriver name): "
                           f"{safe_repr_for_error(sorted(entry))}")]
        (name, params), = entry.items()
        errors = []
        if name not in DERIVER_VOCAB:
            errors.append(issue(E_BAD_DERIVE,
                                 f"{label} key {safe_repr_for_error(name)} not in "
                                 f"{sorted(DERIVER_VOCAB)}"))
        name_disp = name if name in DERIVER_VOCAB else safe_repr_for_error(name)
        if not isinstance(params, dict):
            errors.append(issue(E_BAD_TYPE, f"{label}.{name_disp} params must be a table"))
        else:
            reason = _find_non_json_value(params)
            if reason is not None:
                errors.append(issue(E_BAD_DERIVE, f"{label}.{name_disp} params {reason}"))
            if reason is None and name in DERIVER_PARAM_SPECS:
                spec = DERIVER_PARAM_SPECS[name]
                for key in sorted(DERIVER_REQUIRED_PARAMS[name] - set(params)):
                    errors.append(issue(
                        E_BAD_DERIVE,
                        f"{label}.{name} missing required parameter {key}",
                    ))
                for key in sorted(params):
                    key_label = f"{label}.{name}.{key}"
                    if key not in spec:
                        errors.append(issue(
                            E_BAD_DERIVE,
                            f"{key_label} is not an allowed parameter for deriver {name}",
                        ))
                        continue
                    value = params[key]
                    kind = spec[key]
                    valid = True
                    if kind == "integer>=1":
                        valid = is_schema_integer(value) and value >= 1
                    elif kind == "number[0,1]":
                        valid = (isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
                                 and math.isfinite(value) and 0 <= value <= 1)
                    elif kind == "boolean":
                        valid = isinstance(value, bool)
                    if not valid:
                        errors.append(issue(
                            E_BAD_DERIVE,
                            f"{key_label} has invalid value for deriver {name}; expected {kind}",
                        ))
                    elif kind == "integer>=1":
                        params[key] = int(value)
                    elif isinstance(value, Decimal):
                        params[key] = float(value)
        return errors
    return [issue(E_BAD_TYPE, f"{label} must be a string or a single-key table")]


def validate_one_doc(doc: dict, file_label: str) -> list:
    """Shape/enum/glob-grammar validation of ONE loaded rules document, independent of any other
    file (rule-id cross-file uniqueness is checked separately by `validate_merged`)."""
    errors = []
    if not isinstance(doc, dict):
        return [issue(E_BAD_TYPE, f"{file_label}: top level must be a table")]
    for field in TOP_REQUIRED:
        if field not in doc:
            errors.append(issue(E_MISSING_KEY, f"{file_label} missing required field: {field}"))
    unknown = set(doc) - TOP_ALLOWED
    for key in sorted(unknown):
        errors.append(issue(E_UNKNOWN_KEY,
                             f"{file_label} has unknown top-level key: {safe_repr_for_error(key)}"))

    if "impact_version" in doc and not is_nonblank_str(doc["impact_version"]):
        errors.append(issue(E_BAD_TYPE, f"{file_label}.impact_version must be a non-blank string"))
    if "repo" in doc and not _valid_repo_id(doc["repo"]):
        errors.append(issue(E_BAD_TYPE, f"{file_label}.repo is not a valid <repo> id "
                                          f"(^[a-z0-9][a-z0-9_-]*$, 1-64 characters): "
                                          f"{safe_repr_for_error(doc['repo'])}"))

    if "inherit" in doc:
        inherit = doc["inherit"]
        if not isinstance(inherit, list):
            errors.append(issue(E_BAD_TYPE, f"{file_label}.inherit must be an array of strings"))
        else:
            for i, item in enumerate(inherit):
                if not is_nonblank_str(item):
                    errors.append(issue(E_BAD_TYPE,
                                         f"{file_label}.inherit[{i}] must be a non-blank string"))

    if "derive" in doc:
        derive = doc["derive"]
        if not isinstance(derive, list):
            errors.append(issue(E_BAD_TYPE, f"{file_label}.derive must be an array"))
        else:
            for i, item in enumerate(derive):
                errors.extend(_validate_derive_entry(item, i))

    if "rules" in doc:
        rules = doc["rules"]
        if not isinstance(rules, list):
            errors.append(issue(E_BAD_TYPE, f"{file_label}.rules must be an array of tables"))
        else:
            for i, rule in enumerate(rules):
                rule_label = f"{file_label}.rules[{i}]"
                if not isinstance(rule, dict):
                    errors.append(issue(E_BAD_TYPE, f"{rule_label} must be a table"))
                    continue
                for field in RULE_REQUIRED:
                    if field not in rule:
                        errors.append(issue(E_MISSING_KEY, f"{rule_label} missing: {field}"))
                rule_unknown = set(rule) - RULE_ALLOWED
                for key in sorted(rule_unknown):
                    errors.append(issue(E_UNKNOWN_KEY,
                                         f"{rule_label} has unknown key: {safe_repr_for_error(key)}"))

                if "id" in rule and not is_clean_str(rule["id"]):
                    errors.append(issue(E_BAD_TYPE, f"{rule_label}.id must be a non-blank string "
                                                      f"with no control character"))
                if "when" in rule:
                    errors.extend(_validate_when(rule["when"], rule_label))
                if "relation" in rule:
                    relation = rule["relation"]
                    if not is_nonblank_str(relation):
                        errors.append(issue(E_BAD_TYPE, f"{rule_label}.relation must be a string"))
                    elif relation not in RELATION_VOCAB:
                        errors.append(issue(E_BAD_ENUM,
                                             f"{rule_label}.relation not in {sorted(RELATION_VOCAB)}: "
                                             f"{safe_repr_for_error(relation)}"))
                if "must_update" in rule:
                    errors.extend(_validate_must_update(rule["must_update"], rule_label))
                if "decided_by" in rule:
                    errors.extend(_validate_decided_by(rule["decided_by"], rule_label))
                if "importance" in rule:
                    importance = rule["importance"]
                    if not is_nonblank_str(importance):
                        errors.append(issue(E_BAD_TYPE, f"{rule_label}.importance must be a string"))
                    elif importance not in IMPORTANCE_VOCAB:
                        errors.append(issue(E_BAD_ENUM,
                                             f"{rule_label}.importance not in "
                                             f"{sorted(IMPORTANCE_VOCAB)}: "
                                             f"{safe_repr_for_error(importance)}"))
                if "urgency" in rule:
                    errors.extend(_validate_urgency(rule["urgency"], rule_label))
                if "retrigger" in rule:
                    retrigger = rule["retrigger"]
                    if not is_nonblank_str(retrigger):
                        errors.append(issue(E_BAD_TYPE, f"{rule_label}.retrigger must be a string"))
                    elif retrigger not in RETRIGGER_VOCAB:
                        errors.append(issue(E_BAD_ENUM,
                                             f"{rule_label}.retrigger not in "
                                             f"{sorted(RETRIGGER_VOCAB)}: "
                                             f"{safe_repr_for_error(retrigger)}"))
    return errors


def validate_merged(docs: list) -> list:
    """`docs` is a list of (file_label, doc) pairs, in `--rules` argument order. Runs
    `validate_one_doc` on each, then checks rule-id uniqueness ACROSS all of them."""
    errors = []
    for file_label, doc in docs:
        errors.extend(validate_one_doc(doc, file_label))

    seen = {}  # id -> file_label where first seen
    for file_label, doc in docs:
        rules = doc.get("rules")
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rid = rule.get("id")
            if not is_nonblank_str(rid):
                continue
            if rid in seen:
                errors.append(issue(E_DUP_RULE_ID,
                                     f"rule id {safe_repr_for_error(rid)} in {file_label} already "
                                     f"defined in {seen[rid]} (a later --rules file may not "
                                     f"redefine an earlier rule id)"))
            else:
                seen[rid] = file_label
    return errors


def exit_code_for(errors: list) -> int:
    return min((e["code"] for e in errors), default=0)


# ------------------------------------------------------------------------------------------------
# Evaluation
# ------------------------------------------------------------------------------------------------

def glob_match_path(glob_path: str, candidate_path: str) -> bool:
    """Exact glob-to-concrete-path match (NOT glob-covers-glob containment). `glob_path`/
    `candidate_path` are the path parts AFTER any `<repo>:` prefix has been stripped/compared by
    the caller.

    Iterative, not recursive: a dynamic program over (glob segment index, candidate segment
    index), filled from the end of the glob backwards. A recursive segment-by-segment matcher
    consumes one Python stack frame per glob segment (more per '**', since it also branches over
    every possible consumption count) — a long but grammar-valid glob (many segments, in
    particular many '**' segments) must not crash a firing check with a RecursionError. The
    per-segment match itself (`seg_match`) is also not a compiled regex translated from the '*'
    wildcard: a regex built as `.*literal.*literal.*$` can backtrack catastrophically on an
    adversarial candidate; greedy, left-to-right chunk search cannot."""
    g_segs = glob_path.split("/")
    c_segs = candidate_path.split("/")
    n, m = len(g_segs), len(c_segs)

    def seg_match(pattern_seg: str, cand_seg: str) -> bool:
        """One-segment wildcard match: '*' in `pattern_seg` is always a wildcard (any sequence,
        including empty); every character of `cand_seg` — even a literal '*' — is always matched
        literally. Greedy chunk matching: split `pattern_seg` on '*' into a fixed prefix, a fixed
        suffix, and zero or more middle chunks. The prefix/suffix are checked with an anchored
        startswith/endswith; each middle chunk is located with a single left-to-right `str.find`
        from just after the previous match — never rescanning from the start, never asking a
        regex engine to backtrack. Taking the greedy, left-to-right EARLIEST match for every
        middle chunk is correct for a '*'-only wildcard (no '?'): a later placement of one chunk
        never lets a LATER chunk be found any further left than the earliest placement would."""
        if "*" not in pattern_seg:
            return pattern_seg == cand_seg
        chunks = pattern_seg.split("*")
        first, last = chunks[0], chunks[-1]
        middles = chunks[1:-1]
        if first and not cand_seg.startswith(first):
            return False
        if last and not cand_seg.endswith(last):
            return False
        pos = len(first)
        for mid in middles:
            if mid == "":
                continue
            idx = cand_seg.find(mid, pos)
            if idx == -1:
                return False
            pos = idx + len(mid)
        return pos <= len(cand_seg) - len(last)

    # dp[ci] == True iff g_segs[gi:] matches c_segs[ci:], for the gi currently being filled in,
    # starting at gi == n (glob exhausted: matches only if the candidate is exhausted too).
    dp = [ci == m for ci in range(m + 1)]
    for gi in range(n - 1, -1, -1):
        seg = g_segs[gi]
        new_dp = [False] * (m + 1)
        if seg == "**":
            new_dp[m] = dp[m]
            for ci in range(m - 1, -1, -1):
                new_dp[ci] = dp[ci] or new_dp[ci + 1]
        else:
            for ci in range(m):
                new_dp[ci] = seg_match(seg, c_segs[ci]) and dp[ci + 1]
            # new_dp[m] stays False: a literal segment has no candidate segment left to consume.
        dp = new_dp
    return dp[0]


def _split_repo_prefix(glob: str):
    """Returns (repo_or_None, path_part) for one `when.changed[]` glob, already grammar-validated."""
    return _split_cross_repo(glob)


def _split_changed_path(path: str):
    """Returns (repo_or_None, path_part) for one `--changed`/`--changed-file` entry, already
    grammar-validated by `validate_changed_paths` before this is ever called."""
    return _split_cross_repo(path)


def glob_matches_any(glob: str, changed_paths: list) -> list:
    """All changed_paths entries a single (already grammar-valid) glob matches, sorted."""
    glob_repo, glob_path = _split_repo_prefix(glob)
    hits = []
    for cp in changed_paths:
        cp_repo, cp_path = _split_changed_path(cp)
        if glob_repo is None:
            if cp_repo is not None:
                continue
            if glob_match_path(glob_path, cp_path):
                hits.append(cp)
        else:
            if cp_repo != glob_repo:
                continue
            if glob_match_path(glob_path, cp_path):
                hits.append(cp)
    return sorted(set(hits))


def _urgency_rank(urgency: str) -> tuple:
    if urgency == "before-commit":
        return (0, 0)
    if urgency == "same-session":
        return (1, 0)
    if urgency.startswith("within:"):
        duration = urgency[len("within:"):]
        unit = duration[-1]
        # `int()` here is safe: `evaluate()` (this function's only caller) runs only after
        # `validate_merged` has gated on every rule's `urgency` already passing
        # `_validate_urgency`'s MAX_DURATION_DIGITS length check.
        n = int(duration[:-1])
        return (2, n * _DURATION_SECONDS[unit])
    if urgency == "next-maturity-event":
        return (3, 0)
    return (99, 0)  # unreachable once validated


def evaluate(docs: list, changed_paths: list, events: list) -> dict:
    """`docs`: list of (file_label, doc) pairs, already validated (caller's responsibility — rules
    validity gates evaluation). `changed_paths`: already validated by `validate_changed_paths`
    (caller's responsibility — a malformed entry is an input error, not something this function
    sees). Returns `{"fired": [...], "not_evaluated_derivers": [...]}`."""
    fired = []
    events_set = set(events)
    for file_label, doc in docs:
        for rule in doc.get("rules", []):
            when = rule["when"]
            matched = None
            if "changed" in when:
                hit_map = {}
                for glob in when["changed"]:
                    hits = glob_matches_any(glob, changed_paths)
                    if hits:
                        hit_map[glob] = hits
                if hit_map:
                    all_hits = sorted({p for hits in hit_map.values() for p in hits})
                    matched = {"changed_paths": all_hits}
            else:
                event = when["event"]
                if event in events_set:
                    matched = {"event": event}
            if matched is None:
                continue
            for mu in rule["must_update"]:
                entry = {
                    "rule_id": rule["id"],
                    "origin": "declared",
                    "target": mu["target"],
                    "action": mu["action"],
                    "decided_by": dict(rule["decided_by"]),
                    "importance": rule["importance"],
                    "urgency": rule["urgency"],
                    "retrigger": rule.get("retrigger", "every-event"),
                    "relation": rule["relation"],
                    "trigger": {"rule_id": rule["id"], "when": dict(when), "matched": matched},
                }
                if "check" in mu:
                    entry["check"] = mu["check"]
                if "note" in mu:
                    entry["note"] = mu["note"]
                fired.append(entry)

    fired.sort(key=lambda e: (IMPORTANCE_RANK[e["importance"]], _urgency_rank(e["urgency"]),
                               e["rule_id"], e["target"]))

    not_evaluated = []
    for file_label, doc in docs:
        for item in doc.get("derive", []):
            if isinstance(item, str):
                not_evaluated.append({"deriver": item, "status": "not-evaluated",
                                       "note": "derivers are validated against the typed registry "
                                               "but not evaluated; no deriver logic runs"})
            else:
                (name, params), = item.items()
                not_evaluated.append({"deriver": name, "status": "not-evaluated", "params": params,
                                       "note": "derivers are validated against the typed registry "
                                               "but not evaluated; no deriver logic runs"})

    return {"fired": fired, "not_evaluated_derivers": not_evaluated}


def project_obligation(fired_entry: dict, now: datetime) -> dict:
    """Builds a REPORT-ONLY summary of one fired entry, shaped like a small subset of a
    `format_obligation` duty record's fields (`trigger`, `action`, `target`, `required_by`,
    `discharge_predicate`, `retrigger`, `priority`). This is NOT a `format_obligation` duty record
    and does not conform to that format — it carries none of that format's own required identity/
    provenance/status fields (an id, a natural key, a scope, a source, created_by/created_at,
    occurrences, status, …). A caller that wants a real duty record must build one separately,
    using this summary as input, not by relabeling it. `now` is an aware UTC datetime."""
    urgency = fired_entry["urgency"]
    if urgency.startswith("within:"):
        duration = urgency[len("within:"):]
        unit = duration[-1]
        # `int()` here is safe: `fired_entry` was built by `evaluate()` from a rule whose
        # `urgency` already passed `_validate_urgency`'s MAX_DURATION_DIGITS length check (rules
        # validity gates evaluation).
        n = int(duration[:-1])
        seconds = n * _DURATION_SECONDS[unit]
        required_by = (now + timedelta(seconds=seconds)).isoformat()
    else:
        required_by = urgency  # symbolic: before-commit | same-session | next-maturity-event

    if "check" in fired_entry:
        discharge_predicate = {"kind": "command", "command": fired_entry["check"]}
    else:
        discharge_predicate = {"kind": "recorded-judgment", "fix": fired_entry["decided_by"]["fix"]}

    return {
        "trigger": fired_entry["trigger"],
        "action": fired_entry["action"],
        "target": fired_entry["target"],
        "required_by": required_by,
        "discharge_predicate": discharge_predicate,
        "retrigger": fired_entry["retrigger"],
        "priority": fired_entry["importance"],
    }


def parse_now(value: str) -> datetime:
    """Parses `--now` against ONE strict, documented ISO-8601 UTC form (`_NOW_RE`, checked with
    `.fullmatch` — never `.strip()`ped first: a leading/trailing space or newline is a grammar
    failure, not silently trimmed, the same full-string-grammar contract every other field in this
    format uses). Raises `ValueError` (the caller turns this into a usage error, exit 64) on: a
    grammar mismatch, an otherwise-well-formed but impossible calendar date/time (e.g. month 13),
    a year past `MAX_NOW_YEAR`, or a timezone conversion that overflows/underflows the
    representable `datetime` range (e.g. `0001-01-01T00:00:00+01:00`, which underflows converting
    to UTC; `9999-12-31T23:59:59-01:00`, which overflows) — `datetime.astimezone` raises
    `OverflowError` for exactly this, a DIFFERENT exception type than the `ValueError` every other
    failure here raises, so it is caught explicitly and re-raised as one, rather than left to reach
    a caller that only expects `ValueError` and would otherwise let it escape as an uncaught
    runtime failure."""
    if not _NOW_RE.fullmatch(value):
        raise ValueError(f"--now {safe_repr_for_error(value)} does not match the required form "
                          f"YYYY-MM-DDTHH:MM:SS(Z|+HH:MM|-HH:MM)")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"--now {safe_repr_for_error(value)} is not a valid calendar date/time: {exc}")
    try:
        dt = dt.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"--now {safe_repr_for_error(value)} is out of range once converted to UTC: {exc}")
    # Bounded together with MAX_DURATION_DAYS (see its own comment): this is what makes
    # project_obligation's `now + timedelta(days=<=MAX_DURATION_DAYS)` provably unable to ever
    # reach datetime's year-9999 ceiling, rather than merely unlikely to.
    if dt.year > MAX_NOW_YEAR:
        raise ValueError(f"--now {safe_repr_for_error(value)} has a year past {MAX_NOW_YEAR} "
                          f"(the maximum this format supports)")
    return dt
