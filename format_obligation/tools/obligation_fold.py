#!/usr/bin/env python3
"""Reference fold and validator for pure-core Obligation event logs. Not an enforcement service.

Usage:
  python3 obligation_fold.py fold --events EVENTS.jsonl --boundary BOUNDARY.json
  python3 obligation_fold.py --selftest

Exit codes:
  0   valid, unblocked fold; or all selftests pass
  1   valid fold whose aggregate block bit is set
  10  unknown event kind or invalid event envelope
  11  missing or invalid required event/duty/payload field
  13  duty/natural-key/occurrence identity failure or unresolved activation operand
  14  unknown occurrence
  15  illegal transition
  16  per-duty sequence gap
  17  predicate, evidence, revision, migration, or staleness binding failure
  18  invalid boundary schema, scope, cutoff, or caller-supplied clock field
  19  incomplete or inconsistent evaluator batch
  20  input uses an authority extension outside the core grammar
  64  usage/argument error
  65  unreadable, malformed, duplicate-key, or non-object input; unexpected error
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

Validation codes take precedence over the valid-fold block code. When several
validation classes occur, the numerically lowest class is the process exit.

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

import argparse
import contextlib
import copy
import datetime
import hashlib
import itertools
import io
import json
import math
import os
import pathlib
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

REPORT_SCHEMA = ("schema/obligation-fold-0.1.0-draft.schema.json", "fold_report")

import obligation_primitives as primitives
from schema_conformance import boundary_defect_controls

primitives.register_report_schema(__file__, REPORT_SCHEMA)


HERE = pathlib.Path(__file__).resolve().parent
SCRIPT_PATH = pathlib.Path(__file__).resolve()
FIXTURES = HERE.parent / "fixtures" / "fold-cases"
# A dated required_by crosses at 00:00 on the next day in this time zone. The zone is fixed
# in the reference implementation; it is not a field of the record.
DEADLINE_TIME_ZONE = "Europe/Zagreb"

E_BLOCKED = 1
E_EVENT_SCHEMA = 10
E_REQUIRED_FIELD = 11
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

# Every integer FIELD this format defines (`seq`, `evaluator_batch.event_count`, an
# `event_frontier` value) is a signed 64-bit integer: MIN_INT64 <= value <= MAX_INT64. Nothing in
# spec/ states a narrower or wider range for any of them, so this is the stated, enforced default.
# A value outside this range is a shape/input error, never a crash: JSON integers are themselves
# unbounded, but Python's own int-to-decimal-string conversion (`sys.get_int_max_str_digits()`,
# 4300 digits by default) would itself raise for a sufficiently huge one, so every comparison
# against these bounds uses plain binary `<`/`>` (never a string conversion of the value).
MAX_INT64 = 2 ** 63 - 1
MIN_INT64 = -(2 ** 63)

EVENT_FIELDS = {"id", "seq", "kind", "recorded_at", "actor", "batch_id", "payload"}
KINDS = {
    "minted", "fired", "predicate_evaluated", "deadline_crossed", "stale_detected",
    "superseded", "discharged", "source_revised", "dropped",
}
EVALUATOR_KINDS = {"deadline_crossed", "predicate_evaluated", "stale_detected"}
PAYLOAD_FIELDS = {
    "minted": {"duty"},
    "fired": {"occurrence_id", "trigger_event_id", "trigger", "required_by", "target_revision"},
    "predicate_evaluated": {
        "occurrence_id", "result", "bound_revision", "evaluated_at",
        "predicate_digest", "evidence_digest", "context_hash", "boundary_id",
    },
    "deadline_crossed": {"occurrence_id", "at", "bound_revision", "boundary_id"},
    "stale_detected": {
        "occurrence_ids", "context_hash_old", "context_hash_new", "reason",
        "bound_revision", "boundary_id",
    },
    "superseded": {"by"},
    "discharged": {"occurrence_ids", "evidence_refs"},
    "source_revised": {"from_revision", "to_revision"},
    "dropped": {"reason"},
}
DUTY_FIELDS = {
    "id", "natural_key", "scope", "source", "source_revision", "trigger", "action", "target",
    "retrigger", "duty_kind", "occurrence_rule", "required_by", "discharge_predicate",
    "created_by", "created_at", "occurrences", "status", "discharge_evidence", "supersedes",
}
OWNER_KINDS = {"fired", "superseded", "source_revised", "dropped"}
BOUNDARY_FIELDS = {
    "schema", "boundary_id", "recorded_cutoff", "in_scope_duty_ids", "evaluator_batch",
    "observations_manifest", "event_frontier",
}
BATCH_FIELDS = {"batch_id", "input_snapshot_digest", "committed", "event_count", "event_ids"}
MANIFEST_ENTRY_FIELDS = {"kind", "event_id", "bound_revision"}
# RFC 3339 date-time: full-date "T"/"t" full-time, where full-time is
# partial-time (with optional fractional seconds) followed by a time-offset
# of "Z"/"z" or a numeric +hh:mm/-hh:mm offset (hh 00-23, mm 00-59). ASCII digits
# only; every field is range-bounded. Second 60 passes the grammar but parse_time
# rejects it: leap seconds and year 0000 are outside the supported subset. Fractions
# are at most six digits (microseconds; longer ones would be truncated and could
# reorder two times), and years 0001 and 9999 are rejected (a deadline's time-zone
# conversion could overflow at either end). Checked before parsing so the
# more lenient ISO-8601 grammar accepted by datetime.fromisoformat (e.g. week
# dates) cannot pass as RFC 3339.
RFC3339_RE = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])[Tt]"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:(?:[0-5][0-9]|60)(?:\.[0-9]{1,6})?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


class FoldFail(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise FoldFail(E_USAGE, message)


class TypedEvent(dict):
    """Shape-safe event produced by the one post-JSON typing pass.

    ``shape_valid`` records whether the raw envelope/payload could be represented without a
    type or closed-shape repair.  Downstream phases only see these records; a repaired event is
    retained for sequence/group validation but is never interpreted as a lifecycle event.
    """

    def __init__(self, value, *, shape_valid: bool):
        super().__init__(value)
        self.shape_valid = shape_valid


class TypedBoundary(dict):
    """Shape-safe boundary produced by the one post-JSON typing pass."""


def _typed_string(value) -> str:
    return value if isinstance(value, str) else ""


def _typed_string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_typed_string(item) for item in value]


def _payload_type_valid(kind: str, payload: dict) -> bool:
    """Coarse JSON types required before lifecycle code may read a payload directly."""
    strings = {
        "fired": {"occurrence_id", "trigger_event_id", "required_by", "target_revision"},
        "predicate_evaluated": {
            "occurrence_id", "bound_revision", "evaluated_at", "predicate_digest",
            "evidence_digest", "context_hash", "boundary_id",
        },
        "deadline_crossed": {"occurrence_id", "at", "bound_revision", "boundary_id"},
        "stale_detected": {
            "context_hash_old", "context_hash_new", "reason", "bound_revision", "boundary_id",
        },
        "superseded": {"by"},
        "source_revised": {"from_revision", "to_revision"},
        "dropped": {"reason"},
    }
    if any(not isinstance(payload.get(field), str) for field in strings.get(kind, set())):
        return False
    if kind == "predicate_evaluated" and not isinstance(payload.get("result"), bool):
        return False
    if kind == "fired":
        trigger = payload.get("trigger")
        if (not exact_fields(trigger, {"event", "condition"})
                or any(not isinstance(value, str) for value in trigger.values())):
            return False
    if kind in {"stale_detected", "discharged"}:
        if not isinstance(payload.get("occurrence_ids"), list) or any(
                not isinstance(value, str) for value in payload["occurrence_ids"]):
            return False
    if kind == "discharged":
        if not isinstance(payload.get("evidence_refs"), list) or any(
                not isinstance(value, str) for value in payload["evidence_refs"]):
            return False
    return True


def _duty_type_valid(duty) -> bool:
    if not exact_fields(duty, DUTY_FIELDS):
        return isinstance(duty, dict)
    string_fields = DUTY_FIELDS - {
        "trigger", "discharge_predicate", "occurrences", "discharge_evidence", "supersedes",
    }
    if any(not isinstance(duty[field], str) for field in string_fields):
        return False
    trigger = duty["trigger"]
    if (not exact_fields(trigger, {"event", "condition"})
            or any(not isinstance(value, str) for value in trigger.values())):
        return False
    predicate = duty["discharge_predicate"]
    if (not exact_fields(predicate, {"resolver", "operands", "resolved"})
            or not isinstance(predicate.get("resolver"), str)
            or not isinstance(predicate.get("operands"), str)
            or not isinstance(predicate.get("resolved"), bool)):
        return False
    return (isinstance(duty["occurrences"], dict)
            and isinstance(duty["discharge_evidence"], list)
            and all(isinstance(value, str) for value in duty["discharge_evidence"])
            and (duty["supersedes"] is None or isinstance(duty["supersedes"], str)))


def _type_event(raw: dict, errors: list) -> TypedEvent:
    """Turn one parsed JSON object into a total, shape-safe event record."""
    row_id = raw.get("id")
    event_id = row_id if is_nonblank(row_id) else None
    shape_valid = True
    if set(raw) != EVENT_FIELDS:
        issue(errors, E_EVENT_SCHEMA, "event must contain exactly the closed envelope fields",
              row_id, event_id)
        shape_valid = False

    kind_raw = raw.get("kind")
    if not isinstance(kind_raw, str) or kind_raw not in KINDS:
        issue(errors, E_EVENT_SCHEMA, f"unknown event kind: {safe_repr_for_error(kind_raw)}",
              row_id, event_id)
        shape_valid = False
    kind = kind_raw if isinstance(kind_raw, str) else "__invalid_kind__"

    if not isinstance(raw.get("id"), str):
        issue(errors, E_REQUIRED_FIELD, "event.id must be a string", row_id, event_id)
        shape_valid = False
    if not isinstance(raw.get("seq"), int) or isinstance(raw.get("seq"), bool):
        issue(errors, E_REQUIRED_FIELD,
              "event.seq must be a positive integer within the signed 64-bit range (1..2**63-1)",
              row_id, event_id)
        shape_valid = False
    for field in ("recorded_at", "actor", "batch_id"):
        if not isinstance(raw.get(field), str):
            issue(errors, E_REQUIRED_FIELD, f"event.{field} must be a string", row_id, event_id)
            shape_valid = False

    payload_raw = raw.get("payload")
    payload = copy.deepcopy(payload_raw) if isinstance(payload_raw, dict) else {}
    if not isinstance(payload_raw, dict):
        issue(errors, E_REQUIRED_FIELD, "event.payload must be an object", row_id, event_id)
        shape_valid = False
    elif kind in KINDS:
        if set(payload) != PAYLOAD_FIELDS[kind]:
            issue(errors, E_REQUIRED_FIELD,
                  f"{kind} payload must contain exactly its required fields", row_id, event_id)
            shape_valid = False
        elif kind == "minted":
            if not _duty_type_valid(payload.get("duty")):
                issue(errors, E_REQUIRED_FIELD, "minted duty fields have invalid JSON types",
                      row_id, event_id)
                shape_valid = False
        elif not _payload_type_valid(kind, payload):
            issue(errors, E_REQUIRED_FIELD, f"{kind} payload fields have invalid JSON types",
                  row_id, event_id)
            shape_valid = False

        # Normalize even a rejected payload.  Boundary/batch cross-reference checks may inspect
        # retained events that lifecycle folding correctly skipped, so those checks must still see
        # total container/string types rather than the raw malformed values.
        for field in {
            "occurrence_id", "trigger_event_id", "required_by", "target_revision",
            "bound_revision", "evaluated_at", "predicate_digest", "evidence_digest",
            "context_hash", "boundary_id", "at", "context_hash_old", "context_hash_new",
            "reason", "by", "from_revision", "to_revision",
        } & set(payload):
            payload[field] = _typed_string(payload[field])
        for field in {"occurrence_ids", "evidence_refs"} & set(payload):
            payload[field] = _typed_string_list(payload[field])
        if "result" in payload and not isinstance(payload["result"], bool):
            payload["result"] = False
        if "trigger" in payload and not isinstance(payload["trigger"], dict):
            payload["trigger"] = {}
        if "duty" in payload and not isinstance(payload["duty"], dict):
            payload["duty"] = {}

    typed = {
        "id": _typed_string(raw.get("id")),
        "seq": raw.get("seq") if isinstance(raw.get("seq"), int)
        and not isinstance(raw.get("seq"), bool) else 0,
        "kind": kind,
        "recorded_at": _typed_string(raw.get("recorded_at")),
        "actor": _typed_string(raw.get("actor")),
        "batch_id": _typed_string(raw.get("batch_id")),
        "payload": payload,
    }
    return TypedEvent(typed, shape_valid=shape_valid)


def _type_boundary(raw: dict, errors: list) -> TypedBoundary:
    """Turn a parsed boundary object into a total record safe for every later phase."""
    if set(raw) != BOUNDARY_FIELDS:
        extra = set(raw) - BOUNDARY_FIELDS
        reason = ("caller-supplied clock field is forbidden"
                  if extra & {"clock", "now", "current_time"}
                  else "boundary must contain exactly the closed fields")
        issue(errors, E_BOUNDARY, reason)

    batch_raw = raw.get("evaluator_batch")
    batch_raw = batch_raw if isinstance(batch_raw, dict) else {}
    event_ids_raw = batch_raw.get("event_ids")
    event_ids_raw = event_ids_raw if isinstance(event_ids_raw, dict) else {}
    batch = {
        "batch_id": _typed_string(batch_raw.get("batch_id")),
        "input_snapshot_digest": _typed_string(batch_raw.get("input_snapshot_digest")),
        "committed": batch_raw.get("committed") if isinstance(batch_raw.get("committed"), bool) else False,
        "event_count": batch_raw.get("event_count") if isinstance(batch_raw.get("event_count"), int)
        and not isinstance(batch_raw.get("event_count"), bool) else -1,
        "event_ids": {
            kind: _typed_string_list(event_ids_raw.get(kind)) for kind in EVALUATOR_KINDS
        },
    }
    if set(batch_raw) != BATCH_FIELDS:
        issue(errors, E_BOUNDARY, "evaluator_batch must contain exactly the closed metadata fields")
    if set(event_ids_raw) != EVALUATOR_KINDS:
        issue(errors, E_BATCH, "evaluator batch metadata is incomplete or invalid")
    if any(not isinstance(event_ids_raw.get(kind), list)
           or any(not isinstance(value, str) for value in event_ids_raw.get(kind, []))
           for kind in EVALUATOR_KINDS):
        issue(errors, E_BATCH, "evaluator batch event_ids fields have invalid JSON types")

    scope_raw = raw.get("in_scope_duty_ids")
    scope = _typed_string_list(scope_raw)
    manifest_raw = raw.get("observations_manifest")
    manifest = {}
    if isinstance(manifest_raw, dict):
        for duty_id, duty_manifest_raw in manifest_raw.items():
            if not isinstance(duty_manifest_raw, dict):
                issue(errors, E_BATCH, "duty observation manifest must be an object", duty_id)
                manifest[duty_id] = {}
                continue
            duty_manifest = {}
            for oid, entries_raw in duty_manifest_raw.items():
                if not isinstance(entries_raw, list):
                    issue(errors, E_BATCH, "observation manifest occurrence entries must be an array",
                          duty_id, occurrence_id=oid)
                    duty_manifest[oid] = []
                    continue
                entries = []
                for entry_raw in entries_raw:
                    if not isinstance(entry_raw, dict):
                        entries.append({})
                        continue
                    entries.append({key: _typed_string(entry_raw.get(key))
                                    for key in MANIFEST_ENTRY_FIELDS}
                                   if set(entry_raw) == MANIFEST_ENTRY_FIELDS else copy.deepcopy(entry_raw))
                duty_manifest[oid] = entries
            manifest[duty_id] = duty_manifest

    frontier_raw = raw.get("event_frontier")
    frontier = {}
    if isinstance(frontier_raw, dict):
        for key, value in frontier_raw.items():
            frontier[key] = value if isinstance(value, int) and not isinstance(value, bool) else 0

    return TypedBoundary({
        "schema": _typed_string(raw.get("schema")),
        "boundary_id": _typed_string(raw.get("boundary_id")),
        "recorded_cutoff": _typed_string(raw.get("recorded_cutoff")),
        "in_scope_duty_ids": scope,
        "evaluator_batch": batch,
        "observations_manifest": manifest,
        "event_frontier": frontier,
    })


def type_inputs(raw_events: list[dict], raw_boundary: dict, errors: list) -> tuple[list[TypedEvent], TypedBoundary]:
    """The sole raw-JSON-to-runtime-record conversion used by the core validator."""
    return ([_type_event(event, errors) for event in raw_events],
            _type_boundary(raw_boundary, errors))


# ------------------------------------------------------------------------------------------------
# Bounded diagnostics: every `reason` text this tool ever reports stays bounded for ANY input,
# including an adversarial one (a 10,000-character --events/--boundary path, a million-character
# field value, a hostile key name, hundreds of bad entries aggregated into one message). Byte-for-
# byte the same helpers (and the same reasoning) as `format_impact-rule`'s own `impact_rules.py`
# and `format_fill-contract`'s own `fill_common.py` (`_cap_with_marker`, `_safe_repr_scalar`,
# `safe_repr_for_error`, `bound_reason`, `bound_path_preview`, `join_error_list`).
# ------------------------------------------------------------------------------------------------

# The DOCUMENTED diagnostic bound: no single issue entry's `reason` this tool ever reports is
# longer than this many characters, for ANY input, including an adversarial one. Comfortably above
# `_SAFE_REPR_MAX_CHARS` (the cap on one `safe_repr_for_error` call's own output) to leave headroom
# for the fixed surrounding text every error message also carries.
DIAGNOSTIC_REASON_MAX_CHARS = 2000

_SAFE_REPR_MAX_CHARS = 1000
_SAFE_REPR_MAX_ITEMS = 500

# A `str` value longer than this is truncated to THIS MANY CHARACTERS BEFORE `repr()` ever sees it
# — never after. `repr()` on an arbitrarily large string is itself O(len(v)) work, and a hostile
# million-character string value must never reach `repr()` in full, whether at the top level or
# nested inside a container.
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
    """Truncates `s` so the RETURNED string, INCLUDING `marker`, is never longer than `max_chars`
    — the one place every bounded-text helper below (`_safe_repr_scalar`, `safe_repr_for_error`,
    `bound_reason`, `bound_path_preview`) enforces its own cap, so the marker's own length can
    never itself push a result past the cap it exists to enforce. The cut point is also backed off
    (`_backoff_escape_boundary`) so it never splits a `\\uXXXX` escape sequence in half."""
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
      formatting. `bit_length() > 64` is the chosen threshold. The digit count itself is an
      ESTIMATE from the bit length (`bit_length * log10(2)`, never by stringifying `v`).
    - A `str` longer than `_SAFE_REPR_STR_PREVIEW_CHARS` is sliced to that many characters BEFORE
      `repr()` is called on it — never repr'd in full first and truncated after."""
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
    foreign value this tool reads (any JSON scalar, or a container nesting one, to any depth). An
    explicit, heap-allocated work stack (never Python function recursion, so an adversarially DEEP
    container cannot exhaust the interpreter's own recursion limit) walks each container's elements
    through `itertools.islice` (never materializing a container in full first, so an adversarially
    WIDE container costs only the remaining item budget). At most `_SAFE_REPR_MAX_ITEMS` values
    (any kind, any depth) are rendered, and the assembled text is hard-truncated to
    `_SAFE_REPR_MAX_CHARS` characters (plus a trailing `...>` marker) before this function returns."""
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
    marker it appends — for ANY input, of any length, built through ANY path. Called from `issue()`
    and `make_failure_report()`, the two places every reason text this tool reports passes through
    on its way into a report, so the bound holds even for a call site that forgot to self-bound."""
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
    """Returns a short, BOUNDED preview of a filesystem path (or any other path-like value) safe to
    interpolate DIRECTLY into an error message — for ANY input, including an adversarially long
    `--events`/`--boundary` argument (a 10,000-character string), AND a path that is not valid
    Unicode at all. A CLI argument is decoded by Python from the OS's raw bytes using
    `surrogateescape` (e.g. a POSIX filename byte `\\xff` with no valid UTF-8 interpretation becomes
    the Python string `"\\udcff"`, a LONE UTF-16 surrogate) — `str(path)` on such a value is still a
    `str`, but one that cannot itself encode as UTF-8, so interpolating it RAW into a message would
    put a string this tool's own reports can never actually serialize into the report. Every such
    character is replaced with a visible, UTF-8-safe backslash escape here, BEFORE the length cap
    below, so the cap still holds on the ESCAPED text (escaping can only lengthen a string, never
    shorten it — capping first and escaping after could let a result exceed the cap)."""
    s = str(path)
    s = _escape_unicode_safe(s)
    if len(s) <= _SAFE_REPR_STR_PREVIEW_CHARS:
        return s
    return _cap_with_marker(s, _SAFE_REPR_STR_PREVIEW_CHARS)


JOIN_ERROR_LIST_MAX_ITEMS = 10

# Each item is bounded to THIS many characters (plus a trailing marker if truncated) BEFORE it is
# joined — not just the (possibly nonexistent) bounding a caller already applied. `join_error_list`
# does NOT assume its items are already individually bounded (an `occurrence_ids` entry is a raw,
# foreign string of up to the format's own length limits, not something the caller runs through
# `safe_repr_for_error` first): the chosen caller, `unknown occurrence(s)` in `fold_row`, passes the
# RAW occurrence_id strings straight from a `discharged`/`waived`/`stale_detected`/`waiver_lapsed`
# payload. `JOIN_ERROR_LIST_MAX_ITEMS` (10) of these, each capped to this many characters, plus the
# fixed `", ... and <N> more"` suffix (at most ~40 characters for any realistic count), is always
# comfortably inside `DIAGNOSTIC_REASON_MAX_CHARS` (2000) even before any caller-added prefix text —
# which is what makes the omitted-count suffix survive `bound_reason`'s own final cap: the defect
# this bound fixes is a handful of very long items alone consuming the ENTIRE 2,000-character budget,
# pushing the "... and N more" suffix (the only place a caller learns anything was left out) past the
# cut.
JOIN_ERROR_LIST_ITEM_MAX_CHARS = 100


def _bound_join_item(item) -> str:
    s = item if isinstance(item, str) else safe_repr_for_error(item)
    if len(s) <= JOIN_ERROR_LIST_ITEM_MAX_CHARS:
        return s
    return _cap_with_marker(s, JOIN_ERROR_LIST_ITEM_MAX_CHARS)


def join_error_list(items: list, limit: int = JOIN_ERROR_LIST_MAX_ITEMS) -> str:
    """Joins a LIST of strings into one `", "`-separated summary, bounded on TWO independent axes:
    an adversarial input (e.g. a `discharged`/`waived`/`stale_detected`/`waiver_lapsed` event naming
    hundreds of unknown `occurrence_ids`) can make `len(items)` itself unbounded (bounded here by
    `limit`, defaulting to `JOIN_ERROR_LIST_MAX_ITEMS`), AND any one item can itself be arbitrarily
    long (bounded here by `JOIN_ERROR_LIST_ITEM_MAX_CHARS`, applied to EACH of the `limit` shown
    items individually, BEFORE they are joined — never relying on a caller to have already bounded
    them). At most `limit` entries are ever shown, each at most `JOIN_ERROR_LIST_ITEM_MAX_CHARS`
    characters; a `", ... and N more"` suffix (computed from the FULL, original item count — never
    silently dropped, and never itself truncated by the per-item cap) names exactly how many were
    left out of the `limit` shown. Bounding each item's OWN length here, rather than only bounding
    the limit on item COUNT, is what keeps the omitted-count suffix from being squeezed out by a
    handful of very long items alone exhausting the final report-wide character cap
    (`DIAGNOSTIC_REASON_MAX_CHARS`) before the suffix is ever reached."""
    shown = items[:limit]
    text = ", ".join(_bound_join_item(item) for item in shown)
    remaining = len(items) - len(shown)
    if remaining > 0:
        text += f", ... and {remaining} more"
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


MAX_JSON_DEPTH = 64


def find_json_input_defect(obj, max_depth: int = MAX_JSON_DEPTH):
    """ITERATIVE walk (an explicit stack, not a function calling itself once per level of the
    input's own nesting) over one parsed JSON value. Returns a reason string for the first defect
    found, or None if the whole structure is clean:

    - a string (a dict KEY or a VALUE, at any nesting depth) that cannot encode as UTF-8
      (json.loads accepts a unicode escape for a lone UTF-16 surrogate, U+D800-U+DFFF — valid
      JSON syntax, but not a valid Unicode scalar value; canonical()'s own json.dumps would
      serialize it without complaint, and only digest()'s later .encode("utf-8") would crash —
      far from, and long after, the record that should have been rejected at load time instead);
    - a non-finite float (`inf`, `-inf`, `nan`), at any depth. `NaN`/`Infinity`/`-Infinity` as a
      bare JSON token are already rejected by `reject_constant` before this walk ever runs, but
      a finite-LOOKING numeral that overflows a Python float during parsing (`1e400`) is not a
      `parse_constant` case — Python's float() silently returns `inf` for it, so this is the one
      place that class of value is caught, at any nesting depth, as an input error rather than
      reaching a report (where `json.dumps(..., allow_nan=False)` would only raise at the last
      possible moment, or — without that flag — silently emit a non-standard `Infinity` token);
    - nesting deeper than max_depth (undocumented-if-unbounded nesting is itself a defect this
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
        elif isinstance(value, float) and not math.isfinite(value):
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


def force_utf8_stdout() -> None:
    """Delegate stdout encoding setup to the shared policy-free boundary."""
    return primitives.force_utf8_stdout()


def write_report(text: str) -> None:
    """Delegate complete report emission to the shared policy-free boundary."""
    return primitives.write_report(text)


def _quarantine_stdout_fd(failed_stdout=None) -> None:
    """Delegate shutdown quarantine to the shared policy-free boundary."""
    return primitives._quarantine_stdout_fd(failed_stdout)


def run_cli(main, argv=None) -> int:
    """Delegate CLI execution to the shared policy-free stdout boundary."""
    return primitives.run_cli(main, argv)


def output_boundary_selftest(script_path: pathlib.Path, valid_argv: list[str],
                             usage_argv: list[str]) -> list[tuple[str, bool, str]]:
    """Delegate the subprocess regression matrix to the shared policy-free boundary."""
    return primitives.output_boundary_selftest(script_path, valid_argv, usage_argv)


def emit_report(report, **dumps_kwargs) -> str:
    """The ONE funnel every report this tool prints to stdout is routed through before it reaches
    `write_report()`: recursively escapes every string in `report` the same way `bound_path_preview`
    already escapes one path, then serializes with `json.dumps(..., allow_nan=False)` — `allow_nan`
    is forced here regardless of what a caller passes. This is the BACKSTOP, not a substitute for
    bounding/escaping each field at the point it is built: every report constructor in this tool
    still runs every `reason` through `bound_reason` and every path through `bound_path_preview` at
    the source — this funnel exists so a report field that reaches `write_report()` without having gone
    through either (an argparse usage message built straight from the raw, `surrogateescape`-decoded
    argv text, or a future call site not yet written) still cannot make this tool emit a report its
    own strict reader would reject."""
    dumps_kwargs["allow_nan"] = False
    return json.dumps(_escape_report_value(report), **dumps_kwargs)


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


def load_events(path: pathlib.Path) -> list[dict]:
    """Split JSONL on LF bytes; accept CRLF and an optional final newline.

    Reject bare CR, including a final CR without LF. Decode each row strictly
    as UTF-8. Unicode line separators inside JSON strings remain string data.
    """
    try:
        lines = path.read_bytes().split(b"\n")
    except (OSError, ValueError):
        raise FoldFail(E_INPUT, f"cannot read UTF-8 input: {bound_path_preview(path.name)}")
    rows = []
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
            raise FoldFail(E_INPUT,
                            f"invalid JSON or duplicate keys at {bound_path_preview(path.name)}:{number}")
        if not isinstance(row, dict):
            raise FoldFail(E_INPUT,
                            f"event line must be a JSON object at {bound_path_preview(path.name)}:{number}")
        defect = find_json_input_defect(row)
        if defect is not None:
            raise FoldFail(E_INPUT, f"{defect} at {bound_path_preview(path.name)}:{number}")
        if "_line" in row:
            raise FoldFail(E_EVENT_SCHEMA,
                            f"event must contain exactly the closed envelope fields at "
                            f"{bound_path_preview(path.name)}:{number}")
        row["_line"] = number
        rows.append(row)
    if not rows:
        raise FoldFail(E_INPUT, f"events file is empty: {bound_path_preview(path.name)}")
    return rows


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def occurrence_id(duty_id: str, trigger_event_id: str) -> str:
    return digest({"v": 1, "duty": duty_id, "trigger_event_id": trigger_event_id})


def event_ref(event: dict) -> str:
    """Boundary-stable reference for an event in the (row id, seq) event schema."""
    return f"{event.get('id')}#{event.get('seq')}"


def natural_key(duty: dict) -> str:
    return digest({
        "v": 1,
        "scope": duty.get("scope"), "source": duty.get("source"),
        "trigger_event": duty.get("trigger", {}).get("event") if isinstance(duty.get("trigger"), dict) else None,
        "trigger_condition": duty.get("trigger", {}).get("condition") if isinstance(duty.get("trigger"), dict) else None,
        "action": duty.get("action"), "target": duty.get("target"),
    })


def is_nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
    return is_iso_date(value) or (isinstance(value, str) and value.startswith("boundary:")
                                  and is_nonblank(value[len("boundary:"):]))


def recorded_date(value: str) -> datetime.date:
    return datetime.date.fromisoformat(value[:10])


def deadline_tightens(rule: str, firing: str) -> bool:
    if is_iso_date(rule) and is_iso_date(firing):
        return recorded_date(firing) <= recorded_date(rule)
    return firing == rule


def parse_time(value: str) -> datetime.datetime:
    """Parse an RFC 3339 date-time; both letter cases of the "T" separator and "Z" offset are allowed."""
    return datetime.datetime.fromisoformat(
        value[:10] + "T" + value[11:].replace("Z", "+00:00").replace("z", "+00:00"))


def dated_deadline_crossed(required_by: str, at: str) -> bool:
    """A dated duty crosses at the next midnight in DEADLINE_TIME_ZONE, using recorded time only."""
    return parse_time(at).astimezone(ZoneInfo(DEADLINE_TIME_ZONE)).date() > recorded_date(required_by)


def issue(errors: list, code: int, reason: str, duty_id=None, event_id=None, occurrence_id=None) -> None:
    """Appends one error/warning item to `errors`. `reason` passes through `bound_reason()` here,
    UNCONDITIONALLY, immediately before it leaves this constructor — the one STRUCTURAL place
    every reported `reason` this tool ever produces is checked against the documented diagnostic
    bound, so the bound holds even for a call site that forgot to self-bound (interpolated a raw,
    unbounded value, or joined an attacker-controlled number of sub-items without its own cap).

    `duty_id`/`event_id`/`occurrence_id` are identifiers this tool's own grammar expects to be a
    non-blank string (every CALLER site reads them straight off an untrusted event's `id`/`seq`-
    derived ref or a `payload.occurrence_id`/`occurrence_ids[i]` field that has NOT necessarily been
    validated yet at the point the call is made — several `fold_row` call sites pass
    `payload.get("occurrence_id")` precisely BECAUSE it failed validation, to say which occurrence
    the failure concerns). Every error report this tool emits must itself pass its own strict input
    rules when re-parsed (depth <= 64, every string UTF-8-encodable) — so a malformed value here
    (an attacker-chosen container nested to an adversarial depth, a non-string, a value that cannot
    encode as UTF-8) is NEVER copied RAW, as-is, into one of these three typed identifier fields: only
    an already-grammar-valid (non-blank string) identifier ever occupies `duty_id`/`event_id`/
    `occurrence_id`. Anything else is left OUT of the typed field and folded into `reason` instead,
    as a bounded summary (`safe_repr_for_error`, which never recurses more than
    `_SAFE_REPR_MAX_ITEMS` levels/items deep and never exceeds `_SAFE_REPR_MAX_CHARS`) — so the
    reported identity is always visible, but never at the cost of smuggling an unvalidated, possibly
    deeply-nested or unencodable value into a field a reader expects to be a plain identifier
    string."""
    item = {"code": code, "reason": bound_reason(reason)}
    invalid_bits = []
    for field, value in (("duty_id", duty_id), ("event_id", event_id), ("occurrence_id", occurrence_id)):
        if value is None:
            continue
        if is_nonblank(value):
            item[field] = value
        else:
            invalid_bits.append(f"{field}={safe_repr_for_error(value)}")
    if invalid_bits:
        item["reason"] = bound_reason(item["reason"] + " [" + "; ".join(invalid_bits) + "]")
    errors.append(item)


def exact_fields(value, expected: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == expected


def validate_duty(duty, row_id: str, errors: list, event_id: str) -> bool:
    before = len(errors)
    if not exact_fields(duty, DUTY_FIELDS):
        issue(errors, E_REQUIRED_FIELD, "minted duty must contain exactly the closed duty fields", row_id, event_id)
        return False
    scalar_fields = (
        "id", "natural_key", "scope", "source", "source_revision", "action", "target",
        "occurrence_rule", "required_by", "created_by", "created_at",
    )
    if any(not is_nonblank(duty[field]) for field in scalar_fields):
        issue(errors, E_REQUIRED_FIELD, "minted duty string fields must be non-blank", row_id, event_id)
        return False
    if not is_deadline(duty["required_by"]):
        issue(errors, E_REQUIRED_FIELD, "duty.required_by must be an ISO date or boundary:<id>", row_id, event_id)
    if duty["id"] != row_id:
        issue(errors, E_IDENTITY, "event row id must equal minted duty id", row_id, event_id)
    if not exact_fields(duty["trigger"], {"event", "condition"}) or not all(
            is_nonblank(v) for v in duty["trigger"].values()):
        issue(errors, E_REQUIRED_FIELD, "duty.trigger must contain non-blank event and condition", row_id, event_id)
    if duty["retrigger"] not in {"once", "every-event"} or duty["duty_kind"] not in {"one-time", "continuing"}:
        issue(errors, E_REQUIRED_FIELD, "retrigger or duty_kind is outside its closed vocabulary", row_id, event_id)
    predicate = duty["discharge_predicate"]
    if not exact_fields(predicate, {"resolver", "operands", "resolved"}) or not is_nonblank(
            predicate.get("resolver") if isinstance(predicate, dict) else None
    ) or not is_nonblank(predicate.get("operands") if isinstance(predicate, dict) else None) or not isinstance(
            predicate.get("resolved") if isinstance(predicate, dict) else None, bool):
        issue(errors, E_REQUIRED_FIELD, "discharge_predicate must contain resolver, operands, and boolean resolved", row_id, event_id)
    elif not predicate["resolved"]:
        issue(errors, E_IDENTITY, "unresolved discharge predicate rejects activation", row_id, event_id)
    if duty["supersedes"] is not None and (not is_nonblank(duty["supersedes"])
                                            or duty["supersedes"] == duty["id"]):
        issue(errors, E_IDENTITY, "duty.supersedes must be null or name a different duty", row_id, event_id)
    if not is_rfc3339(duty["created_at"]):
        issue(errors, E_REQUIRED_FIELD, "duty.created_at must be RFC3339 with an offset", row_id, event_id)
    if duty["occurrences"] != {} or duty["status"] != "open" or duty["discharge_evidence"] != []:
        issue(errors, E_TRANSITION, "minted derived fields must be empty/open initial values", row_id, event_id)
    if duty["natural_key"] != natural_key(duty):
        issue(errors, E_IDENTITY, "natural_key does not match the identity operands", row_id, event_id)
    return len(errors) == before


def event_envelope_valid(event: dict, errors: list) -> bool:
    if isinstance(event, TypedEvent) and not event.shape_valid:
        return False
    return primitives.validate_event_envelope_shape(
        event, errors, envelope_fields=EVENT_FIELDS, kinds=KINDS,
        payload_fields=PAYLOAD_FIELDS, required_nonblank=("actor", "batch_id"), issue_fn=issue,
        metadata_fields=set())


def actor_identity_valid(event: dict, duty: dict, errors: list) -> bool:
    owner_required = event["kind"] == "minted" or event["kind"] in OWNER_KINDS
    if owner_required and event["actor"] != duty["created_by"]:
        rule = "R1" if event["kind"] == "minted" else "R2"
        issue(errors, E_IDENTITY, f"{rule} owner identity mismatch for {event['kind']}", duty["id"], event["id"])
        return False
    return True


def string_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(is_nonblank(item) for item in value) and len(value) == len(set(value))


def empty_occurrence(oid: str, payload: dict) -> dict:
    return {
        "occurrence_id": oid, "state": "open", "required_by": payload["required_by"],
        "target_revision": payload["target_revision"], "trigger_event_id": payload["trigger_event_id"],
        "crossed": False, "pending": False, "evaluation": None, "discharge_seq": None,
        "evidence_refs": [], "context_hash": None, "stale_observed": False,
    }


def evaluation_binding(duty: dict, occ: dict, payload: dict) -> str:
    return digest({
        "source_revision": duty["source_revision"], "predicate_digest": payload["predicate_digest"],
        "target_revision": occ["target_revision"], "evidence_digest": payload["evidence_digest"],
        "context_hash": payload["context_hash"],
    })


def valid_evaluation(duty: dict, occ: dict, payload: dict) -> bool:
    return (isinstance(payload.get("result"), bool)
            and is_rfc3339(payload.get("evaluated_at"))
            and is_nonblank(payload.get("predicate_digest"))
            and is_nonblank(payload.get("evidence_digest"))
            and is_nonblank(payload.get("context_hash"))
            and payload.get("predicate_digest") == digest(duty["discharge_predicate"])
            and payload.get("bound_revision") == evaluation_binding(duty, occ, payload)
            and (occ["context_hash"] is None or payload["context_hash"] == occ["context_hash"]))


def evaluation_matches_final(duty: dict, occ: dict, evaluation: dict) -> bool:
    expected = digest({
        "source_revision": duty["source_revision"],
        "predicate_digest": digest(duty["discharge_predicate"]),
        "target_revision": occ["target_revision"],
        "evidence_digest": evaluation["evidence_digest"],
        "context_hash": evaluation["context_hash"],
    })
    return (not evaluation["invalidated"]
            and evaluation["source_revision"] == duty["source_revision"]
            and evaluation["predicate_digest"] == digest(duty["discharge_predicate"])
            and evaluation["context_hash"] == occ["context_hash"]
            and evaluation["bound_revision"] == expected)


# Shared mechanisms are supplied by the policy-free primitive layer.  This core module owns only
# its closed grammar, identity/lifecycle policy, boundary validation and projection.
FoldFail = primitives.FoldFail
StdoutUnavailable = primitives.StdoutUnavailable
safe_repr_for_error = primitives.safe_repr_for_error
bound_reason = primitives.bound_reason
bound_path_preview = primitives.bound_path_preview
join_error_list = primitives.join_error_list
loads_strict = primitives.loads_strict
find_json_input_defect = primitives.find_json_input_defect
load_object = primitives.load_object
load_events = primitives.load_events
load_event_records = primitives.load_event_records
canonical = primitives.canonical
digest = primitives.digest
occurrence_id = primitives.occurrence_id
event_ref = primitives.event_ref
natural_key = primitives.natural_key
is_nonblank = primitives.is_nonblank
is_rfc3339 = primitives.is_rfc3339
is_iso_date = primitives.is_iso_date
is_deadline = primitives.is_deadline
recorded_date = primitives.recorded_date
deadline_tightens = primitives.deadline_tightens
parse_time = primitives.parse_time
dated_deadline_crossed = primitives.dated_deadline_crossed
exact_fields = primitives.exact_fields
string_list = primitives.string_list
evaluation_binding = primitives.evaluation_binding
valid_evaluation = primitives.valid_evaluation
evaluation_matches_final = primitives.evaluation_matches_final
emit_report = primitives.emit_report
force_utf8_stdout = primitives.force_utf8_stdout
write_report = primitives.write_report
run_cli = primitives.run_cli
output_boundary_selftest = primitives.output_boundary_selftest
worst_of = primitives.worst_of


def summarize(occurrences: dict) -> str:
    states = {occ["state"] for occ in occurrences.values()}
    for state in ("expired", "open", "discharged"):
        if state in states:
            return state
    return "open"


def invalid_row(row_id: str, occurrences=None, duty=None) -> dict:
    known = {oid: {"state": "invalid", "contribution": "FAIL"}
             for oid in sorted(occurrences or {})}
    row = {"id": row_id, "state": "invalid", "contribution": "FAIL", "occurrences": known}
    if duty is not None:
        row["_admitted_duty"] = duty
    if occurrences is not None:
        row["_occurrences"] = occurrences
    return row


def identity_claim(duty) -> dict | None:
    """Return the independently valid identity/link metadata carried by a mint.

    Admission of the enclosing event and admission of the duty's identity are deliberately
    separate.  A rejected envelope must not erase a usable natural-key or supersession claim:
    invalid duties participate in the graph fixed point too.
    """
    if not isinstance(duty, dict):
        return None
    scalar_fields = ("id", "natural_key", "scope", "source", "action", "target", "created_by")
    if any(not is_nonblank(duty.get(field)) for field in scalar_fields):
        return None
    trigger = duty.get("trigger")
    if (not exact_fields(trigger, {"event", "condition"})
            or any(not is_nonblank(value) for value in trigger.values())):
        return None
    supersedes = duty.get("supersedes")
    if supersedes is not None and (not is_nonblank(supersedes) or supersedes == duty["id"]):
        return None
    try:
        expected_key = natural_key(duty)
    except (KeyError, TypeError, ValueError):
        return None
    if duty["natural_key"] != expected_key:
        return None
    return {field: duty[field] for field in ("id", "natural_key", "created_by")} | {
        "supersedes": supersedes,
    }


def mint_identity_claim(event: dict) -> dict | None:
    if event.get("kind") != "minted" or not isinstance(event.get("payload"), dict):
        return None
    return identity_claim(event["payload"].get("duty"))


def fold_row(row_id: str, events: list[dict], duties: dict, errors: list) -> dict:
    before = len(errors)
    # Admission happens before rows are grouped, so its diagnostics already exist at ``before``.
    # Keep the validity bit on the typed records: every admission failure on any event (including
    # the minted duty record) invalidates the whole duty after the remaining independent envelope
    # checks have been collected.  This makes admission part of the same validity set consumed by
    # graph convergence and projection, rather than a diagnostics-only pre-pass.
    admission_invalid = any(
        isinstance(event, TypedEvent) and not event.shape_valid for event in events
    )
    if [event.get("seq") for event in events] != list(range(1, len(events) + 1)):
        issue(errors, E_SEQUENCE, "per-duty seq must start at 1 and have no gaps", row_id)
    envelope_results = [event_envelope_valid(event, errors) for event in events]
    claim = next((candidate for event in events
                  if (candidate := mint_identity_claim(event)) is not None), None)
    if not events or events[0].get("kind") != "minted":
        issue(errors, E_TRANSITION, "first retained row event must be minted", row_id)
        return invalid_row(row_id, duty=claim)
    mint = events[0]
    if not envelope_results[0]:
        return invalid_row(row_id, duty=claim)
    duty = mint["payload"].get("duty")
    if not validate_duty(duty, row_id, errors, mint["id"]):
        return invalid_row(row_id, duty=claim)
    if not actor_identity_valid(mint, duty, errors):
        return invalid_row(row_id, duty=claim)
    occurrences = {}
    superseded_by = None
    dropped = False
    revision_events = []
    stale_events = []
    for event, envelope_ok in zip(events[1:], envelope_results[1:]):
        if not envelope_ok:
            continue
        if superseded_by is not None or dropped:
            issue(errors, E_TRANSITION,
                  "superseded is absorbing" if superseded_by is not None else "dropped is absorbing",
                  row_id, event["id"])
            continue
        if not actor_identity_valid(event, duty, errors):
            continue
        payload = event["payload"]
        kind = event["kind"]
        if kind == "minted":
            issue(errors, E_TRANSITION, "a duty may be minted only once", row_id, event["id"])
        elif kind == "fired":
            if not is_deadline(payload["required_by"]):
                issue(errors, E_REQUIRED_FIELD, "fired.required_by must be an ISO date or boundary:<id>",
                      row_id, event["id"], payload.get("occurrence_id"))
                continue
            # Each new recurring occurrence binds its own rule through authorized firing.
            # Redelivery must preserve that binding; it cannot extend an existing deadline.
            rule = duty["required_by"] if duty["retrigger"] == "once" else payload["required_by"]
            if duty["retrigger"] == "every-event" and isinstance(payload["occurrence_id"], str):
                existing = occurrences.get(payload["occurrence_id"])
                if existing is not None:
                    rule = existing["required_by"]
            if not deadline_tightens(rule, payload["required_by"]):
                issue(errors, E_BINDING, "firing required_by loosens the duty deadline rule",
                      row_id, event["id"], payload.get("occurrence_id"))
                continue
            required = (is_nonblank(payload["occurrence_id"]) and is_nonblank(payload["trigger_event_id"])
                        and payload["trigger"] == duty["trigger"]
                        and is_nonblank(payload["target_revision"]))
            expected = occurrence_id(row_id, payload["trigger_event_id"]) if is_nonblank(payload["trigger_event_id"]) else ""
            if not required or payload["occurrence_id"] != expected:
                issue(errors, E_IDENTITY, "firing identity, trigger, or deadline binding does not match", row_id, event["id"], payload.get("occurrence_id"))
            elif payload["occurrence_id"] in occurrences:
                old = occurrences[payload["occurrence_id"]]
                if any(old[key] != payload[key] for key in ("trigger_event_id", "required_by", "target_revision")):
                    issue(errors, E_IDENTITY, "redelivered occurrence has conflicting bindings", row_id, event["id"], payload["occurrence_id"])
            elif duty["retrigger"] == "once" and occurrences:
                issue(errors, E_TRANSITION, "once duty received a second distinct firing", row_id, event["id"])
            else:
                occurrences[payload["occurrence_id"]] = empty_occurrence(payload["occurrence_id"], payload)
        elif kind in {"predicate_evaluated", "deadline_crossed"}:
            oid = payload["occurrence_id"]
            if oid not in occurrences:
                issue(errors, E_UNKNOWN_OCCURRENCE, f"{kind} names an unknown occurrence", row_id, event["id"], oid)
                continue
            occ = occurrences[oid]
            if kind == "predicate_evaluated":
                valid = is_nonblank(payload["boundary_id"]) and valid_evaluation(duty, occ, payload)
                if not valid:
                    issue(errors, E_BINDING, "predicate evaluation bindings are invalid", row_id, event["id"], oid)
                    continue
                evaluation = {
                    "seq": event["seq"], "event_id": event_ref(event), "result": payload["result"],
                    "source_revision": duty["source_revision"], "bound_revision": payload["bound_revision"],
                    "predicate_digest": payload["predicate_digest"], "evidence_digest": payload["evidence_digest"],
                    "context_hash": payload["context_hash"], "boundary_id": payload["boundary_id"],
                    "invalidated": False,
                }
                occ.setdefault("evaluations", []).append(evaluation)
                occ["evaluation"] = evaluation
                occ["context_hash"] = payload["context_hash"]
                occ["pending"] = False
                if not payload["result"] and occ["state"] == "open" and occ["crossed"]:
                    occ["state"] = "expired"
            elif (not is_rfc3339(payload["at"]) or not is_nonblank(payload["boundary_id"])
                  or payload["bound_revision"] != duty["source_revision"]):
                issue(errors, E_BINDING, "deadline crossing boundary or revision binding is invalid", row_id, event["id"], oid)
            elif is_iso_date(occ["required_by"]) and not dated_deadline_crossed(occ["required_by"], payload["at"]):
                issue(errors, E_BINDING, "deadline_crossed.at precedes the end of required_by in Europe/Zagreb", row_id, event["id"], oid)
            else:
                occ["crossed"] = True
                if occ["state"] == "open" and not occ["pending"]:
                    occ["state"] = "expired"
        elif kind in {"discharged", "stale_detected"}:
            ids = payload["occurrence_ids"]
            if not string_list(ids):
                issue(errors, E_REQUIRED_FIELD, f"{kind}.occurrence_ids must be a nonempty unique string array", row_id, event["id"])
                continue
            unknown = [oid for oid in ids if oid not in occurrences]
            if unknown:
                issue(errors, E_UNKNOWN_OCCURRENCE,
                      f"{kind} names unknown occurrence(s): {join_error_list(unknown)}", row_id, event["id"])
                continue
            if kind == "discharged":
                if not string_list(payload["evidence_refs"]):
                    issue(errors, E_REQUIRED_FIELD, "discharged.evidence_refs must be a nonempty unique string array", row_id, event["id"])
                    continue
                expected_evidence = digest(payload["evidence_refs"])
                for oid in ids:
                    occ = occurrences[oid]
                    evaluation = occ["evaluation"]
                    if occ["state"] not in {"open", "expired", "discharged"}:
                        issue(errors, E_TRANSITION, "illegal discharge transition", row_id, event["id"])
                    elif not evaluation or not evaluation["result"] or evaluation["evidence_digest"] != expected_evidence:
                        issue(errors, E_BINDING, "discharge requires a current true evaluation bound to its evidence", row_id, event["id"], oid)
                    elif evaluation["invalidated"]:
                        issue(errors, E_BINDING, "discharge evaluation was invalidated", row_id, event["id"], oid)
                    else:
                        occ["state"] = "discharged"
                        occ["discharge_seq"] = event["seq"]
                        occ["evidence_refs"] = payload["evidence_refs"]
            elif kind == "stale_detected":
                valid = primitives.stale_binding_valid(payload, duty)
                if not valid:
                    issue(errors, E_BINDING, "stale event must bind changed hashes and reevaluation_pending", row_id, event["id"])
                    continue
                if duty["duty_kind"] == "continuing":
                    for oid in ids:
                        occ = occurrences[oid]
                        if occ["context_hash"] is None or payload["context_hash_old"] != occ["context_hash"]:
                            issue(errors, E_BINDING, "stale context_hash_old does not match the stored context binding", row_id, event["id"], oid)
                            continue
                        stale_events.append({"seq": event["seq"], "batch_id": event["batch_id"],
                                             "occurrence_id": oid, "old": payload["context_hash_old"],
                                             "new": payload["context_hash_new"]})
                        occ["stale_observed"] = {"batch_id": event["batch_id"], "boundary_id": payload["boundary_id"]}
                        if occ["evaluation"] is not None:
                            occ["evaluation"]["invalidated"] = True
                        occ["evaluation"] = None
                        occ["context_hash"] = payload["context_hash_new"]
                        if occ["state"] == "discharged":
                            occ["state"] = "open"
                        if occ["state"] == "open":
                            occ["pending"] = True
        elif kind == "source_revised":
            valid = (payload["from_revision"] == duty["source_revision"] and is_nonblank(payload["to_revision"])
                     and payload["to_revision"] != payload["from_revision"])
            if not valid:
                issue(errors, E_BINDING, "source revision migration binding is invalid", row_id, event["id"])
            else:
                affected = []
                invalidated = []
                for oid, occ in occurrences.items():
                    old_evaluations = [evaluation for evaluation in occ.get("evaluations", [])
                                       if evaluation["source_revision"] == payload["from_revision"]]
                    if old_evaluations or occ["state"] == "discharged":
                        affected.append(oid)
                    for evaluation in old_evaluations:
                        evaluation["invalidated"] = True
                        invalidated.append((oid, evaluation["seq"]))
                duty["source_revision"] = payload["to_revision"]
                revision_events.append((event, affected, invalidated))
        elif kind == "superseded":
            if not is_nonblank(payload["by"]) or payload["by"] == row_id:
                issue(errors, E_IDENTITY, "superseded.by must name a different duty", row_id, event["id"])
            else:
                superseded_by = payload["by"]
                for occ in occurrences.values():
                    occ["state"] = "superseded"
        elif kind == "dropped":
            if not is_nonblank(payload["reason"]):
                issue(errors, E_REQUIRED_FIELD, "dropped.reason must be a non-blank string",
                      row_id, event["id"])
            else:
                dropped = True
                for occ in occurrences.values():
                    occ["state"] = "dropped"
    if revision_events and duty["duty_kind"] == "continuing":
        for revision, affected, invalidated in revision_events:
            uncovered = [(oid, seq) for oid, seq in invalidated
                         if not any(stale["occurrence_id"] == oid
                                    and stale["batch_id"] == revision["batch_id"]
                                    and stale["seq"] > seq for stale in stale_events)]
            if uncovered or (affected and not invalidated):
                oid = uncovered[0][0] if uncovered else affected[0]
                issue(errors, E_BINDING, "source migration requires later stale_detected coverage for every invalidated evaluation", row_id, revision["id"], oid)
    for oid, occ in occurrences.items():
        evaluation = occ.get("evaluation")
        if evaluation is not None and not evaluation_matches_final(duty, occ, evaluation):
            evaluation["invalidated"] = True
            issue(errors, E_BINDING, "current evaluation does not revalidate against final folded bindings", row_id, occurrence_id=oid)
    if admission_invalid or len(errors) > before:
        return invalid_row(row_id, occurrences, duty)
    return {"id": row_id, "state": "superseded" if superseded_by else "dropped" if dropped else summarize(occurrences),
            "superseded_by": superseded_by, "_duty": duty, "_admitted_duty": duty,
            "_occurrences": occurrences}


def validate_boundary(boundary: dict, retained: list[dict], errors: list) -> None:
    if set(boundary) != BOUNDARY_FIELDS:
        extra = set(boundary) - BOUNDARY_FIELDS
        reason = "caller-supplied clock field is forbidden" if extra & {"clock", "now", "current_time"} else "boundary must contain exactly the closed fields"
        issue(errors, E_BOUNDARY, reason)
        return
    if boundary["schema"] != "obligation-boundary/v0" or not is_nonblank(boundary["boundary_id"]):
        issue(errors, E_BOUNDARY, "invalid boundary schema or boundary_id")
    if not is_rfc3339(boundary["recorded_cutoff"]):
        issue(errors, E_BOUNDARY, "recorded_cutoff must be RFC3339 with an offset")
    ids = boundary["in_scope_duty_ids"]
    if not string_list(ids):
        issue(errors, E_BOUNDARY, "in_scope_duty_ids must be a nonempty unique string array")
    batch = boundary["evaluator_batch"]
    if not exact_fields(batch, BATCH_FIELDS):
        issue(errors, E_BOUNDARY, "evaluator_batch must contain exactly the closed metadata fields")
        return
    listed = batch["event_ids"]
    # `event_count` is a signed 64-bit field (MAX_INT64, see its own comment): the comparison
    # below is a plain binary `<`/`>` against the bound, never a string conversion of the value,
    # so an absurdly large `event_count` (however many decimal digits) is rejected here as a
    # normal shape error, never by Python's own int-to-string conversion raising first.
    if (not is_nonblank(batch["batch_id"]) or not is_nonblank(batch["input_snapshot_digest"])
            or batch["committed"] is not True or not isinstance(batch["event_count"], int)
            or isinstance(batch["event_count"], bool) or not (0 <= batch["event_count"] <= MAX_INT64)
            or not isinstance(listed, dict) or set(listed) != EVALUATOR_KINDS
            or any(not isinstance(value, list) or any(not is_nonblank(v) for v in value) for value in listed.values())):
        issue(errors, E_BATCH, "evaluator batch metadata is incomplete or invalid")
        return
    flat = [event_id for kind in sorted(listed) for event_id in listed[kind]]
    if len(flat) != len(set(flat)) or batch["event_count"] != len(flat):
        issue(errors, E_BATCH, "evaluator batch event count or uniqueness is inconsistent")
    by_event_id = {event_ref(event): event for event in retained if is_nonblank(event.get("id"))}
    for kind, event_ids in listed.items():
        for event_id in event_ids:
            event = by_event_id.get(event_id)
            if event is None or event.get("kind") != kind or event.get("batch_id") != batch["batch_id"]:
                issue(errors, E_BATCH, f"listed evaluator event is absent or mismatched: {event_id}")
    actual = sorted(event_ref(event) for event in retained
                    if event.get("kind") in EVALUATOR_KINDS and event.get("batch_id") == batch["batch_id"]
                    and event.get("id") in set(ids))
    if sorted(flat) != actual:
        issue(errors, E_BATCH, "batch event_ids do not exactly enumerate retained in-scope evaluator events")


def observation_covers(event: dict, kind: str, occurrence_id_value: str) -> bool:
    if event.get("kind") != kind or not isinstance(event.get("payload"), dict):
        return False
    if kind in {"predicate_evaluated", "deadline_crossed"}:
        return event["payload"].get("occurrence_id") == occurrence_id_value
    return occurrence_id_value in event["payload"].get("occurrence_ids", [])


def required_observation_kinds(occ: dict, boundary: dict) -> set[str]:
    if occ["state"] in {"superseded", "dropped"}:
        return set()
    required = {"predicate_evaluated"}
    due = ((is_iso_date(occ["required_by"])
            and dated_deadline_crossed(occ["required_by"], boundary["recorded_cutoff"]))
           or occ["required_by"] == f"boundary:{boundary['boundary_id']}")
    if due:
        required.add("deadline_crossed")
    if occ["pending"]:
        required.add("stale_detected")
    current_batch = {"batch_id": boundary["evaluator_batch"]["batch_id"], "boundary_id": boundary["boundary_id"]}
    if occ.get("stale_observed") == current_batch:
        required.add("stale_detected")
    return required


def validate_observations(boundary: dict, retained: list[dict], rows: dict, errors: list) -> None:
    manifest = boundary.get("observations_manifest")
    frontier = boundary.get("event_frontier")
    in_scope = boundary.get("in_scope_duty_ids", [])
    if not isinstance(manifest, dict) or set(manifest) != set(in_scope):
        issue(errors, E_BOUNDARY, "observations_manifest must contain exactly every in-scope duty")
        return
    # Every `event_frontier` value is a signed 64-bit field (MAX_INT64): a plain binary bound
    # comparison, never a string conversion of the value.
    if (not isinstance(frontier, dict) or set(frontier) != set(in_scope)
            or any(not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= MAX_INT64)
                   for value in frontier.values())):
        issue(errors, E_BOUNDARY, "event_frontier must contain one positive integer for every in-scope duty")
        return
    by_ref = {event_ref(event): event for event in retained if is_nonblank(event.get("id"))}
    batch = boundary.get("evaluator_batch", {})
    batch_id = batch.get("batch_id")
    snapshot = batch.get("input_snapshot_digest")
    for duty_id in in_scope:
        duty_events = [event for event in retained if event.get("id") == duty_id]
        beyond = [event for event in duty_events if isinstance(event.get("seq"), int)
                  and event["seq"] > frontier[duty_id]]
        if beyond:
            issue(errors, E_BATCH, "event exists beyond the declared event_frontier", duty_id, event_ref(beyond[0]))
        if duty_events and max(event.get("seq", 0) for event in duty_events if isinstance(event.get("seq"), int)) != frontier[duty_id]:
            issue(errors, E_BATCH, "event_frontier is not the highest consumed per-duty sequence", duty_id)
        row = rows.get(duty_id, {})
        occurrences = row.get("_occurrences", {})
        duty_manifest = manifest.get(duty_id)
        if row.get("state") == "dropped":
            if duty_manifest != {}:
                issue(errors, E_BATCH, "a dropped duty observation manifest must be an empty map", duty_id)
            continue
        if not isinstance(duty_manifest, dict) or set(duty_manifest) != set(occurrences):
            issue(errors, E_BATCH, "duty observation manifest must contain exactly every known occurrence", duty_id)
            continue
        for oid, occ in occurrences.items():
            entries = duty_manifest.get(oid)
            if (not isinstance(entries, list)
                    or any(not exact_fields(entry, MANIFEST_ENTRY_FIELDS) for entry in entries)):
                issue(errors, E_BATCH, "observation manifest entries have invalid structure", duty_id, occurrence_id=oid)
                continue
            kinds = [entry["kind"] for entry in entries]
            if len(kinds) != len(set(kinds)) or set(kinds) != required_observation_kinds(occ, boundary):
                issue(errors, E_BATCH, "observation manifest does not enumerate the exact required kinds", duty_id, occurrence_id=oid)
            for entry in entries:
                event = by_ref.get(entry.get("event_id"))
                kind = entry.get("kind")
                if (kind not in EVALUATOR_KINDS or event is None
                        or not observation_covers(event, kind, oid)
                        or event.get("batch_id") != batch_id
                        or event.get("payload", {}).get("boundary_id") != boundary.get("boundary_id")
                        or event.get("payload", {}).get("bound_revision") != entry.get("bound_revision")):
                    issue(errors, E_BATCH,
                          "required observation is absent or not bound to this boundary snapshot",
                          duty_id, entry.get("event_id"), oid)
    event_count = batch.get("event_count")
    any_open = any(row.get("state") == "open" for duty_id, row in rows.items() if duty_id in in_scope)
    if event_count == 0 and any_open:
        issue(errors, E_BATCH, "zero-event batch is invalid while an in-scope duty is open")
    if not is_nonblank(snapshot):
        issue(errors, E_BATCH, "input_snapshot_digest must be recorded for observation binding")


def project(row: dict, boundary: dict, all_rows: dict, errors: list) -> dict:
    if row["state"] == "invalid":
        return row
    duty = row.pop("_duty")
    occurrences = row.pop("_occurrences")
    projected = {}
    block = False
    if row["state"] == "dropped":
        for oid in sorted(occurrences):
            projected[oid] = {"state": "dropped", "contribution": "PASS"}
        row["contribution"] = "PASS"
    elif row["state"] == "superseded":
        replacement = all_rows.get(row["superseded_by"])
        valid = (replacement is not None and replacement["state"] != "invalid"
                 and row["superseded_by"] in boundary["in_scope_duty_ids"])
        contribution = "PASS" if valid else "FAIL"
        block = not valid
        if not valid:
            row["projection_reason"] = "superseded replacement is absent, invalid, or out of scope"
        for oid in sorted(occurrences):
            projected[oid] = {"state": "superseded", "contribution": contribution}
        row["contribution"] = contribution
    else:
        contributions = []
        for oid in sorted(occurrences):
            occ = occurrences[oid]
            state = occ["state"]
            reason = None
            if state == "expired":
                contribution = "FAIL"
            elif state == "open" and occ["pending"]:
                contribution = "PENDING"
            elif state == "open" and (occ["crossed"] or occ["required_by"] == f"boundary:{boundary['boundary_id']}"):
                contribution = "FAIL"
            elif state == "open":
                contribution = "WARN-recorded"
            elif state == "discharged":
                evaluation = occ["evaluation"]
                valid_discharge = (evaluation is not None and evaluation["result"] is True
                                   and evaluation_matches_final(duty, occ, evaluation)
                                   and evaluation["evidence_digest"] == digest(occ["evidence_refs"]))
                valid = valid_discharge
                contribution = "PASS" if valid else "FAIL"
                if not valid:
                    reason = "discharge lacks a current true evaluation with valid final bindings"
            else:
                contribution = "FAIL"
            item = {"state": state, "contribution": contribution}
            if reason:
                item["reason"] = reason
            projected[oid] = item
            contributions.append(contribution)
            block = block or contribution in {"FAIL", "PENDING"}
        row["contribution"] = worst_of(contributions)
    row["occurrences"] = projected
    row["_block"] = block
    row["_raw_occurrences"] = occurrences
    return row


def action_key(action: dict) -> str:
    return canonical(action)


def build_required_actions(rows: list[dict]) -> list[dict]:
    actions = {}
    for row in rows:
        raw = row.get("_raw_occurrences", {})
        if row["state"] == "invalid":
            continue
        if not raw and row["state"] in {"open", "expired"}:
            action = {"kind": "obligation", "duty_id": row["id"], "occurrence_id": None, "state": row["state"]}
            actions[action_key(action)] = action
        for oid, occ in raw.items():
            if occ["state"] in {"open", "expired"}:
                action = {"kind": "obligation", "duty_id": row["id"], "occurrence_id": oid, "state": occ["state"]}
                actions[action_key(action)] = action
    return [actions[key] for key in sorted(actions)]


def make_failure_report(code: int, reason: str) -> dict:
    # `bound_reason` here too: a FoldFail's own message (built outside `issue()`, e.g. from
    # `read_text`/`load_object`/`load_events`, or an outer `str(exc)`) passes through the same
    # structural bound before it ever leaves this report constructor.
    return {"boundary_id": None, "duties": [], "aggregate": {"block": True, "required_actions": []},
            "validation": {"status": "invalid", "exit_code": code,
                           "errors": [{"code": code, "reason": bound_reason(reason)}], "warnings": [],
                           "duplicates_ignored": []}}


EXTENSION_PAYLOAD_KEYS = {
    "waiver_id", "expires", "next_review_cutoff", "later_date_authority_ref",
}


def extension_detections(raw_events: list[dict], boundary: dict) -> list[dict]:
    """Return every structural authority-extension marker from design §4, and nothing else."""
    detections = []

    def detected(reason: str, event=None) -> None:
        issue(detections, E_EXTENSION, f"uses the authority extension: {reason}",
              event.get("id") if isinstance(event, dict) else None,
              event_ref(event) if isinstance(event, dict) and is_nonblank(event.get("id"))
              and isinstance(event.get("seq"), int) else None)

    for event in raw_events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        payload = event.get("payload")
        if kind == "minted" and isinstance(payload, dict) and isinstance(payload.get("duty"), dict):
            for key in ("authority", "waiver"):
                if key in payload["duty"]:
                    detected(f"minted.duty.{key}", event)
        if isinstance(kind, str) and kind in {"waived", "waiver_lapsed"}:
            detected(f"event kind {kind}", event)
        if "authority_ref" in event:
            detected("event.authority_ref", event)
        if kind == "source_revised" and isinstance(payload, dict) and "authority_ref" in payload:
            detected("source_revised.payload.authority_ref", event)
        if isinstance(kind, str) and kind in KINDS and isinstance(payload, dict):
            for key in sorted(EXTENSION_PAYLOAD_KEYS & set(payload)):
                detected(f"{kind}.payload.{key}", event)

    batch = boundary.get("evaluator_batch") if isinstance(boundary, dict) else None
    if isinstance(batch, dict):
        event_ids = batch.get("event_ids")
        if isinstance(event_ids, dict) and "waiver_lapsed" in event_ids:
            detected("evaluator_batch.event_ids.waiver_lapsed")
        if "lapse_satisfaction" in batch:
            detected("evaluator_batch.lapse_satisfaction")
    if isinstance(boundary, dict) and "lapse_satisfaction" in boundary:
        detected("boundary.lapse_satisfaction")
    manifest = boundary.get("observations_manifest") if isinstance(boundary, dict) else None
    if isinstance(manifest, dict):
        for duty_manifest in manifest.values():
            if not isinstance(duty_manifest, dict):
                continue
            for entries in duty_manifest.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("kind") == "waiver_lapsed":
                        detected("observations_manifest entry kind waiver_lapsed")
    return detections


def validate_supersession_graph(rows: dict, in_scope: list[str], errors: list) -> None:
    """Invalidate supersession/natural-key defects to a fixed point.

    Every pass rebuilds claimants, cycle walks and valid adjacency.  This is intentionally one
    convergence loop: a natural-key invalidation can break a reciprocal link, which can invalidate
    another duty, which can in turn split a formerly exempt same-key chain.
    """
    scope = set(in_scope)

    def duty_of(row_id):
        row = rows.get(row_id, {})
        return row.get("_duty") or row.get("_admitted_duty")

    def invalidate(row_id: str, reason: str) -> bool:
        row = rows.get(row_id)
        if row is None or row.get("state") == "invalid":
            return False
        issue(errors, E_IDENTITY, reason, row_id)
        rows[row_id] = invalid_row(row_id, row.get("_occurrences"), duty_of(row_id))
        return True

    changed = True
    while changed:
        changed = False

        valid_ids = {row_id for row_id, row in rows.items() if row.get("state") != "invalid"}
        claimants = {}
        for row_id in valid_ids:
            duty = duty_of(row_id)
            if duty and duty.get("supersedes") is not None:
                claimants.setdefault(duty["supersedes"], []).append(row_id)
        for predecessor, successors in sorted(claimants.items()):
            if len(successors) > 1:
                for successor in sorted(successors):
                    changed |= invalidate(successor, f"supersession branches from {predecessor}")

        valid_ids = {row_id for row_id, row in rows.items() if row.get("state") != "invalid"}
        next_link = {row_id: (duty_of(row_id) or {}).get("supersedes") for row_id in valid_ids}
        visited_global = set()
        for start in sorted(valid_ids):
            if start in visited_global:
                continue
            path, positions, cursor = [], {}, start
            while cursor in valid_ids and cursor not in positions and cursor not in visited_global:
                positions[cursor] = len(path)
                path.append(cursor)
                cursor = next_link.get(cursor)
            visited_global.update(path)
            if cursor in positions:
                for row_id in path[positions[cursor]:]:
                    changed |= invalidate(row_id, "supersession cycle detected")

        for row_id in sorted(rows):
            row = rows[row_id]
            if row.get("state") == "invalid":
                continue
            duty = duty_of(row_id)
            predecessor = duty.get("supersedes") if duty else None
            if predecessor is not None:
                pred_row = rows.get(predecessor)
                pred_duty = duty_of(predecessor)
                valid = (pred_row is not None and predecessor in scope
                         and pred_row.get("state") != "invalid"
                         and pred_row.get("state") == "superseded"
                         and pred_row.get("superseded_by") == row_id
                         and pred_duty is not None
                         and pred_duty.get("created_by") == duty.get("created_by"))
                if not valid:
                    changed |= invalidate(row_id, "duty.supersedes is dangling, mismatched, out of scope, or owner-inconsistent")
                    continue
            if row.get("state") == "superseded":
                successor = row.get("superseded_by")
                successor_row = rows.get(successor)
                successor_duty = duty_of(successor)
                valid = (successor_row is not None and successor in scope
                         and successor_row.get("state") != "invalid"
                         and successor_duty is not None
                         and successor_duty.get("supersedes") == row_id
                         and successor_duty.get("created_by") == duty.get("created_by"))
                if not valid:
                    changed |= invalidate(row_id, "superseded.by is dangling, mismatched, out of scope, or owner-inconsistent")

        # Rebuild only after every link invalidation in this pass.  Same-key claimants include
        # invalid records: an invalid duplicate still breaks the uniqueness claim for its valid
        # peer, while connectivity itself is computed exclusively over final-candidate records.
        valid_adjacency = {
            row_id: set() for row_id, row in rows.items() if row.get("state") != "invalid"
        }
        for row_id in valid_adjacency:
            predecessor = (duty_of(row_id) or {}).get("supersedes")
            if (predecessor in valid_adjacency
                    and rows[predecessor].get("superseded_by") == row_id):
                valid_adjacency[row_id].add(predecessor)
                valid_adjacency[predecessor].add(row_id)

        keys = {}
        for row_id in rows:
            duty = duty_of(row_id)
            if duty:
                keys.setdefault(duty["natural_key"], []).append(row_id)
        for claimants_for_key in keys.values():
            if len(claimants_for_key) < 2:
                continue
            valid_claimants = {row_id for row_id in claimants_for_key
                               if row_id in valid_adjacency}
            connected = len(valid_claimants) == len(claimants_for_key)
            if connected:
                seen, stack = set(), [claimants_for_key[0]]
                while stack:
                    cursor = stack.pop()
                    if cursor in seen:
                        continue
                    seen.add(cursor)
                    stack.extend(valid_adjacency[cursor] - seen)
                connected = valid_claimants.issubset(seen)
            if not connected:
                for row_id in claimants_for_key:
                    changed |= invalidate(
                        row_id,
                        "natural_key is claimed by duties outside one valid reciprocal chain",
                    )


def fold_records(raw_events: list[dict], raw_boundary: dict,
                 event_lines: list[int] | None = None, graph_validator=None) -> tuple[int, dict]:
    """Validate already-parsed JSON objects in-process.

    Parsing is deliberately outside this function.  For any JSON-object inputs, this function's
    first operation after the extension pre-pass is the sole typing conversion; all phases below
    consume only its total records and collect validation errors instead of raising on input data.
    """
    if event_lines is None:
        event_lines = list(range(1, len(raw_events) + 1))
    extension_errors = extension_detections(raw_events, raw_boundary)
    if extension_errors:
        return E_EXTENSION, {
            "boundary_id": (raw_boundary.get("boundary_id")
                            if isinstance(raw_boundary.get("boundary_id"), str) else None),
            "duties": [], "aggregate": {"block": True, "required_actions": []},
            "validation": {"status": "invalid", "exit_code": E_EXTENSION,
                           "errors": extension_errors, "warnings": [], "duplicates_ignored": []},
        }
    errors, warnings, duplicates = [], [], []
    retained_typed, boundary = type_inputs(raw_events, raw_boundary, errors)
    retained, seen_pairs = [], set()
    for event, line_number in zip(retained_typed, event_lines):
        pair = (event.get("id"), event.get("seq"))
        valid_pair = (is_nonblank(pair[0]) and isinstance(pair[1], int)
                      and not isinstance(pair[1], bool))
        if valid_pair and pair in seen_pairs:
            duplicates.append({"id": pair[0], "seq": pair[1], "line": line_number})
            warnings.append({"reason": "duplicate (id, seq) ignored; first retained", "id": pair[0], "seq": pair[1]})
            continue
        if valid_pair:
            seen_pairs.add(pair)
        retained.append(event)
    retained.sort(key=lambda event: (str(event.get("id", "")), event.get("seq") if isinstance(event.get("seq"), int) else -1))
    validate_boundary(boundary, retained, errors)

    # Boundary-independent phases always run.  In particular, an unusable boundary cannot mask a
    # lower event code: grouping, envelope checks, duty admission and lifecycle folding happen
    # before the first boundary-dependent skip.
    grouped = {}
    for event in retained:
        # A mint whose envelope id was rejected can still carry an independently valid duty id.
        # Use that id only as the grouping fallback; a usable envelope id remains authoritative
        # and validate_duty reports any disagreement between the two.
        claim = mint_identity_claim(event)
        row_id = event.get("id", "")
        if not is_nonblank(row_id) and claim is not None:
            row_id = claim["id"]
        grouped.setdefault(row_id, []).append(event)
    rows = {row_id: fold_row(row_id, events, {}, errors)
            for row_id, events in sorted(grouped.items())}

    scope_usable = string_list(boundary.get("in_scope_duty_ids"))
    in_scope = boundary.get("in_scope_duty_ids", []) if scope_usable else []
    if scope_usable:
        for missing in sorted(set(in_scope) - set(rows)):
            issue(errors, E_BOUNDARY, "in-scope duty is absent from events", missing)
            rows[missing] = invalid_row(missing)
        (graph_validator or validate_supersession_graph)(rows, in_scope, errors)

    boundary_usable = (is_nonblank(boundary.get("boundary_id"))
                       and is_rfc3339(boundary.get("recorded_cutoff"))
                       and scope_usable)
    if not boundary_usable:
        code = min((item["code"] for item in errors), default=E_BOUNDARY)
        return code, {
            "boundary_id": boundary.get("boundary_id"), "duties": [],
            "aggregate": {"block": True, "required_actions": []},
            "validation": {"status": "invalid", "exit_code": code, "errors": errors,
                           "warnings": warnings, "duplicates_ignored": duplicates},
        }
    validate_observations(boundary, retained, rows, errors)
    boundary_invalid = any(item["code"] in {E_BOUNDARY, E_BATCH} for item in errors)
    projected = []
    for row_id in sorted(set(in_scope)):
        projected.append(project(rows[row_id], boundary, rows, errors))
    if boundary_invalid:
        for row in projected:
            row["contribution"] = "FAIL"
            for occurrence in row.get("occurrences", {}).values():
                occurrence["contribution"] = "FAIL"
                occurrence["reason"] = "invalid boundary or evaluator batch dominates projection"
            row["_block"] = True
    actions = build_required_actions(projected)
    block = bool(errors) or any(row.get("_block", row["state"] == "invalid") for row in projected)
    for row in projected:
        row.pop("_block", None)
        row.pop("_raw_occurrences", None)
        row.pop("_occurrences", None)
        row.pop("_admitted_duty", None)
        row.pop("_duty", None)
        row.pop("superseded_by", None)
    code = min((item["code"] for item in errors), default=(E_BLOCKED if block else 0))
    report = {
        "boundary_id": boundary.get("boundary_id"), "duties": projected,
        "aggregate": {"block": block, "required_actions": actions},
        "validation": {"status": "invalid" if errors else "valid", "exit_code": code,
                       "errors": errors, "warnings": warnings, "duplicates_ignored": duplicates},
    }
    return code, report


def fold_files(events_path: pathlib.Path, boundary_path: pathlib.Path) -> tuple[int, dict]:
    input_errors = []
    try:
        raw_events, event_lines = load_event_records(events_path)
    except FoldFail as exc:
        input_errors.append(exc)
        raw_events, event_lines = [], []
    try:
        raw_boundary = load_object(boundary_path)
    except FoldFail as exc:
        input_errors.append(exc)
        raw_boundary = {}
    if input_errors:
        raise input_errors[0]
    return fold_records(raw_events, raw_boundary, event_lines)


def occurrence_projection(report: dict) -> list[dict]:
    result = []
    for duty in report["duties"]:
        for oid, occurrence in duty.get("occurrences", {}).items():
            result.append({"duty_id": duty["id"], "occurrence_id": oid,
                           "state": occurrence["state"], "contribution": occurrence["contribution"]})
    return sorted(result, key=canonical)


def case_matches(case: dict, code: int, report: dict) -> bool:
    expected_occurrences = sorted(case.get("expected_occurrences", []), key=canonical)
    expected_errors = case.get("expected_errors", [])
    return (code == case["expected_exit"]
            and report["aggregate"]["block"] is case["expected_block"]
            and occurrence_projection(report) == expected_occurrences
            and report["aggregate"]["required_actions"] == case.get("expected_required_actions", [])
            and ("expected_duties" not in case or case["expected_duties"] == [
                {key: duty[key] for key in ("id", "state", "contribution")} for duty in report["duties"]])
            and report["validation"]["errors"] == expected_errors)


def _run_fixture_via_cli(case: dict) -> tuple[int, dict]:
    """Run one fixture through main(), including serialization and boundary validation."""
    captured = io.StringIO()
    argv = ["fold", "--events", str(FIXTURES / case["events"]),
            "--boundary", str(FIXTURES / case["boundary"])]
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    return code, loads_strict(captured.getvalue())


def _run_fold_via_cli(events_path: pathlib.Path, boundary_path: pathlib.Path) -> tuple[int, dict]:
    captured = io.StringIO()
    argv = ["fold", "--events", str(events_path), "--boundary", str(boundary_path)]
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    return code, loads_strict(captured.getvalue())


def _run_generated_fold_via_cli(events: list[dict], boundary: dict, event_lines: list[int],
                                fold_control=None, graph_validator=None) -> tuple[int, dict]:
    """Run generated objects through ``main()`` and the real report writer.

    The generators deliberately keep their inputs in memory: serializing roughly fourteen
    thousand pairs to disk would make the exhaustive self-test needlessly slow.  Only the two
    input readers are substituted.  Dispatch remains ``main -> fold_files -> fold_records`` and
    stdout still crosses ``write_report``'s schema boundary before these bytes are parsed back.
    Controls may wrap the production fold or graph validator, but they do so on the far side of
    that same boundary.
    """
    events_path = pathlib.Path("/__obligation_generated_events__.jsonl")
    boundary_path = pathlib.Path("/__obligation_generated_boundary__.json")
    original_load_events = globals()["load_event_records"]
    original_load_object = globals()["load_object"]
    original_fold = globals()["fold_records"]
    original_graph = globals()["validate_supersession_graph"]

    def generated_load_events(path):
        if path == events_path:
            return events, event_lines
        return original_load_events(path)

    def generated_load_object(path):
        if path == boundary_path:
            return boundary
        return original_load_object(path)

    globals()["load_event_records"] = generated_load_events
    globals()["load_object"] = generated_load_object
    if fold_control is not None:
        globals()["fold_records"] = lambda raw_events, raw_boundary, lines=None, **kwargs: fold_control(
            original_fold, raw_events, raw_boundary, lines, **kwargs)
    if graph_validator is not None:
        globals()["validate_supersession_graph"] = graph_validator

    raw_stdout = io.BytesIO()
    text_stdout = io.TextIOWrapper(raw_stdout, encoding="utf-8", errors="strict")
    try:
        with contextlib.redirect_stdout(text_stdout):
            code = main(["fold", "--events", str(events_path),
                         "--boundary", str(boundary_path)])
        text_stdout.flush()
        stdout_bytes = raw_stdout.getvalue()
    finally:
        globals()["load_event_records"] = original_load_events
        globals()["load_object"] = original_load_object
        globals()["fold_records"] = original_fold
        globals()["validate_supersession_graph"] = original_graph
    return code, loads_strict(stdout_bytes.decode("utf-8"))


def selftest_result(results: list[bool]) -> int:
    return 0 if results and all(results) else E_SELFTEST


def _contribution_schema_drift(document: dict) -> list[str]:
    expected = set(primitives.CONTRIBUTIONS)
    definitions = document.get("$defs", {})
    return [name for name in ("occurrence", "duty")
            if set(definitions.get(name, {}).get("properties", {}).get(
                "contribution", {}).get("enum", [])) != expected]


_DELETE = object()


def _json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _mutation_values() -> list[tuple[str, object]]:
    return [("array", []), ("object", {}), ("null", None), ("number", 7),
            ("bool", True), ("string", "mutation")]


def _record_paths(root) -> tuple[list[tuple], list[tuple]]:
    """Return every object-field path and every object path without recursive Python calls."""
    fields, objects, stack = [], [], [((), root)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            objects.append(path)
            for key, child in value.items():
                child_path = path + (key,)
                fields.append((child_path, child))
                stack.append((child_path, child))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                stack.append((path + (index,), child))
    return fields, objects


def _normalized_object_path(target: str, path: tuple, root: dict) -> str:
    """Stable coverage name: array/map members use ``[]``; payloads carry their event kind."""
    parts = ["boundary" if target == "boundary" else "event"]
    in_manifest = False
    in_occurrences = False
    for index, part in enumerate(path):
        if isinstance(part, int):
            parts[-1] += "[]"
        elif target == "event" and index == 0 and part == "payload":
            parts.append(f"payload({root.get('kind', '<invalid-kind>')})")
        elif in_manifest or in_occurrences:
            parts[-1] += "[]"
        else:
            parts.append(str(part))
            in_manifest = target == "boundary" and part == "observations_manifest"
            in_occurrences = target == "event" and part == "occurrences"
    return ".".join(parts)


def _path_changed(root, path: tuple, replacement):
    """Copy only containers on ``path``; large untouched fixture values remain shared."""
    if not path:
        return replacement
    head, tail = path[0], path[1:]
    if isinstance(root, dict):
        changed = dict(root)
        if not tail and replacement is _DELETE:
            changed.pop(head, None)
        else:
            changed[head] = _path_changed(root[head], tail, replacement)
        return changed
    changed = list(root)
    changed[head] = _path_changed(root[head], tail, replacement)
    return changed


def _mutated_input(events, boundary, target: str, index: int | None,
                   path: tuple, replacement) -> tuple[list[dict], dict]:
    if target == "boundary":
        return events, _path_changed(boundary, path, replacement)
    changed_events = list(events)
    changed_events[index] = _path_changed(events[index], path, replacement)
    return changed_events, boundary


def _strict_report(report: dict) -> bool:
    try:
        reparsed = loads_strict(emit_report(report, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return False
    return find_json_input_defect(reparsed) is None


_EVENT_TOP_LEVEL_MUTATION_CODES = {
    "id": E_REQUIRED_FIELD,
    "seq": E_REQUIRED_FIELD,
    "kind": E_EVENT_SCHEMA,
    "recorded_at": E_REQUIRED_FIELD,
    "actor": E_REQUIRED_FIELD,
    "batch_id": E_REQUIRED_FIELD,
    "payload": E_REQUIRED_FIELD,
}

_BOUNDARY_TOP_LEVEL_MUTATION_CODES = {
    "schema": E_BOUNDARY,
    "boundary_id": E_BOUNDARY,
    "recorded_cutoff": E_BOUNDARY,
    "in_scope_duty_ids": E_BOUNDARY,
    "evaluator_batch": E_BOUNDARY,
    "observations_manifest": E_BOUNDARY,
    "event_frontier": E_BOUNDARY,
}


def _mutation_expected_code(target: str, path: tuple, mutation_kind: str) -> int:
    """Static schema-field expectation table for the mutation generator.

    These tables encode the documented class assigned to each schema region.  They do not call
    the validator or derive an answer from its diagnostics.  Closed-object deletion is classified
    at the object that lost the key; an unknown field is classified at the object that gained it.
    """
    if target == "event":
        if not path:  # unknown field in the closed event envelope
            return E_EVENT_SCHEMA
        if len(path) == 1 and mutation_kind == "delete":
            return E_EVENT_SCHEMA
        if len(path) == 1:
            return _EVENT_TOP_LEVEL_MUTATION_CODES[path[0]]
        if path[-1] == "supersedes" and mutation_kind == "string":
            return E_IDENTITY
        if path[-1] == "occurrences" and mutation_kind == "unknown-field":
            return E_TRANSITION
        return E_REQUIRED_FIELD

    if not path:  # unknown field in the closed boundary object
        return E_BOUNDARY
    top = path[0]
    if len(path) == 1:
        return _BOUNDARY_TOP_LEVEL_MUTATION_CODES[top]
    if top == "evaluator_batch":
        # The batch object itself is boundary structure; its values and event-id map are batch
        # metadata.  Deleting a required batch key also violates the closed boundary sub-object.
        if len(path) == 1 or (len(path) == 2 and mutation_kind == "delete"):
            return E_BOUNDARY
        return E_BATCH
    if top == "observations_manifest":
        # An extra duty key changes boundary scope; fields below a duty are batch observations.
        if len(path) == 1 or (len(path) == 2 and mutation_kind == "delete"):
            return E_BOUNDARY
        return E_BATCH
    return E_BOUNDARY


def _control_accept_every_mutation(_original_fold, _events, boundary, _lines, **_kwargs):
    """Planted mutation-generator defect: classify every mutated input as accepted."""
    return 0, {
        "boundary_id": boundary.get("boundary_id"), "duties": [],
        "aggregate": {"block": False, "required_actions": []},
        "validation": {"status": "valid", "exit_code": 0, "errors": [],
                       "warnings": [], "duplicates_ignored": []},
    }


def _undeclared_report_key_control() -> tuple[bool, int, str]:
    """Wrap fold_records in a temp copy and require its real self-test to exit nonzero."""
    if os.environ.get("OBLIGATION_REPORT_KEY_CONTROL_ACTIVE") == "1":
        return True, E_SELFTEST, "nested control suppressed"
    original_head = '''def fold_records(raw_events: list[dict], raw_boundary: dict,
                 event_lines: list[int] | None = None, graph_validator=None) -> tuple[int, dict]:
    """Validate already-parsed JSON objects in-process.'''
    renamed_head = '''def _fold_records_without_control(raw_events: list[dict], raw_boundary: dict,
                 event_lines: list[int] | None = None, graph_validator=None) -> tuple[int, dict]:
    """Validate already-parsed JSON objects in-process.'''
    insertion_point = ("\ndef fold_files(events_path: pathlib.Path, boundary_path: pathlib.Path) "
                       "-> tuple[int, dict]:")
    wrapper = '''
def fold_records(raw_events: list[dict], raw_boundary: dict,
                 event_lines: list[int] | None = None, graph_validator=None) -> tuple[int, dict]:
    code, report = _fold_records_without_control(
        raw_events, raw_boundary, event_lines, graph_validator=graph_validator)
    report["__selftest_undeclared__"] = True
    return code, report


def fold_files(events_path: pathlib.Path, boundary_path: pathlib.Path) -> tuple[int, dict]:'''
    with tempfile.TemporaryDirectory(prefix="obligation-report-key-control-") as tmp:
        tmp_root = pathlib.Path(tmp)
        copied = tmp_root / "format_obligation"
        shutil.copytree(HERE.parent, copied)
        # A read-only checkout copies as read-only; the control must patch its own copy.
        for path in (copied, *copied.rglob("*")):
            path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR
                       | (stat.S_IXUSR if path.is_dir() else 0))
        tool = copied / "tools/obligation_fold.py"
        source = tool.read_text(encoding="utf-8")
        control_pos = source.find("\ndef _undeclared_report_key_control()")
        fold_pos = source.find(original_head, 0, control_pos)
        files_pos = source.find(insertion_point, 0, control_pos)
        if fold_pos < 0 or files_pos < 0 or files_pos <= fold_pos:
            return False, 0, "could not plant fold_records report-key wrapper"
        source = source[:fold_pos] + renamed_head + source[fold_pos + len(original_head):]
        files_pos = source.find(insertion_point, 0, control_pos)
        source = source[:files_pos] + "\n" + wrapper + source[files_pos + len(insertion_point):]
        tool.write_text(source, encoding="utf-8")
        env = dict(os.environ)
        env["OBLIGATION_REPORT_KEY_CONTROL_ACTIVE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-B", str(tool), "--selftest"], cwd=tmp_root,
            capture_output=True, text=True, env=env, timeout=300,
        )
    return proc.returncode == E_SELFTEST, proc.returncode, (proc.stdout + proc.stderr)[-500:]


_MINT_ENVELOPE_DEFECTS = (
    # name, field, replacement, static admission code.  ``None`` names a planted extra field.
    ("extra-field", None, True, E_EVENT_SCHEMA),
    ("missing-id", "id", _DELETE, E_EVENT_SCHEMA),
    ("id-type", "id", [], E_REQUIRED_FIELD),
    ("id-blank", "id", "", E_REQUIRED_FIELD),
    ("missing-seq", "seq", _DELETE, E_EVENT_SCHEMA),
    ("seq-type", "seq", [], E_REQUIRED_FIELD),
    ("seq-zero", "seq", 0, E_REQUIRED_FIELD),
    ("seq-overflow", "seq", MAX_INT64 + 1, E_REQUIRED_FIELD),
    ("missing-recorded-at", "recorded_at", _DELETE, E_EVENT_SCHEMA),
    ("recorded-at-type", "recorded_at", [], E_REQUIRED_FIELD),
    ("recorded-at-invalid", "recorded_at", "not-rfc3339", E_REQUIRED_FIELD),
    ("missing-actor", "actor", _DELETE, E_EVENT_SCHEMA),
    ("actor-type", "actor", [], E_REQUIRED_FIELD),
    ("actor-blank", "actor", "", E_REQUIRED_FIELD),
    ("missing-batch-id", "batch_id", _DELETE, E_EVENT_SCHEMA),
    ("batch-id-type", "batch_id", [], E_REQUIRED_FIELD),
    ("batch-id-blank", "batch_id", "", E_REQUIRED_FIELD),
)


def _combined_static_exit(*codes: int) -> int:
    """Static precedence for combined mutations; independent of produced diagnostics."""
    if E_EXTENSION in codes:
        return E_EXTENSION
    validation = [code for code in codes if code not in {0, E_BLOCKED}]
    return min(validation, default=E_BLOCKED)


def _run_mutation_fuzz(cases: list[dict], seed: int = 0x0B11A710,
                       fold_control=None, graph_validator=None) -> tuple[bool, int, list]:
    """Exhaust schema mutations; require crash freedom, rejection, and static classification."""
    rng = random.Random(seed)
    documented = {0, 1, 10, 11, *range(13, 21)}
    # Start only from schema-valid accepted/blocked fixtures.  Mutating an already-invalid or
    # extension-bearing input would make the requested field classification unobservable behind
    # the baseline defect and could let an accept-everything validator appear to pass.
    fixture_pairs = list(dict.fromkeys(
        (case["events"], case["boundary"])
        for case in cases if case.get("expected_exit") in {0, E_BLOCKED}
    ))
    rng.shuffle(fixture_pairs)
    checked, failures, unknown_key_coverage = 0, [], set()

    def check(events, boundary, lines, label, expected_code, expected_invalid_ids=()):
        nonlocal checked
        checked += 1
        try:
            code, report = _run_generated_fold_via_cli(
                events, boundary, lines, fold_control=fold_control,
                graph_validator=graph_validator)
            duties = {duty.get("id"): duty for duty in report.get("duties", [])}
            ok = (code in documented and code == expected_code and code not in {0, E_BLOCKED, E_INPUT}
                  and report.get("validation", {}).get("status") == "invalid"
                  and all(duties.get(row_id, {}).get("state") == "invalid"
                          for row_id in expected_invalid_ids)
                  and _strict_report(report))
        except Exception as exc:  # the assertion records the exception; it never escapes selftest
            ok, code, report = False, None, f"{type(exc).__name__}: {exc}"
        if not ok and len(failures) < 20:
            failures.append((label, code, report if isinstance(report, str) else
                             report.get("validation", {}).get("errors", [])[:2]))
        return ok

    for events_name, boundary_name in fixture_pairs:
        try:
            events, lines = load_event_records(FIXTURES / events_name)
            boundary = load_object(FIXTURES / boundary_name)
        except FoldFail:
            continue  # malformed/non-object input is exactly the documented exit-65 domain
        # Keep classification mutations isolated from graph knock-on effects.  Graph invalidation
        # has its own exhaustive generator below; here one mutated schema field must determine the
        # exit class directly rather than invalidating a linked neighbour at a lower code.
        if len({event.get("id") for event in events}) != 1:
            continue
        roots = [("event", index, event) for index, event in enumerate(events)]
        roots.append(("boundary", None, boundary))
        for target, index, root in roots:
            fields, objects = _record_paths(root)
            for path, original in fields:
                for type_name, replacement in _mutation_values():
                    if type_name == _json_type(original):
                        continue
                    mutated_events, mutated_boundary = _mutated_input(
                        events, boundary, target, index, path, replacement)
                    expected_code = _mutation_expected_code(target, path, type_name)
                    check(mutated_events, mutated_boundary, lines,
                          (events_name, boundary_name, target, index, path, type_name),
                          expected_code)
                mutated_events, mutated_boundary = _mutated_input(
                    events, boundary, target, index, path, _DELETE)
                expected_code = _mutation_expected_code(target, path, "delete")
                check(mutated_events, mutated_boundary, lines,
                      (events_name, boundary_name, target, index, path, "delete"),
                      expected_code)
            for path in objects:
                obj = root
                for part in path:
                    obj = obj[part]
                if "__unknown_mutation_field__" in obj:
                    continue
                changed = dict(obj)
                changed["__unknown_mutation_field__"] = True
                mutated_events, mutated_boundary = _mutated_input(
                    events, boundary, target, index, path, changed)
                expected_code = _mutation_expected_code(target, path, "unknown-field")
                rejected = check(
                    mutated_events, mutated_boundary, lines,
                    (events_name, boundary_name, target, index, path, "unknown-field"),
                    expected_code)
                if rejected:
                    unknown_key_coverage.add(_normalized_object_path(target, path, root))

    # Event-kind grammar coverage is a typing question, not a lifecycle-validity question.  Some
    # documented kinds occur only in fixtures whose later transition ordering is deliberately
    # invalid.  Start from each individually shape-valid envelope, plant an unknown key at every
    # object level, and require the sole raw-to-typed pass itself to reject it.
    all_event_files = list(dict.fromkeys(case["events"] for case in cases))
    for events_name in all_event_files:
        try:
            events, _lines = load_event_records(FIXTURES / events_name)
        except FoldFail:
            continue
        for event in events:
            baseline_errors = []
            baseline_typed = _type_event(event, baseline_errors)
            if baseline_errors or not baseline_typed.shape_valid:
                continue
            _, objects = _record_paths(event)
            for path in objects:
                obj = event
                for part in path:
                    obj = obj[part]
                changed = dict(obj)
                changed["__unknown_mutation_field__"] = True
                mutated = _path_changed(event, path, changed)
                errors = []
                typed = _type_event(mutated, errors)
                if errors and not typed.shape_valid:
                    unknown_key_coverage.add(_normalized_object_path("event", path, event))

    # Coverage sweep: graph-rich fixtures were intentionally excluded above because a single
    # typed mutation can have a lower-class knock-on error in a linked neighbour.  Unknown-key
    # closure does not need that exact-class assertion: it needs proof that each object level is
    # rejected.  Exercise every accepted/blocked fixture here, including multi-duty graphs, and
    # record only mutations that actually produce an invalid report.
    for events_name, boundary_name in fixture_pairs:
        try:
            events, lines = load_event_records(FIXTURES / events_name)
            boundary = load_object(FIXTURES / boundary_name)
        except FoldFail:
            continue
        roots = [("event", index, event) for index, event in enumerate(events)]
        roots.append(("boundary", None, boundary))
        for target, index, root in roots:
            _, objects = _record_paths(root)
            for path in objects:
                obj = root
                for part in path:
                    obj = obj[part]
                if "__unknown_mutation_field__" in obj:
                    continue
                changed = dict(obj)
                changed["__unknown_mutation_field__"] = True
                mutated_events, mutated_boundary = _mutated_input(
                    events, boundary, target, index, path, changed)
                try:
                    code, report = _run_generated_fold_via_cli(
                        mutated_events, mutated_boundary, lines, fold_control=fold_control,
                        graph_validator=graph_validator)
                    rejected = (code not in {0, E_BLOCKED, E_INPUT}
                                and report.get("validation", {}).get("status") == "invalid"
                                and _strict_report(report))
                except Exception:
                    rejected = False
                if rejected:
                    unknown_key_coverage.add(_normalized_object_path(target, path, root))

    # Combined class: every parseable, non-extension fixture carrying an independently valid mint
    # gets an unlinked same-key claimant.  Each envelope defect is planted on that duplicate in
    # turn.  The expected exit comes only from the fixture expectation plus this static table and
    # the natural-key rule; it is never inferred from validator diagnostics.
    for case_index, case in enumerate(cases):
        if case.get("expected_exit") in {E_EXTENSION, E_INPUT}:
            continue
        try:
            events, lines = load_event_records(FIXTURES / case["events"])
            boundary = load_object(FIXTURES / case["boundary"])
        except FoldFail:
            continue
        if not string_list(boundary.get("in_scope_duty_ids")):
            continue
        mint = next((event for event in events if mint_identity_claim(event) is not None), None)
        if mint is None or mint["id"] not in boundary["in_scope_duty_ids"]:
            continue
        duplicate_id = f"mutation-duplicate-{case_index}"
        if duplicate_id in boundary["in_scope_duty_ids"]:
            continue
        duplicate = copy.deepcopy(mint)
        duplicate["id"] = duplicate_id
        duplicate["seq"] = 1
        duplicate["payload"]["duty"]["id"] = duplicate_id
        duplicate["payload"]["duty"]["supersedes"] = None
        combined_boundary = copy.deepcopy(boundary)
        combined_boundary["in_scope_duty_ids"].append(duplicate_id)
        if isinstance(combined_boundary.get("observations_manifest"), dict):
            combined_boundary["observations_manifest"][duplicate_id] = {}
        if isinstance(combined_boundary.get("event_frontier"), dict):
            combined_boundary["event_frontier"][duplicate_id] = 1
        for defect_name, field, replacement, defect_code in _MINT_ENVELOPE_DEFECTS:
            defective = copy.deepcopy(duplicate)
            if field is None:
                defective["__unknown_envelope_field__"] = replacement
            elif replacement is _DELETE:
                defective.pop(field, None)
            else:
                defective[field] = copy.deepcopy(replacement)
            mutated_events = list(events) + [defective]
            expected_code = _combined_static_exit(case["expected_exit"], defect_code, E_IDENTITY)
            check(mutated_events, combined_boundary, lines + [len(lines) + 1],
                  (case["events"], case["boundary"], "duplicate-mint-envelope", defect_name),
                  expected_code, (mint["id"], duplicate_id))
    return not failures and checked > 0, checked, failures, sorted(unknown_key_coverage)


_PRECEDENCE_EXPECTED = {
    (0, 1): 1, (0, 10): 10, (0, 11): 11, (0, 13): 13, (0, 14): 14,
    (0, 15): 15, (0, 16): 16, (0, 17): 17, (0, 18): 18, (0, 19): 19,
    (0, 20): 20,
    (1, 10): 10, (1, 11): 11, (1, 13): 13, (1, 14): 14, (1, 15): 15,
    (1, 16): 16, (1, 17): 17, (1, 18): 18, (1, 19): 19, (1, 20): 20,
    (10, 11): 10, (10, 13): 10, (10, 14): 10, (10, 15): 10, (10, 16): 10,
    (10, 17): 10, (10, 18): 10, (10, 19): 10, (10, 20): 20,
    (11, 13): 11, (11, 14): 11, (11, 15): 11, (11, 16): 11, (11, 17): 11,
    (11, 18): 11, (11, 19): 11, (11, 20): 20,
    (13, 14): 13, (13, 15): 13, (13, 16): 13, (13, 17): 13, (13, 18): 13,
    (13, 19): 13, (13, 20): 20,
    (14, 15): 14, (14, 16): 14, (14, 17): 14, (14, 18): 14, (14, 19): 14,
    (14, 20): 20,
    (15, 16): 15, (15, 17): 15, (15, 18): 15, (15, 19): 15, (15, 20): 20,
    (16, 17): 16, (16, 18): 16, (16, 19): 16, (16, 20): 20,
    (17, 18): 17, (17, 19): 17, (17, 20): 20,
    (18, 19): 18, (18, 20): 20,
    (19, 20): 20,
}


def _precedence_add_defect(events: list[dict], boundary: dict, code: int) -> None:
    """Mutate one real fixture pair with one documented defect; no expected code is computed."""
    if code in {0, E_BLOCKED}:
        return
    if code == E_EVENT_SCHEMA:
        events[-1]["kind"] = "selftest-unknown-kind"
    elif code == E_REQUIRED_FIELD:
        events[-1]["actor"] = []
    elif code == E_IDENTITY:
        events[0]["payload"]["duty"]["natural_key"] = "sha256:wrong-natural-key"
    elif code == E_UNKNOWN_OCCURRENCE:
        event = next(event for event in events if event["kind"] == "predicate_evaluated")
        event["payload"]["occurrence_id"] = "sha256:selftest-unknown-occurrence"
    elif code == E_TRANSITION:
        row_id = events[0]["id"]
        next_seq = max(event["seq"] for event in events if event["id"] == row_id) + 1
        extra_mint = copy.deepcopy(events[0])
        extra_mint["seq"] = next_seq
        events.append(extra_mint)
        boundary["event_frontier"][row_id] = next_seq
    elif code == E_SEQUENCE:
        row_id = events[-1]["id"]
        events[-1]["seq"] += 2
        boundary["event_frontier"][row_id] = events[-1]["seq"]
    elif code == E_BINDING:
        event = next(event for event in events if event["kind"] == "predicate_evaluated")
        event["payload"]["bound_revision"] = "sha256:selftest-wrong-binding"
    elif code == E_BOUNDARY:
        boundary.pop("recorded_cutoff", None)
    elif code == E_BATCH:
        boundary["evaluator_batch"]["event_count"] += 1
    elif code == E_EXTENSION:
        events[0]["authority_ref"] = "selftest:extension"
    else:
        raise AssertionError(f"no precedence defect for {code}")


def _control_choose_highest_exit(original_fold, events, boundary, lines, **kwargs):
    """Planted precedence defect: choose the highest collected validation code."""
    code, report = original_fold(events, boundary, lines, **kwargs)
    collected = [item["code"] for item in report.get("validation", {}).get("errors", [])]
    if collected:
        code = max(collected)
        report["validation"]["exit_code"] = code
    return code, report


def _run_precedence_pairs(cases: list[dict], fold_control=None) -> tuple[bool, int, list]:
    """Build and fold all 66 combined inputs against the hand-written precedence table."""
    codes = [0, 1, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20]
    failures = []
    valid_case = next(case for case in cases if case.get("expected_exit") == 0)
    blocked_case = next(case for case in cases if case.get("expected_exit") == E_BLOCKED)

    pairs = list(itertools.combinations(codes, 2))
    if set(pairs) != set(_PRECEDENCE_EXPECTED):
        return False, 0, [("static-table-coverage", len(pairs), len(_PRECEDENCE_EXPECTED))]

    for pair in pairs:
        base = blocked_case if E_BLOCKED in pair else valid_case
        events, lines = load_event_records(FIXTURES / base["events"])
        boundary = load_object(FIXTURES / base["boundary"])
        events, boundary = copy.deepcopy(events), copy.deepcopy(boundary)
        for code in pair:
            _precedence_add_defect(events, boundary, code)
        try:
            got, report = _run_generated_fold_via_cli(
                events, boundary, lines + list(range(len(lines) + 1, len(events) + 1)),
                fold_control=fold_control)
        except Exception as exc:
            got, report = None, f"{type(exc).__name__}: {exc}"
        expected = _PRECEDENCE_EXPECTED[pair]
        if (got != expected or not isinstance(report, dict) or not _strict_report(report)):
            failures.append((pair, expected, got))
    return not failures, len(pairs), failures


def _reference_chain_invalid(specs: dict, scope: set[str]) -> set[str]:
    """Small brute-force fixed point, intentionally separate from the production graph walk."""
    invalid = {row_id for row_id, spec in specs.items() if spec["invalid"]}
    while True:
        before = set(invalid)
        valid = set(specs) - invalid

        for predecessor in specs:
            successors = [row_id for row_id in valid if specs[row_id]["pred"] == predecessor]
            if len(successors) > 1:
                invalid.update(successors)

        valid = set(specs) - invalid
        for start in list(valid):
            path, positions, cursor = [], {}, start
            while cursor in valid and cursor not in positions:
                positions[cursor] = len(path)
                path.append(cursor)
                cursor = specs[cursor]["pred"]
            if cursor in positions:
                invalid.update(path[positions[cursor]:])

        valid = set(specs) - invalid
        for row_id in list(valid):
            spec = specs[row_id]
            predecessor, successor = spec["pred"], spec["succ"]
            if predecessor is not None and not (
                    predecessor in valid and predecessor in scope
                    and specs[predecessor]["succ"] == row_id
                    and specs[predecessor]["owner"] == spec["owner"]):
                invalid.add(row_id)
            if successor is not None and not (
                    successor in valid and successor in scope
                    and specs[successor]["pred"] == row_id
                    and specs[successor]["owner"] == spec["owner"]):
                invalid.add(row_id)

        valid = set(specs) - invalid
        adjacency = {row_id: set() for row_id in valid}
        for row_id in valid:
            predecessor = specs[row_id]["pred"]
            if predecessor in valid and specs[predecessor]["succ"] == row_id:
                adjacency[row_id].add(predecessor)
                adjacency[predecessor].add(row_id)
        for key in {spec["key"] for spec in specs.values()}:
            claimants = [row_id for row_id, spec in specs.items() if spec["key"] == key]
            if len(claimants) < 2:
                continue
            if any(row_id not in valid for row_id in claimants):
                invalid.update(claimants)
                continue
            reached, frontier = set(), [claimants[0]]
            while frontier:
                cursor = frontier.pop()
                if cursor not in reached:
                    reached.add(cursor)
                    frontier.extend(adjacency[cursor] - reached)
            if any(row_id not in reached for row_id in claimants):
                invalid.update(claimants)
        if invalid == before:
            return invalid


def _control_skip_fixed_point(_rows, _in_scope, _errors):
    """Planted graph-generator defect: skip every fixed-point iteration."""


def _control_drop_invalid_claimant_metadata(rows, in_scope, errors):
    """Planted defect: reproduce the bug by erasing invalid duties' identity claims."""
    for row in rows.values():
        if row.get("state") == "invalid":
            row.pop("_duty", None)
            row.pop("_admitted_duty", None)
    validate_supersession_graph(rows, in_scope, errors)


def _run_chain_generator(seed: int = 0xC4A17, graph_validator=None) -> tuple[bool, int, list]:
    rng = random.Random(seed)
    generated, failures = 0, []

    def check(specs):
        nonlocal generated
        generated += 1
        events = []
        frontier = {}
        for row_id, spec in specs.items():
            duty = {
                "id": row_id, "natural_key": "", "scope": "generator-scope",
                "source": f"source:{spec['key']}", "source_revision": "R1",
                "trigger": {"event": f"trigger:{spec['key']}", "condition": "owed"},
                "action": f"action:{spec['key']}", "target": f"target:{spec['key']}",
                "retrigger": "once", "duty_kind": "one-time",
                "occurrence_rule": "generated immutable trigger identity",
                "required_by": "2099-01-01",
                "discharge_predicate": {
                    "resolver": "gate-passes:generator", "operands": "generated", "resolved": True,
                },
                "created_by": spec["owner"], "created_at": "2026-09-01T00:00:00Z",
                "occurrences": {}, "status": "open", "discharge_evidence": [],
                "supersedes": spec["pred"],
            }
            duty["natural_key"] = natural_key(duty)
            mint = {
                "id": row_id, "seq": 1, "kind": "minted",
                "recorded_at": "2026-09-01T00:00:00Z", "actor": spec["owner"],
                "batch_id": "generator-ingest", "payload": {"duty": duty},
            }
            if spec["invalid"] == "admission":
                mint["actor"] = []
            row_events = [mint]
            terminal_kind = "superseded" if spec["succ"] else "dropped"
            terminal_payload = {"by": spec["succ"]} if spec["succ"] else {"reason": "generated terminal"}
            row_events.append({
                "id": row_id, "seq": 2, "kind": terminal_kind,
                "recorded_at": "2026-09-02T00:00:00Z", "actor": spec["owner"],
                "batch_id": "generator-ingest", "payload": terminal_payload,
            })
            if spec["invalid"] == "structural":
                extra = copy.deepcopy(mint)
                extra["seq"] = 3
                row_events.append(extra)
            events.extend(row_events)
            frontier[row_id] = len(row_events)
        scope = sorted(specs)
        boundary = {
            "schema": "obligation-boundary/v0", "boundary_id": "chain-generator",
            "recorded_cutoff": "2026-09-03T00:00:00Z", "in_scope_duty_ids": scope,
            "evaluator_batch": {
                "batch_id": "chain-generator", "input_snapshot_digest": "sha256:generated",
                "committed": True, "event_count": 0,
                "event_ids": {kind: [] for kind in EVALUATOR_KINDS},
            },
            "observations_manifest": {row_id: {} for row_id in scope},
            "event_frontier": frontier,
        }
        try:
            _, report = _run_generated_fold_via_cli(
                events, boundary, list(range(1, len(events) + 1)),
                graph_validator=graph_validator)
            got = {row["id"] for row in report["duties"] if row["state"] == "invalid"}
        except Exception as exc:
            got = {f"EXCEPTION:{type(exc).__name__}:{exc}"}
        expected = _reference_chain_invalid(specs, set(scope))
        if got != expected and len(failures) < 20:
            failures.append((generated, sorted(expected), sorted(got)))

    for length in range(1, 31):
        ids = [f"n{length}-{index}" for index in range(length)]
        base = {}
        for index, row_id in enumerate(ids):
            base[row_id] = {
                "pred": ids[index - 1] if index else None,
                "succ": ids[index + 1] if index + 1 < length else None,
                "owner": "owner",
                # Mixed keys, with connected repetitions at both ends when possible.
                "key": "repeat" if index in {0, length - 1} else f"key-{index % 4}",
                "invalid": None,
            }
        check(copy.deepcopy(base))
        for invalid_position in range(length):
            for invalid_kind in ("structural", "admission"):
                case = copy.deepcopy(base)
                case[ids[invalid_position]]["invalid"] = invalid_kind
                check(case)
        # An admission-invalid, unlinked claimant must invalidate an otherwise valid same-key
        # chain solely through its retained metadata.  Dropping that metadata makes this graph
        # disagree with the independent reference and is the generator's dedicated control case.
        admission_claimant = copy.deepcopy(base)
        extra_claimant = f"n{length}-admission-claimant"
        admission_claimant[extra_claimant] = {
            "pred": None, "succ": None, "owner": "owner",
            "key": base[ids[0]]["key"], "invalid": "admission",
        }
        check(admission_claimant)
        if length >= 2:
            branch = copy.deepcopy(base)
            extra = f"n{length}-branch"
            branch[extra] = {"pred": ids[0], "succ": None, "owner": "owner",
                             "key": f"branch-{length}", "invalid": None}
            check(branch)
        if length >= 4:
            forest = copy.deepcopy(base)
            split = rng.randrange(1, length - 1)
            forest[ids[split]]["pred"] = None
            forest[ids[split - 1]]["succ"] = None
            forest[ids[0]]["key"] = forest[ids[-1]]["key"] = "forest-duplicate"
            check(forest)
    return not failures, generated, failures


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    fold = subparsers.add_parser("fold", allow_abbrev=False)
    fold.add_argument("--events", required=True)
    fold.add_argument("--boundary", required=True)
    return parser




def _enumerate_value_options(parser: argparse.ArgumentParser) -> list:
    """Every optional argument this parser accepts, across the top-level parser and every
    subparser, that consumes at least one value (never a `store_true`/`store_false` flag) — read
    PROGRAMMATICALLY from the parser itself, not hand-listed, so a future option is covered
    automatically without this self-test needing an update. Returns a list of
    `(subcommand_or_None, option_string)` pairs."""
    results = []

    def walk(p, subcmd):
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subp in action.choices.items():
                    walk(subp, name)
                continue
            if not action.option_strings or action.nargs == 0:
                continue
            for opt in action.option_strings:
                results.append((subcmd, opt))

    walk(parser, None)
    return results


def _argv_fuzz_report_clean(stdout_bytes: bytes, returncode: int, documented_exits: set) -> tuple:
    """Returns `(ok, detail)`. `ok` iff `returncode` is one of this tool's own documented exit
    codes AND, when stdout is non-empty, it decodes as UTF-8 and parses under this tool's own strict JSON reader
    (duplicate-key rejection, non-finite-number rejection, UTF-8-encodability, bounded nesting)
    with no defect."""
    if returncode not in documented_exits:
        return False, f"undocumented exit code {returncode}"
    try:
        text = stdout_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        return False, f"stdout is not valid UTF-8: {exc}"
    if not text:
        return True, None
    try:
        obj = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        return False, f"stdout does not parse as strict JSON: {type(exc).__name__}: {exc}"
    defect = find_json_input_defect(obj)
    if defect is not None:
        return False, defect
    return True, None


def _encoding_regression_failures(tool_script, cases, timeout: float = 10.0) -> list:
    """Regression for the stdout-encoding backstop: for each `(argv, documented_exits)` pair, runs
    `tool_script` as a subprocess once with `PYTHONIOENCODING=utf-8-sig` and once with
    `PYTHONIOENCODING=utf-16`, and requires the raw captured stdout bytes to start without a
    byte-order mark AND pass `_argv_fuzz_report_clean` under `documented_exits`. Returns a list of
    `(env_value, argv, detail)` failures, empty iff every combination produced a clean, BOM-free
    report. Mutation that turns this red: removing `force_utf8_stdout()` from the shared
    `run_cli()` boundary makes the utf-8-sig case fail with an unexpected-BOM defect on re-parse."""
    boms = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")
    failures = []
    for env_value in ("utf-8-sig", "utf-16"):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = env_value
        for argv, documented_exits in cases:
            proc = subprocess.run([sys.executable, "-B", str(tool_script)] + list(argv),
                                   capture_output=True, env=env, timeout=timeout)
            if proc.stdout.startswith(boms):
                failures.append((env_value, list(argv), "stdout begins with a byte-order mark"))
                continue
            ok, detail = _argv_fuzz_report_clean(proc.stdout, proc.returncode, documented_exits)
            if not ok:
                failures.append((env_value, list(argv), detail))
    return failures


def _run_argv_fuzz(tool_script, parser, baseline_by_subcommand: dict, documented_exits: set,
                    timeout: float = 10.0) -> tuple:
    """For EVERY value-taking option this tool's parser accepts (read programmatically via
    `_enumerate_value_options`), plus one unknown option and one hostile subcommand token, runs
    the CLI in a SEPARATE PROCESS with a hostile value (`b"x\\xff"`, a byte with no valid UTF-8
    interpretation, decoded by `surrogateescape`; and once with a 10,000-character value) in that
    position, and checks the WHOLE captured stdout with `_argv_fuzz_report_clean`. Returns
    `(positions_covered, failures)` — `failures` is a list of `(subcommand, option, hostile_repr,
    returncode, detail)` tuples, empty iff every position produced a clean report."""
    hostile_values = [b"x\xff", "y" * 10_000]
    failures = []
    covered = 0
    for subcmd, opt in _enumerate_value_options(parser):
        baseline = baseline_by_subcommand.get(subcmd)
        if baseline is None:
            continue
        for hostile in hostile_values:
            argv = list(baseline)
            if opt in argv:
                idx = argv.index(opt)
                argv = argv[:idx + 1] + [hostile] + argv[idx + 2:]
            else:
                argv = argv + [opt, hostile]
            covered += 1
            proc = subprocess.run([sys.executable, "-B", str(tool_script)] + argv,
                                   capture_output=True, timeout=timeout)
            ok, detail = _argv_fuzz_report_clean(proc.stdout, proc.returncode, documented_exits)
            if not ok:
                failures.append((subcmd, opt, repr(hostile)[:60], proc.returncode, detail))
    # One unknown option (the option NAME itself is hostile), per baseline subcommand.
    for subcmd, baseline in baseline_by_subcommand.items():
        covered += 1
        argv = list(baseline) + [b"--bad-\xff"]
        proc = subprocess.run([sys.executable, "-B", str(tool_script)] + argv,
                               capture_output=True, timeout=timeout)
        ok, detail = _argv_fuzz_report_clean(proc.stdout, proc.returncode, documented_exits)
        if not ok:
            failures.append((subcmd, "<unknown-option>", "--bad-\\xff", proc.returncode, detail))
    # Subcommand-level invalid value: the positional command token itself is hostile.
    for hostile in hostile_values:
        covered += 1
        proc = subprocess.run([sys.executable, "-B", str(tool_script), hostile],
                               capture_output=True, timeout=timeout)
        ok, detail = _argv_fuzz_report_clean(proc.stdout, proc.returncode, documented_exits)
        if not ok:
            failures.append(("<subcommand>", "<subcommand>", repr(hostile)[:60], proc.returncode, detail))
    return covered, failures

def run_selftest() -> int:
    try:
        cases = load_object(FIXTURES / "cases.json").get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases must be a nonempty array")
        baseline_case = next(case for case in cases if case.get("expected_exit") == 0)
        results = []
        for case in cases:
            code, report = _run_fixture_via_cli(case)
            ok = case_matches(case, code, report)
            results.append(ok)
            print(f"selftest {case['name']}: expected exit={case['expected_exit']} got exit={code}; "
                  f"block={str(report['aggregate']['block']).lower()} [{'ok' if ok else 'MISMATCH'}]")
            if not ok:
                print(emit_report(report, sort_keys=True, allow_nan=False))
        usage_output = io.StringIO()
        with contextlib.redirect_stdout(usage_output):
            usage_code = main(["fold"])
        usage_report = loads_strict(usage_output.getvalue())
        usage_code = (usage_code if usage_report["validation"].get("exit_code") == usage_code
                      else -1)
        results.append(usage_code == E_USAGE)
        print(f"selftest usage-error: expected exit=64 got exit={usage_code} "
              f"[{'ok' if usage_code == E_USAGE else 'MISMATCH'}]")
        original_fold_files = globals()["fold_files"]
        def _boom(*_args, **_kwargs):
            raise RuntimeError("selftest-injected catch-all exception")
        globals()["fold_files"] = _boom
        try:
            exception_code, exception_report = _run_fixture_via_cli(baseline_case)
        finally:
            globals()["fold_files"] = original_fold_files
        exception_ok = (exception_code == E_INPUT and
                        exception_report["validation"]["errors"][0]["reason"].startswith(
                            "input/runtime failure"))
        results.append(exception_ok)
        print(f"selftest exit-65-injected-exception-catch-all: got exit={exception_code} "
              f"[{'ok' if exception_ok else 'MISMATCH'}]")
        extension_case = next(case for case in cases if case.get("expected_exit") == E_EXTENSION)
        precedence_proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_PATH), "fold",
             "--events", str(FIXTURES / extension_case["events"]),
             "--boundary", str(FIXTURES / extension_case["boundary"]), "--unknown-option"],
            capture_output=True)
        usage_precedence_ok = precedence_proc.returncode == E_USAGE
        results.append(usage_precedence_ok)
        print(f"selftest precedence-usage-over-extension: expected exit=64 "
              f"got exit={precedence_proc.returncode} "
              f"[{'ok' if usage_precedence_ok else 'MISMATCH'}]")
        legacy_module = "obligation_fold_" + "legacy"
        core_imports_legacy = bool(re.search(
            rf"^\s*(?:from\s+{legacy_module}\s+import|import\s+{legacy_module})",
            SCRIPT_PATH.read_text(encoding="utf-8"), flags=re.M))
        import_graph_ok = not core_imports_legacy
        results.append(import_graph_ok)
        print(f"selftest core-does-not-import-legacy: imports_legacy={core_imports_legacy} "
              f"[{'ok' if import_graph_ok else 'MISMATCH'}]")
        mismatch_case = dict(baseline_case)
        mismatch_case["expected_exit"] = 12345
        mismatch_fold_code, mismatch_report = _run_fixture_via_cli(mismatch_case)
        mismatch_code = selftest_result([
            case_matches(mismatch_case, mismatch_fold_code, mismatch_report)
        ])
        results.append(mismatch_code == E_SELFTEST)
        print(f"selftest real-harness-mismatch-propagation: expected exit=99 got exit={mismatch_code} "
              f"[{'ok' if mismatch_code == E_SELFTEST else 'MISMATCH'}]")
        expected_exits = {0, 1, 10, 11, *range(13, 21), E_USAGE, E_INPUT, E_SELFTEST}
        covered_exits = {case["expected_exit"] for case in cases} | {usage_code, mismatch_code}
        missing_exits = sorted(expected_exits - covered_exits)
        unexpected_exits = sorted(covered_exits - expected_exits)
        coverage_ok = covered_exits == expected_exits
        results.append(coverage_ok)
        print(f"selftest exit-code-coverage: exits 0,1,10,11,13..20,64,65,99; "
              f"missing={missing_exits or 'none'} unexpected={unexpected_exits or 'none'} "
              f"[{'ok' if coverage_ok else 'MISMATCH'}]")
        used = {case["events"] for case in cases} | {case["boundary"] for case in cases}
        fixture_files = {path.name for path in FIXTURES.iterdir() if path.name != "cases.json"}
        unused_fixtures = sorted(fixture_files - used)
        fixture_coverage = not unused_fixtures
        results.append(fixture_coverage)
        print(f"selftest fixture-coverage: unused={unused_fixtures or 'none'} "
              f"[{'ok' if fixture_coverage else 'MISMATCH'}]")

        schema_document = load_object(HERE.parent / REPORT_SCHEMA[0])
        clean_drift = _contribution_schema_drift(schema_document)
        planted_schema = copy.deepcopy(schema_document)
        planted_schema["$defs"]["occurrence"]["properties"]["contribution"]["enum"].remove(
            "WARN-recorded")
        planted_drift = _contribution_schema_drift(planted_schema)
        contribution_drift_ok = not clean_drift and planted_drift == ["occurrence"]
        results.append(contribution_drift_ok)
        print(f"selftest contribution-schema-enum-drift: clean={clean_drift or 'none'} "
              f"planted={planted_drift} "
              f"[{'ok' if contribution_drift_ok else 'MISMATCH'}]")

        mutation_ok, mutation_count, mutation_failures, mutation_coverage = _run_mutation_fuzz(cases)
        mutation_control_ok, mutation_control_count, _, _ = _run_mutation_fuzz(
            [baseline_case], fold_control=_control_accept_every_mutation)
        mutation_control_fired = not mutation_control_ok
        mutation_metadata_control_ok, mutation_metadata_control_count, _, _ = _run_mutation_fuzz(
            [baseline_case], graph_validator=_control_drop_invalid_claimant_metadata)
        mutation_metadata_control_fired = not mutation_metadata_control_ok
        report_key_control_fired, report_key_control_exit, report_key_control_detail = (
            _undeclared_report_key_control())
        results.extend((mutation_ok, mutation_control_fired, mutation_metadata_control_fired,
                        report_key_control_fired))
        print(f"selftest typed-mutation-fuzz: seed=0x0b11a710 {mutation_count} mutation(s), "
              f"{len(mutation_failures)} failure(s) {mutation_failures[:3]!r} "
              f"control fired={str(mutation_control_fired).lower()} "
              f"({mutation_control_count} control mutation(s)) "
              f"metadata-drop control fired={str(mutation_metadata_control_fired).lower()} "
              f"({mutation_metadata_control_count} control mutation(s)) "
              f"undeclared-report-key real-selftest control fired="
              f"{str(report_key_control_fired).lower()} exit={report_key_control_exit} "
              f"detail={report_key_control_detail!r} "
              f"[{'ok' if mutation_ok and mutation_control_fired and mutation_metadata_control_fired and report_key_control_fired else 'MISMATCH'}]")
        print("selftest typed-mutation-fuzz-coverage: "
              + json.dumps(mutation_coverage, sort_keys=True, separators=(",", ":"),
                           allow_nan=False))

        precedence_ok, precedence_count, precedence_failures = _run_precedence_pairs(cases)
        precedence_control_ok, precedence_control_count, _ = _run_precedence_pairs(
            cases, fold_control=_control_choose_highest_exit)
        precedence_control_fired = not precedence_control_ok
        results.extend((precedence_ok, precedence_control_fired))
        print(f"selftest precedence-pairs: {precedence_count} combined input pair(s), "
              f"{len(precedence_failures)} failure(s) {precedence_failures[:3]!r} "
              f"control fired={str(precedence_control_fired).lower()} "
              f"({precedence_control_count} control pair(s)) "
              f"[{'ok' if precedence_ok and precedence_control_fired else 'MISMATCH'}]")

        chain_ok, chain_count, chain_failures = _run_chain_generator()
        chain_control_ok, chain_control_count, _ = _run_chain_generator(
            graph_validator=_control_skip_fixed_point)
        chain_control_fired = not chain_control_ok
        chain_metadata_control_ok, chain_metadata_control_count, _ = _run_chain_generator(
            graph_validator=_control_drop_invalid_claimant_metadata)
        chain_metadata_control_fired = not chain_metadata_control_ok
        results.extend((chain_ok, chain_control_fired, chain_metadata_control_fired))
        print(f"selftest chain-fixed-point-generator: seed=0x000c4a17 {chain_count} graph(s), "
              f"{len(chain_failures)} failure(s) {chain_failures[:3]!r} "
              f"control fired={str(chain_control_fired).lower()} "
              f"({chain_control_count} control graph(s)) "
              f"metadata-drop control fired={str(chain_metadata_control_fired).lower()} "
              f"({chain_metadata_control_count} control graph(s)) "
              f"[{'ok' if chain_ok and chain_control_fired and chain_metadata_control_fired else 'MISMATCH'}]")

        # H2: every `json.dumps(` call site in THIS module's own source passes `allow_nan=False`
        # — a static, source-level control, independent of any one fixture reaching every call
        # site at runtime. Since the structural fold, every report this tool prints routes through
        # `emit_report` (one funnel that forces `allow_nan=False` regardless of what a caller
        # passes) — its own internal `json.dumps(` call carries `**dumps_kwargs`, not a literal
        # `allow_nan=False`, so it is recognized by name instead of by substring; a BEHAVIORAL check
        # (below) is what actually proves the force holds, not only this textual scan.
        own_source = SCRIPT_PATH.read_text(encoding="utf-8")
        dumps_calls = re.findall(r"json\.dumps\((?:[^()]|\([^()]*\))*\)", own_source, flags=re.S)
        dumps_ok = bool(dumps_calls) and all(
            "allow_nan=False" in call or "dumps_kwargs" in call for call in dumps_calls)
        try:
            emit_report({"n": float("inf")}, allow_nan=True)
            emit_report_forces_allow_nan = False
        except ValueError:
            emit_report_forces_allow_nan = True
        dumps_ok = dumps_ok and emit_report_forces_allow_nan
        results.append(dumps_ok)
        print(f"selftest dumps-allow-nan: {len(dumps_calls)} json.dumps call(s), "
              f"emit_report_forces_allow_nan={emit_report_forces_allow_nan} "
              f"[{'ok' if dumps_ok else 'MISMATCH'}]")

        # H3: `seq` is a signed 64-bit field (MAX_INT64 = 2**63-1). Checked directly against
        # `event_envelope_valid`, since the format's own "no gaps, starts at 1" per-duty sequence
        # rule makes a huge `seq` value unreachable through a full `fold` run — the FIELD's own
        # bound is still real and independently checked here.
        base_event = {
            "id": "selftest-row", "seq": MAX_INT64, "kind": "minted",
            "recorded_at": "2026-01-01T00:00:00Z", "actor": "a",
            "batch_id": "c", "payload": {"duty": {}},
        }
        at_bound_errors: list = []
        at_bound_ok = event_envelope_valid(dict(base_event), at_bound_errors)
        over_bound_errors: list = []
        over_bound_ok = event_envelope_valid(dict(base_event, seq=MAX_INT64 + 1), over_bound_errors)
        seq_range_ok = (at_bound_ok and not at_bound_errors and not over_bound_ok
                        and len(over_bound_errors) == 1
                        and over_bound_errors[0]["code"] == E_REQUIRED_FIELD
                        and "signed 64-bit" in over_bound_errors[0]["reason"])
        results.append(seq_range_ok)
        print(f"selftest seq-signed-64-bit-range: seq=MAX_INT64 ok={at_bound_ok}, "
              f"seq=MAX_INT64+1 ok={over_bound_ok} [{'ok' if seq_range_ok else 'MISMATCH'}]")

        # H3: `event_frontier` values are also signed 64-bit fields. Isolated from the separate
        # "highest consumed per-duty sequence" mismatch check by giving the in-scope duty NO
        # retained events at all, so that check's own `duty_events and ...` guard never fires —
        # only the new bound catches an out-of-range frontier value here.
        def _frontier_boundary(value: int) -> dict:
            return {
                "schema": "obligation-boundary/v0", "boundary_id": "selftest-boundary",
                "recorded_cutoff": "2026-01-01T00:00:00Z", "in_scope_duty_ids": ["selftest-duty"],
                "evaluator_batch": {
                    "batch_id": "selftest-batch", "input_snapshot_digest": "sha256:selftest",
                    "committed": True, "event_count": 0,
                    "event_ids": {"deadline_crossed": [], "predicate_evaluated": [],
                                  "stale_detected": []},
                },
                "observations_manifest": {"selftest-duty": {}},
                "event_frontier": {"selftest-duty": value},
            }
        frontier_rows = {"selftest-duty": {"state": "discharged", "_occurrences": {}}}
        frontier_at_errors: list = []
        validate_observations(_frontier_boundary(MAX_INT64), [], frontier_rows, frontier_at_errors)
        frontier_over_errors: list = []
        validate_observations(_frontier_boundary(MAX_INT64 + 7), [], frontier_rows, frontier_over_errors)
        frontier_range_ok = (not frontier_at_errors and len(frontier_over_errors) == 1
                             and frontier_over_errors[0]["code"] == E_BOUNDARY
                             and "event_frontier" in frontier_over_errors[0]["reason"])
        results.append(frontier_range_ok)
        print(f"selftest event-frontier-signed-64-bit-range: at-bound errors={len(frontier_at_errors)}, "
              f"over-bound errors={len(frontier_over_errors)} [{'ok' if frontier_range_ok else 'MISMATCH'}]")

        # H3: a 401-digit `event_count` (far short of json's own 4300-digit parse guard, but
        # nowhere near a real batch's actual event count) must not crash `validate_boundary` —
        # every comparison against MAX_INT64 is a plain binary comparison, never a string
        # conversion of the value.
        huge_count_boundary = {
            "schema": "obligation-boundary/v0", "boundary_id": "selftest-boundary-2",
            "recorded_cutoff": "2026-01-01T00:00:00Z", "in_scope_duty_ids": ["selftest-duty-2"],
            "evaluator_batch": {
                "batch_id": "selftest-batch-2", "input_snapshot_digest": "sha256:selftest2",
                "committed": True, "event_count": 10 ** 400,
                "event_ids": {"deadline_crossed": [], "predicate_evaluated": [],
                              "stale_detected": []},
            },
            "observations_manifest": {}, "event_frontier": {},
        }
        huge_count_errors: list = []
        validate_boundary(huge_count_boundary, [], huge_count_errors)
        huge_count_ok = (len(huge_count_errors) == 1 and huge_count_errors[0]["code"] == E_BATCH
                         and len(huge_count_errors[0]["reason"]) <= DIAGNOSTIC_REASON_MAX_CHARS)
        results.append(huge_count_ok)
        print(f"selftest event-count-huge-no-crash: errors={len(huge_count_errors)} "
              f"[{'ok' if huge_count_ok else 'MISMATCH'}]")

        # H4: a 10,000-character --events path never reaches an unbounded message.
        huge_path = pathlib.Path("x" * 10_000)
        try:
            load_events(huge_path)
            path_preview_ok = False
            path_preview_detail = "no FoldFail raised"
        except FoldFail as exc:
            path_preview_ok = exc.code == E_INPUT and len(str(exc)) < 300
            path_preview_detail = f"len(reason)={len(str(exc))}"
        results.append(path_preview_ok)
        print(f"selftest bounded-path-preview: 10000-char --events path -> {path_preview_detail} "
              f"[{'ok' if path_preview_ok else 'MISMATCH'}]")

        # H4: `bound_reason`'s own cap, including the truncation marker, holds for an arbitrarily
        # long input.
        capped = bound_reason("z" * 50_000)
        bound_reason_ok = (len(capped) == DIAGNOSTIC_REASON_MAX_CHARS
                           and capped.endswith(_REASON_TRUNCATION_MARKER))
        results.append(bound_reason_ok)
        print(f"selftest bound-reason-cap: len={len(capped)} [{'ok' if bound_reason_ok else 'MISMATCH'}]")

        # H4: `issue()` itself is the structural enforcement point — a raw, unbounded `reason`
        # string passed straight to `issue()` (never pre-bounded by the call site) must still come
        # out capped in the appended item.
        issue_errors: list = []
        issue(issue_errors, E_BINDING, "w" * 50_000)
        issue_reason_ok = (len(issue_errors) == 1
                           and len(issue_errors[0]["reason"]) == DIAGNOSTIC_REASON_MAX_CHARS
                           and issue_errors[0]["reason"].endswith(_REASON_TRUNCATION_MARKER))
        results.append(issue_reason_ok)
        print(f"selftest issue-constructor-bounds-reason: len={len(issue_errors[0]['reason'])} "
              f"[{'ok' if issue_reason_ok else 'MISMATCH'}]")

        # H4: an escape-heavy string must not make `repr()` push the result past the cap (doubling
        # every backslash cannot itself defeat `_cap_with_marker`).
        many_escapes = safe_repr_for_error("\\" * 5000)
        escapes_ok = len(many_escapes) <= _SAFE_REPR_MAX_CHARS
        results.append(escapes_ok)
        print(f"selftest safe-repr-escape-heavy: len={len(many_escapes)} [{'ok' if escapes_ok else 'MISMATCH'}]")

        # H4: 1,000 x U+E0000 (three times) in a single occurrence_id-shaped value, run
        # through the exact composition the `discharged`/`waived`/`stale_detected`/`waiver_lapsed`
        # unknown-occurrence path uses. `join_error_list` bounds EACH item on its own (never relying
        # on the caller to have already bounded it), so three ~3,000-character items come out well
        # under DIAGNOSTIC_REASON_MAX_CHARS without `bound_reason` ever needing to truncate — and
        # (since 3 < JOIN_ERROR_LIST_MAX_ITEMS) carries no omitted-count suffix.
        huge_ids = [("\U000e0000" * 1000) + str(i) for i in range(3)]
        joined_huge = join_error_list(huge_ids)
        aggregated_reason = bound_reason(f"discharged names unknown occurrence(s): {joined_huge}")
        aggregation_cap_ok = (len(aggregated_reason) <= DIAGNOSTIC_REASON_MAX_CHARS
                              and not aggregated_reason.endswith(_REASON_TRUNCATION_MARKER)
                              and "... and" not in joined_huge
                              and all(len(part) <= JOIN_ERROR_LIST_ITEM_MAX_CHARS
                                      for part in joined_huge.split(", ")))
        results.append(aggregation_cap_ok)
        print(f"selftest unknown-occurrence-oversized-value-cap: len={len(aggregated_reason)} "
              f"[{'ok' if aggregation_cap_ok else 'MISMATCH'}]")

        # Regression: 120 occurrence_ids, each ~10,000 characters — must leave the "... and N more"
        # count intact (the defect: the 10 shown items alone, unbounded, consumed the entire
        # DIAGNOSTIC_REASON_MAX_CHARS budget and silently ate the suffix). Mutation that turns this
        # red: reverting `join_error_list` to join raw,
        # unbounded items (the pre-fix code) makes `"... and 110 more" in reason` False and
        # `len(reason) == DIAGNOSTIC_REASON_MAX_CHARS` with the truncation marker instead.
        long_ids = [str(i) + "x" * 10_000 for i in range(120)]
        long_ids_joined = join_error_list(long_ids)
        long_ids_reason = bound_reason(f"discharged names unknown occurrence(s): {long_ids_joined}")
        long_ids_ok = (len(long_ids_reason) <= DIAGNOSTIC_REASON_MAX_CHARS
                       and "... and 110 more" in long_ids_reason
                       and not long_ids_reason.endswith(_REASON_TRUNCATION_MARKER))
        results.append(long_ids_ok)
        print(f"selftest join-error-list-long-ids-count-survives: len={len(long_ids_reason)} "
              f"and-110-more={'... and 110 more' in long_ids_reason} "
              f"[{'ok' if long_ids_ok else 'MISMATCH'}]")

        # H7: the fold's own JSON output, re-parsed under the same strict loader (H1-H4) this tool
        # applies to any foreign input, must come back clean — the producer run against its own
        # rule, not only hand-written fixtures.
        producer_code, producer_report = _run_fold_via_cli(
            FIXTURES / baseline_case["events"], FIXTURES / baseline_case["boundary"])
        producer_text = json.dumps(producer_report, sort_keys=True, allow_nan=False)
        reparsed = loads_strict(producer_text)
        roundtrip_defect = find_json_input_defect(reparsed)
        roundtrip_ok = roundtrip_defect is None and reparsed == producer_report
        results.append(roundtrip_ok)
        print(f"selftest producer-output-roundtrip: defect={roundtrip_defect or 'none'} "
              f"[{'ok' if roundtrip_ok else 'MISMATCH'}]")

        # H7, extended to ERROR reports (not only a passing fold's success output): two hostile
        # cases run against this tool's own strict reader — an `occurrence_id` replaced by a
        # 61-deep nested list (a depth-valid INPUT must never yield a deeper-than-64 OUTPUT), and
        # 120 oversized `occurrence_ids` (the omitted-count suffix must survive). Both mutate a copy
        # of the real `pass` fixture pair and run it through
        # the real `fold_files` entry point — never a hand-built report — so a fix that only patches
        # one diagnostic shape cannot pass here while leaving another broken. Mutation that turns the
        # nested-id case red: reverting `issue()` to copy `occurrence_id` through unchanged (the pre-fix code)
        # makes `nested_defect` report "nesting is deeper than 64 levels" instead of None.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            pass_events = [loads_strict(line) for line in
                           (FIXTURES / baseline_case["events"]).read_text(encoding="utf-8").splitlines()
                           if line.strip()]
            pass_boundary_path = FIXTURES / baseline_case["boundary"]

            nested_value = 0
            for _ in range(61):
                nested_value = [nested_value]
            nested_events = [dict(e) for e in pass_events]
            for event in nested_events:
                if event.get("kind") == "fired":
                    event["payload"] = dict(event["payload"], occurrence_id=nested_value)
            nested_path = tmp_path / "nested-id.events.jsonl"
            nested_path.write_text("\n".join(json.dumps(e, allow_nan=False) for e in nested_events), encoding="utf-8")
            nested_input_strict_ok = all(find_json_input_defect(e) is None for e in nested_events)
            nested_code, nested_report = _run_fold_via_cli(nested_path, pass_boundary_path)
            nested_text = json.dumps(nested_report, sort_keys=True, allow_nan=False)
            nested_defect = find_json_input_defect(loads_strict(nested_text))
            nested_ok = nested_input_strict_ok and nested_defect is None
            results.append(nested_ok)
            print(f"selftest hostile-nested-occurrence-id-error-report: input_strict_ok="
                  f"{nested_input_strict_ok} exit={nested_code} defect={nested_defect or 'none'} "
                  f"[{'ok' if nested_ok else 'MISMATCH'}]")

            long_ids_value = [str(i) + "x" * 10_000 for i in range(120)]
            long_ids_events = [dict(e) for e in pass_events]
            for event in long_ids_events:
                if event.get("kind") == "discharged":
                    event["payload"] = dict(event["payload"], occurrence_ids=long_ids_value)
                    break  # mutate only the FIRST matching event
            long_ids_path = tmp_path / "long-ids.events.jsonl"
            long_ids_path.write_text("\n".join(json.dumps(e, allow_nan=False) for e in long_ids_events), encoding="utf-8")
            long_ids_input_strict_ok = all(find_json_input_defect(e) is None for e in long_ids_events)
            long_ids_code, long_ids_report = _run_fold_via_cli(long_ids_path, pass_boundary_path)
            long_ids_text = json.dumps(long_ids_report, sort_keys=True, allow_nan=False)
            long_ids_defect = find_json_input_defect(loads_strict(long_ids_text))
            long_ids_error = next((e for e in long_ids_report["validation"]["errors"]
                                   if "unknown occurrence" in e["reason"]), None)
            long_ids_report_ok = (long_ids_input_strict_ok and long_ids_defect is None
                                  and long_ids_error is not None
                                  and len(long_ids_error["reason"]) <= DIAGNOSTIC_REASON_MAX_CHARS
                                  and "... and 110 more" in long_ids_error["reason"])
            results.append(long_ids_report_ok)
            print(f"selftest hostile-long-occurrence-ids-error-report: input_strict_ok="
                  f"{long_ids_input_strict_ok} exit={long_ids_code} defect={long_ids_defect or 'none'} "
                  f"reason_len={len(long_ids_error['reason']) if long_ids_error else -1} "
                  f"[{'ok' if long_ids_report_ok else 'MISMATCH'}]")

        # Regression: a `--events` path that is not valid Unicode at all (the OS gives
        # Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a lone
        # UTF-16 surrogate in the decoded argv string) must still produce a report that re-parses
        # strictly, with the exit code unchanged and the reason within the documented cap. Mutation
        # that turns this red: reverting `bound_path_preview` to `s = str(path)` with no escaping
        # (the pre-fix code) makes `defect` report "a string cannot encode as UTF-8" instead of None.
        surrogate_path = b"missing-\xff"
        surrogate_proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_PATH), "fold",
             "--events", surrogate_path, "--boundary", "x"],
            capture_output=True,
        )
        surrogate_report = loads_strict(surrogate_proc.stdout.decode("utf-8"))
        surrogate_defect = find_json_input_defect(surrogate_report)
        surrogate_reason = surrogate_report["validation"]["errors"][0]["reason"]
        surrogate_ok = (surrogate_proc.returncode == E_INPUT and surrogate_defect is None
                        and len(surrogate_reason) <= DIAGNOSTIC_REASON_MAX_CHARS
                        and "\udcff" not in surrogate_reason)
        results.append(surrogate_ok)
        print(f"selftest lone-surrogate-events-path-escaped-in-report: exit={surrogate_proc.returncode} "
              f"defect={surrogate_defect or 'none'} reason={surrogate_reason!r} "
              f"[{'ok' if surrogate_ok else 'MISMATCH'}]")

        # The regression that closes the class (argv fuzz).
        argv_fuzz_baselines = {
            "fold": ["fold", "--events", str(FIXTURES / baseline_case["events"]),
                     "--boundary", str(FIXTURES / baseline_case["boundary"])],
        }
        argv_fuzz_exits = set(expected_exits) | {E_USAGE}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            SCRIPT_PATH, make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_failures = _encoding_regression_failures(
            SCRIPT_PATH,
            [(argv_fuzz_baselines["fold"], {0}), (["fold"], {E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = output_boundary_selftest(
            SCRIPT_PATH, argv_fuzz_baselines["fold"], ["fold"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            SCRIPT_PATH, "make_failure_report", "fold_files",
            ["fold"], argv_fuzz_baselines["fold"])
        for name, (ok, detail) in boundary_controls.items():
            results.append(ok)
            print(f"selftest report-boundary-defect-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls_ok = all(ok for ok, _ in boundary_controls.values())
        print(f"selftest report-schema-conformance: boundary_controls="
              f"{sorted(boundary_controls)} "
              f"[{'ok' if boundary_controls_ok else 'MISMATCH'}]")

        status = selftest_result(results) if boundary_controls_ok else E_SELFTEST
        print(f"SELFTEST {'PASS' if status == 0 else 'FAIL'}: "
              f"{len(cases) + len(output_checks) + 13} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__})")
        return E_SELFTEST


def _main(argv=None) -> int:
    try:
        parser = make_parser()
        args = parser.parse_args(argv)
        if args.selftest:
            if args.command is not None:
                parser.error("--selftest cannot be combined with a command")
            return run_selftest()
        if args.command != "fold":
            parser.error("fold command or --selftest is required")
        code, report = fold_files(pathlib.Path(args.events), pathlib.Path(args.boundary))
    except FoldFail as exc:
        code = exc.code
        report = make_failure_report(code, str(exc))
    except Exception as exc:
        code = E_INPUT
        report = make_failure_report(code, f"input/runtime failure ({type(exc).__name__})")
    write_report(emit_report(report, indent=2, sort_keys=True, allow_nan=False))
    return code



def main(argv=None) -> int:
    return run_cli(_main, argv)


if __name__ == "__main__":
    raise SystemExit(main())
