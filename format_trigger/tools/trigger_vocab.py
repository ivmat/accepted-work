"""trigger_vocab.py — shared, stdlib-only, IMPORTABLE core for `trigger_validate.py` and
`trigger_fire.py`. This module is also the canonical home for the two pieces a conformance gate
would need to import directly: `EVENT_VOCAB` (the closed event -> allowed/required-params table)
and the entity-ref parse/contains functions.

Implements:
  - The entity-ref grammar and containment rule (§1 below).
  - The closed event vocabulary and per-event params shape/enum checking (§2, §3.7, §4 below).
  - TOML loading of one trigger file (tomllib; stdlib, Python 3.11+).
  - Shape/enum/grammar validation of ONE trigger file (§3 below).
  - EVENT record shape/enum validation (§4 below) — the same per-event params rule as a trigger's
    own `when.params` (§3.7), reused unchanged.
  - Glob grammar checking and exact glob-to-path matching (§5 below).
  - The firing rule (§6 below), occurrence_id derivation, and the duplicate/once-vs-every-event
    suppression rule (§7 below).

This module raises nothing on its own for a caller's convenience — `validate_trigger_doc()` and
`validate_event_record()` return lists of `{code, reason}` / plain-string issues; callers
(`trigger_validate.py` / `trigger_fire.py`) decide exit codes and JSON shape. `load_toml_file()`
DOES raise (`TriggerInputError`) for I/O/parse failures.

Stdlib only: tomllib, hashlib, re, datetime, pathlib.

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

import hashlib
import itertools
import io
import json
import math
import os
import re
import select
import sys
import tomllib
from decimal import Decimal
from datetime import datetime, timezone
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

TRIGGER_VOCAB_VERSION = "trigger/first-cut-0.1.0-draft"

# ---- exit-code classes ----
E_BAD_TOP = 10
E_BAD_ID = 11
E_BAD_ENTITY_REF = 12
E_BAD_EVENT = 13
E_BAD_PARAMS = 14
E_BAD_RETRIGGER = 15
E_BAD_GLOB_OR_DURATION = 16
E_USAGE = 64
E_INPUT = 65
E_OUTPUT = 74
E_SELFTEST = 99

# ---- grammars (§1, §3.3, §5, §2 below) ----
REPO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _valid_repo_id(repo) -> bool:
    # .fullmatch, not .match: Python's trailing `$` matches just BEFORE a final '\n', so
    # .match("acme\n") on a `...$` pattern wrongly succeeds.
    return isinstance(repo, str) and 1 <= len(repo) <= 64 and bool(REPO_ID_RE.fullmatch(repo))


TRIGGER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.*-]+$")
DURATION_RE = re.compile(r"^[1-9][0-9]*[dhm]$")
_CONTROL_CHARS = {chr(c) for c in range(0x20)} | {chr(0x7F)}

RETRIGGER_VOCAB = {"once", "every-event"}

# Signed 64-bit range for every integer-valued field this format has (today, only `n` on
# commits-at-least/days-since-setup): the same bound format_impact-rule's own
# `impact_rules.MAX_DERIVE_INT`/`MIN_DERIVE_INT` use, for the same reason — Python's own `json`
# encoder converts an `int` to decimal text to serialize it, and CPython refuses a conversion whose
# digit count crosses `sys.get_int_max_str_digits()` (4300 by default; quadratic-cost guard). A
# value here is compared by magnitude (`<`/`>`, pure binary comparison), never converted to a string
# first — not even to report which value failed (`safe_repr_for_error` below uses `bit_length()`
# instead of `repr()` for exactly this reason).
MAX_INT64 = 2 ** 63 - 1
MIN_INT64 = -(2 ** 63)

# A trigger id's own length: the grammar (`TRIGGER_ID_RE`) bounds the CHARACTER SET but not the
# LENGTH — an id of, say, 100,000 grammar-valid characters is accepted by the regex alone and would
# then be echoed in full in a duplicate-id diagnostic. Authored ids are short by nature; this is a
# sanity ceiling, not a limit any real id approaches.
TRIGGER_ID_MAX_LENGTH = 256

# An entity-ref's own overall length, checked before any further parsing: the grammar below has no
# length ceiling of its own (it is a structural grammar, not a size limit), so an adversarially long
# `when.source` or EVENT `source` value would otherwise be echoed in full by a grammar-failure
# message. Comfortably above any real repo-relative path a real caller would ever author.
ENTITY_REF_MAX_LENGTH = 4096

# A `cadence` trigger's `every` duration string's digit run is bounded the same way
# format_impact-rule's own `impact_rules.MAX_DURATION_DIGITS` bounds its `within:<duration>` urgency
# field: this module never itself converts the digits to a number (`params_match` compares `every`
# as an opaque string), but an absurdly long digit run is already a malformed, oversized value, not
# merely a slow one to compare — and any future caller that DOES convert it must not inherit an
# unbounded digit run from here.
MAX_DURATION_DIGITS = 15

# ---- closed event vocabulary (§2 below): event -> {param_name: "required"|"optional"} ----
EVENT_VOCAB = {
    "path-changed": {"paths": "required"},
    "instantiated": {},
    "first-src-commit": {"src_glob": "optional"},
    "first-test": {},
    "first-spec": {},
    "first-dependency": {},
    "new-dependency": {},
    "first-release": {},
    "public": {},
    "template-tag-moved": {},
    "commits-at-least": {"n": "required"},
    "days-since-setup": {"n": "required"},
    "session-closed": {},
    "session-started": {},
    "rule-added": {},
    "cadence": {"every": "required"},
}

# ---- per-param shape (§3.7, §5 below) ----
PARAM_TYPE = {
    "paths": "glob_list",
    "n": "positive_int",
    "every": "duration",
    "src_glob": "glob_string",
}

TOP_REQUIRED = ("trigger_version", "repo", "triggers")
TOP_ALLOWED = set(TOP_REQUIRED)

TRIGGER_REQUIRED = ("id", "when", "retrigger")
TRIGGER_ALLOWED = set(TRIGGER_REQUIRED)

WHEN_REQUIRED = ("source", "event")
WHEN_ALLOWED = set(WHEN_REQUIRED) | {"params"}

EVENT_RECORD_REQUIRED = ("event_id", "event", "source", "at", "params")
EVENT_RECORD_ALLOWED = set(EVENT_RECORD_REQUIRED)


class TriggerInputError(Exception):
    """I/O or TOML/JSON-syntax failure reading one input file (exit 65)."""

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


def is_nonblank_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ------------------------------------------------------------------------------------------------
# Bounded diagnostics — ported from format_impact-rule's own `impact_rules.py` (same names, same
# algorithms, same bounds), the reference implementation this family's other formats share. Every
# issue `reason` this format's tools ever report is bounded on two independent axes: a single
# interpolated VALUE is bounded by `safe_repr_for_error`/`bound_path_preview` at the call site, and
# the FINAL assembled text is bounded again, unconditionally, by `bound_reason` in each tool's one
# report constructor — so the bound holds even for a call site that forgot to self-bound.
# ------------------------------------------------------------------------------------------------
_SAFE_REPR_MAX_CHARS = 1000
_SAFE_REPR_MAX_ITEMS = 500
_SAFE_REPR_STR_PREVIEW_CHARS = 200

# The DOCUMENTED diagnostic bound: no single reported issue's `reason` this format's tools ever
# emit is longer than this many characters, for ANY input, including an adversarial one. Byte-for-
# byte the same bound, for the same reason, as format_impact-rule's own
# `impact_rules.DIAGNOSTIC_REASON_MAX_CHARS` (and format_fill-contract's own
# `fill_common.DIAGNOSTIC_REASON_MAX_CHARS`).
DIAGNOSTIC_REASON_MAX_CHARS = 2000


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
    the one place every bounded-text helper below enforces its own cap, so the marker's own length
    can never itself push a result past the cap it exists to enforce. The cut point is also backed
    off (`_backoff_escape_boundary`) so it never splits a `\\uXXXX` escape sequence in half.
    Byte-for-byte the same helper (and the same reasoning) as format_impact-rule's own
    `impact_rules._cap_with_marker`."""
    keep = max(max_chars - len(marker), 0)
    keep = _backoff_escape_boundary(s, keep)
    return s[:keep] + marker


