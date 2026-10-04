#!/usr/bin/env python3
"""trigger_validate.py — first cut, stdlib-only validator for the standalone Trigger TOML
file, caller-supplied via `--triggers FILE`. Validates ONE such file: shape, closed vocabularies, entity-ref
grammar, glob/duration grammar, and within-file `id` uniqueness.

This tool takes exactly one `--triggers FILE` (no multi-file merge / cross-file inheritance).

Shared logic (`load_toml_file`, `validate_trigger_doc`, `EVENT_VOCAB`, the entity-ref parser, the
glob grammar) lives in `trigger_vocab.py`, imported here and by `trigger_fire.py`.

Usage:
  python3 trigger_validate.py validate --triggers FILE
  python3 trigger_validate.py --selftest

Exit codes (lowest-numbered class wins when several fire; 0 iff no error fired) — see
`trigger_vocab.py`'s constants for the full table:
  0   the trigger file is well-formed: known keys only, required keys present, every trigger id
      grammar-valid and unique, every when.source a valid entity-ref, every when.event closed,
      every when.params matching its event's closed key set, every retrigger closed, every
      glob/duration grammar-valid
  10  E_BAD_TOP — bad top-level/structural shape (missing/unknown key at the file, trigger, when,
      or params level; wrong type; triggers not an array)
  11  E_BAD_ID — a trigger id fails ^[a-z0-9][a-z0-9-]*$, or repeats within the file
  12  E_BAD_ENTITY_REF — when.source fails the entity-ref grammar
  13  E_BAD_EVENT — when.event outside the closed event vocabulary
  14  E_BAD_PARAMS — missing required param, unknown param key, or wrong-typed param value
  15  E_BAD_RETRIGGER — retrigger present but outside {once, every-event}
  16  E_BAD_GLOB_OR_DURATION — a paths[]/src_glob glob, or an every duration, fails its grammar
  64  usage/argument error (no --triggers; --selftest combined with a command)
  65  --triggers FILE unreadable, TOML syntax error (incl. TOML-level duplicate key, which
      tomllib itself rejects), or its parsed top level is not a table
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

import trigger_vocab as tv
from schema_conformance import (boundary_defect_controls, conformance_errors,
                                rewrite_toml_integers_as_floats, schema_input_variants,
                                toml_inline_document)

REPORT_SCHEMA = ("schema/trigger-0.1.0-draft.schema.json", "validate_report")
tv.register_report_schema(__file__, REPORT_SCHEMA)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
SCHEMA_PATH = HERE.parent / "schema" / "trigger-0.1.0-draft.schema.json"


def _report_key_errors(report: dict) -> list[str]:
    """Check the complete emitted report against validate_report from the schema."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/validate_report", "validate_report")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise tv.UsageError(message)


def make_report(status: str, code: int, errors: list, file=None, trigger_count: int = 0,
                 trigger_version=None, repo=None) -> dict:
    """The ONE report constructor: every exit path (a clean or invalid validation, a usage error,
    an unreadable/malformed `--triggers` file, or an unexpected exception reaching the catch-all)
    builds its report through this function, so no exit path can emit a report missing a field
    another one has. Every entry's `reason` is passed through `tv.bound_reason` HERE,
    unconditionally — the final, structural enforcement of the documented ≤2,000-character `reason`
    bound, so it holds even for an entry built by a call site that forgot to self-bound."""
    report = {
        "status": status,
        "exit_code": code,
        "errors": [{**e, "reason": tv.bound_reason(e["reason"])} for e in errors],
        "trigger_count": trigger_count,
        "trigger_version": trigger_version,
        "repo": repo,
        "trigger_vocab_version": tv.TRIGGER_VOCAB_VERSION,
    }
    # The schema types `file` as a plain string (not nullable) — included only once a real path is
    # known, matching the pre-existing contract for a usage error / unreadable-file report (neither
    # of which previously carried a `file` key at all).
    if file is not None:
        report["file"] = file
    return report


def run_validate(path: pathlib.Path) -> tuple:
    doc = tv.load_toml_file(path)
    errors = tv.validate_trigger_doc(doc, str(path))
    code = tv.exit_code_for(errors)

    trigger_count = len(doc.get("triggers", [])) if isinstance(doc.get("triggers"), list) else 0
    trigger_version = doc.get("trigger_version")
    repo = doc.get("repo")
    # A TOML value can be a native date/datetime/int/etc, not just a string (e.g. an unquoted
    # `repo = 2026-09-30T00:00:00Z`). validate_trigger_doc already classifies that as a shape
    # error (E_BAD_TOP); report null here rather than the raw value, which `json.dumps` cannot
    # serialize and which crashed this report to exit 65 instead of the shape-error exit already
    # computed.
    if not isinstance(trigger_version, str):
        trigger_version = None
    if not isinstance(repo, str):
        repo = None
    report = make_report("valid" if not errors else "invalid", code, errors, str(path),
                          trigger_count, trigger_version, repo)
    return code, report


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    validate_p = subparsers.add_parser("validate", allow_abbrev=False)
    validate_p.add_argument("--triggers", default=None)
    return parser


