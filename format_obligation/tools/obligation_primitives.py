#!/usr/bin/env python3
"""Policy-free primitives shared by the core and authority-composed Obligation folds.

This module owns byte/input handling, bounded diagnostics, identity and binding arithmetic,
deadline arithmetic, chain walking, worst-of projection, and the stdout boundary.  It deliberately
does not define a duty grammar, event vocabulary, actor-admission rule, lifecycle, or projection.

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

import datetime
import hashlib
import io
import itertools
import json
import math
import os
import pathlib
import re
import select
import subprocess
import sys
from zoneinfo import ZoneInfo

REPORT_SCHEMA = None
REPORT_SCHEMA_REASON = "library; report contracts are declared by the calling tools"

DEADLINE_TIME_ZONE = "Europe/Zagreb"
E_BLOCKED = 1
E_EVENT_SCHEMA = 10
E_REQUIRED_FIELD = 11
E_AUTHORITY = 12
E_IDENTITY = 13
E_UNKNOWN_OCCURRENCE = 14
E_TRANSITION = 15
E_SEQUENCE = 16
E_BINDING = 17
E_BOUNDARY = 18
E_BATCH = 19
E_EXTENSION = 20
E_USAGE = 64
E_INPUT = 65
E_OUTPUT = 74
E_SELFTEST = 99
MAX_INT64 = 2 ** 63 - 1
MIN_INT64 = -(2 ** 63)
MAX_JSON_DEPTH = 64
DIAGNOSTIC_REASON_MAX_CHARS = 2000
_SAFE_REPR_MAX_CHARS = 1000
_SAFE_REPR_MAX_ITEMS = 500
_SAFE_REPR_STR_PREVIEW_CHARS = 200
_REASON_TRUNCATION_MARKER = "...<truncated>"
JOIN_ERROR_LIST_MAX_ITEMS = 10
JOIN_ERROR_LIST_ITEM_MAX_CHARS = 100

RFC3339_RE = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])[Tt]"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:(?:[0-5][0-9]|60)(?:\.[0-9]{1,6})?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)
_ESCAPE_SEQUENCE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")


class FoldFail(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


def _escape_unicode_safe(s: str) -> str:
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


def _backoff_escape_boundary(s: str, keep: int) -> int:
    for match in _ESCAPE_SEQUENCE_RE.finditer(s, max(0, keep - 5), min(len(s), keep + 6)):
        if match.start() < keep < match.end():
            return match.start()
    return keep


def _cap_with_marker(s: str, max_chars: int, marker: str = "...>") -> str:
    keep = _backoff_escape_boundary(s, max(max_chars - len(marker), 0))
    return s[:keep] + marker


def _safe_repr_is_container(value) -> bool:
    return isinstance(value, (list, tuple, dict, set, frozenset))


def _safe_repr_scalar(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 64:
        return f"<{int(value.bit_length() * math.log10(2)) + 1}-digit integer>"
    if isinstance(value, str) and len(value) > _SAFE_REPR_STR_PREVIEW_CHARS:
        text = repr(value[:_SAFE_REPR_STR_PREVIEW_CHARS]) + "...>"
    else:
        text = repr(value)
    return _cap_with_marker(text, _SAFE_REPR_MAX_CHARS) if len(text) > _SAFE_REPR_MAX_CHARS else text


def safe_repr_for_error(value) -> str:
    if not _safe_repr_is_container(value):
        return _safe_repr_scalar(value)
    parts, total_len, items_seen = [], 0, 0
    stack = [("val", value)]
    while stack:
        if total_len >= _SAFE_REPR_MAX_CHARS or items_seen >= _SAFE_REPR_MAX_ITEMS:
            break
        kind, payload = stack.pop()
        if kind == "tok":
            parts.append(payload)
            total_len += len(payload)
            continue
        value = payload
        items_seen += 1
        if not _safe_repr_is_container(value):
            text = _safe_repr_scalar(value)
            parts.append(text)
            total_len += len(text)
            continue
        if isinstance(value, dict):
            open_b, close_b, source = "{", "}", value.items()
        elif isinstance(value, tuple):
            open_b, close_b, source = "(", ")", value
        elif isinstance(value, list):
            open_b, close_b, source = "[", "]", value
        else:
            open_b, close_b, source = "{", "}", value
        total_n = len(value)
        take = min(max(_SAFE_REPR_MAX_ITEMS - items_seen, 0), total_n)
        entries = list(itertools.islice(source, take))
        sequence = [("tok", open_b)]
        for index, item in enumerate(entries):
            if index:
                sequence.append(("tok", ", "))
            if isinstance(value, dict):
                key, val = item
                sequence.extend((("val", key), ("tok", ": "), ("val", val)))
            else:
                sequence.append(("val", item))
        if total_n > take:
            if entries:
                sequence.append(("tok", ", "))
            sequence.append(("tok", f"...({total_n - take} more)"))
        sequence.append(("tok", close_b))
        stack.extend(reversed(sequence))
    text = "".join(parts)
    return _cap_with_marker(text, _SAFE_REPR_MAX_CHARS) if len(text) > _SAFE_REPR_MAX_CHARS or stack else text


def bound_reason(text) -> str:
    text = _escape_unicode_safe(text if isinstance(text, str) else str(text))
    return text if len(text) <= DIAGNOSTIC_REASON_MAX_CHARS else _cap_with_marker(
        text, DIAGNOSTIC_REASON_MAX_CHARS, marker=_REASON_TRUNCATION_MARKER)


def bound_path_preview(path) -> str:
    text = _escape_unicode_safe(str(path))
    return text if len(text) <= _SAFE_REPR_STR_PREVIEW_CHARS else _cap_with_marker(
        text, _SAFE_REPR_STR_PREVIEW_CHARS)


def _bound_join_item(item) -> str:
    text = item if isinstance(item, str) else safe_repr_for_error(item)
    return text if len(text) <= JOIN_ERROR_LIST_ITEM_MAX_CHARS else _cap_with_marker(
        text, JOIN_ERROR_LIST_ITEM_MAX_CHARS)


def join_error_list(items: list, limit: int = JOIN_ERROR_LIST_MAX_ITEMS) -> str:
    shown = items[:limit]
    text = ", ".join(_bound_join_item(item) for item in shown)
    if len(items) > len(shown):
        text += f", ... and {len(items) - len(shown)} more"
    return text


def unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def loads_strict(source: str):
    return json.loads(source, object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def find_json_input_defect(obj, max_depth: int = MAX_JSON_DEPTH):
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return ("a string cannot encode as UTF-8 (contains a lone UTF-16 surrogate): "
                        f"{safe_repr_for_error(value)}")
        elif isinstance(value, float) and not math.isfinite(value):
            return "non-finite number is not a JSON value"
        elif isinstance(value, dict):
            if depth > max_depth:
                return f"nesting is deeper than {max_depth} levels"
            for key, val in value.items():
                stack.extend(((key, depth), (val, depth + 1)))
        elif isinstance(value, list):
            if depth > max_depth:
                return f"nesting is deeper than {max_depth} levels"
            stack.extend((item, depth + 1) for item in value)
    return None


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        raise FoldFail(E_INPUT, f"cannot read UTF-8 input: {bound_path_preview(path.name)}")


def load_object(path: pathlib.Path) -> dict:
    try:
        value = loads_strict(read_text(path))
    except FoldFail:
        raise
    except (ValueError, TypeError, RecursionError):
        raise FoldFail(E_INPUT, f"invalid JSON or duplicate keys: {bound_path_preview(path.name)}")
    if not isinstance(value, dict):
        raise FoldFail(E_INPUT, f"top level must be a JSON object: {bound_path_preview(path.name)}")
    defect = find_json_input_defect(value)
    if defect is not None:
        raise FoldFail(E_INPUT, f"{defect} in {bound_path_preview(path.name)}")
    return value


def load_event_records(path: pathlib.Path, *, allow_empty: bool = False) -> tuple[list[dict], list[int]]:
    """Parse JSONL records and return source line numbers separately from record data."""
    try:
        lines = path.read_bytes().split(b"\n")
    except (OSError, ValueError):
        raise FoldFail(E_INPUT, f"cannot read UTF-8 input: {bound_path_preview(path.name)}")
    rows, line_numbers = [], []
    for number, line in enumerate(lines, 1):
        try:
            if number < len(lines) and line.endswith(b"\r"):
                line = line[:-1]
            if b"\r" in line:
                raise ValueError("bare CR is not a JSONL record separator")
            source = line.decode("utf-8", errors="strict")
            if not source.strip():
                continue
            row = loads_strict(source)
        except (ValueError, TypeError, RecursionError):
            raise FoldFail(E_INPUT, f"invalid JSON or duplicate keys at {bound_path_preview(path.name)}:{number}")
        if not isinstance(row, dict):
            raise FoldFail(E_INPUT, f"event line must be a JSON object at {bound_path_preview(path.name)}:{number}")
        defect = find_json_input_defect(row)
        if defect is not None:
            raise FoldFail(E_INPUT, f"{defect} at {bound_path_preview(path.name)}:{number}")
        rows.append(row)
        line_numbers.append(number)
    if not rows and not allow_empty:
        raise FoldFail(E_INPUT, f"events file is empty: {bound_path_preview(path.name)}")
    return rows, line_numbers


def load_events(path: pathlib.Path, *, allow_empty: bool = False) -> list[dict]:
    """Legacy adapter retaining the pre-split synthetic line field and its collision error."""
    rows, line_numbers = load_event_records(path, allow_empty=allow_empty)
    for row, number in zip(rows, line_numbers):
        if "_line" in row:
            raise FoldFail(
                E_EVENT_SCHEMA,
                f"event must contain exactly the closed envelope fields at "
                f"{bound_path_preview(path.name)}:{number}",
            )
        row["_line"] = number
    return rows


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def occurrence_id(duty_id: str, trigger_event_id: str) -> str:
    return digest({"v": 1, "duty": duty_id, "trigger_event_id": trigger_event_id})


def event_ref(event: dict) -> str:
    return f"{event.get('id')}#{event.get('seq')}"


def natural_key(duty: dict) -> str:
    trigger = duty.get("trigger")
    return digest({
        "v": 1, "scope": duty.get("scope"), "source": duty.get("source"),
        "trigger_event": trigger.get("event") if isinstance(trigger, dict) else None,
        "trigger_condition": trigger.get("condition") if isinstance(trigger, dict) else None,
        "action": duty.get("action"), "target": duty.get("target"),
    })


def is_nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_time(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value[:10] + "T" + value[11:].replace("Z", "+00:00").replace("z", "+00:00"))


def is_rfc3339(value) -> bool:
    if not is_nonblank(value) or not RFC3339_RE.match(value):
        return False
    try:
        parsed = parse_time(value)
        return parsed.tzinfo is not None and 2 <= parsed.year <= 9998
    except ValueError:
        return False


def is_iso_date(value) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def is_deadline(value) -> bool:
    return is_iso_date(value) or (isinstance(value, str) and value.startswith("boundary:") and is_nonblank(value[9:]))


def recorded_date(value: str) -> datetime.date:
    return datetime.date.fromisoformat(value[:10])


def deadline_tightens(rule: str, firing: str) -> bool:
    return recorded_date(firing) <= recorded_date(rule) if is_iso_date(rule) and is_iso_date(firing) else firing == rule


def dated_deadline_crossed(required_by: str, at: str) -> bool:
    return parse_time(at).astimezone(ZoneInfo(DEADLINE_TIME_ZONE)).date() > recorded_date(required_by)


def evaluation_binding(duty: dict, occurrence: dict, payload: dict) -> str:
    return digest({
        "source_revision": duty["source_revision"], "predicate_digest": payload["predicate_digest"],
        "target_revision": occurrence["target_revision"], "evidence_digest": payload["evidence_digest"],
        "context_hash": payload["context_hash"],
    })


def valid_evaluation(duty: dict, occurrence: dict, payload: dict) -> bool:
    return (isinstance(payload.get("result"), bool) and is_rfc3339(payload.get("evaluated_at"))
            and is_nonblank(payload.get("predicate_digest")) and is_nonblank(payload.get("evidence_digest"))
            and is_nonblank(payload.get("context_hash"))
            and payload.get("predicate_digest") == digest(duty["discharge_predicate"])
            and payload.get("bound_revision") == evaluation_binding(duty, occurrence, payload)
            and (occurrence["context_hash"] is None or payload["context_hash"] == occurrence["context_hash"]))


def evaluation_matches_final(duty: dict, occurrence: dict, evaluation: dict) -> bool:
    expected = digest({
        "source_revision": duty["source_revision"], "predicate_digest": digest(duty["discharge_predicate"]),
        "target_revision": occurrence["target_revision"], "evidence_digest": evaluation["evidence_digest"],
        "context_hash": evaluation["context_hash"],
    })
    return (not evaluation["invalidated"] and evaluation["source_revision"] == duty["source_revision"]
            and evaluation["predicate_digest"] == digest(duty["discharge_predicate"])
            and evaluation["context_hash"] == occurrence["context_hash"]
            and evaluation["bound_revision"] == expected)


def stale_binding_valid(payload: dict, duty: dict) -> bool:
    return (is_nonblank(payload.get("context_hash_old")) and is_nonblank(payload.get("context_hash_new"))
            and payload.get("context_hash_old") != payload.get("context_hash_new")
            and payload.get("reason") == "reevaluation_pending"
            and payload.get("bound_revision") == duty.get("source_revision")
            and is_nonblank(payload.get("boundary_id")))


def walk_chain(start: str, next_id, *, max_steps: int | None = None) -> tuple[list[str], bool]:
    """Return the visited ids and whether following ``next_id`` found a cycle."""
    visited, positions, cursor = [], {}, start
    while cursor is not None and (max_steps is None or len(visited) < max_steps):
        if cursor in positions:
            return visited, True
        positions[cursor] = len(visited)
        visited.append(cursor)
        cursor = next_id(cursor)
    return visited, False


CONTRIBUTIONS = ("FAIL", "PENDING", "WARN-recorded", "PASS")


def worst_of(contributions: list[str], *, empty: str = "WARN-recorded") -> str:
    for value in CONTRIBUTIONS:
        if value in contributions:
            return value
    return empty


def exact_fields(value, expected: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == expected


def string_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(is_nonblank(item) for item in value) and len(value) == len(set(value))


def validate_event_envelope_shape(event: dict, errors: list, *, envelope_fields: set[str],
                                  kinds: set[str], payload_fields: dict[str, set[str]],
                                  required_nonblank: tuple[str, ...], issue_fn,
                                  metadata_fields: set[str] = frozenset({"_line"})) -> bool:
    """Validate the common closed-envelope mechanics against caller-supplied policy tables."""
    before = len(errors)
    row_id = event.get("id")
    event_id = row_id if is_nonblank(row_id) else None
    if set(event) - metadata_fields != envelope_fields:
        issue_fn(errors, E_EVENT_SCHEMA, "event must contain exactly the closed envelope fields", row_id, event_id)
        return False
    if (not is_nonblank(event["id"]) or not isinstance(event["seq"], int)
            or isinstance(event["seq"], bool) or not (1 <= event["seq"] <= MAX_INT64)):
        issue_fn(errors, E_REQUIRED_FIELD,
                 "event id must be non-blank and seq must be a positive integer within the signed 64-bit range (1..2**63-1)",
                 row_id, event_id)
        return False
    if not isinstance(event["kind"], str) or event["kind"] not in kinds:
        issue_fn(errors, E_EVENT_SCHEMA, f"unknown event kind: {safe_repr_for_error(event['kind'])}", row_id, event["id"])
        return False
    if not is_rfc3339(event["recorded_at"]):
        issue_fn(errors, E_REQUIRED_FIELD, "recorded_at must be RFC3339 with an offset", row_id, event["id"])
    for field in required_nonblank:
        if not is_nonblank(event[field]):
            issue_fn(errors, E_REQUIRED_FIELD, f"event.{field} must be non-blank", row_id, event["id"])
    if not exact_fields(event["payload"], payload_fields[event["kind"]]):
        issue_fn(errors, E_REQUIRED_FIELD, f"{event['kind']} payload must contain exactly its required fields", row_id, event["id"])
    return len(errors) == before


def _escape_report_value(value):
    if isinstance(value, str):
        return _escape_unicode_safe(value)
    if isinstance(value, dict):
        return {(_escape_unicode_safe(k) if isinstance(k, str) else k): _escape_report_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_escape_report_value(v) for v in value]
    return value


def emit_report(report, **dumps_kwargs) -> str:
    dumps_kwargs["allow_nan"] = False
    return json.dumps(_escape_report_value(report), **dumps_kwargs)


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
    format_root = pathlib.Path(module_file).resolve().parent.parent
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
            return declaration, pathlib.Path(module_file).resolve().parent.parent
        frame = frame.f_back
    return None


def _internal_error_report(def_name: str) -> dict:
    error = {"code": E_INPUT, "reason": "report failed its schema"}
    return {"boundary_id": None, "duties": [],
            "aggregate": {"block": True, "required_actions": []},
            "validation": {"status": "unknown", "exit_code": E_INPUT,
                           "errors": [error], "warnings": [],
                           "duplicates_ignored": []}}


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
    """Run one Obligation CLI at the stdout byte boundary.

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


def output_boundary_selftest(script_path: pathlib.Path, valid_argv: list[str],
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