def _safe_repr_is_container(v) -> bool:
    return isinstance(v, (list, tuple, dict, set, frozenset))


def _safe_repr_scalar(v) -> str:
    """`repr(v)` for any non-container value, bounded on two independent axes, NEITHER of which
    ever calls `repr()`/`str()` on more of `v` than a fixed prefix: an oversized Python `int`
    (`bit_length() > 64`) would make `repr()`/`str()` ITSELF raise once past CPython's own
    integer-to-string conversion guard, so it is replaced by a cheap digit-count ESTIMATE from the
    bit length, never by stringifying `v`; a `str` longer than `_SAFE_REPR_STR_PREVIEW_CHARS` is
    sliced to that many characters BEFORE `repr()` is called on it. Byte-for-byte the same algorithm
    as format_impact-rule's own `impact_rules._safe_repr_scalar`."""
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
    foreign value this format's tools read — a TOML/JSON scalar (an oversized integer included), or
    a CONTAINER (list/tuple/dict/set) nesting one, to any depth. Byte-for-byte the same algorithm as
    format_impact-rule's own `impact_rules.safe_repr_for_error` (see its docstring for the full
    rationale): an explicit, heap-allocated work stack (never Python function recursion, so an
    adversarially DEEP container cannot exhaust the interpreter's own recursion limit), walking each
    container's elements through `itertools.islice` (never materializing a container in full first).
    At most `_SAFE_REPR_MAX_ITEMS` values are rendered, and the assembled text is hard-truncated to
    `_SAFE_REPR_MAX_CHARS` characters (plus a trailing `...>` marker) before this returns."""
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
        text = _cap_with_marker(text, _SAFE_REPR_MAX_CHARS)
    return text


_REASON_TRUNCATION_MARKER = "...<truncated>"


def bound_reason(text) -> str:
    """The FINAL, STRUCTURAL enforcement point for the documented diagnostic bound: returns `text`
    (coerced to `str` first if it is not already one) truncated, if necessary, so the RETURNED
    string is NEVER longer than `DIAGNOSTIC_REASON_MAX_CHARS` characters, including the truncation
    marker it appends — for ANY input, of any length, built through ANY path. This is the one place
    EVERY issue `reason` this format's tools ever report is checked again, unconditionally,
    immediately before it leaves a report constructor — so the bound holds even for a call site
    that forgot to self-bound. Byte-for-byte the same helper (and the same reasoning) as
    format_impact-rule's own `impact_rules.bound_reason`."""
    if not isinstance(text, str):
        text = str(text)
    # Escaped BEFORE the length check/cap, not after — see `impact_rules.bound_reason`'s own
    # a usage-error reason built straight from argparse's own message
    # (which embeds the raw, `surrogateescape`-decoded argv text) must never carry a lone surrogate
    # past this, the final, unconditional backstop.
    text = _escape_unicode_safe(text)
    if len(text) <= DIAGNOSTIC_REASON_MAX_CHARS:
        return text
    return _cap_with_marker(text, DIAGNOSTIC_REASON_MAX_CHARS, marker=_REASON_TRUNCATION_MARKER)


def bound_path_preview(path) -> str:
    """Returns a short, BOUNDED preview of a filesystem path (or any other path-like value: a
    `pathlib.Path`, or a raw CLI string) safe to interpolate DIRECTLY into an error message — for
    ANY input, including an adversarially long `--triggers`/`--events`/`--history` argument (a
    10,000-character string). Truncated to `_SAFE_REPR_STR_PREVIEW_CHARS` characters (plus a
    trailing marker if truncated) BEFORE any further formatting. Call this at every site that
    interpolates a path value directly, and in place of an exception's own `str(exc)` wherever that
    exception is known to embed the same (or a derived) path in full — use the exception's CLASS
    name alongside this bounded preview instead, never the exception's unbounded `str()`.
    Byte-for-byte the same helper (and the same reasoning) as format_impact-rule's own
    `impact_rules.bound_path_preview`, PLUS one more guarantee neither that docstring nor the
    original implementation had: a CLI argument is decoded by Python from the OS's raw bytes using
    `surrogateescape` (e.g. a POSIX filename byte `\\xff` with no valid UTF-8 interpretation becomes
    the Python string `"\\udcff"`, a LONE UTF-16 surrogate) — `str(path)` on such a value is still a
    `str`, but one that cannot itself encode as UTF-8, so interpolating it RAW into a message would
    put a string this format's own reports can never actually serialize into. Every such character
    is replaced with a visible, UTF-8-safe backslash escape here, BEFORE the length cap below, so the
    cap still holds on the ESCAPED text (escaping can only lengthen a string, never shorten it —
    capping first and escaping after could let a result exceed the cap)."""
    s = str(path)
    s = s.encode("utf-8", "backslashreplace").decode("utf-8")
    if len(s) <= _SAFE_REPR_STR_PREVIEW_CHARS:
        return s
    return _cap_with_marker(s, _SAFE_REPR_STR_PREVIEW_CHARS)


def _find_non_finite(value):
    """ITERATIVE walk (an explicit stack, never recursion over input size) over one already-parsed
    TOML value. Returns a reason-fragment string for the first non-finite float found (`nan`, `inf`,
    `-inf`, or any float expression that overflows to one of them, e.g. `1e400`), or None if the
    whole structure is clean, at any depth and in any container. TOML's float grammar allows all
    three; JSON's grammar has none of them, and this format's own JSON/JSONL input (events/history,
    see `find_json_input_defect` below) is checked by the same rule. A non-finite value is rejected
    at LOAD time, as a plain input error, rather than carried through to a `json.dumps` call that
    would otherwise print the bare, non-standard words `NaN`/`Infinity`/`-Infinity` into what must be
    strictly-standard JSON output (every `json.dumps` call in this format's tools also passes
    `allow_nan=False` as a second, independent safeguard)."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, bool):
            continue
        if isinstance(v, (float, Decimal)) and not math.isfinite(v):
            return f"contains a non-finite number (not allowed): {v!r}"
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return None


def load_toml_file(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # `str(exc)` on an `OSError` embeds its own `filename` argument IN FULL (the same value
        # `path` already names) — the exception's CLASS name plus one bounded path preview, never
        # `str(exc)` itself, keeps this message's length independent of how long a `--triggers`
        # argument happens to be.
        raise TriggerInputError(f"cannot read {bound_path_preview(path)}: {type(exc).__name__}")
    try:
        doc = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except UnicodeDecodeError as exc:
        raise TriggerInputError(f"not valid UTF-8 ({bound_path_preview(path)}): {exc}")
    except tomllib.TOMLDecodeError as exc:
        raise TriggerInputError(f"TOML syntax error ({bound_path_preview(path)}): {exc}")
    except ValueError as exc:
        # tomllib's own integer parsing calls Python's int() on a decimal literal, which refuses a
        # digit run longer than sys.get_int_max_str_digits() (4300 by default) with a plain
        # ValueError, not TOMLDecodeError — that conversion is quadratic in digit count, so a
        # 5000-digit literal is a malformed/oversized INPUT (this file cannot even be parsed into a
        # document), reported here as the same clean, documented input-error class every other
        # unparseable file already uses — never an uncaught traceback, and never silently reaching
        # this tool's own generic catch-all with a less specific message. CPython's own message here
        # names the digit COUNT, never the offending digits, so it is already safe to interpolate.
        raise TriggerInputError(f"TOML parse failure ({bound_path_preview(path)}): {exc}")
    if not isinstance(doc, dict):
        raise TriggerInputError(f"top level of {bound_path_preview(path)} must be a TOML table")
    defect = _find_non_finite(doc)
    if defect is not None:
        raise TriggerInputError(f"{bound_path_preview(path)}: {defect}")
    return doc


# ---- the JSON input contract every JSON/JSONL record in this format is checked against ----
# (a) no duplicate object keys; (b) no NaN/Infinity/-Infinity; (c) every string, key or value,
# must encode as UTF-8; (d) nesting no deeper than MAX_JSON_DEPTH. All four are checked at LOAD
# time (load_jsonl_file), not left for whichever later step happens to trip over the record —
# and none of the checking is itself recursive, so a maliciously (or just very) deep input cannot
# turn a should-be-rejected record into a crash.
MAX_JSON_DEPTH = 64


def unique_pairs(pairs: list) -> dict:
    """`object_pairs_hook` for `json.loads`: rejects a duplicate key instead of silently keeping
    only the last value for it (`json.loads`'s own default `dict(pairs)` behavior). A duplicate
    key lets a document carry two values for one field: a reader that inspects the first and a
    plain dict() build that keeps the last would disagree about what the document says, which is
    one way to slip a value (e.g. a lone surrogate) past a check of the expected field."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    """`parse_constant` for `json.loads`: JSON's grammar has no NaN/Infinity/-Infinity, but
    Python's decoder accepts them anyway (`json.loads("NaN")` succeeds) unless this hook is set —
    reject them, rather than let a non-JSON numeric constant into what should be a
    strictly-standard JSON parse."""
    raise ValueError(f"non-JSON numeric constant: {value}")


def find_json_input_defect(obj, max_depth: int = MAX_JSON_DEPTH):
    """ITERATIVE walk (an explicit stack, not a function calling itself once per level of the
    input's own nesting) over one parsed JSON value. Returns a reason string for the first defect
    found, or None if the whole structure is clean: a string (a dict KEY or a VALUE, at any
    nesting depth) that cannot encode as UTF-8 (`json.loads` accepts a `\\uXXXX` escape for a lone
    UTF-16 surrogate, U+D800-U+DFFF — valid JSON syntax, but not a valid Unicode scalar value; the
    decoded Python `str` can hold it, but nothing that later encodes the string to bytes can), or
    nesting deeper than `max_depth` (undocumented-if-unbounded nesting is itself a defect this
    format rejects, not only a possible route to a stack-depth crash in a less careful walk).

    The top-level value is depth 1; each dict/list nested one level inside another adds 1."""
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
            # JSON's grammar has no token for a non-finite number, but `json.loads` happily parses
            # a finite-looking literal that OVERFLOWS to one (e.g. `1e400` -> `inf`) without ever
            # calling `parse_constant` (that hook only fires for the literal `NaN`/`Infinity`/
            # `-Infinity` tokens `unique_pairs`'s caller already rejects via `reject_constant`) —
            # checked here too, at any depth, so an overflowing float cannot slip past both guards.
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
                           "fired": None, "suppressed": None, "now": None,
                           "trigger_vocab_version": TRIGGER_VOCAB_VERSION}}
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
    """Run one Trigger CLI at the stdout byte boundary.

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


def load_jsonl_file(path: Path) -> list:
    """Reads a JSON-Lines file: one JSON object per non-blank line, split on LF ONLY (never on a
    Unicode line separator such as U+2028/U+2029, which `str.splitlines()` would wrongly treat as
    a record boundary even though it is valid, unescaped JSON string content). A single trailing
    CR per line (CRLF) is accepted and stripped; a bare CR is rejected as a record separator.
    Raises TriggerInputError on any I/O failure, JSON syntax error, a duplicate object key, a
    NaN/Infinity/-Infinity numeric constant, a line that does not decode to a JSON object, or a
    JSON-input-contract defect found by `find_json_input_defect` (an unencodable string or
    excessive nesting) — checked once here, at load time. A `RecursionError` from the parse itself
    (on sufficiently deep, if unusual, input) is caught and reported the same way, never left to
    surface as a crash."""
    import json

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TriggerInputError(f"cannot read {bound_path_preview(path)}: {type(exc).__name__}")
    lines = raw.split(b"\n")
    records = []
    for lineno, line in enumerate(lines, start=1):
        if lineno < len(lines) and line.endswith(b"\r"):
            line = line[:-1]
        if b"\r" in line:
            raise TriggerInputError(
                f"{bound_path_preview(path)}:{lineno}: bare CR is not a JSONL record separator")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TriggerInputError(f"{bound_path_preview(path)}:{lineno}: not valid UTF-8: {exc}")
        if not text.strip():
            continue
        try:
            obj = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        except json.JSONDecodeError as exc:
            raise TriggerInputError(f"{bound_path_preview(path)}:{lineno}: not valid JSON: {exc}")
        except (ValueError, RecursionError) as exc:
            # Includes a decimal-integer literal past Python's own int() digit-count guard (a
            # 5000-digit event/history field) — `json.loads`'s own ValueError for that case never
            # embeds the offending digits, only the digit count, so `{exc}` is already safe here.
            raise TriggerInputError(f"{bound_path_preview(path)}:{lineno}: invalid JSON input ({exc})")
        if not isinstance(obj, dict):
            raise TriggerInputError(
                f"{bound_path_preview(path)}:{lineno}: each line must decode to a JSON object")
        defect = find_json_input_defect(obj)
        if defect is not None:
            raise TriggerInputError(f"{bound_path_preview(path)}:{lineno}: {defect}")
        records.append(obj)
    return records


def parse_at(value) -> datetime:
    """ISO-8601 UTC parser (Z suffix or an explicit UTC offset). Raises ValueError on failure —
    including a value whose UTC-offset conversion overflows datetime's representable range (e.g.
    a year-0001 date with a positive offset), which `datetime.astimezone` reports as
    OverflowError, not ValueError; callers that only catch ValueError must still see one error
    type from this function."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-blank ISO-8601 timestamp string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"{safe_repr_for_error(value)} has no UTC offset/Z suffix")
    try:
        return dt.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"{safe_repr_for_error(value)} is out of range once converted to UTC: {type(exc).__name__}")


# ------------------------------------------------------------------------------------------------
# Entity-ref grammar, parsing, containment (§1 below)
# ------------------------------------------------------------------------------------------------

def _validate_path_grammar(path: str):
    """Returns (ok, reason_or_None) for the <path> part of a file/subtree entity-ref, AFTER any
    trailing '/' (subtree marker) has already been stripped by the caller."""
    if path == "":
        return False, "empty path"
    if path.startswith("/"):
        return False, "leading '/' not allowed"
    if "\\" in path:
        return False, "backslash not allowed"
    if any(ch in _CONTROL_CHARS for ch in path):
        return False, "control character not allowed"
    if "*" in path:
        return False, "'*'/'**' not allowed in an entity-ref path (globs belong to params, not refs)"
    for seg in path.split("/"):
        if seg == "":
            return False, "empty path segment (double slash or trailing slash)"
        if seg in (".", ".."):
            return False, f"'{seg}' path segment not allowed"
    return True, None


def _malformed_resource_part(value: str) -> bool:
    """The relation format owns this grammar; this is an exact copy of its resource <name>/<host>
    check, so both tools accept/reject the same strings: each component must be non-blank (not
    merely non-empty — a lone space is not a name), and forbidden are '/', '*', ':' (and, for
    <host> only, '@' — checked by the caller)."""
    return not is_nonblank_str(value) or "/" in value or "*" in value or ":" in value


def _parse_repo_relative(ref, repo, rest):
    """`rest` is whatever followed `<repo>:` or `@self:` — one of the whole-repo `**` marker, a
    subtree (trailing `/`), or a plain file path. `repo` is already a validated <repo> id (the
    caller resolved it, whether from the ref's own `<repo>:` prefix or from `self_repo`); this
    function parses the SUFFIX only and never re-parses `ref` as a whole string again — doing that
    would let a `repo` value that happens to spell a marker word (e.g. a repo literally named
    "resource") make `f"{repo}:{rest}"` collide with the unrelated `resource:<name>@<host>` form."""
    if rest == "":
        return False, None, f"entity-ref has an empty path after '<repo>:': {safe_repr_for_error(ref)}"
    if rest == "**":
        return True, {"kind": "whole-repo", "repo": repo, "raw": ref}, None
    is_dir = rest.endswith("/")
    path = rest[:-1] if is_dir else rest
    ok, reason = _validate_path_grammar(path)
    if not ok:
        return False, None, f"entity-ref path is invalid: {safe_repr_for_error(ref)} ({reason})"
    kind = "subtree" if is_dir else "file"
    return True, {"kind": kind, "repo": repo, "path": path, "raw": ref}, None


def parse_entity_ref(ref, self_repo=None):
    """Returns (ok, parsed_dict_or_None, reason_or_None). `parsed_dict` has at least a "kind" key:
    one of "whole-repo" | "subtree" | "file" | "resource". `self_repo`, if given, resolves a
    leading '@self'/'@self:<path>' form against that repo id — pass None (the default)
    to make '@self' itself an error (this is what an EVENT record must do)."""
    if not isinstance(ref, str) or ref == "":
        return False, None, "entity-ref must be a non-empty string"
    if len(ref) > ENTITY_REF_MAX_LENGTH:
        return False, None, (f"entity-ref exceeds the {ENTITY_REF_MAX_LENGTH}-character limit "
                              f"({len(ref)} characters): {safe_repr_for_error(ref)}")
    if "\\" in ref:
        return False, None, f"entity-ref must not contain a backslash: {safe_repr_for_error(ref)}"
    if any(ch in _CONTROL_CHARS for ch in ref):
        return False, None, f"entity-ref must not contain a control character: {safe_repr_for_error(ref)}"

    if ref == "@self" or ref.startswith("@self:"):
        if self_repo is None:
            return False, None, ("'@self' entity-ref requires a resolving repo (the trigger "
                                  "file's own 'repo' field); none available here")
        if not _valid_repo_id(self_repo):
            # Every real caller already validates the trigger file's own 'repo' field before
            # this point (validate_trigger_doc only assigns self_repo once that check passes) —
            # this is a second, defense-in-depth check, so this function's own contract does not
            # depend on trusting a caller.
            return False, None, f"'@self' has no valid resolving repo: {safe_repr_for_error(self_repo)}"
        if ref == "@self":
            return True, {"kind": "whole-repo", "repo": self_repo, "raw": ref}, None
        # Parse the suffix as repo-relative directly, with self_repo already assigned — never
        # rebuild "self_repo:suffix" as a plain string and re-dispatch it through this same
        # function, which cannot tell that string's "<repo>:" apart from the "resource:" marker.
        return _parse_repo_relative(ref, self_repo, ref[len("@self:"):])

    if ref.startswith("resource:"):
        rest = ref[len("resource:"):]
        # exactly one '@': rpartition on the LAST '@' silently folded an earlier one into <name>
        # instead of being rejected — relation_vocab.py's resource parser applies this identical
        # rule.
        if rest.count("@") != 1:
            return False, None, (f"resource entity-ref must contain exactly one '@' "
                                  f"(want resource:<name>@<host>): {safe_repr_for_error(ref)}")
        name, host = rest.split("@")
        if _malformed_resource_part(name):
            return False, None, f"resource entity-ref has a malformed name: {safe_repr_for_error(ref)}"
        if _malformed_resource_part(host):
            return False, None, f"resource entity-ref has a malformed host: {safe_repr_for_error(ref)}"
        return True, {"kind": "resource", "name": name, "host": host, "raw": ref}, None

    if ":" not in ref:
        if not _valid_repo_id(ref):
            return False, None, f"bare entity-ref is not a valid <repo>: {safe_repr_for_error(ref)}"
        return True, {"kind": "whole-repo", "repo": ref, "raw": ref}, None

    repo, _, rest = ref.partition(":")
    if not _valid_repo_id(repo):
        return False, None, f"entity-ref repo prefix is invalid: {safe_repr_for_error(ref)}"
    return _parse_repo_relative(ref, repo, rest)


def contains(a: dict, b: dict) -> bool:
    """contains(a, b): a is the (wider or equal) scope, b is the concrete/narrower ref."""
    if a["kind"] == "resource" or b["kind"] == "resource":
        return (a["kind"] == "resource" and b["kind"] == "resource"
                and a["name"] == b["name"] and a["host"] == b["host"])
    if a.get("repo") != b.get("repo"):
        return False
    if a["kind"] == "whole-repo":
        return True
    if a["kind"] == "subtree":
        if b["kind"] == "whole-repo":
            return False
        return b["path"] == a["path"] or b["path"].startswith(a["path"] + "/")
    if a["kind"] == "file":
        return b["kind"] == "file" and b["path"] == a["path"]
    return False


# ------------------------------------------------------------------------------------------------
# Glob grammar and matching (§5 below)
# ------------------------------------------------------------------------------------------------

# Size limits, checked at VALIDATION (not only at firing): a trigger's own glob pattern is
# necessarily small (authored by hand), while an event's reported path is a real value from
# outside and gets the larger of the two ceilings. Both exist so that neither the outer
# segment-count DP (glob_match_path) nor the per-segment '*'-count in seg_match's chunk search can
# be driven arbitrarily large by an ordinary-looking but oversized input — every step in both is
# now polynomial and bounded, but "polynomial" is not "free": a 20,000-character glob segment is
# needless even though it no longer hangs.
GLOB_MAX_LENGTH = 1024
GLOB_MAX_SEGMENTS = 64
GLOB_MAX_STARS_PER_SEGMENT = 16
EVENT_PATH_MAX_LENGTH = 4096
EVENT_PATH_MAX_SEGMENTS = 256


def validate_glob_grammar(glob: str, *, is_event_path: bool = False):
    """Returns None if `glob` is grammar-valid, else a reason string. `is_event_path=True`
    validates a reported EVENT path against the (larger) event-path size ceiling instead of the
    (smaller) glob-pattern ceiling, and skips the per-segment '*'-count limit (an event path is a
    literal value, not a pattern; a literal '*' character in it is not a wildcard — see
    `seg_match`)."""
    if not isinstance(glob, str) or glob == "":
        return "must be a non-empty string"
    max_length = EVENT_PATH_MAX_LENGTH if is_event_path else GLOB_MAX_LENGTH
    max_segments = EVENT_PATH_MAX_SEGMENTS if is_event_path else GLOB_MAX_SEGMENTS
    if len(glob) > max_length:
        return f"exceeds the {max_length}-character limit ({len(glob)} characters): {glob[:40]!r}..."
    if glob.startswith("/"):
        return f"absolute path not allowed: {safe_repr_for_error(glob)}"
    segments = glob.split("/")
    if len(segments) > max_segments:
        return f"exceeds the {max_segments}-segment limit ({len(segments)} segments)"
    for seg in segments:
        if seg == "":
            return f"empty path segment in {safe_repr_for_error(glob)}"
        if seg in (".", ".."):
            return f"'{seg}' segment not allowed in {safe_repr_for_error(glob)}"
        if seg == "**":
            continue
        if "**" in seg:
            return (f"'**' must be its own path segment, not mixed with other characters "
                     f"({safe_repr_for_error(seg)} in {safe_repr_for_error(glob)})")
        if not is_event_path and seg.count("*") > GLOB_MAX_STARS_PER_SEGMENT:
            return (f"segment {safe_repr_for_error(seg)} has more than {GLOB_MAX_STARS_PER_SEGMENT} '*' "
                    f"characters")
        if not SEGMENT_RE.fullmatch(seg):  # .fullmatch: see parse_entity_ref's comment on .match
            return (f"segment {safe_repr_for_error(seg)} in {safe_repr_for_error(glob)} has a disallowed character (allowed: letters, "
                    f"digits, '_', '.', '-', '*')")
    return None


def glob_match_path(glob_path: str, candidate_path: str) -> bool:
    """Exact glob-to-concrete-path match (not glob-covers-glob containment).

    Iterative, not recursive: a dynamic program over (glob segment index, candidate segment
    index), filled from the end of the glob backwards. A recursive segment-by-segment matcher
    consumes one Python stack frame per glob segment (more per '**', since it also branches over
    every possible consumption count) — a long but entirely valid glob (many segments, in
    particular many '**' segments) must not crash a firing check with a RecursionError."""
    g_segs = glob_path.split("/")
    c_segs = candidate_path.split("/")
    n, m = len(g_segs), len(c_segs)

    def seg_match(pattern_seg: str, cand_seg: str) -> bool:
        """One-segment wildcard match: '*' in `pattern_seg` is always a wildcard (any sequence,
        including empty); every character of `cand_seg` — even a literal '*' — is always matched
        literally; '*' is not a special character on the candidate side. Greedy chunk matching:
        split `pattern_seg` on '*' into a fixed prefix, a fixed suffix, and zero or more middle
        chunks. The prefix and suffix are checked with an anchored `startswith`/`endswith`; each
        middle chunk is located with a single left-to-right `str.find` from just after the
        previous match — never rescanning from the start of the remaining candidate, and never
        asking a regex engine to backtrack. (`validate_glob_grammar` forbids '**' from appearing
        inside a mixed segment — only as a whole segment on its own — so no middle chunk here is
        ever empty from two adjacent '*'s; a defensive empty-chunk check below is redundant with
        that grammar rule, not a case this function relies on the grammar for.)

        Taking the greedy, left-to-right EARLIEST match for every middle chunk is provably
        correct for this glob's `*`-only wildcard (no `?`): if some valid placement exists using a
        LATER occurrence of a middle chunk, the search position after the EARLIER occurrence is
        no further right, so every subsequent chunk (including the fixed suffix) that could be
        found after the later occurrence can also be found — at the same or an earlier position —
        after the earlier one."""
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
            # Matches zero segments (new_dp[ci] picks up dp[ci], i.e. "skip '**' entirely here")
            # or at least one (new_dp[ci] picks up new_dp[ci + 1], i.e. "'**' also consumes
            # c_segs[ci]"), filled right-to-left so new_dp[ci + 1] is already known.
            new_dp[m] = dp[m]
            for ci in range(m - 1, -1, -1):
                new_dp[ci] = dp[ci] or new_dp[ci + 1]
        else:
            for ci in range(m):
                new_dp[ci] = seg_match(seg, c_segs[ci]) and dp[ci + 1]
            # new_dp[m] stays False: a literal segment has no candidate segment left to consume.
        dp = new_dp
    return dp[0]


# ------------------------------------------------------------------------------------------------
# Params validation (§3.7 / §4 below) — shared by a trigger's when.params and an EVENT's params
# ------------------------------------------------------------------------------------------------

def validate_params(event: str, params, label: str, context: str = "trigger") -> list:
    """Returns a list of {code, reason} issues for `params` against EVENT_VOCAB[event]. Assumes
    `event` is already known to be in EVENT_VOCAB (callers check E_BAD_EVENT separately).

    `context` is `"trigger"` (the default, for a trigger's own `when.params` — an authored glob
    PATTERN, small, gets the tighter glob-pattern size limits) or `"event"` (an EVENT record's
    `params` — a reported literal VALUE from outside, gets the larger event-path size limits and
    is never checked for its own '*' count, since '*' there is not a wildcard)."""
    errors = []
    is_event_path = context == "event"
    spec = EVENT_VOCAB[event]
    if not isinstance(params, dict):
        return [issue(E_BAD_PARAMS, f"{label}: params must be a table/object")]
    allowed = set(spec)
    unknown = set(params) - allowed
    for key in sorted(unknown):
        errors.append(issue(E_BAD_PARAMS,
                             f"{label}: unknown param {safe_repr_for_error(key)} for event {safe_repr_for_error(event)} "
                             f"(allowed: {sorted(allowed) or 'none'})"))
    for key, req in spec.items():
        if req == "required" and key not in params:
            errors.append(issue(E_BAD_PARAMS, f"{label}: missing required param {safe_repr_for_error(key)} for "
                                                f"event {safe_repr_for_error(event)}"))
    for key, value in params.items():
        if key not in allowed:
            continue
        ptype = PARAM_TYPE[key]
        if ptype == "glob_list":
            if not isinstance(value, list) or not value:
                errors.append(issue(E_BAD_PARAMS, f"{label}.{key} must be a non-empty array"))
                continue
            for i, g in enumerate(value):
                if not is_nonblank_str(g):
                    errors.append(issue(E_BAD_PARAMS, f"{label}.{key}[{i}] must be a non-blank "
                                                        f"string"))
                    continue
                reason = validate_glob_grammar(g, is_event_path=is_event_path)
                if reason:
                    errors.append(issue(E_BAD_GLOB_OR_DURATION, f"{label}.{key}[{i}]: {reason}"))
        elif ptype == "positive_int":
            # `value > MAX_INT64` is a pure binary-magnitude comparison — never a string conversion
            # — so this bound is checked even for a value whose decimal form Python's own int-to-str
            # guard would refuse to build (TOML's own parser already rejects anything past ~4300
            # digits at LOAD time, see load_toml_file; this is the semantic range for any value that
            # DOES parse). `safe_repr_for_error` below never calls repr()/str() on an oversized int
            # either (it reports a digit-count estimate instead), so echoing the offending value is
            # itself safe regardless of magnitude.
            if (not is_schema_integer(value) or value < 1 or value > MAX_INT64):
                errors.append(issue(E_BAD_PARAMS,
                                     f"{label}.{key} must be an integer >= 1 and <= {MAX_INT64} "
                                     f"(a signed 64-bit range): {safe_repr_for_error(value)}"))
            else:
                params[key] = int(value)
        elif ptype == "duration":
            if not is_nonblank_str(value) or not DURATION_RE.fullmatch(value):  # .fullmatch: see parse_entity_ref
                errors.append(issue(E_BAD_GLOB_OR_DURATION,
                                     f"{label}.{key} must match ^[1-9][0-9]*[dhm]$: {safe_repr_for_error(value)}"))
            elif len(value) - 1 > MAX_DURATION_DIGITS:
                # Checked BEFORE any caller might ever convert the digit run to a number: see
                # MAX_DURATION_DIGITS's own comment above. `value` already matched DURATION_RE, so
                # it is short enough (`len(value) <= EVENT_PATH_MAX_LENGTH`/`GLOB_MAX_LENGTH`-ish)
                # for safe_repr_for_error to render in full here regardless.
                errors.append(issue(E_BAD_GLOB_OR_DURATION,
                                     f"{label}.{key} duration {safe_repr_for_error(value)} has more "
                                     f"than {MAX_DURATION_DIGITS} digits"))
        elif ptype == "glob_string":
            if not is_nonblank_str(value):
                errors.append(issue(E_BAD_PARAMS, f"{label}.{key} must be a non-blank string"))
                continue
            reason = validate_glob_grammar(value, is_event_path=is_event_path)
            if reason:
                errors.append(issue(E_BAD_GLOB_OR_DURATION, f"{label}.{key}: {reason}"))
    return errors


# ------------------------------------------------------------------------------------------------
# Trigger-file validation (§3 below)
# ------------------------------------------------------------------------------------------------

def validate_trigger_doc(doc: dict, file_label: str) -> list:
    """Shape/enum/grammar validation of ONE loaded trigger document (§3 below)."""
    errors = []
    if not isinstance(doc, dict):
        return [issue(E_BAD_TOP, f"{file_label}: top level must be a table")]
    for field in TOP_REQUIRED:
        if field not in doc:
            errors.append(issue(E_BAD_TOP, f"{file_label} missing required field: {field}"))
    unknown = set(doc) - TOP_ALLOWED
    for key in sorted(unknown):
        errors.append(issue(E_BAD_TOP, f"{file_label} has unknown top-level key: {safe_repr_for_error(key)}"))

    if "trigger_version" in doc and not is_nonblank_str(doc["trigger_version"]):
        errors.append(issue(E_BAD_TOP, f"{file_label}.trigger_version must be a non-blank string"))

    self_repo = None
    if "repo" in doc:
        if not is_nonblank_str(doc["repo"]):
            errors.append(issue(E_BAD_TOP, f"{file_label}.repo must be a non-blank string"))
        elif not _valid_repo_id(doc["repo"]):
            errors.append(issue(E_BAD_TOP, f"{file_label}.repo is not a valid <repo> id: "
                                            f"{safe_repr_for_error(doc['repo'])}"))
        else:
            self_repo = doc["repo"]

    if "triggers" not in doc:
        return errors  # already flagged missing above
    triggers = doc["triggers"]
    if not isinstance(triggers, list):
        errors.append(issue(E_BAD_TOP, f"{file_label}.triggers must be an array of tables"))
        return errors

    seen_ids = {}
    for i, trig in enumerate(triggers):
        label = f"{file_label}.triggers[{i}]"
        if not isinstance(trig, dict):
            errors.append(issue(E_BAD_TOP, f"{label} must be a table"))
            continue
        for field in TRIGGER_REQUIRED:
            if field not in trig:
                errors.append(issue(E_BAD_TOP, f"{label} missing: {field}"))
        trig_unknown = set(trig) - TRIGGER_ALLOWED
        for key in sorted(trig_unknown):
            errors.append(issue(E_BAD_TOP, f"{label} has unknown key: {safe_repr_for_error(key)}"))

        if "id" in trig:
            tid = trig["id"]
            if (not is_nonblank_str(tid) or len(tid) > TRIGGER_ID_MAX_LENGTH
                    or not TRIGGER_ID_RE.fullmatch(tid)):  # .fullmatch: see parse_entity_ref
                errors.append(issue(E_BAD_ID,
                                     f"{label}.id does not match ^[a-z0-9][a-z0-9-]*$ or exceeds "
                                     f"the {TRIGGER_ID_MAX_LENGTH}-character limit: "
                                     f"{safe_repr_for_error(tid)}"))
            elif tid in seen_ids:
                errors.append(issue(E_BAD_ID, f"{label}.id {safe_repr_for_error(tid)} duplicates "
                                               f"{seen_ids[tid]}.id"))
            else:
                seen_ids[tid] = label

        if "retrigger" in trig:
            retrigger = trig["retrigger"]
            if not is_nonblank_str(retrigger):
                errors.append(issue(E_BAD_TOP, f"{label}.retrigger must be a string"))
            elif retrigger not in RETRIGGER_VOCAB:
                errors.append(issue(E_BAD_RETRIGGER, f"{label}.retrigger not in "
                                                       f"{sorted(RETRIGGER_VOCAB)}: {safe_repr_for_error(retrigger)}"))

        if "when" in trig:
            errors.extend(_validate_when(trig["when"], label, self_repo))

    return errors


def _validate_when(when, trig_label: str, self_repo) -> list:
    errors = []
    if not isinstance(when, dict):
        return [issue(E_BAD_TOP, f"{trig_label}.when must be a table")]
    for field in WHEN_REQUIRED:
        if field not in when:
            errors.append(issue(E_BAD_TOP, f"{trig_label}.when missing: {field}"))
    unknown = set(when) - WHEN_ALLOWED
    for key in sorted(unknown):
        errors.append(issue(E_BAD_TOP, f"{trig_label}.when has unknown key: {safe_repr_for_error(key)}"))

    event = None
    if "event" in when:
        event = when["event"]
        if not is_nonblank_str(event):
            errors.append(issue(E_BAD_TOP, f"{trig_label}.when.event must be a string"))
            event = None
        elif event not in EVENT_VOCAB:
            errors.append(issue(E_BAD_EVENT, f"{trig_label}.when.event not in "
                                              f"{sorted(EVENT_VOCAB)}: {safe_repr_for_error(event)}"))
            event = None

    if "source" in when:
        source = when["source"]
        if not is_nonblank_str(source):
            errors.append(issue(E_BAD_TOP, f"{trig_label}.when.source must be a string"))
        else:
            ok, _, reason = parse_entity_ref(source, self_repo=self_repo)
            if not ok:
                errors.append(issue(E_BAD_ENTITY_REF, f"{trig_label}.when.source: {reason}"))

    if event is not None:
        params = when.get("params", {})
        errors.extend(validate_params(event, params, f"{trig_label}.when.params"))
    elif "params" in when and not isinstance(when["params"], dict):
        errors.append(issue(E_BAD_TOP, f"{trig_label}.when.params must be a table"))

    return errors


def exit_code_for(errors: list) -> int:
    return min((e["code"] for e in errors), default=0)


# ------------------------------------------------------------------------------------------------
# EVENT record validation (§4 below) — reuses validate_params (§3.7)
# ------------------------------------------------------------------------------------------------

def validate_event_record(record: dict, label: str) -> list:
    """Returns a list of plain-string issues (trigger_fire.py buckets every one under exit 65:
    an EVENT/history record failing shape/vocab is an INPUT error, not one of the trigger-file's
    own 7 semantic classes)."""
    issues = []
    if not isinstance(record, dict):
        return [f"{label}: must be a JSON object"]
    for field in EVENT_RECORD_REQUIRED:
        if field not in record:
            issues.append(f"{label} missing required field: {field}")
    unknown = set(record) - EVENT_RECORD_ALLOWED
    for key in sorted(unknown):
        issues.append(f"{label} has unknown key: {safe_repr_for_error(key)}")

    if "event_id" in record and not is_nonblank_str(record["event_id"]):
        issues.append(f"{label}.event_id must be a non-blank string")

    event = None
    if "event" in record:
        event = record["event"]
        if not is_nonblank_str(event) or event not in EVENT_VOCAB:
            issues.append(f"{label}.event not in {sorted(EVENT_VOCAB)}: {safe_repr_for_error(event)}")
            event = None

    if "source" in record:
        source = record["source"]
        if not is_nonblank_str(source):
            issues.append(f"{label}.source must be a string")
        else:
            ok, _, reason = parse_entity_ref(source, self_repo=None)
            if not ok:
                issues.append(f"{label}.source: {reason}")

    if "at" in record:
        try:
            parse_at(record["at"])
        except ValueError as exc:
            issues.append(f"{label}.at is not a valid ISO-8601 UTC timestamp: {exc}")

    if event is not None:
        params = record.get("params", {})
        for e in validate_params(event, params, f"{label}.params", context="event"):
            issues.append(e["reason"])
    elif "params" in record and not isinstance(record["params"], dict):
        issues.append(f"{label}.params must be a table/object")

    return issues


# ------------------------------------------------------------------------------------------------
# Firing (§6/§7 below)
# ------------------------------------------------------------------------------------------------

def occurrence_id(trigger_id: str, event_id: str) -> str:
    digest = hashlib.sha256((trigger_id + "\n" + event_id).encode("utf-8")).hexdigest()
    return digest[:32]


def params_match(event_kind: str, trigger_params: dict, event_params: dict) -> bool:
    trigger_params = trigger_params or {}
    event_params = event_params or {}
    if event_kind == "path-changed":
        globs = trigger_params.get("paths") or []
        paths = event_params.get("paths") or []
        return any(glob_match_path(g, p) for g in globs for p in paths)
    if event_kind in ("commits-at-least", "days-since-setup"):
        tn = trigger_params.get("n")
        en = event_params.get("n")
        if tn is None or en is None:
            return False
        return en >= tn
    if event_kind == "cadence":
        return trigger_params.get("every") == event_params.get("every")
    if event_kind == "first-src-commit":
        t_glob = trigger_params.get("src_glob")
        if t_glob is None:
            return True
        return event_params.get("src_glob") == t_glob
    return True  # every other event kind carries no params to compare


def evaluate_fire(triggers: list, events: list, history: list, self_repo) -> tuple:
    """The full evaluation rule (§7): returns (fired_list, suppressed_list) — the exact shapes
    `trigger_fire.py`'s report emits. `triggers`/`events`/`history` are already-validated raw
    dicts (parsed TOML triggers, parsed JSONL events/history)."""
    hist_occurrence_ids = {h["occurrence_id"] for h in history}
    hist_fired_trigger_ids = {h["trigger_id"] for h in history}

    candidates = []
    for trig in triggers:
        for ev in events:
            if fires(trig, ev, self_repo=self_repo):
                oid = occurrence_id(trig["id"], ev["event_id"])
                at = parse_at(ev["at"])
                candidates.append((at, trig["id"], ev["event_id"], trig, ev, oid))

    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    fired = []
    suppressed_full = []
    fired_occurrence_ids = set()
    fired_trigger_ids = set()

    for at, trig_id, event_id, trig, ev, oid in candidates:
        if oid in hist_occurrence_ids or oid in fired_occurrence_ids:
            suppressed_full.append((at, trig_id, event_id, oid, "duplicate"))
            continue
        if trig.get("retrigger") == "once" and (
            trig_id in hist_fired_trigger_ids or trig_id in fired_trigger_ids
        ):
            suppressed_full.append((at, trig_id, event_id, oid, "suppressed-once"))
            continue
        fired.append({
            "occurrence_id": oid,
            "trigger_id": trig_id,
            "event_id": event_id,
            "event": ev["event"],
            "source": ev["source"],
            "at": ev["at"],
        })
        fired_occurrence_ids.add(oid)
        fired_trigger_ids.add(trig_id)

    # candidates were processed in (at, trigger_id, event_id) order already, so `fired` and
    # `suppressed_full` are already in that exact relative order — no re-sort needed.
    suppressed = [{"occurrence_id": oid, "reason": reason}
                  for _, _, _, oid, reason in suppressed_full]
    return fired, suppressed


def fires(trigger: dict, event: dict, self_repo=None) -> bool:
    """The full firing rule (§6) for one (trigger, event) pair. `trigger`/`event` are the raw
    parsed TOML/JSON dicts (already validated by the caller). `self_repo` resolves any '@self'
    form in the trigger's own when.source (the trigger FILE's top-level 'repo' field, §1) —
    an EVENT record's own source is always parsed with self_repo=None (§4)."""
    when = trigger["when"]
    if event["event"] != when["event"]:
        return False
    ok_t, t_ref, _ = parse_entity_ref(when["source"], self_repo=self_repo)
    ok_e, e_ref, _ = parse_entity_ref(event["source"], self_repo=None)
    if not ok_t or not ok_e:
        return False
    if not contains(t_ref, e_ref):
        return False
    return params_match(when["event"], when.get("params", {}), event.get("params", {}))
