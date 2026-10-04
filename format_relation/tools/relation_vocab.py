"""relation_vocab.py — shared, stdlib-only core for `relation_validate.py` and `relation_query.py`.

Implements:
  - The entity-ref grammar (§A below): parsing, `@self` resolution, containment, overlap.
  - The relation vocabulary (§C below): the closed set, the `x-<token>` extension, and the
    propagation-direction table.
  - TOML loading of one relations file (tomllib; stdlib, Python 3.11+).
  - Shape/entity-ref/relation/propagates validation of a MERGED set of relations files, plus the
    cross-edge semantic checks (self-loop, duplicate edge, unresolved `@self`) that need every
    edge resolved against one `--self` value.
  - The propagation-graph builder and the cycle-safe, deterministic BFS traversal both
    `impacted`/`depends` share.

This module raises nothing on its own for a caller's convenience where practical —
`validate_merged()` returns `(errors, resolved_edges)`; callers (`relation_validate.py` /
`relation_query.py`) decide exit codes and JSON shape. `load_toml_file()` DOES raise
(`RelationInputError`) for I/O/parse failures.

Stdlib only: tomllib, re, pathlib, collections.

Stdout environment contract: for report-producing commands, stdout may be a real file descriptor
of any number or a text stream with ``write``/``flush``. The tool writes its one report completely
and exits with its documented code, or exits 74 having written nothing further; stdout protocol
failures (OSError, ValueError, TypeError, OverflowError, AttributeError, io.UnsupportedOperation)
and any other stdout object mean stdout unavailable and exit 74; they never produce exit 120 or a
traceback. Argparse ``--help``/usage text is argparse's own output, not a report, and is not routed
through this boundary. External signals (including SIGINT, SIGPIPE
reset to SIG_DFL, and SIGKILL) remain subject to the OS/Python signal behavior. There are no other
scope exclusions.
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
from collections import deque
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
    """Normalize integral TOML/JSON numbers before integer-only validation operations."""
    if is_schema_integer(value):
        return int(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: normalize_schema_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_schema_numbers(item) for item in value]
    return value

RELATION_VOCAB_VERSION = "relation/first-cut-0.1.0-draft"

# ---- exit-code classes (shared numbering between validate and query) ----
# Priority order, starting at 10: bad top-level, bad entity-ref, unknown relation, propagates on
# a known relation or bad value, self-loop edge, duplicate identical edge, unresolved @self
# without --self. When several apply, the exit code is the lowest class.
E_BAD_TOP = 10
E_BAD_ENTITY_REF = 11
E_UNKNOWN_RELATION = 12
E_BAD_PROPAGATES = 13
E_SELF_LOOP = 14
E_DUP_EDGE = 15
E_UNRESOLVED_SELF = 16
E_USAGE = 64
E_INPUT = 65
E_OUTPUT = 74
E_SELFTEST = 99


class RelationInputError(Exception):
    """I/O or TOML-syntax failure reading one --relations file (exit 65)."""

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


class EntityRefError(Exception):
    """A raw string fails the §A entity-ref grammar (exit 11)."""


class UnresolvedSelfError(Exception):
    """An `@self` entity-ref was given no `--self` value to resolve against (exit 16)."""


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
# `impact_rules.DIAGNOSTIC_REASON_MAX_CHARS`.
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
    foreign value this format's tools read — a TOML scalar (an oversized integer included), or a
    CONTAINER (list/tuple/dict/set) nesting one, to any depth. Byte-for-byte the same algorithm as
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
    # Escaped BEFORE the length check/cap, not after: a usage-error reason built
    # straight from argparse's own message (which embeds the raw, `surrogateescape`-decoded argv
    # text) must never carry a lone surrogate past this, the final, unconditional backstop.
    text = _escape_unicode_safe(text)
    if len(text) <= DIAGNOSTIC_REASON_MAX_CHARS:
        return text
    return _cap_with_marker(text, DIAGNOSTIC_REASON_MAX_CHARS, marker=_REASON_TRUNCATION_MARKER)


def finalize_errors(errors: list) -> list:
    """Applies `bound_reason` to every entry's `reason`, unconditionally — the one place every
    `errors[]` list this format's tools report passes through immediately before it reaches a
    report dict, so the ≤2,000-character bound holds for every exit path (shared by
    relation_validate.py and relation_query.py, which each build several such lists)."""
    return [{**e, "reason": bound_reason(e["reason"])} for e in errors]


def bound_path_preview(path) -> str:
    """Returns a short, BOUNDED preview of a filesystem path (or any other path-like value: a
    `pathlib.Path`, or a raw CLI string) safe to interpolate DIRECTLY into an error message — for
    ANY input, including an adversarially long `--relations` argument (a 10,000-character string),
    AND a path that is not valid Unicode at all. A CLI argument is decoded by Python from the OS's
    raw bytes using `surrogateescape` (e.g. a POSIX filename byte `\\xff` with no valid UTF-8
    interpretation becomes the Python string `"\\udcff"`, a LONE UTF-16 surrogate) — `str(path)` on
    such a value is still a `str`, but one that cannot itself encode as UTF-8, so interpolating it
    RAW into a message would put a string this format's own reports can never actually serialize
    into. Every such character is replaced with a visible, UTF-8-safe backslash escape here, BEFORE
    the length cap below, so the cap still holds on the ESCAPED text (escaping can only lengthen a
    string, never shorten it — capping first and escaping after could let a result exceed the cap).
    Byte-for-byte the same helper (and the same reasoning) as format_impact-rule's own
    `impact_rules.bound_path_preview`."""
    s = str(path)
    s = _escape_unicode_safe(s)
    if len(s) <= _SAFE_REPR_STR_PREVIEW_CHARS:
        return s
    return _cap_with_marker(s, _SAFE_REPR_STR_PREVIEW_CHARS)


# ------------------------------------------------------------------------------------------------
# The JSON input contract every JSON value this format's tools PRINT must satisfy: (a) no duplicate
# object keys; (b) no NaN/Infinity/-Infinity; (c) every string, key or value, must encode as UTF-8;
# (d) nesting no deeper than MAX_JSON_DEPTH. This format's own `--relations` input is TOML, not
# JSON, but every report this format's CLIs print IS JSON, and a consumer reading it is entitled to
# apply this same strict contract — `find_json_input_defect` is what this format's own self-tests
# use to prove a REAL printed report satisfies it (H7: producer against rule). Byte-for-byte the
# same helpers (and the same reasoning) as `format_trigger`'s own `trigger_vocab.py`.
# ------------------------------------------------------------------------------------------------
MAX_JSON_DEPTH = 64


def unique_pairs(pairs: list) -> dict:
    """`object_pairs_hook` for `json.loads`: rejects a duplicate key instead of silently keeping
    only the last value for it."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    """`parse_constant` for `json.loads`: rejects the non-standard NaN/Infinity/-Infinity tokens
    Python's decoder otherwise accepts."""
    raise ValueError(f"non-JSON numeric constant: {value}")


