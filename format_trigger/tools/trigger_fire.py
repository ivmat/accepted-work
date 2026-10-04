#!/usr/bin/env python3
"""trigger_fire.py — first cut, stdlib-only PURE evaluator
(`fire(triggers, events, history) -> {fired, suppressed}`). Reads a validated `--triggers FILE`
(gated on `trigger_vocab.validate_trigger_doc` returning no errors — "rules validity gates
firing"), an `--events EVENTS.jsonl` stream, and an optional `--history HIST.jsonl` of prior fired
occurrences, and reports every (trigger, event) pair that fires, plus every candidate suppressed as
a duplicate redelivery or a second distinct occurrence of a `once` trigger.

This tool NEVER mints or writes anything (firing is read-only). It never reads the system clock —
`--now` is required, explicit, and accepted but not otherwise consumed by the firing computation in
this first cut.

Usage:
  python3 trigger_fire.py fire --triggers FILE --events EVENTS.jsonl \\
      [--history HIST.jsonl] --now T
  python3 trigger_fire.py --selftest

Exit codes:
  0   fire ran (status: "ok") — covers zero matches, some matches, some suppressions: all normal
  10-16  the triggers file did NOT validate (see trigger_validate.py's docstring for the class
      table) — status: "unknown", fired/suppressed: null (never an empty list), exit = the lowest
      validation error class
  64  usage/argument error: a required flag missing; a malformed --now; --selftest combined with a
      command
  65  --triggers/--events/--history unreadable or malformed (bad JSON line, non-object line, or an
      EVENT/history record failing its own shape/vocab check) — status: "unknown",
      fired/suppressed: null
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

Stdlib only: argparse, json, pathlib, sys. (`trigger_vocab.py` adds tomllib, hashlib, re,
datetime.)
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import trigger_vocab as tv
from schema_conformance import (boundary_defect_controls, conformance_errors,
                                schema_input_variants)

REPORT_SCHEMA = {
    "fire": ("schema/trigger-0.1.0-draft.schema.json", "fire_report"),
    "run-case": ("schema/trigger-0.1.0-draft.schema.json", "run_case_report"),
}
tv.register_report_schema(__file__, REPORT_SCHEMA)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
SCHEMA_PATH = HERE.parent / "schema" / "trigger-0.1.0-draft.schema.json"


def _report_key_errors(report: dict) -> list[str]:
    """Check the complete emitted report against fire_report from the schema."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/fire_report", "fire_report")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise tv.UsageError(message)


def make_report(status: str, code: int, errors: list, now_str=None) -> dict:
    """The ONE report constructor: every exit path (validation failure, input failure, usage
    error, or an unexpected exception) builds its report through this function, so the documented
    contract — status "unknown" with fired/suppressed both null for every non-"ok" usage/input/
    validation failure — cannot be broken by one exit path
    forgetting a field or using different wording than another."""
    return {
        "status": status,
        "exit_code": code,
        # Every entry's `reason` is passed through `tv.bound_reason` HERE, unconditionally — the
        # final, structural enforcement of the documented ≤2,000-character `reason` bound, so it
        # holds even for an entry built by a call site that forgot to self-bound.
        "errors": [{**e, "reason": tv.bound_reason(e["reason"])} for e in errors],
        "fired": None,
        "suppressed": None,
        "now": now_str,
        "trigger_vocab_version": tv.TRIGGER_VOCAB_VERSION,
    }


def _validated_now(now_str):
    """Returns `now_str` unchanged if it independently passes `tv.parse_at`'s own grammar/range
    check, else None. Every non-"ok" report `_issue_report` builds echoes `now` ONLY once it is
    known-valid: an invalid/unvalidated `--now` argument (a malformed, a
    10,000-character, or a not-valid-Unicode-at-all value) must never be echoed back unchanged,
    regardless of which check actually produced the error being reported — `run_fire` itself
    validates `now_str` before using it for anything else, so by the time control reaches a
    non-"ok" report built directly inside `run_fire` the value is already known-valid; this
    function is what makes the SAME guarantee hold for `_issue_report`'s other callers (`main()`'s
    and `_run_case`'s own exception handlers), which may be reporting a DIFFERENT failure (a usage
    error, an unreadable `--triggers`) while `now_str` itself was never checked at all."""
    if now_str is None:
        return None
    try:
        tv.parse_at(now_str)
    except (ValueError, TypeError):
        return None
    return now_str


