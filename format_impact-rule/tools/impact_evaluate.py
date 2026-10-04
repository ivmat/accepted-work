#!/usr/bin/env python3
"""impact_evaluate.py — first cut, stdlib-only PURE evaluator
(`impact(rules, changed_paths, events) -> fired`). Reads the SAME merged `--rules FILE` set
`impact_validate.py` reads, gates evaluation on that set validating clean ("rules validity gates
evaluation"), validates every supplied `--changed`/`--changed-file` entry against the same grammar
and size limits an authored glob uses (a violating entry is reported as an input error, never left
to crash or to silently match nothing), then matches every rule's `when` against those changed
paths and `--event` names, sorts the fired entries (importance, urgency, rule id, target), and
reports every `derive[]` entry as `not-evaluated` (derivers never fire in this first cut).

This tool NEVER mints anything (evaluation is read-only). `--obligations` adds, to each fired
entry, a REPORT-ONLY SUMMARY shaped like a small subset of a `format_obligation` duty record's
fields — see `impact_rules.project_obligation`'s docstring. This summary does NOT conform to the
`format_obligation` format and is not itself a duty record; a caller that wants a real duty record
must build one from this summary, not treat the summary as one.

Usage:
  python3 impact_evaluate.py evaluate --rules FILE [--rules FILE ...] \\
      (--changed PATH [--changed PATH ...] | --changed-file LIST.txt) \\
      [--event NAME ...] --now T [--obligations]
  python3 impact_evaluate.py --selftest

Exit codes:
  0   evaluation ran (this covers BOTH "rules valid, N rules fired" and "rules valid, zero rules
      fired" — a no-match evaluation is success, never an error; see report status "ok")
  10-18  the rules did NOT validate (see `impact_validate.py`'s docstring for the class table) —
      report status is "unknown", `fired`/`not_evaluated_derivers` are `null` (never an empty
      list — "invalid rules gives unknown not empty"), and the exit code is the LOWEST validation
      error class that fired
  64  usage/argument error: no `--rules`; both `--changed` and `--changed-file` given; an
      `--event` value outside the closed event vocabulary; a malformed `--now` — grammar
      (`impact_rules.parse_now` checks ONE strict form with `.fullmatch`, never trimmed first), an
      impossible calendar date/time, a year past `impact_rules.MAX_NOW_YEAR` (which bounds
      `--obligations`' own deadline arithmetic — see `impact_rules.project_obligation`), or a
      timezone conversion that overflows/underflows the representable range; `--selftest` combined
      with a command (report status "unknown", `fired`/`not_evaluated_derivers` are `null` — the
      schema's `evaluate_report.status` enum is only `ok`/`unknown`, so every non-ok exit, usage
      errors included, reports "unknown", never a third value)
  65  a `--rules FILE` or `--changed-file` itself is unreadable / malformed (bad UTF-8, a bare CR
      as a line separator, too many lines — see `impact_rules.load_changed_file`; or a TOML
      syntax error, propagated from the validation gate); OR at least one `--changed`/
      `--changed-file` entry fails the changed-path grammar or size limits; OR an unexpected
      exception reaches `main()`'s own catch-all (report status "unknown" in every case,
      `fired`/`not_evaluated_derivers` are `null`, `errors[]` lists every offending entry)
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

Stdlib only: argparse, json, pathlib, subprocess, sys, time. (`impact_rules.py` adds `tomllib`,
`datetime`, `re`.)
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

import impact_rules as ir
from schema_conformance import boundary_defect_controls, conformance_errors

REPORT_SCHEMA = {
    "evaluate": ("schema/impact-rule-0.1.0-draft.schema.json", "evaluate_report"),
    "run-case": ("schema/impact-rule-0.1.0-draft.schema.json", "run_case_report"),
}
ir.register_report_schema(__file__, REPORT_SCHEMA)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
SCHEMA_PATH = HERE.parent / "schema" / "impact-rule-0.1.0-draft.schema.json"


def _report_key_errors(report: dict) -> list[str]:
    """Check the complete emitted report against evaluate_report from the schema."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/evaluate_report", "evaluate_report")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise ir.UsageError(message)