def find_json_input_defect(obj, max_depth: int = MAX_JSON_DEPTH):
    """ITERATIVE walk (an explicit stack, never recursion over input size) over one parsed JSON
    value. Returns a reason string for the first defect found, or None if the whole structure is
    clean: a string that cannot encode as UTF-8, a non-finite float, or nesting deeper than
    `max_depth`. The top-level value is depth 1."""
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


def register_report_schema(module_file: str, declaration: tuple[str, str]) -> None:
    """Load and audit an emitting tool's declared schema once, during module import."""
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
    """Return the first emitting caller's declaration and format root."""
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
    error = {"code": E_INPUT, "reason": "report failed its schema"}
    if def_name == "fold_report":
        return {"boundary_id": None, "duties": [],
                "aggregate": {"block": True, "required_actions": []},
                "validation": {"status": "unknown", "exit_code": E_INPUT,
                               "errors": [error], "warnings": [],
                               "duplicates_ignored": []}}
    return {"status": "unknown", "exit_code": E_INPUT, "errors": [error]}


def _schema_checked_report_text(text: str) -> str:
    """Validate the complete report before its first byte reaches stdout."""
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
            schema_path = format_root / schema_rel
            document = json.loads(schema_path.read_text(encoding="utf-8"))
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
    fallback_errors = conformance_errors(
        fallback, document, f"#/$defs/{def_name}", "internal_error_report", audited=True)
    if fallback_errors:
        raise RuntimeError("internal-error report failed its schema")
    return json.dumps(fallback, indent=2, sort_keys=True, allow_nan=False)


def write_report(text: str) -> None:
    """Validate and write one complete JSON report."""
    _write_text(_schema_checked_report_text(text))


def write_text(text: str) -> None:
    """Write a documented non-JSON text rendering."""
    _write_text(text)


def _write_text(text: str) -> None:
    """Write one complete UTF-8 report, translating only stdout failures to a boundary exception.

    A real stdout descriptor uses ``os.write`` so Python's text wrappers cannot conceal a short
    write. A descriptor-less in-process stream keeps the text protocol used by callers that
    redirect ``main()`` to ``io.StringIO``.
    """
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
    """Run one Relation CLI at the stdout byte boundary.

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
    tools still runs every `reason` through `bound_reason`/`finalize_errors` and every path through
    `bound_path_preview` at the source — this funnel exists so a report field that reaches `write_report()`
    without having gone through either still cannot make this format emit a report its own strict
    reader would reject."""
    dumps_kwargs["allow_nan"] = False
    return json.dumps(_escape_report_value(report), **dumps_kwargs)