def _issue_report(code: int, reason: str, now_str=None) -> dict:
    return make_report("unknown", code, [{"code": code, "reason": reason}],
                       _validated_now(now_str))


def make_run_case_report(code: int, report: dict) -> dict:
    return {"code": code, "report": report}


def _validate_history_record(record, label: str) -> list:
    """A history line is a prior fired[] entry (this tool's own output shape). Only occurrence_id
    and trigger_id are actually consumed; both are required and must be non-blank strings —
    everything else is carried, unchecked."""
    issues = []
    if not isinstance(record, dict):
        return [f"{label}: must be a JSON object"]
    if not tv.is_nonblank_str(record.get("occurrence_id")):
        issues.append(f"{label}: missing/blank occurrence_id")
    if not tv.is_nonblank_str(record.get("trigger_id")):
        issues.append(f"{label}: missing/blank trigger_id")
    return issues


def run_fire(triggers_path: pathlib.Path, events_path: pathlib.Path,
             history_path, now_str: str) -> tuple:
    try:
        tv.parse_at(now_str)
    except ValueError as exc:
        raise tv.UsageError(f"--now {tv.safe_repr_for_error(now_str)} is not a valid ISO-8601 UTC timestamp: {exc}")

    doc = tv.load_toml_file(triggers_path)  # raises TriggerInputError (exit 65) on I/O/TOML fail
    trig_errors = tv.validate_trigger_doc(doc, str(triggers_path))
    if trig_errors:
        code = tv.exit_code_for(trig_errors)
        return code, make_report("unknown", code, trig_errors, now_str)

    self_repo = doc.get("repo")
    triggers = doc.get("triggers", [])

    event_records = tv.load_jsonl_file(events_path)  # raises TriggerInputError on I/O/JSON fail
    input_errors = []
    for i, rec in enumerate(event_records):
        for msg in tv.validate_event_record(rec, f"{events_path}[{i}]"):
            input_errors.append(msg)

    history_records = []
    if history_path is not None:
        history_records = tv.load_jsonl_file(history_path)
        for i, rec in enumerate(history_records):
            for msg in _validate_history_record(rec, f"{history_path}[{i}]"):
                input_errors.append(msg)

    if input_errors:
        errors = [{"code": tv.E_INPUT, "reason": msg} for msg in input_errors]
        return tv.E_INPUT, make_report("unknown", tv.E_INPUT, errors, now_str)

    fired, suppressed = tv.evaluate_fire(triggers, event_records, history_records, self_repo)

    report = {
        "status": "ok",
        "exit_code": 0,
        "now": now_str,
        "fired": fired,
        "suppressed": suppressed,
        "trigger_vocab_version": tv.TRIGGER_VOCAB_VERSION,
    }
    return 0, report


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    fire_p = subparsers.add_parser("fire", allow_abbrev=False)
    fire_p.add_argument("--triggers", default=None)
    fire_p.add_argument("--events", default=None)
    fire_p.add_argument("--history", default=None)
    fire_p.add_argument("--now", default=None)
    # Internal: the self-test's own subprocess entry point for a bounded case (see
    # _run_case_bounded) — not a documented user-facing command.
    run_case_p = subparsers.add_parser("run-case", allow_abbrev=False)
    run_case_p.add_argument("name")
    return parser


def _resolve(path_str) -> pathlib.Path:
    if path_str is None:
        return None
    p = pathlib.Path(path_str)
    if not p.is_absolute():
        for sub in ("validate", "example"):
            cand = FIXTURES / sub / path_str
            if cand.exists():
                return cand
        p = FIXTURES / "validate" / path_str
    return p


def _run_case(case: dict) -> tuple:
    triggers_path = _resolve(case["triggers"])
    events_path = _resolve(case["events"])
    history_path = _resolve(case.get("history"))
    now = case.get("now", "2026-09-24T00:00:00Z")
    try:
        return run_fire(triggers_path, events_path, history_path, now)
    except (tv.TriggerInputError, tv.UsageError) as exc:
        return exc.code, _issue_report(exc.code, exc.reason, now)