def make_report(status: str, code: int, errors: list, fired=None,
                 not_evaluated_derivers=None) -> dict:
    """The ONE report constructor: every exit path this tool has — a clean evaluation, a rules-
    validation failure, a changed-path grammar failure, a usage error, an unreadable/malformed
    input file, or an UNEXPECTED exception caught by the catch-all in `main()` — builds its report
    through this function. That is what makes the documented contract (on any non-'ok' report,
    `fired` and `not_evaluated_derivers` are BOTH `null`, never merely absent; `errors` is always
    present, empty only on 'ok') hold for every path, including ones added later, rather than
    depending on each path separately remembering every field. Every entry's `reason` is passed
    through `ir.bound_reason` HERE, unconditionally — the final, structural enforcement of the
    documented ≤2,000-character `reason` bound, so it holds even for an entry built by a call site
    that forgot to self-bound, not only for ones that already used `ir.safe_repr_for_error`/
    `ir.bound_path_preview`."""
    return {
        "status": status,
        "exit_code": code,
        "errors": [{**e, "reason": ir.bound_reason(e["reason"])} for e in errors],
        "fired": fired,
        "not_evaluated_derivers": not_evaluated_derivers,
        "impact_rules_version": ir.IMPACT_RULES_VERSION,
    }


def make_run_case_report(code: int, report: dict) -> dict:
    return {"code": code, "report": report}


def run_evaluate(rules_paths: list, changed_paths: list, events: list, now_str: str,
                  obligations: bool) -> tuple:
    for event in events:
        if event not in ir.EVENT_VOCAB:
            raise ir.UsageError(f"--event {ir.safe_repr_for_error(event)} not in "
                                 f"{sorted(ir.EVENT_VOCAB)}")

    try:
        now = ir.parse_now(now_str)
    except ValueError as exc:
        raise ir.UsageError(f"--now {ir.safe_repr_for_error(now_str)} is not a valid ISO-8601 UTC "
                             f"timestamp: {exc}")

    docs = []
    for path in rules_paths:
        doc = ir.load_toml_file(path)  # raises RulesInputError (exit 65) on I/O/TOML failure
        docs.append((str(path), doc))

    errors = ir.validate_merged(docs)
    if errors:
        code = ir.exit_code_for(errors)
        return code, make_report("unknown", code, errors)

    changed_issues = ir.validate_changed_paths(changed_paths)
    if changed_issues:
        errors = [{"code": ir.E_INPUT, "reason": msg} for msg in changed_issues]
        return ir.E_INPUT, make_report("unknown", ir.E_INPUT, errors)

    result = ir.evaluate(docs, changed_paths, events)
    fired = result["fired"]
    if obligations:
        for entry in fired:
            entry["obligation"] = ir.project_obligation(entry, now)

    return 0, make_report("ok", 0, [], fired=fired,
                           not_evaluated_derivers=result["not_evaluated_derivers"])


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    ev = subparsers.add_parser("evaluate", allow_abbrev=False)
    ev.add_argument("--rules", action="append", default=None)
    ev.add_argument("--changed", action="append", default=None)
    ev.add_argument("--changed-file", default=None)
    ev.add_argument("--event", action="append", default=[])
    ev.add_argument("--now", default=None)
    ev.add_argument("--obligations", action="store_true",
                     help="add a report-only obligation-shaped summary to each fired entry "
                          "(NOT a format_obligation duty record — see the module docstring)")
    # Internal: the self-test's own subprocess entry point for a bounded case (see
    # _run_case_bounded) — not a documented user-facing command.
    run_case_p = subparsers.add_parser("run-case", allow_abbrev=False)
    run_case_p.add_argument("name")
    return parser


def _resolve(path_str: str) -> pathlib.Path:
    p = pathlib.Path(path_str)
    if not p.is_absolute():
        p = (FIXTURES / "validate" / path_str)
        if not p.exists():
            p = (FIXTURES / "example" / pathlib.Path(path_str).name)
    return p