def _find_non_finite(value):
    """ITERATIVE walk (an explicit stack, never recursion over input size) over one already-parsed
    TOML value. Returns a reason-fragment string for the first non-finite float found (`nan`, `inf`,
    `-inf`, or any float expression that overflows to one of them, e.g. `1e400`), or None if the
    whole structure is clean, at any depth and in any container. A non-finite value is rejected at
    LOAD time, as a plain input error, rather than carried through to a `json.dumps` call that would
    otherwise print the bare, non-standard words `NaN`/`Infinity`/`-Infinity` into what must be
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
        # `str(exc)` itself, keeps this message's length independent of how long a `--relations`
        # argument happens to be.
        raise RelationInputError(f"cannot read {bound_path_preview(path)}: {type(exc).__name__}")
    try:
        doc = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except UnicodeDecodeError as exc:
        raise RelationInputError(f"not valid UTF-8 ({bound_path_preview(path)}): {exc}")
    except tomllib.TOMLDecodeError as exc:
        raise RelationInputError(f"TOML syntax error ({bound_path_preview(path)}): {exc}")
    except ValueError as exc:
        # tomllib's own integer parsing calls Python's int() on a decimal literal, which refuses a
        # digit run longer than sys.get_int_max_str_digits() (4300 by default) with a plain
        # ValueError, not TOMLDecodeError — that conversion is quadratic in digit count, so a
        # 5000-digit literal is a malformed/oversized INPUT (this file cannot even be parsed into a
        # document), reported here as the same clean, documented input-error class every other
        # unparseable file already uses — never an uncaught traceback, and never silently reaching
        # this tool's own generic catch-all with a less specific message. CPython's own message here
        # names the digit COUNT, never the offending digits, so it is already safe to interpolate.
        raise RelationInputError(f"TOML parse failure ({bound_path_preview(path)}): {exc}")
    if not isinstance(doc, dict):
        raise RelationInputError(f"top level of {bound_path_preview(path)} must be a TOML table")
    defect = _find_non_finite(doc)
    if defect is not None:
        raise RelationInputError(f"{bound_path_preview(path)}: {defect}")
    return doc


# ==================================================================================================
# §A — entity-ref grammar, parsing, `@self` resolution, containment/overlap
# ==================================================================================================

_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# An entity-ref's own overall length, checked before any further parsing: the grammar below has no
# length ceiling of its own (it is a structural grammar, not a size limit), so an adversarially long
# `from`/`to`/`source` value would otherwise be echoed in full by a grammar-failure message.
# Comfortably above any real repo-relative path a real caller would ever author.
ENTITY_REF_MAX_LENGTH = 4096


def _valid_repo_id(repo) -> bool:
    # .fullmatch, not .match: Python's trailing `$` matches just BEFORE a final '\n', so
    # .match("acme\n") on a `...$` pattern wrongly succeeds. .fullmatch requires the whole string,
    # including any trailing newline, to be consumed by the pattern.
    return isinstance(repo, str) and 1 <= len(repo) <= 64 and bool(_REPO_RE.fullmatch(repo))


def is_valid_repo_id(repo) -> bool:
    """Public wrapper: callers outside this module (e.g. a CLI's own `--self` shape check) use
    this instead of reaching into the private `_valid_repo_id`."""
    return _valid_repo_id(repo)


def _valid_path_segments(path: str) -> bool:
    """`path` is the part after `<repo>:`/`@self:`, with any trailing subtree `/` already
    stripped. No leading `/`, no empty segment, no `.`/`..` segment (§A)."""
    if path == "" or path.startswith("/"):
        return False
    for seg in path.split("/"):
        if seg == "" or seg in (".", ".."):
            return False
    return True


class EntityRef:
    """A parsed entity-ref (§A below). `form` is one of:
      - "repo"     — a whole repo (`<repo>`, `<repo>:**`, or a resolved `@self`/`@self:**`).
      - "file"     — `<repo>:<path>` (repo-relative POSIX file path, no trailing `/`).
      - "subtree"  — `<repo>:<dir>/` (`path` holds the dir WITHOUT the trailing `/`).
      - "resource" — `resource:<name>@<host>`.
    `is_self` is True until `resolve_entity_ref()` substitutes a real `repo` for a bare `@self`/
    `@self:<path>` reference. `repo` is None for an unresolved self-ref and for a resource ref."""

    __slots__ = ("raw", "is_self", "form", "repo", "path", "resource_name", "resource_host")

    def __init__(self, raw, is_self, form, repo=None, path=None, resource_name=None,
                 resource_host=None):
        self.raw = raw
        self.is_self = is_self
        self.form = form
        self.repo = repo
        self.path = path
        self.resource_name = resource_name
        self.resource_host = resource_host

    def __repr__(self):
        return f"EntityRef({self.raw!r})"


def _parse_repo_relative(raw: str, is_self: bool, repo, rest: str) -> "EntityRef":
    """`rest` is whatever followed `<repo>:` or `@self:` — one of the whole-repo `**` marker, a
    subtree (trailing `/`), or a plain file path."""
    if rest == "**":
        return EntityRef(raw, is_self, "repo", repo=repo, path=None)
    if "*" in rest:
        raise EntityRefError(
            f"'*'/'**' not allowed in an entity-ref except the whole-repo '<repo>:**' form: {safe_repr_for_error(raw)}"
        )
    if rest.endswith("/"):
        dir_path = rest[:-1]
        if not _valid_path_segments(dir_path):
            raise EntityRefError(f"bad subtree path in entity-ref: {safe_repr_for_error(raw)}")
        return EntityRef(raw, is_self, "subtree", repo=repo, path=dir_path)
    if not _valid_path_segments(rest):
        raise EntityRefError(f"bad file path in entity-ref: {safe_repr_for_error(raw)}")
    return EntityRef(raw, is_self, "file", repo=repo, path=rest)


def parse_entity_ref(raw) -> "EntityRef":
    """Parses one raw string against the §A entity-ref grammar. Raises `EntityRefError` on any
    grammar violation. Does NOT resolve `@self` — see `resolve_entity_ref()`."""
    if not is_nonblank_str(raw):
        raise EntityRefError(f"entity-ref must be a non-blank string: {safe_repr_for_error(raw)}")
    if len(raw) > ENTITY_REF_MAX_LENGTH:
        raise EntityRefError(f"entity-ref exceeds the {ENTITY_REF_MAX_LENGTH}-character limit "
                              f"({len(raw)} characters): {safe_repr_for_error(raw)}")
    if "\\" in raw:
        raise EntityRefError(f"entity-ref must not contain a backslash: {safe_repr_for_error(raw)}")
    if _CONTROL_CHAR_RE.search(raw):
        raise EntityRefError(
            f"entity-ref must not contain a control character: {safe_repr_for_error(raw)}")

    if raw == "@self":
        return EntityRef(raw, True, "repo", repo=None, path=None)
    if raw.startswith("@self:"):
        return _parse_repo_relative(raw, True, None, raw[len("@self:"):])

    if raw.startswith("resource:"):
        rest = raw[len("resource:"):]
        # exactly one '@': rpartition/split on the wrong occurrence would silently fold a second
        # '@' into <name> or <host> instead of being rejected (trigger_vocab.py's resource parser applies this identical
        # rule).
        if rest.count("@") != 1:
            raise EntityRefError(
                f"resource entity-ref must contain exactly one '@' (want resource:<name>@<host>): {safe_repr_for_error(raw)}")
        name, host = rest.split("@")
        # is_nonblank_str, not "not name"/"not host": a lone space is non-empty but not a name.
        if not is_nonblank_str(name) or "/" in name or "*" in name or ":" in name:
            raise EntityRefError(f"malformed resource <name> in entity-ref: {safe_repr_for_error(raw)}")
        if not is_nonblank_str(host) or "/" in host or "*" in host or ":" in host:
            raise EntityRefError(f"malformed resource <host> in entity-ref: {safe_repr_for_error(raw)}")
        return EntityRef(raw, False, "resource", resource_name=name, resource_host=host)

    if ":" in raw:
        repo, _, rest = raw.partition(":")
        if not _valid_repo_id(repo):
            raise EntityRefError(f"bad <repo> id in entity-ref: {safe_repr_for_error(raw)}")
        return _parse_repo_relative(raw, False, repo, rest)

    # no colon at all -> the bare whole-repo form `<repo>`
    if not _valid_repo_id(raw):
        raise EntityRefError(f"bad <repo> id in entity-ref: {safe_repr_for_error(raw)}")
    return EntityRef(raw, False, "repo", repo=raw, path=None)


def resolve_entity_ref(ref: "EntityRef", self_repo) -> "EntityRef":
    """Substitutes `self_repo` for an unresolved `@self`/`@self:<path>` ref. Returns `ref`
    unchanged if it is not a self-ref. Raises `UnresolvedSelfError` if `ref.is_self` and
    `self_repo` is None. Raises `EntityRefError` if `self_repo` itself is not a valid `<repo>`
    id."""
    if not ref.is_self:
        return ref
    if self_repo is None:
        raise UnresolvedSelfError(f"entity-ref {safe_repr_for_error(ref.raw)} uses '@self' but no --self repo was given")
    if not _valid_repo_id(self_repo):
        raise EntityRefError(f"--self value is not a valid <repo> id: {safe_repr_for_error(self_repo)}")
    return EntityRef(ref.raw, False, ref.form, repo=self_repo, path=ref.path)


def canonical_key(ref: "EntityRef"):
    """A hashable, resolution-required identity key for one entity-ref. Two refs with the same
    key denote the SAME entity (used for self-loop/duplicate detection and BFS node identity)."""
    if ref.form == "resource":
        return ("resource", ref.resource_name, ref.resource_host)
    if ref.form == "repo":
        return ("repo", ref.repo, None)
    segs = tuple(ref.path.split("/")) if ref.path else ()
    return (ref.form, ref.repo, segs)


def canonical_string(ref: "EntityRef") -> str:
    """A deterministic, resolution-required display form (both `<repo>` and `<repo>:**` collapse
    to the bare `<repo>` string)."""
    if ref.form == "resource":
        return f"resource:{ref.resource_name}@{ref.resource_host}"
    if ref.form == "repo":
        return ref.repo
    if ref.form == "subtree":
        return f"{ref.repo}:{ref.path}/"
    return f"{ref.repo}:{ref.path}"


def contains(a: "EntityRef", b: "EntityRef") -> bool:
    """`contains(a, b)` per §A: equal; or `a` is whole-repo and same repo as `b`; or `a` is a
    subtree and `b`'s path starts with that dir (as whole segments). Both refs MUST already be
    resolved (no `@self` — call `resolve_entity_ref` first)."""
    if a.form == "resource" or b.form == "resource":
        return a.form == "resource" and b.form == "resource" and canonical_key(a) == canonical_key(b)
    if a.repo != b.repo:
        return False
    if a.form == "repo":
        return True
    if a.form == "subtree":
        if b.form == "repo":
            return False
        a_segs = a.path.split("/")
        b_segs = b.path.split("/")
        return len(b_segs) >= len(a_segs) and b_segs[: len(a_segs)] == a_segs
    # a.form == "file"
    return b.form == "file" and a.path == b.path


def overlap(a: "EntityRef", b: "EntityRef") -> bool:
    """Overlap = contains(a, b) or contains(b, a) (§A)."""
    return contains(a, b) or contains(b, a)


# ==================================================================================================
# §C — relation vocabulary and propagation direction
# ==================================================================================================

# "read as": A <relation> B; "a change in ... impacts ...": which side, once changed, propagates
# to the other. "to-from" means a change to an edge's `to` impacts its `from`; "from-to" the
# reverse; "none" means the relation is purely structural (never propagates).
RELATION_DIRECTION = {
    "depends-on": "to-from",
    "pins": "to-from",
    "traces-to": "to-from",
    "records": "to-from",
    "documents": "to-from",
    "governs": "to-from",
    "template-of": "from-to",
    "supersedes": "none",
    "owns": "none",
}
KNOWN_RELATIONS = set(RELATION_DIRECTION)
X_TOKEN_RE = re.compile(r"^x-[a-z0-9-]+$")
PROPAGATES_VALUES = {"to-from", "from-to", "none"}


def is_known_relation(relation: str) -> bool:
    return relation in KNOWN_RELATIONS


def is_x_token(relation) -> bool:
    return isinstance(relation, str) and bool(X_TOKEN_RE.fullmatch(relation))


def relation_direction(relation: str, propagates_field):
    """The effective propagation direction for one edge (§C below). For a known (non-`x-`)
    relation this is always the fixed §C table value, regardless of any `propagates` field (a
    `propagates` field on a known relation is itself an E_BAD_PROPAGATES validation error — see
    `_validate_edge_shape`). For an `x-` token, `propagates_field` (the edge's own literal value,
    or None if absent) decides, defaulting to `"none"`."""
    if relation in RELATION_DIRECTION:
        return RELATION_DIRECTION[relation]
    return propagates_field if propagates_field in PROPAGATES_VALUES else "none"


# ==================================================================================================
# Validation (shape and semantics combined into one pass)
# ==================================================================================================

TOP_REQUIRED = ("relation_version", "repo", "edges")
TOP_ALLOWED = set(TOP_REQUIRED)

EDGE_REQUIRED = ("from", "relation", "to")
EDGE_OPTIONAL = ("directed", "propagates", "note", "source")
EDGE_ALLOWED = set(EDGE_REQUIRED) | set(EDGE_OPTIONAL)


def _validate_edge_shape(edge, label: str) -> list:
    errors = []
    if not isinstance(edge, dict):
        return [issue(E_BAD_TOP, f"{label} must be a table")]

    for field in EDGE_REQUIRED:
        if field not in edge:
            errors.append(issue(E_BAD_TOP, f"{label} missing: {field}"))
    unknown = set(edge) - EDGE_ALLOWED
    for key in sorted(unknown):
        errors.append(issue(E_BAD_TOP, f"{label} has unknown key: {safe_repr_for_error(key)}"))

    for field in ("from", "to", "source"):
        if field not in edge:
            continue
        value = edge[field]
        if not is_nonblank_str(value):
            errors.append(issue(E_BAD_TOP, f"{label}.{field} must be a non-blank string"))
            continue
        try:
            parse_entity_ref(value)
        except EntityRefError as exc:
            errors.append(issue(E_BAD_ENTITY_REF, f"{label}.{field}: {exc}"))

    relation = edge.get("relation")
    relation_is_known = False
    if "relation" in edge:
        if not is_nonblank_str(relation):
            errors.append(issue(E_BAD_TOP, f"{label}.relation must be a non-blank string"))
            relation = None
        elif relation in KNOWN_RELATIONS:
            relation_is_known = True
        elif is_x_token(relation):
            pass
        else:
            errors.append(issue(
                E_UNKNOWN_RELATION,
                f"{label}.relation {safe_repr_for_error(relation)} is not in the closed set {sorted(KNOWN_RELATIONS)} "
                f"and does not match the x-token grammar ^x-[a-z0-9-]+$",
            ))

    if "directed" in edge and not isinstance(edge["directed"], bool):
        errors.append(issue(E_BAD_TOP, f"{label}.directed must be a boolean"))

    if "note" in edge and not isinstance(edge["note"], str):
        errors.append(issue(E_BAD_TOP, f"{label}.note must be a string"))

    if "propagates" in edge:
        prop = edge["propagates"]
        if not is_nonblank_str(prop):
            errors.append(issue(E_BAD_TOP, f"{label}.propagates must be a non-blank string"))
        elif prop not in PROPAGATES_VALUES:
            errors.append(issue(E_BAD_PROPAGATES,
                                 f"{label}.propagates {safe_repr_for_error(prop)} not in {sorted(PROPAGATES_VALUES)}"))
        elif relation_is_known:
            errors.append(issue(
                E_BAD_PROPAGATES,
                f"{label}.propagates is set on known relation {safe_repr_for_error(relation)} — propagation direction "
                f"for a closed §C relation is fixed and MUST NOT be overridden by 'propagates'",
            ))
    return errors


def validate_one_doc(doc, file_label: str) -> list:
    """Shape + entity-ref-grammar + relation-vocabulary + propagates validation of ONE loaded
    relations document, independent of any other file and independent of `--self` (self-loop,
    duplicate-edge, and unresolved-`@self` checks need every file merged and a `--self` value —
    see `validate_merged`)."""
    if not isinstance(doc, dict):
        return [issue(E_BAD_TOP, f"{file_label}: top level must be a table")]

    errors = []
    for field in TOP_REQUIRED:
        if field not in doc:
            errors.append(issue(E_BAD_TOP, f"{file_label} missing required field: {field}"))
    unknown = set(doc) - TOP_ALLOWED
    for key in sorted(unknown):
        errors.append(issue(E_BAD_TOP, f"{file_label} has unknown top-level key: {safe_repr_for_error(key)}"))

    if "relation_version" in doc and not is_nonblank_str(doc["relation_version"]):
        errors.append(issue(E_BAD_TOP, f"{file_label}.relation_version must be a non-blank string"))
    if "repo" in doc and not is_nonblank_str(doc["repo"]):
        errors.append(issue(E_BAD_TOP, f"{file_label}.repo must be a non-blank string"))

    if "edges" in doc:
        edges = doc["edges"]
        if not isinstance(edges, list):
            errors.append(issue(E_BAD_TOP, f"{file_label}.edges must be an array of tables"))
        else:
            for i, edge in enumerate(edges):
                errors.extend(_validate_edge_shape(edge, f"{file_label}.edges[{i}]"))
    return errors


def _chain_key(chain):
    return tuple((c["from"], c["relation"], c["to"]) for c in chain)


def validate_merged(docs: list, self_repo) -> tuple:
    """`docs` is a list of `(file_label, doc)` pairs. Returns `(errors, resolved_edges)`.

    `resolved_edges` is a list of dicts — one per edge that parsed and resolved cleanly (every
    `@self` ref in it had a `--self` to resolve against) — each carrying `from_ref`/`to_ref`
    (resolved `EntityRef`s), the edge's `relation`, its raw dict, and `file_label`/`index` for
    provenance. An edge with any grammar/vocabulary error, or an unresolved `@self`, is EXCLUDED
    from `resolved_edges` (it is already reflected in `errors`, so the whole file is invalid
    anyway — self-loop/duplicate detection is best-effort and only ever runs on fully resolved
    edges, never guesses at an unresolved one)."""
    errors = []
    for file_label, doc in docs:
        errors.extend(validate_one_doc(doc, file_label))

    resolved_edges = []
    for file_label, doc in docs:
        edges = doc.get("edges") if isinstance(doc, dict) else None
        if not isinstance(edges, list):
            continue
        for i, edge in enumerate(edges):
            if not isinstance(edge, dict):
                continue
            label = f"{file_label}.edges[{i}]"
            from_raw, to_raw, relation = edge.get("from"), edge.get("to"), edge.get("relation")
            if not (is_nonblank_str(from_raw) and is_nonblank_str(to_raw) and is_nonblank_str(relation)):
                continue  # shape error already recorded by validate_one_doc
            if not (relation in KNOWN_RELATIONS or is_x_token(relation)):
                continue  # E_UNKNOWN_RELATION already recorded

            try:
                from_parsed = parse_entity_ref(from_raw)
                to_parsed = parse_entity_ref(to_raw)
            except EntityRefError:
                continue  # E_BAD_ENTITY_REF already recorded

            try:
                from_ref = resolve_entity_ref(from_parsed, self_repo)
            except UnresolvedSelfError:
                errors.append(issue(E_UNRESOLVED_SELF,
                                     f"{label}.from: {safe_repr_for_error(from_raw)} uses '@self' but --self was not given"))
                continue
            try:
                to_ref = resolve_entity_ref(to_parsed, self_repo)
            except UnresolvedSelfError:
                errors.append(issue(E_UNRESOLVED_SELF,
                                     f"{label}.to: {safe_repr_for_error(to_raw)} uses '@self' but --self was not given"))
                continue

            source_raw = edge.get("source")
            if is_nonblank_str(source_raw):
                try:
                    source_parsed = parse_entity_ref(source_raw)
                except EntityRefError:
                    pass  # E_BAD_ENTITY_REF already recorded by validate_one_doc
                else:
                    try:
                        resolve_entity_ref(source_parsed, self_repo)
                    except UnresolvedSelfError:
                        errors.append(issue(
                            E_UNRESOLVED_SELF,
                            f"{label}.source: {safe_repr_for_error(source_raw)} uses '@self' but --self was not given"))
                        continue

            if canonical_key(from_ref) == canonical_key(to_ref):
                errors.append(issue(
                    E_SELF_LOOP,
                    f"{label}: from and to denote the same entity ({canonical_string(from_ref)!r}) "
                    f"— self-loop edges are rejected",
                ))

            directed_raw = edge.get("directed", True)
            propagates_raw = edge.get("propagates")
            if not isinstance(directed_raw, bool) or not (
                    propagates_raw is None or is_nonblank_str(propagates_raw)):
                # E_BAD_TOP already recorded by validate_one_doc/_validate_edge_shape. Both values
                # become part of the duplicate-edge dedup key below, which must be a hashable tuple
                # — an unhashable `directed`/`propagates` value (e.g. a TOML array) would otherwise
                # raise an uncaught TypeError there and lose this edge's own E_BAD_TOP shape-error
                # exit code (10) to the generic runtime-failure exit (65) instead.
                continue

            resolved_edges.append({
                "file_label": file_label,
                "index": i,
                "edge": edge,
                "relation": relation,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "directed": edge.get("directed", True),
                "propagates": edge.get("propagates"),
            })

    seen = {}
    for rec in resolved_edges:
        key = (canonical_key(rec["from_ref"]), rec["relation"], canonical_key(rec["to_ref"]),
               rec["directed"], rec["propagates"])
        label = f"{rec['file_label']}.edges[{rec['index']}]"
        if key in seen:
            errors.append(issue(
                E_DUP_EDGE,
                f"{label}: duplicate of {seen[key]} (identical from/relation/to/directed/propagates)",
            ))
        else:
            seen[key] = label

    return errors, resolved_edges


def exit_code_for(errors: list) -> int:
    return min((e["code"] for e in errors), default=0)


# ==================================================================================================
# Propagation graph + traversal (the IMPACT QUERY and its `depends` reverse)
# ==================================================================================================

def build_propagation_edges(resolved_edges: list, relation_filter=None) -> list:
    """Returns a list of `(source_ref, target_ref, edge_display)` triples: one per resolved edge
    whose §C-or-`propagates` direction is NOT `"none"`. `edge_display` is
    `{"from": <canonical>, "relation": ..., "to": <canonical>}` — the edge's own literal assertion
    (regardless of which way it propagates), used unchanged as one `chain[]` link."""
    out = []
    for rec in resolved_edges:
        relation = rec["relation"]
        if relation_filter and relation not in relation_filter:
            continue
        direction = relation_direction(relation, rec["propagates"])
        if direction == "none":
            continue
        from_ref, to_ref = rec["from_ref"], rec["to_ref"]
        edge_display = {
            "from": canonical_string(from_ref),
            "relation": relation,
            "to": canonical_string(to_ref),
        }
        if direction == "to-from":
            out.append((to_ref, from_ref, edge_display))
        else:  # "from-to"
            out.append((from_ref, to_ref, edge_display))
    return out


def reverse_propagation_edges(propagation_edges: list) -> list:
    """Swaps source/target on every propagation edge — the `depends` query walks the SAME
    propagation graph `impacted` does, just backward ("walk against propagation")."""
    return [(target_ref, source_ref, edge_display) for source_ref, target_ref, edge_display in
            propagation_edges]


def _has_cycle(used_edges: set) -> bool:
    """Three-color DFS cycle detection over the edges ACTUALLY exercised by one traversal (never
    the whole relations graph — only the subgraph reachable, via overlap, from this query's own
    starting entities).

    Iterative, not recursive: an explicit stack of (node, resumable neighbor iterator) pairs, not
    a function calling itself once per node on a long chain — a deep (not necessarily cyclic)
    chain of edges must not consume Python stack depth per node and crash a query with a
    RecursionError."""
    adj = {}
    for s, t in used_edges:
        adj.setdefault(s, set()).add(t)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}

    for start in list(adj):
        if color.get(start, WHITE) != WHITE:
            continue
        color[start] = GRAY
        stack = [(start, iter(adj.get(start, ())))]
        while stack:
            node, neighbors = stack[-1]
            advanced = False
            for nxt in neighbors:
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    return True
                if c == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adj.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
    return False


def traverse(start_refs: list, propagation_edges: list, max_depth=None) -> tuple:
    """Cycle-safe, deterministic multi-source BFS. Returns `(hits, cycles_detected)`.

    `hits` is a list of `{"entity": <canonical str>, "depth": N, "chain": [...]}`, sorted by
    `(depth, entity)` ("deterministic ordering"). A starting entity is never itself a
    hit unless a real edge chain leads back to it (a cycle through the graph) — depth is the
    number of edges in the shortest chain that explains the hit. When two chains tie at the same
    shortest depth, the lexicographically smaller `(from, relation, to)` chain wins, for
    determinism.

    `propagation_edges` is `(source_ref, target_ref, edge_display)` triples as returned by
    `build_propagation_edges` (for `impacted`) or `reverse_propagation_edges` (for `depends`).

    LEVEL-BY-LEVEL, not a single FIFO queue with in-place chain updates: every node newly
    discovered at one depth is finalized (its lexicographically-smallest tied chain decided, from
    every node at the depth just before it that reaches it) BEFORE expansion to the next depth
    starts. A single shared queue that expands a node as soon as it is dequeued can expand a node
    using a chain that a LATER, still-queued sibling at the same depth would have improved on —
    its own descendants then inherit the stale, non-minimal chain instead of the corrected one.
    Finalizing per level closes that gap; input edge order can no longer change which chain a hit
    reports."""
    seen_start_keys = set()
    frontier = []  # [(ref, chain)] — every entry in one call is at the SAME depth
    for ref in start_refs:
        key = canonical_key(ref)
        if key in seen_start_keys:
            continue
        seen_start_keys.add(key)
        frontier.append((ref, []))

    visited = {}  # canonical_key -> (depth, chain, ref); set once, never revised
    used_edges = set()  # (source_canonical_key, target_canonical_key)
    processed = set()  # canonical_key already expanded as a source (cycle-safe: expand once)
    depth = 0

    while frontier and (max_depth is None or depth < max_depth):
        candidates = {}  # target_key -> (chain, target_ref); the best tied chain THIS level found
        for cur_ref, chain in frontier:
            cur_key = canonical_key(cur_ref)
            if cur_key in processed:
                continue
            processed.add(cur_key)
            for source_ref, target_ref, edge_display in propagation_edges:
                if not overlap(cur_ref, source_ref):
                    continue
                target_key = canonical_key(target_ref)
                used_edges.add((cur_key, target_key))
                new_chain = chain + [edge_display]
                if (target_key not in candidates
                        or _chain_key(new_chain) < _chain_key(candidates[target_key][0])):
                    candidates[target_key] = (new_chain, target_ref)

        depth += 1
        next_frontier = []
        for target_key, (chain, target_ref) in candidates.items():
            if target_key in visited:
                continue  # an earlier (shallower) level already finalized this node
            visited[target_key] = (depth, chain, target_ref)
            if target_key not in processed:
                next_frontier.append((target_ref, chain))
        frontier = next_frontier

    hits = [
        {"entity": canonical_string(ref), "depth": d, "chain": c}
        for _key, (d, c, ref) in visited.items()
    ]
    hits.sort(key=lambda h: (h["depth"], h["entity"]))
    return hits, _has_cycle(used_edges)