def _run_case_bounded(case: dict, max_seconds: float) -> tuple:
    """Runs `case` in a SEPARATE PROCESS with a hard wall-clock timeout, not just a `time.
    monotonic()` measurement taken after an in-process call returns. Measuring elapsed time around
    a call that never returns measures nothing — it cannot interrupt a genuine stall, only report
    on one after the fact (if it ever finishes at all). `subprocess.run(..., timeout=max_seconds)`
    kills the child at the deadline and raises, which this treats as a self-test failure — the
    only way a bounded case can actually enforce its own bound rather than merely observe it.

    Returns (code, report, elapsed, timed_out)."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "run-case", case["name"]],
            cwd=str(HERE), capture_output=True, text=True, timeout=max_seconds,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return None, None, elapsed, True
    elapsed = time.monotonic() - start
    try:
        payload = json.loads(result.stdout)
        return payload["code"], payload["report"], elapsed, False
    except (json.JSONDecodeError, KeyError) as exc:
        return None, make_report(
            "unknown", tv.E_SELFTEST,
            [{"code": tv.E_SELFTEST,
              "reason": f"run-case subprocess produced no usable output "
                        f"({exc}); stderr: {result.stderr.strip()[:500]}"}]), \
            elapsed, False


def _run_case_via_cli(case: dict) -> tuple:
    """Run one fixture through main(), including report serialization and schema validation."""
    outer_code, envelope = _capture_main(["run-case", case["name"]])
    if outer_code != 0 or not isinstance(envelope, dict):
        return outer_code, envelope
    return envelope["code"], envelope["report"]


def case_matches(case: dict, code: int, report: dict) -> bool:
    if _report_key_errors(report):
        return False
    if code != case["expected_exit"]:
        return False
    if "expected_fired" in case:
        got = report.get("fired")
        if got is None:
            return False
        got_keys = [(e["trigger_id"], e["event_id"]) for e in got]
        expected_keys = [tuple(pair) for pair in case["expected_fired"]]
        if got_keys != expected_keys:
            return False
    if case.get("expect_fired_null"):
        if report.get("fired") is not None or report.get("suppressed") is not None:
            return False
    if "expected_suppressed" in case:
        got = report.get("suppressed")
        if got is None:
            return False
        expected_full = [(tid, eid, reason) for tid, eid, reason in case["expected_suppressed"]]
        expected_entries = [{"occurrence_id": tv.occurrence_id(tid, eid), "reason": reason}
                             for tid, eid, reason in expected_full]
        if got != expected_entries:
            return False
    return True


def _capture_main(argv: list) -> tuple:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, json.loads(buf.getvalue())


def _status_schema_ok(label: str, code: int, report: dict, expected_exit: int) -> bool:
    allowed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"]["fire_report"][
        "properties"]["status"]["enum"]
    status = report.get("status")
    ok = code == expected_exit and status in allowed and not _report_key_errors(report)
    if status == "ok":
        ok = ok and isinstance(report.get("fired"), list) and isinstance(report.get("suppressed"), list)
    else:
        ok = ok and status == "unknown" and report.get("fired") is None \
            and report.get("suppressed") is None
    print(f"selftest status-schema-{label}: expected exit={expected_exit} got exit={code} "
          f"status={status!r} allowed={allowed!r} [{'ok' if ok else 'MISMATCH'}]")
    return ok


def _run_case_error_schema_ok(label: str, argv: list[str]) -> bool:
    """A parser failure selected as ``run-case`` must use that command's envelope."""
    code, envelope = _capture_main(argv)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_errors = conformance_errors(
        envelope, schema, "#/$defs/run_case_report", "run_case_report")
    nested = envelope.get("report") if isinstance(envelope, dict) else None
    ok = (code == tv.E_USAGE and envelope.get("code") == tv.E_USAGE
          and isinstance(nested, dict) and nested.get("exit_code") == tv.E_USAGE
          and not schema_errors)
    print(f"selftest run-case-parser-{label}: expected exit={tv.E_USAGE} got exit={code} "
          f"schema_errors={schema_errors!r} [{'ok' if ok else 'MISMATCH'}]")
    return ok




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
    codes AND, when stdout is non-empty, it decodes as UTF-8 and parses under this format's own strict JSON reader
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
        obj = json.loads(text, object_pairs_hook=tv.unique_pairs, parse_constant=tv.reject_constant)
    except (ValueError, RecursionError) as exc:
        return False, f"stdout does not parse as strict JSON: {type(exc).__name__}: {exc}"
    defect = tv.find_json_input_defect(obj)
    if defect is not None:
        return False, defect
    schema_errors = _report_key_errors(obj)
    if schema_errors:
        return False, f"report schema errors: {schema_errors[:3]!r}"
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