def _run_case(case: dict) -> tuple:
    try:
        rules_paths = [_resolve(p) for p in case["rules"]]
        if "changed" in case:
            changed = case["changed"]
        elif "changed_file" in case:
            # Inside the try: a --changed-file's own I/O/encoding/line-contract failure
            # (RulesInputError) must be caught the same way a run_evaluate()-raised one is, not
            # left to propagate past this function as an uncaught exception.
            changed = ir.load_changed_file(_resolve(case["changed_file"]))
        else:
            changed = []
        events = case.get("events", [])
        now = case.get("now", "2026-09-24T00:00:00Z")
        obligations = case.get("obligations", False)
        return run_evaluate(rules_paths, changed, events, now, obligations)
    except (ir.RulesInputError, ir.UsageError) as exc:
        # Every non-ok report this tool emits uses "unknown" — the schema's own
        # `evaluate_report.status` enum is only `ok`/`unknown`, there is no third value, and a
        # usage error is reported the same way every other non-ok exit path is.
        return exc.code, make_report("unknown", exc.code, [{"code": exc.code, "reason": exc.reason}])


def _run_case_bounded(case: dict, max_seconds: float) -> tuple:
    """Runs `case` in a SEPARATE PROCESS with a hard wall-clock timeout, not just an in-process
    `time.monotonic()` measurement taken after the call returns (which cannot interrupt a genuine
    stall, only report on one after the fact). `subprocess.run(..., timeout=max_seconds)` kills the
    child at the deadline and raises, which this treats as a self-test failure. Returns (code,
    report, elapsed, timed_out)."""
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
        return None, {"status": "invalid", "exit_code": ir.E_SELFTEST,
                      "errors": [{"code": ir.E_SELFTEST,
                                  "reason": f"run-case subprocess produced no usable output "
                                            f"({exc}); stderr: {result.stderr.strip()[:500]}"}]}, \
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
        got_keys = [(e["rule_id"], e["target"]) for e in got]
        expected_keys = [tuple(pair) for pair in case["expected_fired"]]
        if got_keys != expected_keys:
            return False
    if case.get("expect_fired_null"):
        if report.get("fired") is not None:
            return False
    if "expected_not_evaluated_derivers" in case:
        got = report.get("not_evaluated_derivers") or []
        got_names = sorted(d["deriver"] for d in got)
        if got_names != sorted(case["expected_not_evaluated_derivers"]):
            return False
    if "expected_required_by" in case:
        got = report.get("fired") or []
        pairs = {(e["rule_id"], e["target"]): e.get("obligation", {}).get("required_by") for e in got}
        for key, want in case["expected_required_by"].items():
            rid, target = key.split("|", 1)
            if pairs.get((rid, target)) != want:
                return False
    if "expected_error_substring" in case:
        got = " ".join(e.get("reason", "") for e in (report.get("errors") or []))
        if case["expected_error_substring"] not in got:
            return False
    return True


def _schema_status_enum() -> list:
    """Reads `evaluate_report.status`'s own enum straight FROM the schema file — never duplicated
    as a Python literal here — so the status-schema-conformance selftest checks every exit path's
    `status` against the one place that actually defines what values are allowed."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["$defs"]["evaluate_report"]["properties"]["status"]["enum"]


def _capture_main(argv: list) -> tuple:
    """Runs `main(argv)` IN-PROCESS (never a subprocess), capturing its stdout JSON report. Used
    only by the status-schema-conformance selftest, which needs to monkeypatch `ir.evaluate` to
    force the catch-all's UNEXPECTED-exception path — a subprocess boundary would not see an
    in-process monkeypatch, so that one path could not be driven any other way."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    report = json.loads(buf.getvalue())
    return code, report


