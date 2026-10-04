#!/usr/bin/env python3
"""fill_evaluate.py — first cut, stdlib-only, PURE decision function for a `fill-contract`. Given a
contract (TOML) and an OBSERVATIONS JSON (as produced by `fill_observe.py`, or any object shaped
like it), decides, per contract file, one of the five states `not-due | due | overdue | filled |
unknown`, using three-valued (Kleene) predicate-tree evaluation — never a silent guess. Reads only
the two named files plus `--now`; no filesystem walk beyond those two reads, no git, no wall clock.

The `--observations` document is FOREIGN input (it need not come from this format's own
`fill_observe.py`), so it is read through the strict JSON-input contract
(`fill_common.load_json_strict`: rejects a duplicate object key, a NaN/Infinity/-Infinity numeric
constant, a string that cannot encode as UTF-8, nesting deeper than `fill_common.MAX_JSON_DEPTH`, or
a parser `RecursionError` on unusually deep input) and then fully shape-validated
(`fill_common.validate_observations`) before a single field of it is evaluated — a malformed
observations document is always one conforming error report (exit 13), never a Python traceback
from `eval_filled_leaf`/`eval_due_leaf` reading a value of the wrong type.

A maturity fact this format decides `due_when` from can be ABSENT from `--observations` (the key is
simply not there) or PRESENT with an explicit value (a date string, or null). Absent is UNKNOWN —
"we have no evidence" — never treated as a confirmed negative; only an explicit null (the observer
looked and the event has not happened) resolves FALSE. `due_when` resolving TRUE with no computable
due date (e.g. `commits-at-least` matched but the observations document carried no `commit_dates`)
is reported `unknown`, never a guessed date and never a crash.

With `--obligations`, additionally emits, per item in state `due`/`overdue`, a REPORT-ONLY summary
shaped loosely after a format_obligation duty record (`obligation_projection`). This projection does
NOT conform to the format_obligation record format and would need conversion before any
acceptance/ledger use — see the field-by-field description of `build_obligation_projection`, below,
and the schema's own `$defs.obligation_projection_item` description. It is NEVER written to any
ledger — stdout only.

Usage:
  python3 fill_evaluate.py evaluate --contract FILE --observations OBS.json --now T [--obligations]
  python3 fill_evaluate.py --selftest

Exit codes (lowest-numbered class wins; 0 iff the report was computed — an item's own `unknown` state
is a valid DECIDED value, not a tool error):
  0   report computed (every item has a decided state, possibly `unknown`)
  10  `--contract` file missing/unreadable or not valid TOML syntax
  11  `--contract` is valid TOML but fails `fill_common.validate_contract` shape/vocabulary checks
  12  `--observations` file missing/unreadable, not valid JSON syntax, or fails the strict
      JSON-input contract (`fill_common.load_json_strict`)
  13  `--observations` is valid, contract-clean JSON but fails `fill_common.validate_observations`
      shape checks (including: not an object, or missing the required top-level `files`/`maturity`
      keys)
  64  usage/argument error
  65  any other unreadable/malformed input (reserved catch-all; not reached by any fixture below)
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

Stdlib only: argparse, copy, json, pathlib, sys, datetime, fill_common (this directory).
"""
from __future__ import annotations

import argparse
import copy
import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

import fill_common as fc
from schema_conformance import (boundary_defect_controls, conformance_errors,
                                rewrite_toml_integers_as_floats, schema_input_variants)

REPORT_SCHEMA = ("schema/fill-contract-0.1.0-draft.schema.json", "report")
fc.register_report_schema(__file__, REPORT_SCHEMA)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"

EVALUATOR_VERSION = "fill-contract/first-cut-0.1.0-draft"

E_CONTRACT_UNREADABLE = 10
E_CONTRACT_INVALID = 11
E_OBS_UNREADABLE = 12
E_OBS_INVALID = 13
E_USAGE = 64
E_INPUT = 65
E_SELFTEST = 99