def _event_input_schema_differential(cases: list[dict]) -> tuple[bool, int, int, list]:
    """Compare event_record schema verdicts with the real fire executable.

    Per-event required/allowed params are the documented semantic exception (spec §§2, 3.7, 4;
    schema ``when_params.description`` and semantic rule ``per-event-params-closed-set``).
    """
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, 0, 0, []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    failures, generated, semantic_rejections = [], 0, 0
    with tempfile.TemporaryDirectory(prefix="trigger-event-schema-differential-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(c for c in cases if c["expected_exit"] == 0):
            events_path = _resolve(case["events"])
            records = tv.load_jsonl_file(events_path)
            for record_index, document in enumerate(records):
                baseline_errors = conformance_errors(
                    document, schema, "#/$defs/event_record", "event_record")
                if baseline_errors:
                    failures.append((case["name"], record_index,
                                     "positive-fixture-schema", baseline_errors[:3]))
                    continue
                for variant_index, (label, variant) in enumerate(schema_input_variants(
                        document, schema, "#/$defs/event_record")):
                    generated += 1
                    schema_accepts = not conformance_errors(
                        variant, schema, "#/$defs/event_record", "event_record")
                    destination = tmp_path / f"{case_index}-{record_index}-{variant_index}.jsonl"
                    changed_records = list(records)
                    changed_records[record_index] = variant
                    destination.write_text(
                        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":"),
                                           allow_nan=False) + "\n" for item in changed_records),
                        encoding="utf-8")
                    argv = [sys.executable, "-B", str(HERE / "trigger_fire.py"), "fire",
                            "--triggers", str(_resolve(case["triggers"])),
                            "--events", str(destination), "--now",
                            case.get("now", "2026-09-24T00:00:00Z")]
                    if case.get("history") is not None:
                        argv.extend(("--history", str(_resolve(case["history"]))))
                    proc = subprocess.run(argv, capture_output=True, timeout=10)
                    cli_accepts = proc.returncode == 0
                    semantic = (schema_accepts and not cli_accepts
                                and proc.returncode == tv.E_INPUT
                                and (label.startswith("add-optional:$.params.")
                                     or label.startswith("remove-optional:$.params.")))
                    if semantic:
                        semantic_rejections += 1
                    elif schema_accepts != cli_accepts:
                        failures.append((case["name"], record_index, label,
                                         schema_accepts, proc.returncode))
                    elif not cli_accepts and proc.returncode != tv.E_INPUT:
                        failures.append((case["name"], record_index, label,
                                         "non-validation-exit", proc.returncode))
    return not failures and generated > 0, generated, semantic_rejections, failures

