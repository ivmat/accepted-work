#!/usr/bin/env python3
"""relation_validate.py — first cut, stdlib-only validator for the Relation TOML edges file,
caller-supplied via one or more `--relations FILE` arguments. Validates the merged
set: each file's own shape/entity-ref-grammar/relation-vocabulary/propagates check, plus the
cross-edge checks that need every edge resolved against one `--self` value: self-loop rejection,
duplicate-identical-edge rejection, and unresolved-`@self` detection.

This is a SHAPE + ENTITY-REF-GRAMMAR + CLOSED-VOCABULARY validator. It does not run the impact
query (a separate tool, not included in this export).

Shared logic (`load_toml_file`, `parse_entity_ref`, `validate_one_doc`, `validate_merged`, the
closed relation vocabulary + propagation table) lives in `relation_vocab.py`, imported here.

Usage:
  python3 relation_validate.py validate --relations FILE [--relations FILE ...] [--self REPO]
  python3 relation_validate.py --selftest

Exit codes (lowest-numbered class wins when several fire; 0 iff no error fired) — see
`relation_vocab.py`'s constants for the full table:
  0   every merged relations file is well-formed and every cross-edge invariant holds
  10  E_BAD_TOP — a top-level/edge shape problem: not a table, unknown key, a required key
      missing (`relation_version`/`repo`/`edges` at top level; `from`/`relation`/`to` on an
      edge), a wrong-typed field (`directed` not bool, `note`/`source` not a string, `edges` not
      an array, an edge not a table)
  11  E_BAD_ENTITY_REF — `from`/`to`/`source` fails the entity-ref grammar
  12  E_UNKNOWN_RELATION — `relation` is outside the closed relation set and does not match
      `^x-[a-z0-9-]+$`
  13  E_BAD_PROPAGATES — `propagates` is set on a known (non-`x-`) relation, or its value is
      outside `{"to-from", "from-to", "none"}`
  14  E_SELF_LOOP — an edge's `from` and `to` denote the same entity
  15  E_DUP_EDGE — two edges (within one file or across `--relations` files) have identical
      resolved from/relation/to/directed/propagates
  16  E_UNRESOLVED_SELF — an edge uses `@self` but no `--self REPO` was given
  64  usage/argument error (no `--relations`; `--selftest` combined with a command)
  65  a `--relations FILE` is unreadable, has a TOML syntax error (incl. a TOML-level duplicate
      key, which `tomllib` itself rejects), or its parsed top level is not a table
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

Stdlib only: argparse, json, pathlib, sys. (`relation_vocab.py` adds `tomllib`, `re`, `collections`.)
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

import relation_vocab as rv
from schema_conformance import (boundary_defect_controls, conformance_errors,
                                rewrite_toml_integers_as_floats, schema_input_variants,
                                toml_inline_document)

REPORT_SCHEMA = ("schema/relation-0.1.0-draft.schema.json", "validate_report")
rv.register_report_schema(__file__, REPORT_SCHEMA)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"


def _report_key_errors(report: dict) -> list[str]:
    schema = json.loads((HERE.parent / "schema" /
                         "relation-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/validate_report", "validate_report")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise rv.UsageError(message)


def make_report(status: str, code: int, errors: list, files=None, edge_count: int = 0,
                 relation_version=None, repo=None, self_repo=None) -> dict:
    """The ONE report constructor: every exit path (a clean or invalid validation, a usage error, an
    unreadable/malformed `--relations` file, or an unexpected exception reaching the catch-all)
    builds its report through this function, so no exit path can emit a report missing a field
    another one has. Every entry's `reason` is passed through `rv.bound_reason` HERE,
    unconditionally — the final, structural enforcement of the documented ≤2,000-character `reason`
    bound, so it holds even for an entry built by a call site that forgot to self-bound. Every
    `files[]` entry is passed through `rv.bound_path_preview` HERE too, unconditionally — the same
    rule `impact_validate.py`'s own `make_report` applies, for the same reason:
    a `--relations` argument is echoed unchanged on the success path, including one that is not
    valid Unicode at all."""
    return {
        "status": status,
        "exit_code": code,
        "errors": rv.finalize_errors(errors),
        "files": [rv.bound_path_preview(f) for f in files] if files is not None else [],
        "edge_count": edge_count,
        "relation_version": relation_version,
        "repo": repo,
        "self": self_repo,
        "relation_vocab_version": rv.RELATION_VOCAB_VERSION,
    }


def run_validate(relations_paths: list, self_repo) -> tuple:
    if self_repo is not None and not rv.is_valid_repo_id(self_repo):
        # Caught here, before any edge processing, rather than left to surface as an uncaught
        # EntityRefError out of resolve_entity_ref (for example a --self value
        # ending in a newline, or any other malformed --self value): a bad CLI argument is a usage
        # error, not a per-edge validation error.
        raise rv.UsageError(f"--self {rv.safe_repr_for_error(self_repo)} is not a valid <repo> id")

    docs = []
    for path in relations_paths:
        doc = rv.load_toml_file(path)
        docs.append((str(path), doc))

    errors, resolved_edges = rv.validate_merged(docs, self_repo)
    code = rv.exit_code_for(errors)

    edge_count = sum(len(doc.get("edges", [])) for _, doc in docs
                      if isinstance(doc, dict) and isinstance(doc.get("edges"), list))
    repo = docs[-1][1].get("repo") if docs and isinstance(docs[-1][1], dict) else None
    relation_version = docs[-1][1].get("relation_version") if docs and isinstance(docs[-1][1], dict) else None
    # A TOML value can be a native date/datetime/int/etc, not just a string (e.g. an unquoted
    # `repo = 2026-09-30T00:00:00Z`). The shape check above already classifies that as an error
    # (E_BAD_TOP; not a string, so `is_nonblank_str` is False); report null here rather than the
    # raw value, which `json.dumps` cannot serialize and which crashed this report to exit 65
    # instead of the shape-error exit already computed.
    if not isinstance(repo, str):
        repo = None
    if not isinstance(relation_version, str):
        relation_version = None

    report = make_report("valid" if not errors else "invalid", code, errors,
                          [str(p) for p in relations_paths], edge_count, relation_version, repo,
                          self_repo)
    return code, report


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    validate_p = subparsers.add_parser("validate", allow_abbrev=False)
    validate_p.add_argument("--relations", action="append", default=None)
    validate_p.add_argument("--self", dest="self_repo", default=None)
    return parser


def _resolve(path_str: str) -> pathlib.Path:
    p = pathlib.Path(path_str)
    if not p.is_absolute():
        candidate = FIXTURES / "validate" / path_str
        if not candidate.exists():
            candidate = FIXTURES / "example" / path_str
        p = candidate
    return p


def _run_case(case: dict) -> tuple:
    paths = [_resolve(p) for p in case["relations"]]
    self_repo = case.get("self")
    try:
        return run_validate(paths, self_repo)
    except (rv.RelationInputError, rv.UsageError) as exc:
        return exc.code, make_report("invalid", exc.code,
                                      [{"code": exc.code, "reason": exc.reason}])


def _run_case_via_cli(case: dict) -> tuple:
    argv = ["validate"]
    for path in case["relations"]:
        argv.extend(("--relations", str(_resolve(path))))
    if case.get("self") is not None:
        argv.extend(("--self", case["self"]))
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    return code, json.loads(captured.getvalue())


def _run_validate_via_cli(paths: list[pathlib.Path], self_repo=None) -> tuple:
    argv = ["validate"]
    for path in paths:
        argv.extend(("--relations", str(path)))
    if self_repo is not None:
        argv.extend(("--self", self_repo))
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    return code, json.loads(captured.getvalue())


def case_matches(case: dict, code: int, report: dict) -> bool:
    if code != case["expected_exit"]:
        return False
    if "expected_error_codes" in case:
        got = sorted(e["code"] for e in report.get("errors", []))
        if got != sorted(case["expected_error_codes"]):
            return False
    if "expected_edge_count" in case:
        if report.get("edge_count") != case["expected_edge_count"]:
            return False
    return True


def _integer_spelling_differential(cases: list[dict]) -> tuple[bool, int, list]:
    """Run every fixture case again with all TOML integers written as integral floats."""
    failures, rewritten = [], 0
    with tempfile.TemporaryDirectory(prefix="relation-integral-floats-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(cases):
            changed_paths = []
            for path_index, path_str in enumerate(case["relations"]):
                source = _resolve(path_str)
                if not source.is_file():
                    changed_paths.append(source)
                    continue
                changed, count = rewrite_toml_integers_as_floats(
                    source.read_text(encoding="utf-8"))
                rewritten += count
                destination = tmp_path / f"{case_index}-{path_index}.toml"
                destination.write_text(changed, encoding="utf-8")
                changed_paths.append(destination)
            original_code, original_report = _run_case_via_cli(case)
            changed_code, changed_report = _run_validate_via_cli(
                changed_paths, case.get("self"))
            verdict_same = changed_report.get("status") == original_report.get("status")
            exact_when_accepted = case["expected_exit"] != 0 or changed_code == original_code
            if not (verdict_same and exact_when_accepted):
                failures.append((case["name"], original_code, changed_code,
                                 original_report.get("status"), changed_report.get("status")))
    return not failures, rewritten, failures


def _input_schema_differential(cases: list[dict]) -> tuple[bool, int, int, list]:
    """Compare the published relations_file schema with the real validator executable.

    The only admitted schema/CLI disagreement is the spec's cross-field rule that ``propagates``
    is forbidden on a closed relation (spec §2.1/§3; schema ``edge.description`` and
    ``x-semantic-only-rules[propagates-only-on-x-token]``).
    """
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, 0, 0, []
    schema = json.loads((HERE.parent / "schema" /
                         "relation-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    failures, generated, semantic_rejections = [], 0, 0
    validation_exits = set(range(rv.E_BAD_TOP, rv.E_UNRESOLVED_SELF + 1))
    with tempfile.TemporaryDirectory(prefix="relation-schema-differential-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(c for c in cases if c["expected_exit"] == 0):
            for file_index, path_str in enumerate(case["relations"]):
                source = _resolve(path_str)
                document = rv.load_toml_file(source)
                baseline_errors = conformance_errors(
                    document, schema, "#/$defs/relations_file", "relations_file")
                if baseline_errors:
                    failures.append((case["name"], "positive-fixture-schema", baseline_errors[:3]))
                    continue
                for variant_index, (label, variant) in enumerate(schema_input_variants(
                        document, schema, "#/$defs/relations_file")):
                    generated += 1
                    schema_accepts = not conformance_errors(
                        variant, schema, "#/$defs/relations_file", "relations_file")
                    destination = tmp_path / f"{case_index}-{file_index}-{variant_index}.toml"
                    destination.write_text(toml_inline_document(variant), encoding="utf-8")
                    argv = [sys.executable, "-B", str(HERE / "relation_validate.py"), "validate"]
                    for other_index, other in enumerate(case["relations"]):
                        argv.extend(("--relations", str(destination if other_index == file_index
                                                       else _resolve(other))))
                    if case.get("self") is not None:
                        argv.extend(("--self", case["self"]))
                    proc = subprocess.run(argv, capture_output=True, timeout=10)
                    cli_accepts = proc.returncode == 0
                    semantic = (schema_accepts and not cli_accepts
                                and proc.returncode == rv.E_BAD_PROPAGATES
                                and label.startswith("add-optional:")
                                and label.endswith(".propagates:string"))
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
        obj = json.loads(text, object_pairs_hook=rv.unique_pairs, parse_constant=rv.reject_constant)
    except (ValueError, RecursionError) as exc:
        return False, f"stdout does not parse as strict JSON: {type(exc).__name__}: {exc}"
    defect = rv.find_json_input_defect(obj)
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
                print(rv.emit_report(report, indent=2, sort_keys=True, allow_nan=False))

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
        print(f"selftest input-schema-cli-differential: {schema_variants} generated variant(s), "
              f"{schema_semantic} documented semantic rejection(s), "
              f"{len(schema_diff_failures)} failure(s) {schema_diff_failures[:3]!r} "
              f"[{'ok' if schema_diff_ok else 'MISMATCH'}]")

        usage_code = main(["validate"])
        results.append(usage_code == rv.E_USAGE)
        print(f"selftest usage-error-no-relations: expected exit=64 got exit={usage_code} "
              f"[{'ok' if usage_code == rv.E_USAGE else 'MISMATCH'}]")

        combo_code = main(["--selftest", "validate", "--relations", "x.toml"])
        results.append(combo_code == rv.E_USAGE)
        print(f"selftest usage-error-selftest-plus-command: expected exit=64 got exit={combo_code} "
              f"[{'ok' if combo_code == rv.E_USAGE else 'MISMATCH'}]")

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
        exception_ok = (exception_code == rv.E_INPUT and
                        exception_report["errors"][0]["reason"].startswith("input/runtime failure"))
        results.append(exception_ok)
        print(f"selftest exit-65-injected-exception-catch-all: got exit={exception_code} "
              f"[{'ok' if exception_ok else 'MISMATCH'}]")

        expected_exits = {0, rv.E_BAD_TOP, rv.E_BAD_ENTITY_REF, rv.E_UNKNOWN_RELATION,
                           rv.E_BAD_PROPAGATES, rv.E_SELF_LOOP, rv.E_DUP_EDGE, rv.E_UNRESOLVED_SELF,
                           rv.E_INPUT}
        covered_exits = {case["expected_exit"] for case in cases}
        missing_exits = sorted(expected_exits - covered_exits)
        coverage_ok = not missing_exits
        results.append(coverage_ok)
        print(f"selftest exit-code-coverage: exits {sorted(expected_exits)}; "
              f"missing={missing_exits or 'none'} [{'ok' if coverage_ok else 'MISMATCH'}]")

        # H2: every call this file's own source makes to the JSON serializer passes allow_nan=False
        # -- a grep-based, structural check. Built
        # by concatenation (and spelled apart in this comment too), so the needle does not appear
        # unchanged in this file's own source and falsely match itself.
        own_source = pathlib.Path(__file__).read_text(encoding="utf-8")
        _needle = "json" + ".dumps("
        allow_nan_ok = True
        dumps_calls_seen = 0
        # Scanned over BOTH this tool's own source AND relation_vocab.py's source: since the structural
        # fold, every report this tool prints routes through `rv.emit_report` (one funnel in
        # relation_vocab.py that forces `allow_nan=False` regardless of what a caller passes), so the one
        # remaining `json.dumps(` call site that matters is INSIDE that function -- its own call
        # text carries `**dumps_kwargs`, not a literal `allow_nan=False`, so it is recognized by
        # name instead of by substring.
        for _source in (own_source, pathlib.Path(rv.__file__).read_text(encoding="utf-8")):
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
        # Behavioral check, not only textual: `rv.emit_report` must force `allow_nan=False`
        # even when a caller explicitly asks for the opposite -- a non-finite float must still
        # raise, never silently serialize as the non-standard `NaN`/`Infinity` token. Disabling the
        # forced `allow_nan=False` inside `emit_report` (passing `**dumps_kwargs` to `json.dumps`
        # unmodified) makes this mutation red.
        try:
            rv.emit_report({"n": float("inf")}, allow_nan=True)
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
            scalar_repr = rv.safe_repr_for_error(huge_int)
            nested_repr = rv.safe_repr_for_error({"a": [huge_int, 1, {"b": huge_int}]})
            huge_int_ok = ("digit integer" in scalar_repr and "digit integer" in nested_repr
                           and len(scalar_repr) <= rv._SAFE_REPR_MAX_CHARS
                           and len(nested_repr) <= rv._SAFE_REPR_MAX_CHARS)
        except Exception:
            huge_int_ok = False
        results.append(huge_int_ok)
        print(f"selftest h3-unbounded-int-never-formatted-directly: [{'ok' if huge_int_ok else 'MISMATCH'}]")

        # H4: bound_reason is the FINAL, unconditional backstop -- any text, however assembled or
        # however long, comes back no longer than DIAGNOSTIC_REASON_MAX_CHARS, marker included.
        very_long = "z" * 1_000_000
        bounded = rv.bound_reason(very_long)
        bound_ok = len(bounded) == rv.DIAGNOSTIC_REASON_MAX_CHARS and bounded.endswith("<truncated>")
        results.append(bound_ok)
        print(f"selftest h4-bound-reason-caps-any-length: len={len(bounded)} "
              f"cap={rv.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if bound_ok else 'MISMATCH'}]")

        # H7: producer against rule. Run a REAL fixture (not a hand-built defect probe) through this
        # tool and re-parse its actual JSON report text for a non-finite number -- proving this
        # validator's own real output, not only a hand fixture, satisfies the rule H1 just added
        # (`allow_nan=False` on serialization is the first line of defense; this re-walks the
        # deserialized structure as an independent check of the same property).
        _, real_report = _run_validate_via_cli([_resolve("00-valid-minimal.toml")], None)
        real_report_text = json.dumps(real_report, indent=2, sort_keys=True, allow_nan=False)
        reparsed = json.loads(real_report_text)
        producer_defect = rv._find_non_finite(reparsed)
        producer_ok = producer_defect is None
        results.append(producer_ok)
        print(f"selftest h7-producer-output-satisfies-its-own-input-contract: "
              f"defect={producer_defect!r} [{'ok' if producer_ok else 'MISMATCH'}]")

        # Regression: a `--relations` path that is not valid Unicode at all (the OS gives
        # Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a lone
        # UTF-16 surrogate in the decoded argv string) must still produce a report that re-parses
        # strictly, with the exit code unchanged and the reason within the documented cap. Mutation
        # that turns this red: reverting `rv.bound_path_preview` to `s = str(path)` with no escaping
        # (the pre-fix code) makes `surrogate_defect` report a lone-surrogate string instead of None.
        surrogate_proc = subprocess.run(
            [sys.executable, "-B", str(HERE / "relation_validate.py"), "validate",
             "--relations", b"missing-\xff"],
            capture_output=True,
        )
        surrogate_report = json.loads(surrogate_proc.stdout)
        surrogate_defect = rv._find_non_finite(surrogate_report)
        surrogate_reason = surrogate_report["errors"][0]["reason"]
        surrogate_utf8_ok = True
        try:
            surrogate_reason.encode("utf-8")
        except UnicodeEncodeError:
            surrogate_utf8_ok = False
        surrogate_ok = (surrogate_proc.returncode == rv.E_INPUT and surrogate_defect is None
                        and surrogate_utf8_ok
                        and len(surrogate_reason) <= rv.DIAGNOSTIC_REASON_MAX_CHARS)
        results.append(surrogate_ok)
        print(f"selftest lone-surrogate-relations-path-escaped-in-report: "
              f"exit={surrogate_proc.returncode} reason={surrogate_reason!r} "
              f"[{'ok' if surrogate_ok else 'MISMATCH'}]")

        print(f"selftest version-handshake: this validator implements {rv.RELATION_VOCAB_VERSION!r}")

        # The regression that closes the class (argv fuzz).
        argv_fuzz_baselines = {
            "validate": ["validate", "--relations", str(FIXTURES / "validate" / "00-valid-minimal.toml")],
        }
        argv_fuzz_exits = {0, rv.E_BAD_TOP, rv.E_BAD_ENTITY_REF, rv.E_UNKNOWN_RELATION,
                            rv.E_BAD_PROPAGATES, rv.E_SELF_LOOP, rv.E_DUP_EDGE,
                            rv.E_UNRESOLVED_SELF, rv.E_USAGE, rv.E_INPUT}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            HERE / "relation_validate.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_failures = _encoding_regression_failures(
            HERE / "relation_validate.py",
            [(argv_fuzz_baselines["validate"], {0}), (["validate"], {rv.E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = rv.output_boundary_selftest(
            HERE / "relation_validate.py", argv_fuzz_baselines["validate"], ["validate"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "relation_validate.py", "make_report", "run_validate",
            ["validate"], argv_fuzz_baselines["validate"])
        for name, (ok, detail) in boundary_controls.items():
            results.append(ok)
            print(f"selftest report-boundary-defect-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
        boundary_controls_ok = all(ok for ok, _ in boundary_controls.values())
        print(f"selftest report-schema-conformance: real boundary controls="
              f"{sorted(boundary_controls)} "
              f"[{'ok' if boundary_controls_ok else 'MISMATCH'}]")

        status = 0 if results and all(results) and boundary_controls_ok else rv.E_SELFTEST
        print(f"SELFTEST {'PASS' if status == 0 else 'FAIL'}: "
              f"{len(cases) + len(output_checks) + 5} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__}: {exc})")
        return rv.E_SELFTEST


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
        if not args.relations:
            parser.error("at least one --relations FILE is required")
        paths = [pathlib.Path(p) for p in args.relations]
        code, report = run_validate(paths, args.self_repo)
    except rv.UsageError as exc:
        code = exc.code
        report = make_report("invalid", code, [{"code": code, "reason": exc.reason}])
    except rv.RelationInputError as exc:
        code = exc.code
        report = make_report("invalid", code, [{"code": code, "reason": exc.reason}])
    except Exception as exc:
        # bound_reason (applied unconditionally by make_report) is the backstop for this
        # message's overall length.
        reason = f"input/runtime failure ({type(exc).__name__}: {exc})"
        code = rv.E_INPUT
        report = make_report("invalid", code, [{"code": code, "reason": reason}])
    rv.write_report(rv.emit_report(report, indent=2, sort_keys=True, allow_nan=False))
    return code



def main(argv=None) -> int:
    return rv.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