def _status_schema_ok(label: str, code: int, report: dict, expected_exit: int,
                       allowed_status: list) -> bool:
    """One status-schema-conformance check: the exit code matches, `status` is IN the schema's own
    enum, and the schema's documented conditional holds (`status=='ok'` requires `fired`/
    `not_evaluated_derivers` to both be arrays; any other status requires both to be `null`)."""
    status = report.get("status")
    ok = code == expected_exit and status in allowed_status and not _report_key_errors(report)
    if status == "ok":
        ok = ok and isinstance(report.get("fired"), list) and \
            isinstance(report.get("not_evaluated_derivers"), list)
    else:
        ok = ok and report.get("fired") is None and report.get("not_evaluated_derivers") is None
    print(f"selftest status-schema-{label}: expected exit={expected_exit} got exit={code} "
          f"status={status!r} allowed={allowed_status} [{'ok' if ok else 'MISMATCH'}]")
    if not ok:
        print(ir.emit_report(report, indent=2, sort_keys=True, default=str, allow_nan=False))
    return ok


def _run_case_error_schema_ok(label: str, argv: list[str]) -> bool:
    """A parser failure selected as ``run-case`` must use that command's envelope."""
    code, envelope = _capture_main(argv)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_errors = conformance_errors(
        envelope, schema, "#/$defs/run_case_report", "run_case_report")
    nested = envelope.get("report") if isinstance(envelope, dict) else None
    ok = (code == ir.E_USAGE and envelope.get("code") == ir.E_USAGE
          and isinstance(nested, dict) and nested.get("exit_code") == ir.E_USAGE
          and not schema_errors)
    print(f"selftest run-case-parser-{label}: expected exit={ir.E_USAGE} got exit={code} "
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
        obj = json.loads(text, object_pairs_hook=ir.unique_pairs, parse_constant=ir.reject_constant)
    except (ValueError, RecursionError) as exc:
        return False, f"stdout does not parse as strict JSON: {type(exc).__name__}: {exc}"
    defect = ir.find_json_input_defect(obj)
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

def run_selftest() -> int:
    try:
        data = json.loads((FIXTURES / "validate" / "cases.json").read_text(encoding="utf-8"))
        cases = data.get("evaluate_cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases.json must contain a nonempty 'evaluate_cases' array")

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
                print(ir.emit_report(report, indent=2, sort_keys=True, default=str, allow_nan=False))

        for label, argv in (
            ("missing-name", ["run-case"]),
            ("unknown-option", ["run-case", cases[0]["name"], "--not-an-option"]),
            ("bad-value", ["run-case", "__no_such_evaluate_case__"]),
        ):
            results.append(_run_case_error_schema_ok(label, argv))

        no_rules_code = _direct_usage_check(extra=["--now", "2026-01-01T00:00:00Z"])
        results.append(no_rules_code == ir.E_USAGE)
        print(f"selftest usage-error-no-rules: expected exit=64 got exit={no_rules_code} "
              f"[{'ok' if no_rules_code == ir.E_USAGE else 'MISMATCH'}]")

        bad_event_code = _direct_usage_check(extra=["--rules", "x.toml", "--event", "not-a-real-event",
                                                      "--now", "2026-01-01T00:00:00Z"])
        results.append(bad_event_code == ir.E_USAGE)
        print(f"selftest usage-error-bad-event: expected exit=64 got exit={bad_event_code} "
              f"[{'ok' if bad_event_code == ir.E_USAGE else 'MISMATCH'}]")

        both_changed_code = _direct_usage_check(extra=["--rules", "x.toml", "--changed", "a",
                                                         "--changed-file", "b.txt",
                                                         "--now", "2026-01-01T00:00:00Z"])
        results.append(both_changed_code == ir.E_USAGE)
        print(f"selftest usage-error-both-changed-forms: expected exit=64 got exit={both_changed_code} "
              f"[{'ok' if both_changed_code == ir.E_USAGE else 'MISMATCH'}]")

        # The changed-file line-count boundary (impact_rules.CHANGED_FILE_MAX_LINES): exactly
        # 100,000 LF-terminated lines must pass; 100,001 must fail. Generated in a fresh
        # tempfile.TemporaryDirectory() at test time, never written into this tree — a 100,000-
        # or 100,001-line fixture file has no business being a committed, reviewed artifact.
        with tempfile.TemporaryDirectory(prefix="impact-evaluate-selftest-") as td:
            at_max = pathlib.Path(td) / "at-max.txt"
            over_max = pathlib.Path(td) / "over-max.txt"
            at_max.write_text("a\n" * ir.CHANGED_FILE_MAX_LINES, encoding="utf-8")
            over_max.write_text("a\n" * (ir.CHANGED_FILE_MAX_LINES + 1), encoding="utf-8")

            at_max_code, at_max_report = _capture_main([
                "evaluate", "--rules", str(FIXTURES / "validate" / "00-valid-minimal.toml"),
                "--changed-file", str(at_max), "--now", "2026-09-24T00:00:00Z",
            ])
            at_max_ok = at_max_code == 0
            results.append(at_max_ok)
            print(f"selftest changed-file-boundary-at-max-lines-passes: "
                  f"{ir.CHANGED_FILE_MAX_LINES} LF-terminated lines, expected exit=0 got "
                  f"exit={at_max_code} [{'ok' if at_max_ok else 'MISMATCH'}]")

            try:
                ir.load_changed_file(over_max)
                over_max_code = 0
            except ir.RulesInputError as exc:
                over_max_code = exc.code
            over_max_ok = over_max_code == ir.E_INPUT
            results.append(over_max_ok)
            print(f"selftest changed-file-boundary-over-max-lines-fails: "
                  f"{ir.CHANGED_FILE_MAX_LINES + 1} LF-terminated lines, expected exit="
                  f"{ir.E_INPUT} got exit={over_max_code} [{'ok' if over_max_ok else 'MISMATCH'}]")

        mismatch_case = dict(cases[0])
        mismatch_case["expected_exit"] = -12345
        mismatch_code, mismatch_report = _run_case_via_cli(mismatch_case)
        mismatch_caught = not case_matches(mismatch_case, mismatch_code, mismatch_report)
        results.append(mismatch_caught)
        print(f"selftest harness-mismatch-detection: forced-bad-expectation caught={mismatch_caught} "
              f"[{'ok' if mismatch_caught else 'MISMATCH'}]")

        print(f"selftest version-handshake: this evaluator implements {ir.IMPACT_RULES_VERSION!r}")

        # Status-schema conformance: every evaluator exit path must report a
        # `status` value that is IN the schema's own `evaluate_report.status` enum — read FROM the
        # schema file, never duplicated here, so this check cannot silently drift from the schema
        # it is checking against — plus the status=='ok' <=> fired/not_evaluated_derivers are
        # arrays, status=='unknown' <=> both are null conditional the schema documents. Exercises
        # exit 0, a validation class (10-18), exit 64 (usage), and exit 65 via BOTH an existing
        # hook (an unreadable --rules file) and an INJECTED exception (the catch-all in main() —
        # the one path no ordinary CLI invocation reaches, so it is forced here by monkeypatching
        # `ir.evaluate` for the duration of one in-process call).
        allowed_status = _schema_status_enum()
        minimal = FIXTURES / "validate" / "00-valid-minimal.toml"
        bad_type_rules = FIXTURES / "validate" / "03-bad-type-empty-must-update.toml"

        schema_checks = [
            ("exit-0", ["evaluate", "--rules", str(minimal), "--now", "2026-09-24T00:00:00Z"], 0),
            ("validation-class-12", ["evaluate", "--rules", str(bad_type_rules), "--now",
                                      "2026-09-24T00:00:00Z"], ir.E_BAD_TYPE),
            ("exit-64-usage-missing-now", ["evaluate", "--rules", str(minimal)], ir.E_USAGE),
            ("exit-65-unreadable-rules-hook",
             ["evaluate", "--rules", "/nonexistent/impact-rule-selftest.toml", "--now",
              "2026-09-24T00:00:00Z"], ir.E_INPUT),
        ]
        for label, argv, expected_exit in schema_checks:
            code, report = _capture_main(argv)
            results.append(_status_schema_ok(label, code, report, expected_exit, allowed_status))

        orig_evaluate = ir.evaluate

        def _boom(*_a, **_k):
            raise RuntimeError("selftest-injected catch-all exception (status-schema-conformance)")

        ir.evaluate = _boom
        try:
            code, report = _capture_main(["evaluate", "--rules", str(minimal), "--now",
                                           "2026-09-24T00:00:00Z"])
        finally:
            ir.evaluate = orig_evaluate
        exception_ok = (_status_schema_ok("exit-65-injected-exception-catch-all", code, report,
                                          ir.E_INPUT, allowed_status)
                        and report.get("errors", [{}])[0].get("reason", "").startswith(
                            "input/runtime failure"))
        results.append(exception_ok)

        # Regression: a `--rules` argument that is itself a hostile, 10,000-character path,
        # run through the FULL CLI entry point (never
        # written to disk — the failure is `load_toml_file`'s own `OSError`). `ir.bound_path_preview`
        # plus the exception's CLASS name at the source, and `make_report`'s own unconditional
        # `ir.bound_reason` pass as the final backstop, keep every reported `reason` no longer than
        # `ir.DIAGNOSTIC_REASON_MAX_CHARS` — the same bound `impact_validate.py`'s own selftest
        # checks for the `validate` command, checked here for `evaluate` too since it shares the
        # same `ir.load_toml_file` call site.
        huge_rules_code, huge_rules_report = _capture_main(
            ["evaluate", "--rules", "x" * 10_000, "--now", "2026-09-24T00:00:00Z"])
        huge_rules_reasons = [e["reason"] for e in huge_rules_report.get("errors") or []]
        huge_rules_ok = (huge_rules_code == ir.E_INPUT and bool(huge_rules_reasons)
                          and all(len(r) <= ir.DIAGNOSTIC_REASON_MAX_CHARS
                                  for r in huge_rules_reasons))
        results.append(huge_rules_ok)
        # Regression: a `--rules` path that is not valid Unicode at all (the OS gives
        # Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a lone
        # UTF-16 surrogate in the decoded argv string) must still produce a report whose `reason`
        # text re-encodes as UTF-8 cleanly. Mutation that turns this red: reverting
        # `ir.bound_path_preview` to `s = str(path)` with no escaping (the pre-fix code) makes
        # `surrogate_utf8_ok` False.
        surrogate_code, surrogate_report = _capture_main(
            ["evaluate", "--rules", "missing-\udcff", "--now", "2026-09-24T00:00:00Z"])
        surrogate_reasons = [e["reason"] for e in surrogate_report.get("errors") or []]
        surrogate_utf8_ok = True
        for r in surrogate_reasons:
            try:
                r.encode("utf-8")
            except UnicodeEncodeError:
                surrogate_utf8_ok = False
        surrogate_ok = (surrogate_code == ir.E_INPUT and bool(surrogate_reasons) and surrogate_utf8_ok
                        and all(len(r) <= ir.DIAGNOSTIC_REASON_MAX_CHARS for r in surrogate_reasons)
                        and not any("\udcff" in r for r in surrogate_reasons))
        results.append(surrogate_ok)
        print(f"selftest lone-surrogate-rules-path-escaped-in-report: exit={surrogate_code} "
              f"reasons={surrogate_reasons!r} [{'ok' if surrogate_ok else 'MISMATCH'}]")

        print(f"selftest cli-reachable-oversized-rules-path: exit={huge_rules_code} "
              f"max_reason_len={max((len(r) for r in huge_rules_reasons), default=0)} "
              f"cap={ir.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if huge_rules_ok else 'MISMATCH'}]")

        # The regression that closes the class (argv fuzz).
        argv_fuzz_baselines = {
            "evaluate": ["evaluate", "--rules", str(FIXTURES / "validate" / "00-valid-minimal.toml")],
        }
        argv_fuzz_exits = {0, ir.E_MISSING_KEY, ir.E_UNKNOWN_KEY, ir.E_BAD_TYPE, ir.E_BAD_ENUM,
                            ir.E_BAD_GLOB, ir.E_BAD_WHEN, ir.E_DUP_RULE_ID, ir.E_BAD_URGENCY,
                            ir.E_BAD_DERIVE, ir.E_USAGE, ir.E_INPUT}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            HERE / "impact_evaluate.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_valid_argv = argv_fuzz_baselines["evaluate"] + ["--now", "2026-09-24T00:00:00Z"]
        encoding_failures = _encoding_regression_failures(
            HERE / "impact_evaluate.py",
            [(encoding_valid_argv, {0}), (["evaluate"], {ir.E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = ir.output_boundary_selftest(
            HERE / "impact_evaluate.py", encoding_valid_argv, ["evaluate"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "impact_evaluate.py", "make_report", "run_evaluate",
            ["evaluate"], encoding_valid_argv, "make_run_case_report")
        for name, (ok, detail) in boundary_controls.items():
            results.append(ok)
            print(f"selftest report-boundary-defect-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
        boundary_controls_ok = all(ok for ok, _ in boundary_controls.values())
        print(f"selftest report-schema-conformance: real boundary controls="
              f"{sorted(boundary_controls)} "
              f"[{'ok' if boundary_controls_ok else 'MISMATCH'}]")

        status = 0 if results and all(results) and boundary_controls_ok else ir.E_SELFTEST
        print(f"SELFTEST {'PASS' if status == 0 else 'FAIL'}: "
              f"{len(cases) + len(output_checks) + 6} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__}: {exc})")
        return ir.E_SELFTEST


def _direct_usage_check(extra=None) -> int:
    """Runs main() against a crafted argv expected to fail with a usage error, WITHOUT touching
    stdout noise concerns — used only by --selftest to prove the usage-error paths."""
    argv = ["evaluate"] + (extra or [])
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    subcommand = next((token for token in argv if not token.startswith("-")), None)
    report_command = "run-case" if subcommand == "run-case" else "evaluate"
    try:
        parser = make_parser()
        args = parser.parse_args(argv)
        if args.selftest:
            if args.command is not None:
                parser.error("--selftest cannot be combined with a command")
            return run_selftest()
        if args.command == "run-case":
            data = json.loads((FIXTURES / "validate" / "cases.json").read_text(encoding="utf-8"))
            case = next((c for c in data.get("evaluate_cases", []) if c["name"] == args.name), None)
            if case is None:
                parser.error(f"run-case: no evaluate_case named {args.name!r}")
            code, report = _run_case(case)
            output = make_run_case_report(code, report)
            exit_code = 0
            render_kwargs = {"default": str, "allow_nan": False}
        elif args.command != "evaluate":
            parser.error("the 'evaluate' command or --selftest is required")
        else:
            if not args.rules:
                parser.error("at least one --rules FILE is required")
            if args.changed and args.changed_file:
                parser.error("--changed and --changed-file are mutually exclusive")
            if args.now is None:
                parser.error("--now is required")

            rules_paths = [pathlib.Path(p) for p in args.rules]
            if args.changed_file:
                changed = ir.load_changed_file(pathlib.Path(args.changed_file))
            else:
                changed = args.changed or []

            exit_code, output = run_evaluate(
                rules_paths, changed, args.event, args.now, args.obligations)
            render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except ir.UsageError as exc:
        # "unknown", not "invalid": the schema's evaluate_report.status enum has only ok/unknown —
        # see make_report's own docstring and the module docstring's exit-code table.
        exit_code = exc.code
        output = make_report("unknown", exit_code,
                             [{"code": exit_code, "reason": exc.reason}])
        if report_command == "run-case":
            output = make_run_case_report(exit_code, output)
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except ir.RulesInputError as exc:
        exit_code = exc.code
        output = make_report("unknown", exit_code,
                             [{"code": exit_code, "reason": exc.reason}])
        if report_command == "run-case":
            output = make_run_case_report(exit_code, output)
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except Exception as exc:
        # The catch-all for a genuinely UNEXPECTED exception: still "unknown", for the same
        # schema-enum reason as the two branches above — there is no "invalid" status to report.
        reason = f"input/runtime failure ({type(exc).__name__}: {exc})"
        exit_code = ir.E_INPUT
        output = make_report("unknown", exit_code, [{"code": exit_code, "reason": reason}])
        if report_command == "run-case":
            output = make_run_case_report(exit_code, output)
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    ir.write_report(ir.emit_report(output, **render_kwargs), report_command=report_command)
    return exit_code



def main(argv=None) -> int:
    return ir.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