def run_selftest() -> int:
    try:
        data = json.loads((FIXTURES / "validate" / "cases.json").read_text(encoding="utf-8"))
        cases = data.get("fire_cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases.json must contain a nonempty 'fire_cases' array")

        results = []
        for case in cases:
            max_seconds = case.get("max_seconds")
            if max_seconds is not None:
                code, report, elapsed, timed_out = _run_case_bounded(case, max_seconds)
                if timed_out:
                    results.append(False)
                    print(f"selftest {case['name']}: killed after {elapsed:.3f}s, over the "
                          f"{max_seconds}s bound (subprocess timeout) [MISMATCH]")
                    continue
                ok = case_matches(case, code, report)
                print(f"selftest {case['name']}: expected exit={case['expected_exit']} got exit={code} "
                      f"[{'ok' if ok else 'MISMATCH'}] ({elapsed:.3f}s, subprocess-bounded)")
            else:
                code, report = _run_case_via_cli(case)
                ok = case_matches(case, code, report)
                print(f"selftest {case['name']}: expected exit={case['expected_exit']} got exit={code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")
            results.append(ok)
            if not ok:
                print(tv.emit_report(report, indent=2, sort_keys=True, default=str, allow_nan=False))

        schema_diff_ok, schema_variants, schema_semantic, schema_diff_failures = (
            _event_input_schema_differential(cases))
        results.append(schema_diff_ok)
        print(f"selftest input-schema-cli-differential-events: "
              f"{schema_variants} generated variant(s), "
              f"{schema_semantic} documented semantic rejection(s), "
              f"{len(schema_diff_failures)} failure(s) {schema_diff_failures[:3]!r} "
              f"[{'ok' if schema_diff_ok else 'MISMATCH'}]")

        for label, argv in (
            ("missing-name", ["run-case"]),
            ("unknown-option", ["run-case", cases[0]["name"], "--not-an-option"]),
            ("bad-value", ["run-case", "__no_such_fire_case__"]),
        ):
            results.append(_run_case_error_schema_ok(label, argv))

        no_triggers_code = _direct_usage_check(extra=["--events", "x.jsonl",
                                                        "--now", "2026-01-01T00:00:00Z"])
        results.append(no_triggers_code == tv.E_USAGE)
        print(f"selftest usage-error-no-triggers: expected exit=64 got exit={no_triggers_code} "
              f"[{'ok' if no_triggers_code == tv.E_USAGE else 'MISMATCH'}]")

        no_events_code = _direct_usage_check(extra=["--triggers", "x.toml",
                                                      "--now", "2026-01-01T00:00:00Z"])
        results.append(no_events_code == tv.E_USAGE)
        print(f"selftest usage-error-no-events: expected exit=64 got exit={no_events_code} "
              f"[{'ok' if no_events_code == tv.E_USAGE else 'MISMATCH'}]")

        no_now_code = _direct_usage_check(extra=["--triggers", "x.toml", "--events", "x.jsonl"])
        results.append(no_now_code == tv.E_USAGE)
        print(f"selftest usage-error-no-now: expected exit=64 got exit={no_now_code} "
              f"[{'ok' if no_now_code == tv.E_USAGE else 'MISMATCH'}]")

        bad_now_code = _direct_usage_check(extra=["--triggers", "00-valid-minimal.toml",
                                                    "--events", "events-empty.jsonl",
                                                    "--now", "not-a-timestamp"])
        results.append(bad_now_code == tv.E_USAGE)
        print(f"selftest usage-error-bad-now: expected exit=64 got exit={bad_now_code} "
              f"[{'ok' if bad_now_code == tv.E_USAGE else 'MISMATCH'}]")

        mismatch_case = dict(cases[0])
        mismatch_case["expected_exit"] = -12345
        mismatch_code, mismatch_report = _run_case_via_cli(mismatch_case)
        mismatch_caught = not case_matches(mismatch_case, mismatch_code, mismatch_report)
        results.append(mismatch_caught)
        print(f"selftest harness-mismatch-detection: forced-bad-expectation caught={mismatch_caught} "
              f"[{'ok' if mismatch_caught else 'MISMATCH'}]")

        valid = cases[0]
        valid_argv = ["fire", "--triggers", str(_resolve(valid["triggers"])),
                      "--events", str(_resolve(valid["events"])), "--now", valid["now"]]
        invalid = next(case for case in cases if 10 <= case["expected_exit"] <= 16)
        invalid_argv = ["fire", "--triggers", str(_resolve(invalid["triggers"])),
                        "--events", str(_resolve(invalid["events"])), "--now", invalid["now"]]
        status_cases = [
            ("exit-0", valid_argv, 0),
            ("validation-class", invalid_argv, invalid["expected_exit"]),
            ("exit-64-usage", ["fire"], tv.E_USAGE),
            ("exit-65-input", ["fire", "--triggers", "/nonexistent/triggers.toml",
                               "--events", str(_resolve(valid["events"])), "--now", valid["now"]],
             tv.E_INPUT),
        ]
        for label, argv, expected_exit in status_cases:
            code, report = _capture_main(argv)
            results.append(_status_schema_ok(label, code, report, expected_exit))

        original_evaluate_fire = tv.evaluate_fire
        def _boom(*_args, **_kwargs):
            raise RuntimeError("selftest-injected catch-all exception")
        tv.evaluate_fire = _boom
        try:
            code, report = _capture_main(valid_argv)
        finally:
            tv.evaluate_fire = original_evaluate_fire
        exception_ok = (_status_schema_ok("exit-65-injected-exception", code, report, tv.E_INPUT)
                        and report.get("errors", [{}])[0].get("reason", "").startswith(
                            "input/runtime failure"))
        results.append(exception_ok)

        # H2: every call this file's own source makes to the JSON serializer passes allow_nan=False
        # -- a grep-based, structural check. Built
        # by concatenation (and spelled apart in this comment too), so the needle does not appear
        # unchanged in this file's own source and falsely match itself.
        own_source = pathlib.Path(__file__).read_text(encoding="utf-8")
        _needle = "json" + ".dumps("
        allow_nan_ok = True
        dumps_calls_seen = 0
        # Scanned over BOTH this tool's own source AND trigger_vocab.py's source: since the structural
        # fold, every report this tool prints routes through `tv.emit_report` (one funnel in
        # trigger_vocab.py that forces `allow_nan=False` regardless of what a caller passes), so the one
        # remaining `json.dumps(` call site that matters is INSIDE that function -- its own call
        # text carries `**dumps_kwargs`, not a literal `allow_nan=False`, so it is recognized by
        # name instead of by substring.
        for _source in (own_source, pathlib.Path(tv.__file__).read_text(encoding="utf-8")):
            idx = 0
            while True:
                idx = _source.find(_needle, idx)
                if idx == -1:
                    break
                depth = 1
                end = idx + len(_needle)
                while depth > 0 and end < len(_source):
                    if _source[end] == "(":
                        depth += 1
                    elif _source[end] == ")":
                        depth -= 1
                    end += 1
                call_text = _source[idx:end]
                dumps_calls_seen += 1
                if "allow_nan=False" not in call_text and "dumps_kwargs" not in call_text:
                    allow_nan_ok = False
                idx = end
        # Behavioral check, not only textual: `tv.emit_report` must force `allow_nan=False`
        # even when a caller explicitly asks for the opposite -- a non-finite float must still
        # raise, never silently serialize as the non-standard `NaN`/`Infinity` token. Disabling the
        # forced `allow_nan=False` inside `emit_report` (passing `**dumps_kwargs` to `json.dumps`
        # unmodified) makes this mutation red.
        try:
            tv.emit_report({"n": float("inf")}, allow_nan=True)
            emit_report_forces_allow_nan = False
        except ValueError:
            emit_report_forces_allow_nan = True
        allow_nan_ok = allow_nan_ok and dumps_calls_seen > 0 and emit_report_forces_allow_nan
        results.append(allow_nan_ok)
        print(f"selftest h2-every-json-dumps-has-allow-nan-false: {dumps_calls_seen} call(s) found "
              f"emit_report_forces_allow_nan={emit_report_forces_allow_nan} "
              f"[{'ok' if allow_nan_ok else 'MISMATCH'}]")

        # H3: no diagnostic helper ever formats an unbounded Python int directly, at any depth --
        # 10**5000 bypasses the TOML-parse digit-count guard entirely (built in Python, never parsed
        # from text), the only way to exercise safe_repr_for_error against a value actually past
        # CPython's own int-to-str conversion limit.
        huge_int = 10 ** 5000
        try:
            scalar_repr = tv.safe_repr_for_error(huge_int)
            nested_repr = tv.safe_repr_for_error({"a": [huge_int, 1, {"b": huge_int}]})
            huge_int_ok = ("digit integer" in scalar_repr and "digit integer" in nested_repr
                           and len(scalar_repr) <= tv._SAFE_REPR_MAX_CHARS
                           and len(nested_repr) <= tv._SAFE_REPR_MAX_CHARS)
        except Exception:
            huge_int_ok = False
        results.append(huge_int_ok)
        print(f"selftest h3-unbounded-int-never-formatted-directly: [{'ok' if huge_int_ok else 'MISMATCH'}]")

        # H4: bound_reason is the FINAL, unconditional backstop on every errors[].reason this report
        # constructor emits -- any text, however assembled or however long, comes back no longer
        # than DIAGNOSTIC_REASON_MAX_CHARS, marker included.
        very_long = "z" * 1_000_000
        bounded = tv.bound_reason(very_long)
        bound_ok = len(bounded) == tv.DIAGNOSTIC_REASON_MAX_CHARS and bounded.endswith("<truncated>")
        results.append(bound_ok)
        print(f"selftest h4-bound-reason-caps-any-length: len={len(bounded)} "
              f"cap={tv.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if bound_ok else 'MISMATCH'}]")

        # H7: producer against rule. Run a REAL fire_case (not a hand-built defect probe) and
        # re-parse its actual JSON report text through the SAME JSON-input-contract this format's
        # own JSONL reader enforces (duplicate-key rejection, the non-finite-number check, UTF-8
        # encodability, bounded nesting) -- proving this evaluator's own real output, not only a
        # hand fixture, satisfies the rule H1 just added.
        real_code, real_report = _run_case_via_cli(cases[0])
        real_report_text = json.dumps(real_report, indent=2, sort_keys=True, default=str,
                                       allow_nan=False)
        reparsed = json.loads(real_report_text, object_pairs_hook=tv.unique_pairs,
                               parse_constant=tv.reject_constant)
        producer_defect = tv.find_json_input_defect(reparsed)
        producer_ok = producer_defect is None
        results.append(producer_ok)
        print(f"selftest h7-producer-output-satisfies-its-own-input-contract: "
              f"defect={producer_defect!r} [{'ok' if producer_ok else 'MISMATCH'}]")

        # Regression: a `--triggers` path that is not valid Unicode at all (the OS gives
        # Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a lone
        # UTF-16 surrogate in the decoded argv string) must still produce a report that re-parses
        # strictly, with the exit code unchanged and the reason within the documented cap. Mutation
        # that turns this red: reverting `tv.bound_path_preview` to `s = str(path)` with no escaping
        # (the pre-fix code) makes `surrogate_defect` report a lone-surrogate string instead of None.
        surrogate_proc = subprocess.run(
            [sys.executable, "-B", str(HERE / "trigger_fire.py"), "fire",
             "--triggers", b"missing-\xff", "--events", "x", "--now", "2026-09-24T00:00:00Z"],
            capture_output=True,
        )
        surrogate_report = json.loads(surrogate_proc.stdout, object_pairs_hook=tv.unique_pairs,
                                       parse_constant=tv.reject_constant)
        surrogate_defect = tv.find_json_input_defect(surrogate_report)
        surrogate_reason = surrogate_report["errors"][0]["reason"]
        surrogate_ok = (surrogate_proc.returncode == tv.E_INPUT and surrogate_defect is None
                        and len(surrogate_reason) <= tv.DIAGNOSTIC_REASON_MAX_CHARS
                        and "\udcff" not in surrogate_reason)
        results.append(surrogate_ok)
        print(f"selftest lone-surrogate-triggers-path-escaped-in-report: exit={surrogate_proc.returncode} "
              f"defect={surrogate_defect!r} reason={surrogate_reason!r} "
              f"[{'ok' if surrogate_ok else 'MISMATCH'}]")

        # Regression: an unvalidated `--now` must never be echoed back unchanged
        # in `now` -- not a lone UTF-16 surrogate byte, and not a 10,000-character value. Mutation
        # that turns this red: reverting `_issue_report` to its pre-fix `now_str` (dropping the
        # `_validated_now` call) makes `bad_now_report["now"]` equal the raw, unvalidated value
        # instead of `None`.
        bad_now_cases = [
            ["fire", "--triggers", "x", "--events", "x", "--now", b"\xff"],
            ["fire", "--triggers", "x", "--events", "x", "--now", "9" * 10_000],
        ]
        now_echo_ok = True
        now_echo_details = []
        for bad_argv in bad_now_cases:
            bad_now_proc = subprocess.run(
                [sys.executable, "-B", str(HERE / "trigger_fire.py")] + bad_argv,
                capture_output=True,
            )
            bad_now_report = json.loads(bad_now_proc.stdout, object_pairs_hook=tv.unique_pairs,
                                         parse_constant=tv.reject_constant)
            bad_now_defect = tv.find_json_input_defect(bad_now_report)
            ok = (bad_now_proc.returncode == tv.E_USAGE and bad_now_report.get("now") is None
                  and bad_now_defect is None)
            now_echo_ok = now_echo_ok and ok
            now_echo_details.append((bad_now_proc.returncode, bad_now_report.get("now"), bad_now_defect))
        results.append(now_echo_ok)
        print(f"selftest unvalidated-now-never-echoed: {now_echo_details} "
              f"[{'ok' if now_echo_ok else 'MISMATCH'}]")

        print(f"selftest version-handshake: this evaluator implements {tv.TRIGGER_VOCAB_VERSION!r}")

        # The regression that closes the class (argv fuzz).
        argv_fuzz_baselines = {
            "fire": ["fire", "--triggers", str(FIXTURES / "validate" / "triggers-all-events.toml"),
                     "--events", str(FIXTURES / "validate" / "events-glob.jsonl"),
                     "--now", "2026-09-24T00:00:00Z"],
        }
        argv_fuzz_exits = {0, tv.E_BAD_TOP, tv.E_BAD_ID, tv.E_BAD_ENTITY_REF, tv.E_BAD_EVENT,
                            tv.E_BAD_PARAMS, tv.E_BAD_RETRIGGER, tv.E_BAD_GLOB_OR_DURATION,
                            tv.E_USAGE, tv.E_INPUT}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            HERE / "trigger_fire.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_failures = _encoding_regression_failures(
            HERE / "trigger_fire.py",
            [(argv_fuzz_baselines["fire"], {0}), (["fire"], {tv.E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = tv.output_boundary_selftest(
            HERE / "trigger_fire.py", argv_fuzz_baselines["fire"], ["fire"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "trigger_fire.py", "make_report", "run_fire",
            ["fire"], argv_fuzz_baselines["fire"], "make_run_case_report")
        for name, (ok, detail) in boundary_controls.items():
            results.append(ok)
            print(f"selftest report-boundary-defect-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
        boundary_controls_ok = all(ok for ok, _ in boundary_controls.values())
        print(f"selftest report-schema-conformance: real boundary controls="
              f"{sorted(boundary_controls)} "
              f"[{'ok' if boundary_controls_ok else 'MISMATCH'}]")

        status = 0 if results and all(results) and boundary_controls_ok else tv.E_SELFTEST
        print(f"SELFTEST {'PASS' if status == 0 else 'FAIL'}: "
              f"{len(cases) + len(output_checks) + 12} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__}: {exc})")
        return tv.E_SELFTEST


def _direct_usage_check(extra=None) -> int:
    argv = ["fire"] + (extra or [])
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _main(argv=None) -> int:
    now_str = None
    argv = list(sys.argv[1:] if argv is None else argv)
    subcommand = next((token for token in argv if not token.startswith("-")), None)
    report_command = "run-case" if subcommand == "run-case" else "fire"
    try:
        parser = make_parser()
        args = parser.parse_args(argv)
        if args.selftest:
            if args.command is not None:
                parser.error("--selftest cannot be combined with a command")
            return run_selftest()
        if args.command == "run-case":
            data = json.loads((FIXTURES / "validate" / "cases.json").read_text(encoding="utf-8"))
            case = next((c for c in data.get("fire_cases", []) if c["name"] == args.name), None)
            if case is None:
                parser.error(f"run-case: no fire_case named {args.name!r}")
            code, report = _run_case(case)
            output = make_run_case_report(code, report)
            exit_code = 0
            render_kwargs = {"default": str, "allow_nan": False}
        elif args.command != "fire":
            parser.error("the 'fire' command or --selftest is required")
        else:
            if not args.triggers:
                parser.error("--triggers FILE is required")
            if not args.events:
                parser.error("--events FILE is required")
            if not args.now:
                parser.error("--now is required")
            now_str = args.now

            triggers_path = pathlib.Path(args.triggers)
            events_path = pathlib.Path(args.events)
            history_path = pathlib.Path(args.history) if args.history else None

            exit_code, output = run_fire(triggers_path, events_path, history_path, args.now)
            render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except (tv.UsageError, tv.TriggerInputError) as exc:
        exit_code = exc.code
        output = _issue_report(exit_code, exc.reason, now_str)
        if report_command == "run-case":
            output = make_run_case_report(exit_code, output)
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except Exception as exc:
        reason = f"input/runtime failure ({type(exc).__name__}: {exc})"
        exit_code = tv.E_INPUT
        output = _issue_report(exit_code, reason, now_str)
        if report_command == "run-case":
            output = make_run_case_report(exit_code, output)
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    tv.write_report(tv.emit_report(output, **render_kwargs), report_command=report_command)
    return exit_code



def main(argv=None) -> int:
    return tv.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