def _output_schema_errors(report: dict) -> list[str]:
    schema = json.loads((HERE.parent / "schema" /
                         "fill-contract-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/report", "report")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise fc.FillError(E_USAGE, message)


def _parse_now(now_arg: str) -> datetime:
    return fc.parse_now(now_arg)


# ---------------------------------------------------------------------------
# filled_when evaluation (Kleene, no due_at needed)
# ---------------------------------------------------------------------------
def eval_filled_leaf(leaf: dict, obs_file: dict, entry: dict, now: datetime) -> str:
    name = leaf["predicate"]
    if name in fc.UNOBSERVED_FILLED_PREDICATES:
        return fc.UNKNOWN

    if name == "no-marker":
        markers = obs_file.get("markers_present")
        return fc.UNKNOWN if markers is None else (fc.TRUE if markers == [] else fc.FALSE)

    if name == "not-contains":
        contains = obs_file.get("contains_text", {}).get(leaf["text"])
        return fc.UNKNOWN if contains is None else (fc.FALSE if contains else fc.TRUE)

    if name == "rows-beyond-template":
        template_rows = entry.get("template_rows")
        actual = obs_file.get("table_rows_actual")
        if template_rows is None or actual is None:
            return fc.UNKNOWN
        return fc.TRUE if (actual - template_rows) >= leaf["min"] else fc.FALSE

    if name == "min-lines":
        lc = obs_file.get("line_count")
        return fc.UNKNOWN if lc is None else (fc.TRUE if lc >= leaf["n"] else fc.FALSE)

    if name == "stamp":
        stamps = obs_file.get("stamps", {})
        if leaf["key"] not in stamps:
            return fc.UNKNOWN
        return fc.TRUE if stamps[leaf["key"]] else fc.FALSE

    if name == "fresh":
        max_age = fc.parse_duration(leaf["max_age"])
        key = leaf.get("key")
        if key:
            stamps = obs_file.get("stamps", {})
            if key not in stamps:
                return fc.UNKNOWN
            d = stamps[key]
            if d is None:
                return fc.FALSE
        else:
            d = obs_file.get("last_modified_date")
            if d is None:
                return fc.UNKNOWN
        parsed = fc.parse_iso_date(d)
        if parsed is None:
            # d is already validated by fc.validate_file_observation before evaluation ever
            # starts, so this is unreachable in normal operation — defense in depth, not a
            # documented behavior change, exactly like the depth-cap checks elsewhere in this
            # format.
            return fc.UNKNOWN
        age = now.date() - parsed
        return fc.TRUE if age <= max_age else fc.FALSE

    if name == "digest-present":
        v = obs_file.get("digest_present")
        return fc.UNKNOWN if v is None else (fc.TRUE if v else fc.FALSE)

    if name == "declared-rules":
        v = obs_file.get("declared_rules_count")
        return fc.UNKNOWN if v is None else (fc.TRUE if v >= leaf["min"] else fc.FALSE)

    return fc.UNKNOWN  # unreachable: fc.validate_contract already restricted the vocabulary


def eval_filled_tree(expr: dict, obs_file: dict, entry: dict, now: datetime) -> str:
    if "predicate" in expr:
        return eval_filled_leaf(expr, obs_file, entry, now)
    if "any" in expr:
        return fc.eval_any([eval_filled_tree(e, obs_file, entry, now) for e in expr["any"]])
    if "all" in expr:
        return fc.eval_all([eval_filled_tree(e, obs_file, entry, now) for e in expr["all"]])
    if "not" in expr:
        return fc.eval_not(eval_filled_tree(expr["not"], obs_file, entry, now))
    return fc.UNKNOWN  # unreachable: fc.validate_contract already rejects malformed nodes


# ---------------------------------------------------------------------------
# due_when evaluation (Kleene + due_at propagation). Recursion here (eval_due_tree/eval_filled_tree)
# is safe without an explicit depth bound: both are only ever called on a `filled_when`/`due_when`
# tree that already passed fc.validate_contract, which caps nesting at fc.MAX_EXPR_DEPTH (64) —
# unlike validate_expr/walk_leaves/expr_has_predicate, which must accept an AS-YET-unvalidated tree
# and so bound themselves directly.
# ---------------------------------------------------------------------------
_DUE_DATE_FIELD = {
    "first-src-commit": "first_src_commit_date",
    "first-test": "first_test_date",
    "first-spec": "first_spec_date",
    "first-dependency": "first_dependency_date",
    "first-release": "first_release_date",
}


def _due_date_fact(maturity: dict, field: str) -> tuple:
    """One maturity date fact: ABSENT (the key is not in `maturity` at all — the observer never
    reported on this event, or a hand-written observations document simply omitted the optional
    field) is UNKNOWN ("we have no evidence"), never FALSE; PRESENT-but-null (the observer looked
    and the event has not happened) is FALSE — a real, confirmed negative. Conflating the two would
    let missing evidence read as a confirmed negative, silently turning `unknown` into `not-due`."""
    if field not in maturity:
        return fc.UNKNOWN, None
    d = maturity[field]
    return (fc.TRUE, d) if d else (fc.FALSE, None)


def eval_due_leaf(leaf: dict, maturity: dict) -> tuple:
    """Returns (kleene_value, due_at_iso_date_or_None). due_at is a real ISO date whenever the
    value is TRUE and a date could be determined; TRUE with due_at=None means "resolved true but no
    date could be computed" (e.g. commits-at-least matched but the observations document did not
    include commit_dates) — callers (evaluate_item) must treat that as unknown, not guess a date."""
    name = leaf["predicate"]
    if name in fc.UNOBSERVED_DUE_PREDICATES:
        return fc.UNKNOWN, None

    if name == "instantiated":
        return _due_date_fact(maturity, "instantiated_date")

    if name in _DUE_DATE_FIELD:
        return _due_date_fact(maturity, _DUE_DATE_FIELD[name])

    if name == "commits-at-least":
        if "commits_count" not in maturity:
            return fc.UNKNOWN, None
        count = maturity["commits_count"]
        n = leaf["n"]  # validated >= fc.PREDICATE_INT_MIN (1) by fc.validate_contract
        if count < n:
            return fc.FALSE, None
        dates = maturity.get("commit_dates") or []
        idx = n - 1
        due_at = dates[idx] if 0 <= idx < len(dates) else None
        return fc.TRUE, due_at

    if name == "days-since-setup":
        if "days_since_setup" not in maturity or "instantiated_date" not in maturity:
            return fc.UNKNOWN, None
        dss = maturity["days_since_setup"]
        instantiated = maturity["instantiated_date"]
        if dss is None or instantiated is None:
            return fc.UNKNOWN, None
        n = leaf["n"]
        if dss < n:
            return fc.FALSE, None
        parsed_start = fc.parse_iso_date(instantiated)
        if parsed_start is None:
            # instantiated_date is already validated by fc.validate_maturity before evaluation
            # starts — unreachable in normal operation, defense in depth.
            return fc.UNKNOWN, None
        try:
            due_at = (parsed_start + timedelta(days=n)).isoformat()
        except OverflowError:
            # Even a bounded duration can overflow near date.max; report unknown.
            return fc.UNKNOWN, None
        return fc.TRUE, due_at

    return fc.UNKNOWN, None  # unreachable: fc.validate_contract already restricted the vocabulary


def eval_due_tree(expr: dict, maturity: dict) -> tuple:
    """Returns (kleene_value, due_at_iso_date_or_None). DATE UNCERTAINTY PROPAGATES THROUGH
    COMPOSITION: a child can itself be individually TRUE with an unknown due date (see
    eval_due_leaf's own docstring — e.g. commits-at-least matched but commit_dates was not
    supplied). Silently dropping such a child's None before taking min/max — the earlier bug — lets
    a KNOWN sibling date stand in for a date that was never actually determined, guessing a
    deadline (and, downstream, a wrong `due`/`overdue` state) instead of reporting `unknown`.

    - `all`'s combined due_at is the LATEST date among its (all TRUE, since eval_all only reaches
      TRUE here if every child is TRUE) children — but if ANY of them has due_at=None, the true
      maximum cannot be determined (a still-undated child might turn out to be the latest), so the
      combined due_at is None too.
    - `any`'s combined due_at is documented as the EARLIEST date among its TRUE children — the
      CONSERVATIVE reading applies here as well: if any TRUE child's own due_at is None, that
      child's real date might in fact be earlier than every known one, so the combined due_at is
      None (`unknown`) rather than the minimum of only the children that happen to carry a date."""
    if "predicate" in expr:
        return eval_due_leaf(expr, maturity)
    if "any" in expr:
        results = [eval_due_tree(e, maturity) for e in expr["any"]]
        val = fc.eval_any([v for v, _ in results])
        if val != fc.TRUE:
            return val, None
        true_dates = [fc.parse_iso_date(d) for v, d in results if v == fc.TRUE]
        if any(d is None for d in true_dates):
            return val, None
        return val, min(true_dates).isoformat()
    if "all" in expr:
        results = [eval_due_tree(e, maturity) for e in expr["all"]]
        val = fc.eval_all([v for v, _ in results])
        if val != fc.TRUE:
            return val, None
        true_dates = [fc.parse_iso_date(d) for _, d in results]  # eval_all == TRUE means every child here IS TRUE
        if any(d is None for d in true_dates):
            return val, None
        return val, max(true_dates).isoformat()
    return fc.UNKNOWN, None  # 'not' is rejected in due_when by fc.validate_contract


# ---------------------------------------------------------------------------
# top-level evaluate
# ---------------------------------------------------------------------------
def evaluate_item(entry: dict, obs_file, maturity: dict, now: datetime) -> dict:
    if obs_file is None:
        return {"file": entry["file"], "state": "unknown",
                "reasons": ["no observation recorded for this file"],
                "due_at": None, "required_by": None, "importance": entry["importance"]}
    if not obs_file.get("exists", False):
        return {"file": entry["file"], "state": "unknown",
                "reasons": ["file does not exist / unreadable at observation time"],
                "due_at": None, "required_by": None, "importance": entry["importance"]}

    filled_val = eval_filled_tree(entry["filled_when"], obs_file, entry, now)
    if filled_val == fc.UNKNOWN:
        return {"file": entry["file"], "state": "unknown",
                "reasons": ["filled_when could not be resolved (an unresolved predicate dominates)"],
                "due_at": None, "required_by": None, "importance": entry["importance"]}
    if filled_val == fc.TRUE:
        return {"file": entry["file"], "state": "filled", "reasons": ["filled_when is satisfied"],
                "due_at": None, "required_by": None, "importance": entry["importance"]}

    due_val, due_at = eval_due_tree(entry["due_when"], maturity)
    if due_val == fc.UNKNOWN:
        return {"file": entry["file"], "state": "unknown",
                "reasons": ["due_when could not be resolved"],
                "due_at": None, "required_by": None, "importance": entry["importance"]}
    if due_val == fc.FALSE:
        return {"file": entry["file"], "state": "not-due", "reasons": ["due_when not yet satisfied"],
                "due_at": None, "required_by": None, "importance": entry["importance"]}
    if due_at is None:
        # due_when resolved TRUE but no due date could be determined (see eval_due_leaf's
        # docstring) — we cannot compute required_by, so this is unknown, never a guess and never
        # a crash from date.fromisoformat(None).
        return {"file": entry["file"], "state": "unknown",
                "reasons": ["due_when is satisfied but its due date could not be determined"],
                "due_at": None, "required_by": None, "importance": entry["importance"]}

    urgency = fc.parse_duration(entry["urgency_after_due"])
    parsed_due_at = fc.parse_iso_date(due_at)
    if parsed_due_at is None:
        # due_at is already validated (it comes from fc.validate_maturity-checked fields or from
        # eval_due_leaf's own already-canonical computed dates) — unreachable in normal operation,
        # defense in depth rather than a documented behavior change.
        return {"file": entry["file"], "state": "unknown",
                "reasons": ["due_when's date could not be parsed"],
                "due_at": due_at, "required_by": None, "importance": entry["importance"]}
    if now.date() < parsed_due_at:
        # The state machine must be MONOTONE in --now: `due_at` is a date in `maturity`'s own
        # chronology (a commit date, or a computed days-since/commits-at-least date), which can be
        # LATER than the `--now` this evaluation is being asked about (an observation taken at one
        # point in time, evaluated against an earlier --now — e.g. replaying an older snapshot, or a
        # due_when composed from a future-dated fact). Only comparing `now` against `required_by`
        # (overdue vs due) and never against `due_at` itself let such a case report `due` with a
        # `required_by` that has not arrived either — not-due is the correct, monotone answer: the
        # event that makes this due has not happened yet AS OF --now, even though the observations
        # document already knows it eventually will.
        return {"file": entry["file"], "state": "not-due",
                "reasons": [f"due_when is satisfied at {due_at}, which is after --now "
                            f"(not yet due as of --now)"],
                "due_at": due_at, "required_by": None, "importance": entry["importance"]}
    try:
        required_by_date = parsed_due_at + urgency
    except OverflowError:
        # A bounded urgency can still overflow near date.max; report unknown.
        return {"file": entry["file"], "state": "unknown",
                "reasons": ["due_when's date plus urgency_after_due is not representable"],
                "due_at": due_at, "required_by": None, "importance": entry["importance"]}
    required_by = required_by_date.isoformat()
    # Compare the DATE OBJECTS already in hand (now.date(), required_by_date) — never re-parse the
    # required_by STRING we just built from them; one canonical grammar, applied consistently, not
    # re-derived at the last step.
    state = "overdue" if now.date() > required_by_date else "due"
    return {"file": entry["file"], "state": state,
            "reasons": [f"due_when satisfied at {due_at}; required_by {required_by}"],
            "due_at": due_at, "required_by": required_by, "importance": entry["importance"]}


def build_obligation_projection(contract_entries: dict, items: list) -> list:
    """Builds the REPORT-ONLY summary `--obligations` prints, per item in state due/overdue. This is
    NOT a format_obligation duty record and does not conform to that format: `trigger`/
    `discharge_predicate` carry this format's own predicate-expression trees (a different shape than
    an obligation record's trigger/discharge fields), and `id`/`natural_key`/`authority`/
    `created_by`/`created_at` are explicitly null (a report alone cannot state any of them — no duty
    id is minted here, no authority binding happens here). A caller that wants a real
    format_obligation record must convert this summary, not treat it as one."""
    out = []
    for item in items:
        if item["state"] not in ("due", "overdue"):
            continue
        entry = contract_entries[item["file"]]
        out.append({
            "action": "update",
            "target": item["file"],
            "trigger": entry["due_when"],
            "required_by": item["required_by"],
            "discharge_predicate": entry["filled_when"],
            "duty_kind": "one-time",
            "retrigger": "every-event",
            "status": item["state"],
            "id": None, "natural_key": None, "authority": None,
            "created_by": None, "created_at": None,
        })
    return out


def run_evaluate(contract_path: pathlib.Path, observations_path: pathlib.Path, now_arg: str,
                  obligations: bool = False) -> dict:
    try:
        contract = fc.load_toml(contract_path)
    except fc.FillError as exc:
        raise fc.FillError(E_CONTRACT_UNREADABLE, str(exc))
    errors = fc.validate_contract(contract)
    if errors:
        raise fc.FillError(E_CONTRACT_INVALID,
                            f"--contract fails shape/vocabulary validation ({len(errors)} error(s)); "
                            f"run fill_validate.py for the full list")

    try:
        obs_text = observations_path.read_text(encoding="utf-8")
    except OSError as exc:
        # `str(exc)` on an `OSError` embeds its own `filename` argument IN FULL (the same value
        # `observations_path` already names) — the exception's CLASS name plus one bounded path
        # preview, never `str(exc)` itself, keeps this message's length independent of how long a
        # `--observations` argument happens to be.
        raise fc.FillError(E_OBS_UNREADABLE, f"cannot read --observations: "
                            f"{fc.bound_path_preview(observations_path)} "
                            f"({type(exc).__name__})")
    except UnicodeError as exc:
        raise fc.FillError(E_OBS_UNREADABLE, f"cannot read --observations: "
                            f"{fc.bound_path_preview(observations_path)} ({exc})")
    observations, reason = fc.load_json_strict(obs_text)
    if reason is not None:
        raise fc.FillError(E_OBS_UNREADABLE, f"--observations fails the JSON-input contract: {reason}")
    if not isinstance(observations, dict) or "files" not in observations or "maturity" not in observations:
        raise fc.FillError(E_OBS_INVALID,
                            "--observations must be a JSON object with 'files' and 'maturity' keys")
    shape_errors = fc.validate_observations(observations)
    if shape_errors:
        # `fc.join_error_list`, not a bare `"; ".join(...)`: an adversarial `--observations`
        # document can carry hundreds of malformed `files[]` entries, each already-bounded error
        # string individually, so a plain join would still make the AGGREGATE message's length the
        # product of entry count and per-entry length — bounded at the source, listing at most a
        # fixed number of entries and naming how many more were left out, rather than depending on
        # `bound_reason`'s own final truncation to cut the combined string off mid-entry.
        raise fc.FillError(E_OBS_INVALID,
                            f"--observations fails shape validation ({len(shape_errors)} error(s)): "
                            + fc.join_error_list(shape_errors))

    now = _parse_now(now_arg)
    obs_by_file = {f.get("file"): f for f in observations.get("files", []) if isinstance(f, dict)}
    maturity = observations.get("maturity") or {}

    items = [evaluate_item(entry, obs_by_file.get(entry["file"]), maturity, now)
             for entry in contract["contracts"]]

    rank = fc.IMPORTANCE_RANK
    items.sort(key=lambda it: (rank[it["importance"]], fc.parse_iso_date(it["required_by"]) or date.max, it["file"]))

    report = {
        "contract": str(contract_path),
        "observations": str(observations_path),
        "now": now_arg,
        "evaluator_version": EVALUATOR_VERSION,
        "items": items,
    }
    if obligations:
        entries_by_file = {e["file"]: e for e in contract["contracts"]}
        report["obligation_projection"] = build_obligation_projection(entries_by_file, items)
    return report


def case_matches(case: dict, report: dict) -> bool:
    got_states = {it["file"]: it["state"] for it in report["items"]}
    return got_states == case["expected_states"]


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    evaluate_p = subparsers.add_parser("evaluate", allow_abbrev=False)
    evaluate_p.add_argument("--contract", required=True)
    evaluate_p.add_argument("--observations", required=True)
    evaluate_p.add_argument("--now", required=True)
    evaluate_p.add_argument("--obligations", action="store_true")
    return parser


def _run_case_file(case: dict) -> dict:
    contract_path = FIXTURES / "validate" / case["contract"]
    obs_path = FIXTURES / "validate" / case["observations"]
    return run_evaluate(contract_path, obs_path, case["now"], obligations=case.get("obligations", False))


def _run_case_via_cli(case: dict) -> tuple[int, dict]:
    argv = ["evaluate", "--contract", str(FIXTURES / "validate" / case["contract"]),
            "--observations", str(FIXTURES / "validate" / case["observations"]),
            "--now", case["now"]]
    if case.get("obligations"):
        argv.append("--obligations")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    return code, json.loads(captured.getvalue())


def _run_evaluate_via_cli(contract_path: pathlib.Path, observations_path: pathlib.Path,
                          now: str, obligations: bool = False) -> dict:
    """Preserve run_evaluate's self-test calling convention while crossing the real CLI boundary."""
    argv = ["evaluate", "--contract", str(contract_path),
            "--observations", str(observations_path), "--now", now]
    if obligations:
        argv.append("--obligations")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    report = json.loads(captured.getvalue())
    if code != 0:
        reasons = report.get("errors") or []
        reason = reasons[0].get("reason", "CLI evaluation failed") if reasons else "CLI evaluation failed"
        raise fc.FillError(code, reason)
    return report


def _integers_to_integral_floats(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, dict):
        return {key: _integers_to_integral_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_integers_to_integral_floats(item) for item in value]
    return value


def _integer_spelling_differential(cases: list[dict]) -> tuple[bool, int, list]:
    failures, rewritten = [], 0
    with tempfile.TemporaryDirectory(prefix="fill-evaluate-integral-floats-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(cases):
            contract_source = FIXTURES / "validate" / case["contract"]
            contract_text, contract_count = rewrite_toml_integers_as_floats(
                contract_source.read_text(encoding="utf-8"))
            contract_path = tmp_path / f"{case_index}.toml"
            contract_path.write_text(contract_text, encoding="utf-8")
            observations = json.loads(
                (FIXTURES / "validate" / case["observations"]).read_text(encoding="utf-8"))
            changed_observations = _integers_to_integral_floats(observations)
            observation_count = sum(1 for old, new in zip(
                json.dumps(observations, sort_keys=True),
                json.dumps(changed_observations, sort_keys=True)) if old != new)
            observations_path = tmp_path / f"{case_index}.json"
            observations_path.write_text(
                json.dumps(changed_observations, allow_nan=False), encoding="utf-8")
            rewritten += contract_count + observation_count
            original_code, original_report = _run_case_via_cli(case)
            argv = ["evaluate", "--contract", str(contract_path),
                    "--observations", str(observations_path), "--now", case["now"]]
            if case.get("obligations"):
                argv.append("--obligations")
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                changed_code = main(argv)
            changed_report = json.loads(captured.getvalue())
            if (changed_code != original_code
                    or changed_report.get("status") != original_report.get("status")):
                failures.append((case["name"], original_code, changed_code,
                                 original_report.get("status"), changed_report.get("status")))
    return not failures and rewritten > 0, rewritten, failures


def _absent_evidence_boundary_control() -> tuple[bool, str]:
    """Plant the reported absent-evidence-only extra key and run the real self-test."""
    probe = r'''
import contextlib, importlib.util, io, json, os, pathlib, sys
script = sys.argv[1]
sys.path.insert(0, os.path.dirname(script))
spec = importlib.util.spec_from_file_location("absent_evidence_boundary_target", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
original = module.run_evaluate
def wrapped(contract_path, *args, **kwargs):
    report = original(contract_path, *args, **kwargs)
    if pathlib.Path(contract_path).name == "_absent_evidence_contract.toml":
        report["__selftest_absent_evidence_undeclared_key__"] = True
    return report
module.run_evaluate = wrapped
module.boundary_defect_controls = lambda *args, **kwargs: {
    "usage": (True, "nested control suppressed"),
    "exception": (True, "nested control suppressed"),
}
module._absent_evidence_boundary_control = lambda: (True, "nested control suppressed")
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    status = module.run_selftest()
print(json.dumps({"status": status, "tail": captured.getvalue()[-600:]}))
'''
    proc = subprocess.run(
        [sys.executable, "-B", "-c", probe, str(pathlib.Path(__file__).resolve())],
        capture_output=True, text=True, timeout=240,
    )
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = {}
    status = result.get("status")
    ok = proc.returncode == 0 and isinstance(status, int) and status != 0
    return ok, (f"probe_exit={proc.returncode} selftest_exit={status!r} "
                f"tail={result.get('tail', '')!r} stderr={proc.stderr[-160:]!r}")




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
    codes AND, when stdout is non-empty, it decodes as UTF-8 and parses under `fc.load_json_strict`
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
    obj, defect = fc.load_json_strict(text)
    if defect is not None:
        return False, defect
    schema_errors = _output_schema_errors(obj)
    if schema_errors:
        return False, f"stdout violates $defs.report: {schema_errors}"
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


def _observations_input_schema_differential(cases: list[dict]) -> tuple[bool, int, int, list]:
    """Compare observations schema verdicts with the real evaluator executable.

    Canonical calendar-date spelling is the sole documented semantic exception reached by these
    generated variants (spec §§13.1, 14.1; schema semantic rule
    ``canonical-calendar-dates-and-finite-values``).
    """
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, 0, 0, []
    schema = json.loads((HERE.parent / "schema" /
                         "fill-contract-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    failures, generated, semantic_rejections = [], 0, 0
    with tempfile.TemporaryDirectory(prefix="fill-observations-schema-differential-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(cases):
            source = FIXTURES / "validate" / case["observations"]
            document, defect = fc.load_json_strict(source.read_text(encoding="utf-8"))
            if defect is not None:
                failures.append((case["name"], "positive-fixture-json", defect))
                continue
            baseline_errors = conformance_errors(
                document, schema, "#/$defs/observations", "observations")
            if baseline_errors:
                failures.append((case["name"], "positive-fixture-schema", baseline_errors[:3]))
                continue
            variants = schema_input_variants(document, schema, "#/$defs/observations")
            reason_properties = schema["$defs"]["file_observation"]["properties"]
            reason_witnesses = (("space", " "), ("tab", "\t"), ("nbsp", "\u00a0"),
                                ("python-strip-only-u001c", "\u001c"))
            for file_index, observation in enumerate(document["files"]):
                reason_field = ("scan_skipped_reason" if observation["exists"]
                                else "unreadable_reason")
                if reason_properties[reason_field].get("pattern") != r"\S":
                    failures.append((case["name"], reason_field,
                                     "schema pattern is not exactly \\S"))
                    continue
                for witness_name, witness in reason_witnesses:
                    variant = copy.deepcopy(document)
                    variant["files"][file_index][reason_field] = witness
                    variants.append((f"pattern-witness:$.files[{file_index}].{reason_field}:"
                                     f"{witness_name}", variant))
            for variant_index, (label, variant) in enumerate(variants):
                generated += 1
                schema_accepts = not conformance_errors(
                    variant, schema, "#/$defs/observations", "observations")
                destination = tmp_path / f"{case_index}-{variant_index}.json"
                destination.write_text(json.dumps(variant, ensure_ascii=False, separators=(",", ":"),
                                                   allow_nan=False), encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, "-B", str(HERE / "fill_evaluate.py"), "evaluate",
                     "--contract", str(FIXTURES / "validate" / case["contract"]),
                     "--observations", str(destination), "--now", case["now"]],
                    capture_output=True, timeout=10)
                cli_accepts = proc.returncode == 0
                semantic = (schema_accepts and not cli_accepts
                            and proc.returncode == E_OBS_INVALID
                            and label.startswith("allowed-type:$.maturity.")
                            and label.endswith("_date:string"))
                if semantic:
                    semantic_rejections += 1
                elif schema_accepts != cli_accepts:
                    failures.append((case["name"], label, schema_accepts, proc.returncode))
                elif not cli_accepts and proc.returncode != E_OBS_INVALID:
                    failures.append((case["name"], label, "non-validation-exit", proc.returncode))
    return not failures and generated > 0, generated, semantic_rejections, failures


def _old_unreadable_null_rejection_control() -> tuple[bool, int, str]:
    """Restore the old null rejection and require the real evaluator self-test to turn red."""
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, E_SELFTEST, "nested boundary control suppressed"
    if os.environ.get("FILL_UNREADABLE_NULL_CONTROL_ACTIVE") == "1":
        return True, E_SELFTEST, "nested control suppressed"
    old = '''if "unreadable_reason" in obs and not (
            obs["unreadable_reason"] is None or is_nonblank_reason(obs["unreadable_reason"])
        ):'''
    new = '''if "unreadable_reason" in obs and not is_nonblank_reason(obs["unreadable_reason"]):'''
    with tempfile.TemporaryDirectory(prefix="fill-old-unreadable-null-control-") as tmp:
        tmp_root = pathlib.Path(tmp)
        copied = tmp_root / "format_fill-contract"
        shutil.copytree(HERE.parent, copied)
        # A read-only checkout copies as read-only; the control must patch its own copy.
        for path in (copied, *copied.rglob("*")):
            path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR
                       | (stat.S_IXUSR if path.is_dir() else 0))
        common = copied / "tools/fill_common.py"
        source = common.read_text(encoding="utf-8")
        if source.count(old) != 1:
            return False, 0, "could not plant old unreadable_reason null rejection"
        common.write_text(source.replace(old, new, 1), encoding="utf-8")
        env = dict(os.environ)
        env["FILL_UNREADABLE_NULL_CONTROL_ACTIVE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-B", str(copied / "tools/fill_evaluate.py"), "--selftest"],
            cwd=tmp_root, capture_output=True, text=True, env=env, timeout=300)
    mismatch_line = next((line for line in proc.stdout.splitlines()
                          if line.startswith("selftest input-schema-cli-differential-observations:")),
                         "")
    caught_by_differential = "MISMATCH" in mismatch_line
    detail = f"differential={mismatch_line!r}; tail={(proc.stdout + proc.stderr)[-350:]!r}"
    return (proc.returncode == E_SELFTEST and caught_by_differential,
            proc.returncode, detail)

def run_selftest() -> int:
    try:
        cases_data = json.loads((FIXTURES / "validate" / "evaluate_cases.json").read_text(encoding="utf-8"))
        cases = cases_data.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("evaluate_cases.json must contain a nonempty 'cases' array")

        results = []
        for case in cases:
            code, report = _run_case_via_cli(case)
            ok = case_matches(case, report)
            ok = ok and code == 0
            results.append(ok)
            got_states = {it["file"]: it["state"] for it in report["items"]}
            print(f"selftest {case['name']}: expected={case['expected_states']} got={got_states} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
            if not ok:
                print(fc.emit_report(report, indent=2, sort_keys=True, allow_nan=False))
            if case.get("expect_obligation_files") is not None:
                got_ob_files = sorted(o["target"] for o in report.get("obligation_projection", []))
                ob_ok = got_ob_files == sorted(case["expect_obligation_files"])
                results.append(ob_ok)
                print(f"selftest {case['name']} obligation-projection: expected="
                      f"{sorted(case['expect_obligation_files'])} got={got_ob_files} "
                      f"[{'ok' if ob_ok else 'MISMATCH'}]")

        integer_diff_ok, integer_rewrites, integer_diff_failures = (
            _integer_spelling_differential(cases))
        results.append(integer_diff_ok)
        print(f"selftest schema-integer-integral-float-observations-differential: "
              f"{integer_rewrites} spelling change(s), "
              f"{len(integer_diff_failures)} failure(s) {integer_diff_failures[:3]!r} "
              f"[{'ok' if integer_diff_ok else 'MISMATCH'}]")

        schema_diff_ok, schema_variants, schema_semantic, schema_diff_failures = (
            _observations_input_schema_differential(cases))
        old_null_control_ok, old_null_control_exit, old_null_control_detail = (
            _old_unreadable_null_rejection_control())
        results.extend((schema_diff_ok, old_null_control_ok))
        print(f"selftest input-schema-cli-differential-observations: "
              f"{schema_variants} generated variant(s), "
              f"{schema_semantic} documented semantic rejection(s), "
              f"{len(schema_diff_failures)} failure(s) {schema_diff_failures[:3]!r} "
              f"[{'ok' if schema_diff_ok else 'MISMATCH'}]")
        print(f"selftest old-unreadable-reason-null-rejection-control: "
              f"real selftest exit={old_null_control_exit} detail={old_null_control_detail!r} "
              f"[{'ok' if old_null_control_ok else 'MISMATCH'}]")

        usage_output = io.StringIO()
        with contextlib.redirect_stdout(usage_output):
            usage_code = main(["evaluate"])
        usage_report = json.loads(usage_output.getvalue())
        usage_code = usage_code if usage_report.get("exit_code") == usage_code else -1
        results.append(usage_code == E_USAGE)
        print(f"selftest usage-error: expected exit=64 got exit={usage_code} "
              f"[{'ok' if usage_code == E_USAGE else 'MISMATCH'}]")

        # input-error classes. Every generated input below is written into a fresh
        # tempfile.TemporaryDirectory — NEVER into FIXTURES/validate — so this self-test writes
        # nothing into the tree it was loaded from: safe on a read-only checkout, and safe to run
        # concurrently with another instance of itself (no fixed filenames to collide on).
        import tempfile
        with tempfile.TemporaryDirectory(prefix="fill_evaluate_selftest_") as tmp:
            scratch = pathlib.Path(tmp)

            bad_contract = scratch / "_nope_contract.toml"  # deliberately never written
            try:
                _run_evaluate_via_cli(
                    bad_contract, FIXTURES / "validate" / cases[0]["observations"], cases[0]["now"])
                results.append(False)
                print("selftest missing-contract: expected exit=10, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_CONTRACT_UNREADABLE
                results.append(ok)
                print(f"selftest missing-contract: expected exit=10 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            invalid_contract = scratch / "_invalid_shape.toml"
            invalid_contract.write_text('fill_version = "0.1.0"\ntemplate = "t"\n', encoding="utf-8")
            try:
                _run_evaluate_via_cli(
                    invalid_contract, FIXTURES / "validate" / cases[0]["observations"], cases[0]["now"])
                results.append(False)
                print("selftest invalid-contract-shape: expected exit=11, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_CONTRACT_INVALID
                results.append(ok)
                print(f"selftest invalid-contract-shape: expected exit=11 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            missing_obs = scratch / "_nope_obs.json"  # deliberately never written
            try:
                _run_evaluate_via_cli(
                    FIXTURES / "validate" / cases[0]["contract"], missing_obs, cases[0]["now"])
                results.append(False)
                print("selftest missing-observations: expected exit=12, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_OBS_UNREADABLE
                results.append(ok)
                print(f"selftest missing-observations: expected exit=12 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            malshaped_obs = scratch / "_malshaped_obs.json"
            malshaped_obs.write_text(json.dumps({"nope": True}), encoding="utf-8")
            try:
                _run_evaluate_via_cli(
                    FIXTURES / "validate" / cases[0]["contract"], malshaped_obs, cases[0]["now"])
                results.append(False)
                print("selftest malshaped-observations: expected exit=13, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_OBS_INVALID
                results.append(ok)
                print(f"selftest malshaped-observations: expected exit=13 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # two observations.files[] entries naming the SAME file are rejected at shape
            # validation (exit 13), not silently resolved by "whichever one is last in the array
            # wins" (run_evaluate's own obs_by_file lookup is a plain dict keyed by `file`) — proven
            # in BOTH orders, since "last wins" means the two orders would otherwise decide
            # DIFFERENT states for the identical pair of observations.
            dup_file_obs = scratch / "_dup_file_obs.json"
            dup_file_obs.write_text(json.dumps({
                "files": [
                    {"file": "X.md", "exists": True, "markers_present": ["todo"]},
                    {"file": "X.md", "exists": True, "markers_present": []},
                ],
                "maturity": {},
            }), encoding="utf-8")
            try:
                _run_evaluate_via_cli(
                    FIXTURES / "validate" / cases[0]["contract"], dup_file_obs, cases[0]["now"])
                results.append(False)
                print("selftest duplicate-observation-file: expected exit=13, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_OBS_INVALID and "duplicate file entry" in str(exc)
                results.append(ok)
                print(f"selftest duplicate-observation-file: expected exit=13 got exit={exc.code} "
                      f"reason-names-duplicate={('duplicate file entry' in str(exc))} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            dup_order_ok = True
            for markers_order in (["x"], []):
                files = [
                    {"file": "X.md", "exists": True, "markers_present": markers_order},
                    {"file": "X.md", "exists": True, "markers_present": []},
                ]
                errs = fc.validate_observations({"files": files, "maturity": {}})
                dup_order_ok &= any("duplicate file entry" in e for e in errs)
            results.append(dup_order_ok)
            print(f"selftest duplicate-observation-file-rejected-regardless-of-order: "
                  f"[{'ok' if dup_order_ok else 'MISMATCH'}]")

            # Strict JSON-input contract: a duplicate top-level key, a NaN constant, and nesting
            # deeper than fc.MAX_JSON_DEPTH are all rejected at load time (exit 12), never left for
            # the shape checker or the evaluator to trip over.
            dup_key_obs = scratch / "_dup_key_obs.json"
            dup_key_obs.write_text('{"files": [], "maturity": {}, "maturity": {}}', encoding="utf-8")
            try:
                _run_evaluate_via_cli(
                    FIXTURES / "validate" / cases[0]["contract"], dup_key_obs, cases[0]["now"])
                results.append(False)
                print("selftest duplicate-json-key: expected exit=12, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_OBS_UNREADABLE
                results.append(ok)
                print(f"selftest duplicate-json-key: expected exit=12 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            nan_obs = scratch / "_nan_obs.json"
            nan_obs.write_text('{"files": [], "maturity": {"commits_count": NaN}}', encoding="utf-8")
            try:
                _run_evaluate_via_cli(
                    FIXTURES / "validate" / cases[0]["contract"], nan_obs, cases[0]["now"])
                results.append(False)
                print("selftest nan-constant: expected exit=12, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_OBS_UNREADABLE
                results.append(ok)
                print(f"selftest nan-constant: expected exit=12 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            deep_obs = scratch / "_deep_nesting_obs.json"
            deep_obs.write_text('{"files": ' + ("[" * 100) + ("]" * 100) + ', "maturity": {}}',
                                 encoding="utf-8")
            try:
                _run_evaluate_via_cli(
                    FIXTURES / "validate" / cases[0]["contract"], deep_obs, cases[0]["now"])
                results.append(False)
                print("selftest deep-json-nesting: expected exit=12, got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_OBS_UNREADABLE
                results.append(ok)
                print(f"selftest deep-json-nesting: expected exit=12 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # Type fuzz across observations.files[]/maturity fields: every wrongly-typed value
            # below would otherwise reach Python arithmetic/date-parsing inside
            # eval_filled_leaf/eval_due_leaf and crash (a list has no .get, "five" - 3 raises
            # TypeError, "not-a-date" raises ValueError in date.fromisoformat, "three" < 5 raises
            # TypeError). validate_observations catches every one before evaluation starts; the
            # report is one FillError, never a traceback.
            fuzz_obs = scratch / "_type_fuzz_obs.json"
            fuzz_obs.write_text(json.dumps({
                "files": [
                    {"file": "X.md", "exists": True, "contains_text": ["not", "a", "dict"]},
                    {"file": "Y.md", "exists": True, "table_rows_actual": "five"},
                    {"file": "Z.md", "exists": True, "stamps": {"k": "not-a-date"}},
                ],
                "maturity": {"commits_count": "three", "days_since_setup": "nine"},
            }), encoding="utf-8")
            try:
                _run_evaluate_via_cli(
                    FIXTURES / "validate" / cases[0]["contract"], fuzz_obs, cases[0]["now"])
                results.append(False)
                print("selftest observations-type-fuzz: expected exit=13, got none [MISMATCH]")
            except fc.FillError as exc:
                reason = str(exc)
                ok = exc.code == E_OBS_INVALID and all(
                    s in reason for s in ("contains_text", "table_rows_actual", "stamps",
                                           "commits_count", "days_since_setup"))
                results.append(ok)
                print(f"selftest observations-type-fuzz: expected exit=13 got exit={exc.code}, "
                      f"every fuzzed field named in the report={ok} [{'ok' if ok else 'MISMATCH'}]")

            # Absent evidence is not negative evidence: a due predicate whose maturity fact is
            # simply ABSENT (not merely null) from the observations document must resolve unknown,
            # never false/not-due — proven against a hand-written, minimal observations document
            # (not fill_observe.py's own output, which always sets every maturity key). `files`
            # MUST name the tracked file with `exists: true` and a `filled_when` that resolves
            # FALSE (a marker present) — an empty `files: []` makes evaluate_item return early at
            # its "no observation recorded" check, never reaching due_when evaluation at all, so a
            # case built that way would not actually exercise this rule.
            absent_evidence_obs = scratch / "_absent_evidence_obs.json"
            absent_evidence_obs.write_text(json.dumps({
                "files": [{"file": "X.md", "exists": True, "markers_present": ["todo"]}],
                "maturity": {},
            }), encoding="utf-8")
            absent_contract = scratch / "_absent_evidence_contract.toml"
            absent_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "X.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { predicate = "no-marker" }\n'
                'due_when = { predicate = "first-test" }\n',
                encoding="utf-8")
            report = _run_evaluate_via_cli(absent_contract, absent_evidence_obs, "2026-09-30")
            item = report["items"][0]
            ok = (item["state"] == "unknown"
                  and item["reasons"] == ["due_when could not be resolved"])
            results.append(ok)
            print(f"selftest absent-maturity-evidence-is-unknown-not-false: got "
                  f"state={item['state']!r} reasons={item['reasons']!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # commits-at-least resolved TRUE but with no commit_dates to compute a due date from:
            # evaluate_item must report unknown (it cannot state a due date it does not have),
            # never crash calling date.fromisoformat(None). Same `files`-population fix as above.
            no_dates_obs = scratch / "_no_commit_dates_obs.json"
            no_dates_obs.write_text(json.dumps({
                "files": [{"file": "X.md", "exists": True, "markers_present": ["todo"]}],
                "maturity": {"commits_count": 1},
            }), encoding="utf-8")
            no_dates_contract = scratch / "_no_commit_dates_contract.toml"
            no_dates_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "X.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { predicate = "no-marker" }\n'
                'due_when = { predicate = "commits-at-least", n = 1 }\n',
                encoding="utf-8")
            report = _run_evaluate_via_cli(no_dates_contract, no_dates_obs, "2026-09-30")
            item = report["items"][0]
            ok = (item["state"] == "unknown"
                  and item["reasons"] == ["due_when is satisfied but its due date could not be "
                                           "determined"])
            results.append(ok)
            print(f"selftest true-due-without-a-date-is-unknown-not-a-crash: got "
                  f"state={item['state']!r} reasons={item['reasons']!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # Date uncertainty must propagate through `all`/`any` composition: a `due_when`
            # combinator with one child individually TRUE-but-undated must report unknown for the
            # WHOLE node, never silently drop the undated child and guess a deadline from a known
            # sibling.
            compose_obs = scratch / "_compose_date_uncertainty_obs.json"
            compose_obs.write_text(json.dumps({
                "files": [{"file": "X.md", "exists": True, "markers_present": ["todo"]}],
                "maturity": {"instantiated_date": "2026-01-01", "commits_count": 2},
            }), encoding="utf-8")
            compose_all_contract = scratch / "_compose_all_contract.toml"
            compose_all_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "X.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { predicate = "no-marker" }\n'
                'due_when = { all = [ { predicate = "instantiated" }, '
                '{ predicate = "commits-at-least", n = 2 } ] }\n',
                encoding="utf-8")
            report = _run_evaluate_via_cli(compose_all_contract, compose_obs, "2026-09-30")
            item = report["items"][0]
            ok = item["state"] == "unknown" and item["due_at"] is None
            results.append(ok)
            print(f"selftest due-when-all-with-one-undated-true-child-is-unknown: got "
                  f"state={item['state']!r} due_at={item['due_at']!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            compose_any_contract = scratch / "_compose_any_contract.toml"
            compose_any_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "X.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { predicate = "no-marker" }\n'
                'due_when = { any = [ { predicate = "instantiated" }, '
                '{ predicate = "commits-at-least", n = 2 } ] }\n',
                encoding="utf-8")
            report = _run_evaluate_via_cli(compose_any_contract, compose_obs, "2026-09-30")
            item = report["items"][0]
            # CONSERVATIVE reading of `any` (documented in eval_due_tree's own docstring): even
            # though "instantiated" has a KNOWN date (2026-01-01), the sibling
            # "commits-at-least" is ALSO individually TRUE with an UNDATED due_at — the real date
            # of that sibling might be earlier than 2026-01-01, so the combined date (and hence
            # the state) is unknown, not "due"/"overdue" computed from the known date alone.
            ok = item["state"] == "unknown" and item["due_at"] is None
            results.append(ok)
            print(f"selftest due-when-any-with-one-undated-true-child-is-unknown: got "
                  f"state={item['state']!r} due_at={item['due_at']!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        date_ok = True
        for value in ("20260101", "2026-W01-1", "2026-02-30", "2026-01-01\n",
                      " 2026-01-01", "2026-1-01", "0000-01-01"):
            for field in fc._MATURITY_DATE_FIELDS:
                date_ok &= bool(fc.validate_maturity({field: value}, "m"))
            date_ok &= bool(fc.validate_maturity({"commit_dates": [value]}, "m"))
            date_ok &= bool(fc.validate_file_observation(
                {"file": "x", "exists": True, "last_modified_date": value}, "f"))
            date_ok &= bool(fc.validate_file_observation(
                {"file": "x", "exists": True, "stamps": {"k": value}}, "f"))
            date_ok &= bool(fc.validate_observations({"files": [], "maturity": {}, "now": value}))
        date_ok &= fc.parse_iso_date("2024-02-29") == date(2024, 2, 29)
        results.append(date_ok)
        print(f"selftest canonical-calendar-dates-every-field: [{'ok' if date_ok else 'MISMATCH'}]")

        # commit_dates must be sorted oldest-first (non-decreasing), and its length must match
        # commits_count when both are present — `commits-at-least`'s own eval_due_leaf indexes
        # commit_dates[n-1] assuming exactly that ordering/length, so a document that violates
        # either would otherwise silently hand back the WRONG commit's date for a given n.
        commit_dates_ok = (
            bool(fc.validate_maturity(
                {"commits_count": 3, "commit_dates": ["2026-09-05", "2026-03-01", "2026-01-01"]}, "m"))
            and bool(fc.validate_maturity(
                {"commits_count": 1, "commit_dates": ["2026-01-01", "2026-01-02"]}, "m"))
            and not fc.validate_maturity(
                {"commits_count": 2, "commit_dates": ["2026-01-01", "2026-01-02"]}, "m")
            and not fc.validate_maturity({"commit_dates": ["2026-01-01", "2026-01-01"]}, "m")
        )
        results.append(commit_dates_ok)
        print(f"selftest commit-dates-ordered-and-length-matches-count: "
              f"[{'ok' if commit_dates_ok else 'MISMATCH'}]")

        # T24:00:00 is not accepted as a second spelling of the next day's midnight — only one
        # canonical spelling per instant.
        t2400_ok = True
        try:
            fc.parse_now("2026-09-30T24:00:00")
            t2400_ok = False
        except fc.FillError:
            pass
        results.append(t2400_ok)
        print(f"selftest no-t24-00-00-midnight-spelling: [{'ok' if t2400_ok else 'MISMATCH'}]")

        # no error message ever formats an unbounded Python int directly — a value this large
        # would make CPython's own int-to-str conversion guard raise ValueError while building the
        # message meant to explain why the value is rejected, defeating the "never raises on
        # malformed data" contract every validator function in this format promises.
        huge_int = 10 ** 5000
        huge_int_ok = True
        try:
            errs = fc.validate_contract({
                "fill_version": "1", "template": "t",
                "contracts": [{"file": "x", "importance": "normal", "urgency_after_due": "1d",
                                "filled_when": {"predicate": "min-lines", "n": huge_int},
                                "due_when": {"predicate": "instantiated"}}],
            })
            huge_int_ok &= bool(errs) and all("digit integer" in r for _, r in errs
                                               if "min-lines" in r)
        except Exception:
            huge_int_ok = False
        try:
            errs = fc.validate_contract({
                "fill_version": "1", "template": "t",
                "contracts": [{"file": "x", "importance": "normal", "urgency_after_due": "1d",
                                "template_rows": huge_int,
                                "filled_when": {"predicate": "no-marker"},
                                "due_when": {"predicate": "instantiated"}}],
            })
            huge_int_ok &= bool(errs) and any("digit integer" in r for _, r in errs)
        except Exception:
            huge_int_ok = False
        # `validate_observations` itself promises never to raise on ANY caller's malformed data, not
        # only the CLI path — a library caller, or a hand-built dict as a test harness might pass,
        # can reach it with no JSON-parse step in between at all, exactly like this 5000-digit case.
        try:
            huge_int_ok &= bool(fc.validate_observations(
                {"files": [], "maturity": {}, "now": huge_int}))
        except Exception:
            huge_int_ok = False
        results.append(huge_int_ok)
        print(f"selftest unbounded-int-never-formatted-in-error-message: "
              f"[{'ok' if huge_int_ok else 'MISMATCH'}]")

        # The ACTUAL CLI is narrower than the in-process check above: `--observations` is JSON text,
        # and `json.loads`'s own int() conversion hits CPython's int-to-str digit-limit guard BEFORE
        # this tool's own code ever runs, for anything past ~4300 digits (`load_json_strict` catches
        # that ValueError and reports exit 12, "invalid JSON input" — never reaching
        # `validate_observations` at all). So a 5000-digit `now` is NOT reachable through the CLI the
        # way the in-process check above is; what IS reachable through the CLI is an integer past the
        # `bit_length() > 64` threshold but still within the ~4300-digit JSON ceiling — exercised here
        # with a real `--observations` file and the real CLI entry point (`main`), not an in-process
        # function call.
        import contextlib as _contextlib
        import io as _io
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory(prefix="fill_evaluate_selftest_cli_bigint_") as cli_tmp:
            cli_obs = pathlib.Path(cli_tmp) / "obs.json"
            cli_obs.write_text(json.dumps({"files": [], "maturity": {}, "now": 10 ** 20}),
                                encoding="utf-8")
            output = _io.StringIO()
            with _contextlib.redirect_stdout(output):
                cli_code = main(["evaluate",
                                  "--contract", str(FIXTURES / "validate" / cases[0]["contract"]),
                                  "--observations", str(cli_obs), "--now", "2026-09-10T00:00:00Z"])
            cli_report = json.loads(output.getvalue())
            cli_ok = (cli_code == E_OBS_INVALID
                      and "now must be a canonical date or timestamp" in
                      (cli_report.get("errors") or [{}])[0].get("reason", ""))
        results.append(cli_ok)
        print(f"selftest cli-reachable-oversized-int-in-observations-now: expected "
              f"exit={E_OBS_INVALID} got exit={cli_code} [{'ok' if cli_ok else 'MISMATCH'}]")

        # Regression: an `--observations` document with 100 malformed `files[]` entries (each
        # already an individually-bounded shape error) used to make the AGGREGATED
        # "--observations fails shape validation (N error(s)): ..." message the
        # PRODUCT of entry count and per-entry length, not merely as long as the single longest entry
        # — an earlier reproduction measured 6,942 characters for a 2,000-character bound.
        # `fc.join_error_list` (list at most 10, then "... and N more") bounds this at the source;
        # `_error_report`'s own unconditional `fc.bound_reason` pass is the final backstop.
        with _tempfile.TemporaryDirectory(prefix="fill_evaluate_selftest_cli_aggregate_") as agg_tmp:
            agg_obs_path = pathlib.Path(agg_tmp) / "obs.json"
            agg_obs = {"maturity": {}, "files": [
                {"file": str(n), "exists": True, "line_count": -1} for n in range(100)]}
            agg_obs_path.write_text(json.dumps(agg_obs), encoding="utf-8")
            agg_output = _io.StringIO()
            with _contextlib.redirect_stdout(agg_output):
                agg_code = main(["evaluate",
                                  "--contract", str(FIXTURES / "validate" / "eval-due-state.toml"),
                                  "--observations", str(agg_obs_path), "--now", "2026-10-01T00:00:00Z"])
            agg_report = json.loads(agg_output.getvalue())
            agg_reason = (agg_report.get("errors") or [{}])[0].get("reason", "")
            agg_ok = (agg_code == E_OBS_INVALID
                      and len(agg_reason) <= fc.DIAGNOSTIC_REASON_MAX_CHARS
                      and "more" in agg_reason)
        results.append(agg_ok)
        print(f"selftest cli-reachable-aggregated-observations-shape-errors: exit={agg_code} "
              f"reason_len={len(agg_reason)} cap={fc.DIAGNOSTIC_REASON_MAX_CHARS} "
              f"[{'ok' if agg_ok else 'MISMATCH'}]")

        # `fc.join_error_list` itself, directly: at most `limit` entries are joined, and a "... and
        # N more" suffix names exactly how many were left out — never a silently truncated list.
        join_items = [f"error-{i}" for i in range(25)]
        joined = fc.join_error_list(join_items, limit=10)
        join_ok = (joined.count("error-") == 10 and "... and 15 more" in joined
                   and all(f"error-{i}" in joined for i in range(10))
                   and not any(f"error-{i}" in joined for i in range(10, 25)))
        results.append(join_ok)
        print(f"selftest join-error-list-bounds-aggregate-and-names-the-remainder: "
              f"joined={joined!r} [{'ok' if join_ok else 'MISMATCH'}]")

        # Regression: 120 items, each ~10,000 characters, must leave the "... and N more" count
        # intact (the defect: the 10 shown items alone, unbounded, could consume the entire
        # DIAGNOSTIC_REASON_MAX_CHARS budget and silently eat the suffix — `fill_evaluate.py`'s own
        # CALL SITES happen to pass short, index-labeled entries today, but `fc.join_error_list`
        # itself must not depend on that; a future call site joining raw, foreign strings must be
        # safe by construction). Mutation that turns this red: reverting `fc.join_error_list` to
        # join raw, unbounded items (the pre-fix code) makes `"... and 110 more" in long_items_reason`
        # False and the reason land at exactly `DIAGNOSTIC_REASON_MAX_CHARS` with the truncation
        # marker instead.
        long_items = [str(i) + "x" * 10_000 for i in range(120)]
        long_items_joined = fc.join_error_list(long_items)
        long_items_reason = fc.bound_reason(f"--observations fails shape validation (120 error(s)): "
                                             f"{long_items_joined}")
        long_items_ok = (len(long_items_reason) <= fc.DIAGNOSTIC_REASON_MAX_CHARS
                         and "... and 110 more" in long_items_reason
                         and not long_items_reason.endswith("<truncated>"))
        results.append(long_items_ok)
        print(f"selftest join-error-list-long-items-count-survives: len={len(long_items_reason)} "
              f"and-110-more={'... and 110 more' in long_items_reason} "
              f"[{'ok' if long_items_ok else 'MISMATCH'}]")

        # Direct, non-CLI exactness checks on the cap primitives themselves: `fc.bound_reason` must
        # return a result no longer than the requested cap for ANY input, including one already
        # exactly at the cap (no marker should be added when none is needed). Bypassing
        # `fc.bound_reason` in any report constructor, or reverting `fc._cap_with_marker` to a bare
        # `s[:n] + marker` (the pre-fold defect that returned 1,004 characters for a 1,000-character
        # cap), makes one of these fail.
        exact_cap_text = "z" * fc.DIAGNOSTIC_REASON_MAX_CHARS
        exact_cap_ok = (fc.bound_reason(exact_cap_text) == exact_cap_text)
        one_over_result = fc.bound_reason("z" * (fc.DIAGNOSTIC_REASON_MAX_CHARS + 1))
        one_over_ok = len(one_over_result) == fc.DIAGNOSTIC_REASON_MAX_CHARS
        huge_reason_result = fc.bound_reason("w" * 5_000_000)
        huge_reason_ok = len(huge_reason_result) == fc.DIAGNOSTIC_REASON_MAX_CHARS
        cap_exactness_ok = exact_cap_ok and one_over_ok and huge_reason_ok
        results.append(cap_exactness_ok)
        print(f"selftest bound-reason-exactness: exact_cap_len={len(exact_cap_text)} "
              f"one_over_result_len={len(one_over_result)} "
              f"huge_result_len={len(huge_reason_result)} "
              f"[{'ok' if cap_exactness_ok else 'MISMATCH'}]")

        # Regression: an `--observations` path that is not valid Unicode at all (the OS
        # gives Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a
        # lone UTF-16 surrogate in the decoded argv string) must still produce a report whose
        # `reason` text re-encodes as UTF-8 cleanly, with the exit code unchanged and the reason
        # within the documented cap. Mutation that turns this red: reverting `fc.bound_path_preview`
        # to `s = str(path)` with no escaping (the pre-fix code) makes `surrogate_utf8_ok` False.
        surrogate_obs_path = pathlib.Path("missing-\udcff")
        try:
            _run_evaluate_via_cli(
                FIXTURES / "validate" / cases[0]["contract"], surrogate_obs_path, cases[0]["now"])
            surrogate_obs_reason = None
            surrogate_obs_code = None
        except fc.FillError as exc:
            surrogate_obs_code = exc.code
            surrogate_obs_reason = str(exc)
        surrogate_obs_utf8_ok = True
        if surrogate_obs_reason is not None:
            try:
                surrogate_obs_reason.encode("utf-8")
            except UnicodeEncodeError:
                surrogate_obs_utf8_ok = False
        surrogate_obs_ok = (surrogate_obs_code == E_OBS_UNREADABLE and surrogate_obs_reason is not None
                            and surrogate_obs_utf8_ok
                            and len(surrogate_obs_reason) <= fc.DIAGNOSTIC_REASON_MAX_CHARS
                            and "\udcff" not in surrogate_obs_reason)
        results.append(surrogate_obs_ok)
        print(f"selftest lone-surrogate-observations-path-escaped-in-report: "
              f"exit={surrogate_obs_code} reason={surrogate_obs_reason!r} "
              f"[{'ok' if surrogate_obs_ok else 'MISMATCH'}]")

        dates = {"first_src_commit_date": "2026-01-01", "first_test_date": "2026-09-01"}
        children = [{"predicate": "first-src-commit"}, {"predicate": "first-test"}]
        composition_ok = (eval_due_tree({"any": children}, dates) == (fc.TRUE, "2026-01-01")
                          and eval_due_tree({"all": children}, dates) == (fc.TRUE, "2026-09-01"))
        # An undated TRUE inside a nested expression stays undated at its parent.
        undated = {"predicate": "commits-at-least", "n": 1}
        for inner in ("all", "any"):
            for outer in ("all", "any"):
                expr = {outer: [{inner: [children[0], undated]}, children[1]]}
                composition_ok &= eval_due_tree(expr, dict(dates, commits_count=1)) == (fc.TRUE, None)
        results.append(composition_ok)
        print(f"selftest chronological-and-nested-due-composition: "
              f"[{'ok' if composition_ok else 'MISMATCH'}]")

        now_ok = True
        for value in ("20260930", "2026-09-30\n", "2026-09-30T00:00:00Z\n",
                      "0001-01-01T00:00:00+01:00", "9999-12-31T23:00:00-02:00",
                      "2026-09-30T00:00:00+00:60", "2026-02-30",
                      "2026-09-30T24:00:00"):  # not a second spelling of next-day midnight
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["evaluate", "--contract", str(FIXTURES / "validate" / cases[0]["contract"]),
                             "--observations", str(FIXTURES / "validate" / cases[0]["observations"]),
                             "--now", value])
            now_ok &= code == E_USAGE and json.loads(output.getvalue())["exit_code"] == E_USAGE
        now_ok &= _parse_now("2026-09-30T01:00:00+01:00") == _parse_now("2026-09-30")
        results.append(now_ok)
        print(f"selftest canonical-now-cli-usage-errors: [{'ok' if now_ok else 'MISMATCH'}]")

        finite_ok = all(fc.load_json_strict(text)[1] is not None
                        for text in ('{"x":1e999}', '{"x":[-1e999]}'))
        results.append(finite_ok)
        print(f"selftest json-exponent-overflow-rejected: [{'ok' if finite_ok else 'MISMATCH'}]")

        mismatch_case = dict(cases[0])
        mismatch_case["expected_states"] = {"__forced_bad__": "nope"}
        _, mismatch_report = _run_case_via_cli(cases[0])
        mismatch_caught = not case_matches(mismatch_case, mismatch_report)
        results.append(mismatch_caught)
        print(f"selftest harness-mismatch-detection: forced-bad-expectation caught={mismatch_caught} "
              f"[{'ok' if mismatch_caught else 'MISMATCH'}]")

        original_run_evaluate = globals()["run_evaluate"]
        def _boom(*_args, **_kwargs):
            raise RuntimeError("selftest-injected catch-all exception")
        globals()["run_evaluate"] = _boom
        try:
            exception_code, exception_report = _run_case_via_cli(cases[0])
        finally:
            globals()["run_evaluate"] = original_run_evaluate
        exception_ok = (exception_code == E_INPUT and
                        exception_report["errors"][0]["reason"].startswith("input/runtime failure"))
        results.append(exception_ok)
        print(f"selftest exit-65-injected-exception-catch-all: got exit={exception_code} "
              f"[{'ok' if exception_ok else 'MISMATCH'}]")

        all_states = set()
        for case in cases:
            all_states |= set(case["expected_states"].values())
        expected_states = {"not-due", "due", "overdue", "filled", "unknown"}
        missing_states = sorted(expected_states - all_states)
        state_coverage_ok = not missing_states
        results.append(state_coverage_ok)
        print(f"selftest state-coverage: states {sorted(expected_states)}; "
              f"missing={missing_states or 'none'} [{'ok' if state_coverage_ok else 'MISMATCH'}]")

        from unittest.mock import patch
        output = io.StringIO()
        with patch("tempfile.TemporaryDirectory", side_effect=OSError("temp directory unavailable")):
            with contextlib.redirect_stdout(output):
                infra_code = run_selftest()
        infra_ok = (infra_code == E_SELFTEST and "selftest infrastructure failure" in output.getvalue()
                    and "temp directory unavailable" in output.getvalue())
        results.append(infra_ok)
        print(f"selftest temp-directory-infrastructure-error: [{'ok' if infra_ok else 'MISMATCH'}]")

        print(f"selftest version-handshake: this evaluator implements {EVALUATOR_VERSION!r}")

        # The regression that closes the class (argv fuzz).
        argv_fuzz_baselines = {
            "evaluate": ["evaluate",
                         "--contract", str(FIXTURES / "validate" / "eval-due-state.toml"),
                         "--observations", str(FIXTURES / "validate" / "eval-obs-not-due.json"),
                         "--now", "2026-09-01"],
        }
        argv_fuzz_exits = {0, E_CONTRACT_UNREADABLE, E_CONTRACT_INVALID, E_OBS_UNREADABLE,
                            E_OBS_INVALID, E_USAGE, E_INPUT}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            HERE / "fill_evaluate.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_failures = _encoding_regression_failures(
            HERE / "fill_evaluate.py",
            [(argv_fuzz_baselines["evaluate"], {0}), (["evaluate"], {E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = fc.output_boundary_selftest(
            HERE / "fill_evaluate.py", argv_fuzz_baselines["evaluate"], ["evaluate"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "fill_evaluate.py", "_error_report", "run_evaluate",
            ["evaluate"], argv_fuzz_baselines["evaluate"])
        for name, (ok, detail) in boundary_controls.items():
            results.append(ok)
            print(f"selftest report-boundary-defect-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
        absent_control_ok, absent_control_detail = _absent_evidence_boundary_control()
        results.append(absent_control_ok)
        print(f"selftest report-boundary-defect-absent-evidence-only: "
              f"{absent_control_detail} [{'ok' if absent_control_ok else 'MISMATCH'}]")
        boundary_controls_ok = (
            all(ok for ok, _ in boundary_controls.values()) and absent_control_ok
        )
        print(f"selftest report-schema-conformance: real boundary controls="
              f"{sorted(boundary_controls) + ['absent-evidence-only']} "
              f"[{'ok' if boundary_controls_ok else 'MISMATCH'}]")

        status = 0 if results and all(results) and boundary_controls_ok else E_SELFTEST
        print(f"SELFTEST {'PASS' if status == 0 else 'FAIL'}: {len(results)} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__}: {exc})")
        return E_SELFTEST


def _error_report(code: int, reason: str) -> dict:
    """The ONE error-report constructor: every exit path that is not a computed report (a
    FillError, or the catch-all for an unexpected exception) builds its output through this
    function, so the documented shape (`status`/`exit_code`/`errors`) cannot drift between one exit
    path and another — including the catch-all, which is exercised by nothing in this format's own
    fixtures precisely because every foreseeable defect already has its own numbered class.
    `reason` is passed through `fc.bound_reason` HERE, unconditionally — the final, structural
    enforcement of the documented ≤2,000-character `reason` bound, so it holds even for a `reason`
    built by a call site that forgot to self-bound (a raw exception's `str()`, for instance), not
    only for ones that already used `fc.safe_repr_for_error`/`fc.bound_path_preview`/
    `fc.join_error_list`."""
    return {"status": "unknown", "exit_code": code,
            "errors": [{"code": code, "reason": fc.bound_reason(reason)}]}


def _main(argv=None) -> int:
    try:
        parser = make_parser()
        args = parser.parse_args(argv)
        if args.selftest:
            if args.command is not None:
                parser.error("--selftest cannot be combined with a command")
            return run_selftest()
        if args.command != "evaluate":
            parser.error("the 'evaluate' command or --selftest is required")
        report = run_evaluate(pathlib.Path(args.contract), pathlib.Path(args.observations), args.now,
                               obligations=args.obligations)
        code = 0
    except fc.FillError as exc:
        code = exc.code
        report = _error_report(code, str(exc))
    except Exception as exc:
        code = E_INPUT
        report = _error_report(code, f"input/runtime failure ({type(exc).__name__}: {exc})")
    fc.write_report(fc.emit_report(report, indent=2, sort_keys=True, allow_nan=False))
    return code



def main(argv=None) -> int:
    return fc.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