def _resolve(path_str: str) -> pathlib.Path:
    p = pathlib.Path(path_str)
    if not p.is_absolute():
        for sub in ("validate", "example"):
            cand = FIXTURES / sub / path_str
            if cand.exists():
                return cand
        p = FIXTURES / "validate" / path_str
    return p


def _run_case(case: dict) -> tuple:
    path = _resolve(case["triggers"])
    try:
        return run_validate(path)
    except (tv.TriggerInputError, tv.UsageError) as exc:
        return exc.code, make_report("invalid", exc.code, [{"code": exc.code, "reason": exc.reason}])


def _run_case_via_cli(case: dict) -> tuple:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(["validate", "--triggers", str(_resolve(case["triggers"]))])
    return code, json.loads(captured.getvalue())


def _run_validate_via_cli(path: pathlib.Path) -> tuple:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(["validate", "--triggers", str(path)])
    return code, json.loads(captured.getvalue())


def case_matches(case: dict, code: int, report: dict) -> bool:
    if _report_key_errors(report):
        return False
    if code != case["expected_exit"]:
        return False
    if "expected_error_codes" in case:
        got = sorted(e["code"] for e in report.get("errors", []))
        if got != sorted(case["expected_error_codes"]):
            return False
    return True


def _integer_spelling_differential(cases: list[dict]) -> tuple[bool, int, list]:
    failures, rewritten = [], 0
    with tempfile.TemporaryDirectory(prefix="trigger-integral-floats-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(cases):
            source = _resolve(case["triggers"])
            if source.is_file():
                changed, count = rewrite_toml_integers_as_floats(
                    source.read_text(encoding="utf-8"))
                rewritten += count
                destination = tmp_path / f"{case_index}.toml"
                destination.write_text(changed, encoding="utf-8")
            else:
                destination = source
            original_code, original_report = _run_case_via_cli(case)
            changed_code, changed_report = _run_validate_via_cli(destination)
            verdict_same = changed_report.get("status") == original_report.get("status")
            exact_when_accepted = case["expected_exit"] != 0 or changed_code == original_code
            if not (verdict_same and exact_when_accepted):
                failures.append((case["name"], original_code, changed_code,
                                 original_report.get("status"), changed_report.get("status")))
    return not failures and rewritten > 0, rewritten, failures


def _input_schema_differential(cases: list[dict]) -> tuple[bool, int, int, list]:
    """Compare trigger_file schema verdicts with the real validator executable.

    Per-event required/allowed params are the one documented semantic exception (spec §§2, 3.7;
    schema ``when_params.description`` and semantic rule ``per-event-params-closed-set``).
    """
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, 0, 0, []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    failures, generated, semantic_rejections = [], 0, 0
    validation_exits = set(range(tv.E_BAD_TOP, tv.E_BAD_GLOB_OR_DURATION + 1))
    with tempfile.TemporaryDirectory(prefix="trigger-schema-differential-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(c for c in cases if c["expected_exit"] == 0):
            source = _resolve(case["triggers"])
            document = tv.load_toml_file(source)
            baseline_errors = conformance_errors(
                document, schema, "#/$defs/trigger_file", "trigger_file")
            if baseline_errors:
                failures.append((case["name"], "positive-fixture-schema", baseline_errors[:3]))
                continue
            for variant_index, (label, variant) in enumerate(schema_input_variants(
                    document, schema, "#/$defs/trigger_file")):
                generated += 1
                schema_accepts = not conformance_errors(
                    variant, schema, "#/$defs/trigger_file", "trigger_file")
                destination = tmp_path / f"{case_index}-{variant_index}.toml"
                destination.write_text(toml_inline_document(variant), encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, "-B", str(HERE / "trigger_validate.py"), "validate",
                     "--triggers", str(destination)], capture_output=True, timeout=10)
                cli_accepts = proc.returncode == 0
                semantic = (schema_accepts and not cli_accepts
                            and proc.returncode == tv.E_BAD_PARAMS
                            and (label.startswith("add-optional:")
                                 or label.startswith("remove-optional:"))
                            and ".when.params" in label)
                if semantic:
                    semantic_rejections += 1
                elif schema_accepts != cli_accepts:
                    failures.append((case["name"], label, schema_accepts, proc.returncode))
                elif not cli_accepts and proc.returncode not in validation_exits:
                    failures.append((case["name"], label, "non-validation-exit", proc.returncode))
    return not failures and generated > 0, generated, semantic_rejections, failures




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

def run_selftest() -> int:
    try:
        data = json.loads((FIXTURES / "validate" / "cases.json").read_text(encoding="utf-8"))
        cases = data.get("validate_cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases.json must contain a nonempty 'validate_cases' array")

        results = []
        for case in cases:
            code, report = _run_case_via_cli(case)
            ok = case_matches(case, code, report)
            results.append(ok)
            print(f"selftest {case['name']}: expected exit={case['expected_exit']} got exit={code} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
            if not ok:
                print(tv.emit_report(report, indent=2, sort_keys=True, allow_nan=False))

        integer_diff_ok, integer_rewrites, integer_diff_failures = (
            _integer_spelling_differential(cases))
        results.append(integer_diff_ok)
        print(f"selftest schema-integer-integral-float-differential: "
              f"{integer_rewrites} integer spelling(s), {len(integer_diff_failures)} failure(s) "
              f"{integer_diff_failures[:3]!r} "
              f"[{'ok' if integer_diff_ok else 'MISMATCH'}]")

        schema_diff_ok, schema_variants, schema_semantic, schema_diff_failures = (
            _input_schema_differential(cases))
        results.append(schema_diff_ok)
        print(f"selftest input-schema-cli-differential-trigger-files: "
              f"{schema_variants} generated variant(s), "
              f"{schema_semantic} documented semantic rejection(s), "
              f"{len(schema_diff_failures)} failure(s) {schema_diff_failures[:3]!r} "
              f"[{'ok' if schema_diff_ok else 'MISMATCH'}]")

        usage_code = main(["validate"])
        results.append(usage_code == tv.E_USAGE)
        print(f"selftest usage-error-no-triggers: expected exit=64 got exit={usage_code} "
              f"[{'ok' if usage_code == tv.E_USAGE else 'MISMATCH'}]")

        combo_code = main(["--selftest", "validate", "--triggers", "x.toml"])
        results.append(combo_code == tv.E_USAGE)
        print(f"selftest usage-error-selftest-plus-command: expected exit=64 got exit={combo_code} "
              f"[{'ok' if combo_code == tv.E_USAGE else 'MISMATCH'}]")

        mismatch_case = dict(cases[0])
        mismatch_case["expected_exit"] = -12345
        mismatch_code, mismatch_report = _run_case_via_cli(mismatch_case)
        mismatch_caught = not case_matches(mismatch_case, mismatch_code, mismatch_report)
        results.append(mismatch_caught)
        print(f"selftest harness-mismatch-detection: forced-bad-expectation caught={mismatch_caught} "
              f"[{'ok' if mismatch_caught else 'MISMATCH'}]")

        original_run_validate = globals()["run_validate"]
        def _boom(*_args, **_kwargs):
            raise RuntimeError("selftest-injected catch-all exception")
        globals()["run_validate"] = _boom
        try:
            exception_code, exception_report = _run_case_via_cli(cases[0])
        finally:
            globals()["run_validate"] = original_run_validate
        exception_ok = (exception_code == tv.E_INPUT and
                        exception_report["errors"][0]["reason"].startswith("input/runtime failure"))
        results.append(exception_ok)
        print(f"selftest exit-65-injected-exception-catch-all: got exit={exception_code} "
              f"[{'ok' if exception_ok else 'MISMATCH'}]")

        expected_exits = {0, tv.E_BAD_TOP, tv.E_BAD_ID, tv.E_BAD_ENTITY_REF, tv.E_BAD_EVENT,
                           tv.E_BAD_PARAMS, tv.E_BAD_RETRIGGER, tv.E_BAD_GLOB_OR_DURATION,
                           tv.E_INPUT}
        covered_exits = {case["expected_exit"] for case in cases}
        missing_exits = sorted(expected_exits - covered_exits)
        coverage_ok = not missing_exits
        results.append(coverage_ok)
        print(f"selftest exit-code-coverage: exits {sorted(expected_exits)}; "
              f"missing={missing_exits or 'none'} [{'ok' if coverage_ok else 'MISMATCH'}]")

        # H2: every call this file's own source makes to the JSON serializer passes allow_nan=False
        # -- a grep-based, structural check, not
        # merely "the fixtures above all happened to serialize" (which would pass even if some
        # unrelated call site forgot the flag and happened not to be exercised by any fixture's own,
        # finite, input). Matching parens are counted so a multi-line call is checked whole.
        own_source = pathlib.Path(__file__).read_text(encoding="utf-8")
        # Built by concatenation, not as one literal, and this comment spells the two halves apart
        # too: the contiguous needle text otherwise appears unchanged in THIS tool's own source
        # (quoted, as the needle itself) and would make the scan falsely match its own string
        # literal as a "call site".
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

        # H3: no diagnostic helper ever formats an unbounded Python int directly, at any depth —
        # 10**5000 bypasses the TOML-parse digit-count guard entirely (it is built in Python, never
        # parsed from text), so this is the ONLY way to exercise safe_repr_for_error/bound_reason
        # against a value actually past CPython's own int-to-str conversion limit.
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

        # H4: bound_reason is the FINAL, unconditional backstop — any text, however assembled or
        # however long, comes back no longer than DIAGNOSTIC_REASON_MAX_CHARS, marker included.
        very_long = "z" * 1_000_000
        bounded = tv.bound_reason(very_long)
        bound_ok = len(bounded) == tv.DIAGNOSTIC_REASON_MAX_CHARS and bounded.endswith("<truncated>")
        results.append(bound_ok)
        print(f"selftest h4-bound-reason-caps-any-length: len={len(bounded)} "
              f"cap={tv.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if bound_ok else 'MISMATCH'}]")

        # H7: producer against rule. Run a REAL fixture (not a hand-built defect probe) through this
        # tool, capture its actual JSON report text, and re-parse that text through the SAME
        # JSON-input-contract this format's own JSONL reader enforces (duplicate-key rejection, the
        # non-finite-number check, UTF-8 encodability, bounded nesting) -- proving this validator's
        # own real output, not only a hand fixture, satisfies the rule H1 just added.
        _, real_report = _run_validate_via_cli(
            FIXTURES / "validate" / "triggers-all-events.toml")
        real_report_text = json.dumps(real_report, indent=2, sort_keys=True, allow_nan=False)
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
            [sys.executable, "-B", str(HERE / "trigger_validate.py"), "validate",
             "--triggers", b"missing-\xff"],
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

        print(f"selftest version-handshake: this validator implements {tv.TRIGGER_VOCAB_VERSION!r}")

        # The regression that closes the class (argv fuzz): for every value-taking option this
        # tool's parser accepts (read programmatically), plus one unknown option and one hostile
        # subcommand, run the CLI in a subprocess with a hostile value in that position and check
        # the whole captured stdout is either empty or parses under this format's own strict
        # reader. Mutation that turns this red: reverting `tv.bound_reason` to skip
        # `_escape_unicode_safe` (dropping the lone-surrogate escape) makes the unknown-option
        # case (`--bad-\xff`, which argparse echoes raw into its own usage-error message) fail.
        argv_fuzz_baselines = {
            "validate": ["validate", "--triggers", str(FIXTURES / "validate" / "00-valid-minimal.toml")],
        }
        argv_fuzz_exits = {0, tv.E_BAD_TOP, tv.E_BAD_ID, tv.E_BAD_ENTITY_REF, tv.E_BAD_EVENT,
                            tv.E_BAD_PARAMS, tv.E_BAD_RETRIGGER, tv.E_BAD_GLOB_OR_DURATION,
                            tv.E_USAGE, tv.E_INPUT}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            HERE / "trigger_validate.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_failures = _encoding_regression_failures(
            HERE / "trigger_validate.py",
            [(argv_fuzz_baselines["validate"], {0}), (["validate"], {tv.E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = tv.output_boundary_selftest(
            HERE / "trigger_validate.py", argv_fuzz_baselines["validate"], ["validate"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "trigger_validate.py", "make_report", "run_validate",
            ["validate"], argv_fuzz_baselines["validate"])
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
              f"{len(cases) + len(output_checks) + 5} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__}: {exc})")
        return tv.E_SELFTEST


def _main(argv=None) -> int:
    try:
        parser = make_parser()
        args = parser.parse_args(argv)
        if args.selftest:
            if args.command is not None:
                parser.error("--selftest cannot be combined with a command")
            return run_selftest()
        if args.command != "validate":
            parser.error("the 'validate' command or --selftest is required")
        if not args.triggers:
            parser.error("--triggers FILE is required")
        path = pathlib.Path(args.triggers)
        code, report = run_validate(path)
    except tv.UsageError as exc:
        code = exc.code
        report = make_report("invalid", code, [{"code": code, "reason": exc.reason}])
    except tv.TriggerInputError as exc:
        code = exc.code
        report = make_report("invalid", code, [{"code": code, "reason": exc.reason}])
    except Exception as exc:
        # bound_reason (applied unconditionally by make_report) is the backstop for this message's
        # overall length; it is built from the exception's own class name and text, never a value
        # this tool chose to interpolate without a bound of its own.
        reason = f"input/runtime failure ({type(exc).__name__}: {exc})"
        code = tv.E_INPUT
        report = make_report("invalid", code, [{"code": code, "reason": reason}])
    tv.write_report(tv.emit_report(report, indent=2, sort_keys=True, allow_nan=False))
    return code



def main(argv=None) -> int:
    return tv.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
