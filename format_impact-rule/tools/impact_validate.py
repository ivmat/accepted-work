#!/usr/bin/env python3
"""impact_validate.py — first cut, stdlib-only validator for the impact-rule TOML rules file.
Validates ONE OR MORE `--rules FILE` arguments as a single merged set: each file's own
shape/enum/glob-grammar, plus rule-`id` uniqueness across all files together (a later file may not
redefine an earlier rule id).

This is a SHAPE + CLOSED-VOCABULARY + GLOB-GRAMMAR validator. It does not evaluate any rule against
changed paths or events (that is `impact_evaluate.py`), and it never resolves the `inherit` field to
an actual file — the only inheritance mechanism this first cut has is passing multiple `--rules
FILE` arguments, in order.

Shared logic (`load_toml_file`, `validate_one_doc`, `validate_merged`, the closed vocabularies, the
glob-grammar checker) lives in `impact_rules.py`, imported here and by `impact_evaluate.py`.

Usage:
  python3 impact_validate.py validate --rules FILE [--rules FILE ...]
  python3 impact_validate.py --selftest

Exit codes (lowest-numbered class wins when several fire; 0 iff no error fired) — see
`impact_rules.py`'s constants for the full table:
  0   every merged rules file is well-formed: known keys only, required keys present, every enum
      value closed, a valid `<repo>` id (own repo, and any glob's cross-repo prefix — own-repo and
      cross-repo prefix ids both match `^[a-z0-9][a-z0-9_-]*$`, 1-64 characters, no control
      character), every `when.changed[]` glob grammar-valid (including the size limits below), a
      clean (control-character-free) rule id and `must_update[].target`, every `urgency`/
      `derive[]` entry closed, no duplicate rule id within or across files
  10  E_MISSING_KEY — a required key is absent (top level, a rule, `when`, `decided_by`, or a
      `must_update` element)
  11  E_UNKNOWN_KEY — a key present that is not in the closed schema for its block
  12  E_BAD_TYPE — a present key has the wrong shape (not the required string/array/table; an array
      required to be non-empty is empty; the top-level `repo`, a rule `id`, or a `must_update[].
      target` is blank, not a string, or is a valid string but not a valid `<repo>` id / contains a
      control character, as applicable)
  13  E_BAD_ENUM — `relation` / `must_update[].action` / `decided_by.detect` / `decided_by.fix` /
      `importance` / `retrigger` / `when.event` outside its closed set
  14  E_BAD_GLOB — a `when.changed[]` entry fails glob grammar (absolute path, `.`/`..` segment,
      malformed `**`, disallowed character, malformed cross-repo prefix, or exceeds the
      1024-character / 64-segment / 16-'*'-per-segment size limits)
  15  E_BAD_WHEN — `when` has both `changed` and `event`, or neither
  16  E_DUP_RULE_ID — a rule `id` repeats, within one file or across `--rules` files
  17  E_BAD_URGENCY — `urgency` is not a closed literal and does not match `within:<Nd|Nh|Nm>`,
      or the duration exceeds the 36500-day maximum
  18  E_BAD_DERIVE — a `derive[]` entry is outside the closed deriver list; a table parameter is
      unknown for its deriver, wrong-typed, or out of range; or a parameter value is not safely
      JSON-representable / within the signed-64-bit integer bound
  64  usage/argument error (no `--rules` given; `--selftest` combined with a command)
  65  a `--rules FILE` is unreadable, has a TOML syntax error (incl. a TOML-level duplicate key,
      which `tomllib` itself rejects), or its parsed top level is not a table
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

Stdlib only: argparse, json, pathlib, sys. (`impact_rules.py` adds `tomllib`, `datetime`, `re`.)
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import impact_rules as ir
from schema_conformance import (boundary_defect_controls, conformance_errors,
                                rewrite_toml_integers_as_floats, schema_input_variants,
                                toml_inline_document)

REPORT_SCHEMA = ("schema/impact-rule-0.1.0-draft.schema.json", "validate_report")
ir.register_report_schema(__file__, REPORT_SCHEMA)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"


def _report_key_errors(report: dict) -> list[str]:
    schema = json.loads((HERE.parent / "schema" /
                         "impact-rule-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/validate_report", "validate_report")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise ir.UsageError(message)


def make_report(status: str, code: int, errors: list, files=None, rule_count: int = 0,
                 impact_version=None, repo=None) -> dict:
    """The ONE report constructor: every exit path this tool has — a clean or invalid validation,
    a usage error, an unreadable/malformed `--rules` file, or an UNEXPECTED exception caught by the
    catch-all in `main()` — builds its report through this function, so no exit path can emit a
    report missing a field another one has (the same discipline `impact_evaluate.py::make_report`
    uses). Every entry's `reason` is passed through `ir.bound_reason` HERE, unconditionally — the
    final, structural enforcement of the documented ≤2,000-character `reason` bound, so it holds
    even for an entry built by a call site that forgot to self-bound, not only for ones that already
    used `ir.safe_repr_for_error`/`ir.bound_path_preview`. Every `files[]` entry is passed through
    `ir.bound_path_preview` HERE too, unconditionally — a `--rules` argument is echoed here unchanged
    on every exit path, including one that is not valid Unicode at all (a POSIX byte with no UTF-8
    interpretation, decoded by `surrogateescape` into a lone UTF-16 surrogate), and every call site
    above already builds `files` from `str(path)`, never from an already-escaped value."""
    return {
        "status": status,
        "exit_code": code,
        "errors": [{**e, "reason": ir.bound_reason(e["reason"])} for e in errors],
        "files": [ir.bound_path_preview(f) for f in files] if files is not None else [],
        "rule_count": rule_count,
        "impact_version": impact_version,
        "repo": repo,
        "impact_rules_version": ir.IMPACT_RULES_VERSION,
    }


def run_validate(rules_paths: list) -> tuple:
    docs = []
    for path in rules_paths:
        doc = ir.load_toml_file(path)
        docs.append((str(path), doc))

    errors = ir.validate_merged(docs)
    code = ir.exit_code_for(errors)

    rule_count = sum(len(doc.get("rules", [])) for _, doc in docs if isinstance(doc.get("rules"), list))
    repo = docs[-1][1].get("repo") if docs else None
    impact_version = docs[-1][1].get("impact_version") if docs else None
    # A TOML value can be a native date/datetime/int/table/etc, not just a string (e.g. an
    # unquoted `repo = 2026-09-30`). The shape check above already classifies that as an error
    # (E_BAD_TYPE; not a string, so `_valid_repo_id`/`is_nonblank_str` is False); report null here
    # rather than the raw value, which `json.dumps` cannot serialize and would otherwise crash
    # this report to exit 65 ("... is not JSON serializable") instead of the shape-error exit
    # already computed above.
    if not isinstance(repo, str):
        repo = None
    if not isinstance(impact_version, str):
        impact_version = None

    report = make_report("valid" if not errors else "invalid", code, errors,
                          files=[str(p) for p in rules_paths], rule_count=rule_count,
                          impact_version=impact_version, repo=repo)
    return code, report


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    validate_p = subparsers.add_parser("validate", allow_abbrev=False)
    validate_p.add_argument("--rules", action="append", default=None)
    return parser


def _resolve(path_str: str) -> pathlib.Path:
    p = pathlib.Path(path_str)
    if not p.is_absolute():
        p = (FIXTURES / "validate" / path_str)
    return p


def _run_case(case: dict) -> tuple:
    paths = [_resolve(p) for p in case["rules"]]
    try:
        return run_validate(paths)
    except (ir.RulesInputError, ir.UsageError) as exc:
        return exc.code, make_report("invalid", exc.code,
                                      [{"code": exc.code, "reason": exc.reason}],
                                      files=[str(p) for p in paths])


def _run_case_via_cli(case: dict) -> tuple:
    argv = ["validate"]
    for path in case["rules"]:
        argv.extend(("--rules", str(_resolve(path))))
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    return code, json.loads(captured.getvalue())


def _run_validate_via_cli(paths: list[pathlib.Path]) -> tuple:
    argv = ["validate"]
    for path in paths:
        argv.extend(("--rules", str(path)))
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    report = json.loads(captured.getvalue())
    if code == ir.E_INPUT:
        reasons = report.get("errors") or []
        reason = reasons[0].get("reason", "CLI validation failed") if reasons else "CLI validation failed"
        raise ir.RulesInputError(reason)
    return code, report


def _integer_spelling_differential(cases: list[dict]) -> tuple[bool, int, list, tuple]:
    failures, rewritten, out_of_range_exits = [], 0, ()
    with tempfile.TemporaryDirectory(prefix="impact-integral-floats-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(cases):
            changed_paths = []
            for path_index, path_str in enumerate(case["rules"]):
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
            try:
                changed_code, changed_report = _run_validate_via_cli(changed_paths)
            except ir.RulesInputError:
                changed_code, changed_report = ir.E_INPUT, {"status": "invalid"}
            verdict_same = changed_report.get("status") == original_report.get("status")
            exact_exit = changed_code == original_code
            if case["name"] == "36-bad-derive-params-int-overflow-2to63":
                out_of_range_exits = (original_code, changed_code)
            if not (verdict_same and exact_exit):
                failures.append((case["name"], original_code, changed_code,
                                 original_report.get("status"), changed_report.get("status")))
    return (not failures and rewritten > 0 and out_of_range_exits == (ir.E_BAD_DERIVE,
                                                                      ir.E_BAD_DERIVE),
            rewritten, failures, out_of_range_exits)


def _input_schema_differential(cases: list[dict]) -> tuple[bool, int, int, list]:
    """Compare rules_file schema verdicts with the real validator executable.

    The authored ``when`` XOR is the one documented semantic exception (spec §2.3; schema
    ``when.description`` and semantic rule ``when-exactly-one-of-changed-or-event``).
    """
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, 0, 0, []
    schema_path = HERE.parent / "schema" / "impact-rule-0.1.0-draft.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    failures, generated, semantic_rejections = [], 0, 0
    validation_exits = set(range(ir.E_MISSING_KEY, ir.E_BAD_DERIVE + 1))
    with tempfile.TemporaryDirectory(prefix="impact-schema-differential-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(c for c in cases if c["expected_exit"] == 0):
            for file_index, path_str in enumerate(case["rules"]):
                source = _resolve(path_str)
                document = ir.normalize_schema_numbers(ir.load_toml_file(source))
                baseline_errors = conformance_errors(
                    document, schema, "#/$defs/rules_file", "rules_file")
                if baseline_errors:
                    failures.append((case["name"], "positive-fixture-schema", baseline_errors[:3]))
                    continue
                for variant_index, (label, variant) in enumerate(schema_input_variants(
                        document, schema, "#/$defs/rules_file")):
                    generated += 1
                    schema_accepts = not conformance_errors(
                        variant, schema, "#/$defs/rules_file", "rules_file")
                    destination = tmp_path / f"{case_index}-{file_index}-{variant_index}.toml"
                    destination.write_text(toml_inline_document(variant), encoding="utf-8")
                    argv = [sys.executable, "-B", str(HERE / "impact_validate.py"), "validate"]
                    for other_index, other in enumerate(case["rules"]):
                        argv.extend(("--rules", str(destination if other_index == file_index
                                                   else _resolve(other))))
                    proc = subprocess.run(argv, capture_output=True, timeout=10)
                    cli_accepts = proc.returncode == 0
                    semantic = (schema_accepts and not cli_accepts
                                and proc.returncode == ir.E_BAD_WHEN
                                and (label.startswith("add-optional:")
                                     or label.startswith("remove-optional:"))
                                and (label.endswith(".when.changed:array")
                                     or label.endswith(".when.changed")
                                     or label.endswith(".when.event:string")
                                     or label.endswith(".when.event")))
                    if semantic:
                        semantic_rejections += 1
                    elif schema_accepts != cli_accepts:
                        failures.append((case["name"], label, schema_accepts, proc.returncode))
                    elif not cli_accepts and proc.returncode not in validation_exits:
                        failures.append((case["name"], label, "non-validation-exit", proc.returncode))
    return not failures and generated > 0, generated, semantic_rejections, failures


def case_matches(case: dict, code: int, report: dict) -> bool:
    if code != case["expected_exit"]:
        return False
    if "expected_error_codes" in case:
        got = sorted(e["code"] for e in report.get("errors", []))
        if got != sorted(case["expected_error_codes"]):
            return False
    if "expected_error_substrings" in case:
        reasons = "\n".join(e.get("reason", "") for e in report.get("errors", []))
        if any(fragment not in reasons for fragment in case["expected_error_substrings"]):
            return False
    return True


def _schema_deriver_param_registry_errors(schema: dict) -> list[str]:
    """Return every schema/Python deriver-parameter registry mismatch.

    The schema registry is discovered through ``deriver_table`` and its parameter-definition
    references rather than being restated here.  This makes additions, removals, renamed
    parameters, kind/bound changes, open parameter objects, and either report/input reference
    list drifting independently all observable by one check.
    """
    errors = []
    defs = schema.get("$defs", {})
    expected_names = set(ir.DERIVER_PARAM_SPECS)
    if set(ir.DERIVER_REQUIRED_PARAMS) != expected_names:
        errors.append("Python deriver requiredness registry names do not match parameter registry")
    actual_def_names = {name for name in defs if name.startswith("deriver_params_")}
    expected_refs = {f"#/$defs/{name}" for name in actual_def_names}
    if len(actual_def_names) != len(expected_names):
        errors.append("schema does not have exactly one params def per Python deriver")
    for def_name in actual_def_names:
        params_def = defs.get(def_name)
        if not isinstance(params_def, dict) or params_def.get("type") != "object" \
                or not isinstance(params_def.get("properties"), dict):
            errors.append(f"params def {def_name!r} is not an object with properties")
        elif params_def.get("additionalProperties") is not False:
            errors.append(f"params def {def_name!r} is not closed")
    enum_names = defs.get("deriver_string", {}).get("enum")
    if not isinstance(enum_names, list) or set(enum_names) != expected_names \
            or len(enum_names) != len(expected_names):
        errors.append("deriver_string enum does not exactly match DERIVER_PARAM_SPECS")

    table_branches = defs.get("deriver_table", {}).get("oneOf")
    table_ref_by_name = {}
    if not isinstance(table_branches, list):
        errors.append("deriver_table.oneOf is not an array")
        table_branches = []
    for branch in table_branches:
        properties = branch.get("properties") if isinstance(branch, dict) else None
        if not isinstance(properties, dict) or len(properties) != 1:
            errors.append("a deriver_table branch does not have exactly one property")
            continue
        name, ref_node = next(iter(properties.items()))
        ref = ref_node.get("$ref") if isinstance(ref_node, dict) else None
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            errors.append(f"deriver_table branch {name!r} has no local params-def reference")
            continue
        if ref_node != {"$ref": ref}:
            errors.append(f"deriver_table branch {name!r} does not contain only its params-def ref")
        if name in table_ref_by_name:
            errors.append(f"deriver_table repeats branch {name!r}")
        table_ref_by_name[name] = ref
        if branch.get("type") != "object" or branch.get("required") != [name] \
                or branch.get("additionalProperties") is not False:
            errors.append(f"deriver_table branch {name!r} is not a closed required singleton")

    if set(table_ref_by_name) != expected_names or len(table_ref_by_name) != len(expected_names):
        errors.append("deriver_table names do not exactly match DERIVER_PARAM_SPECS")

    expected_kind_shapes = {
        "integer>=1": {"type": "integer", "minimum": 1},
        "number[0,1]": {"type": "number", "minimum": 0, "maximum": 1},
        "boolean": {"type": "boolean"},
    }
    for name, python_params in ir.DERIVER_PARAM_SPECS.items():
        ref = table_ref_by_name.get(name)
        if ref not in expected_refs:
            errors.append(f"deriver_table has no per-deriver params-def reference for {name!r}")
            continue
        def_name = ref[len("#/$defs/"):]
        params_def = defs.get(def_name)
        if not isinstance(params_def, dict):
            errors.append(f"missing params def {def_name!r} for {name!r}")
            continue
        if params_def.get("type") != "object":
            errors.append(f"params def for {name!r} is not an object")
        if params_def.get("additionalProperties") is not False:
            errors.append(f"params def for {name!r} is not closed")
        schema_params = params_def.get("properties")
        if not isinstance(schema_params, dict):
            errors.append(f"params def for {name!r} has no properties object")
            continue
        if set(schema_params) != set(python_params):
            errors.append(f"parameter names for {name!r} do not match DERIVER_PARAM_SPECS")
        schema_required = params_def.get("required", [])
        if not isinstance(schema_required, list) or len(schema_required) != len(set(schema_required)) \
                or set(schema_required) != set(ir.DERIVER_REQUIRED_PARAMS.get(name, set())):
            errors.append(f"required parameter names for {name!r} do not match Python registry")
        for param_name, python_kind in python_params.items():
            expected_shape = expected_kind_shapes.get(python_kind)
            if expected_shape is None:
                errors.append(f"unknown Python parameter kind {python_kind!r}")
            elif json.dumps(schema_params.get(param_name), sort_keys=True, separators=(",", ":")) \
                    != json.dumps(expected_shape, sort_keys=True, separators=(",", ":")):
                errors.append(f"schema kind/bounds for {name!r}.{param_name} do not match {python_kind}")

    table_refs = list(table_ref_by_name.values())
    if set(table_refs) != expected_refs or len(table_refs) != len(expected_refs):
        errors.append("deriver_table does not reference exactly the per-deriver params defs")

    report_refs_node = (defs.get("not_evaluated_deriver", {}).get("properties", {})
                        .get("params", {}).get("anyOf"))
    report_refs = []
    if isinstance(report_refs_node, list):
        report_refs = [node.get("$ref") for node in report_refs_node if isinstance(node, dict)]
    report_refs_are_bare = (isinstance(report_refs_node, list)
                            and all(node == {"$ref": node.get("$ref")}
                                    for node in report_refs_node if isinstance(node, dict))
                            and len(report_refs) == len(report_refs_node))
    if not report_refs_are_bare or set(report_refs) != expected_refs \
            or len(report_refs) != len(expected_refs):
        errors.append("not_evaluated_deriver.params does not reference exactly the params defs")
    return errors


def _non_json_helper_assertions(find_non_json) -> bool:
    """Directly pin the helper's policy, independently of typed-registry fixture outcomes."""
    rejected = [
        ir.date(2026, 10, 3), float("nan"), float("inf"), float("-inf"), float("1e400"),
        2 ** 63, -(2 ** 63) - 1, {"deep": [{"value": 10 ** 5000}]},
    ]
    accepted = [2 ** 63 - 1, -(2 ** 63)]
    return (all(find_non_json(value) is not None for value in rejected)
            and all(find_non_json(value) is None for value in accepted))




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
                print(ir.emit_report(report, indent=2, sort_keys=True, allow_nan=False))

        integer_diff_ok, integer_rewrites, integer_diff_failures, out_of_range_exits = (
            _integer_spelling_differential(cases))
        results.append(integer_diff_ok)
        print(f"selftest schema-integer-integral-float-differential: "
              f"{integer_rewrites} integer spelling(s), {len(integer_diff_failures)} failure(s) "
              f"{integer_diff_failures[:3]!r}; signed-64 overflow int/float exits="
              f"{out_of_range_exits!r} "
              f"[{'ok' if integer_diff_ok else 'MISMATCH'}]")

        schema_diff_ok, schema_variants, schema_semantic, schema_diff_failures = (
            _input_schema_differential(cases))
        results.append(schema_diff_ok)
        print(f"selftest input-schema-cli-differential-rules: "
              f"{schema_variants} generated variant(s), "
              f"{schema_semantic} documented semantic rejection(s), "
              f"{len(schema_diff_failures)} failure(s) {schema_diff_failures[:3]!r} "
              f"[{'ok' if schema_diff_ok else 'MISMATCH'}]")

        usage_code = main(["validate"])  # no --rules given: main()'s own required-arg check
        results.append(usage_code == ir.E_USAGE)
        print(f"selftest usage-error-no-rules: expected exit=64 got exit={usage_code} "
              f"[{'ok' if usage_code == ir.E_USAGE else 'MISMATCH'}]")

        combo_code = main(["--selftest", "validate", "--rules", "x.toml"])
        results.append(combo_code == ir.E_USAGE)
        print(f"selftest usage-error-selftest-plus-command: expected exit=64 got exit={combo_code} "
              f"[{'ok' if combo_code == ir.E_USAGE else 'MISMATCH'}]")

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
        exception_ok = (exception_code == ir.E_INPUT and
                        exception_report["errors"][0]["reason"].startswith("input/runtime failure"))
        results.append(exception_ok)
        print(f"selftest exit-65-injected-exception-catch-all: got exit={exception_code} "
              f"[{'ok' if exception_ok else 'MISMATCH'}]")

        expected_exits = {0, ir.E_MISSING_KEY, ir.E_UNKNOWN_KEY, ir.E_BAD_TYPE, ir.E_BAD_ENUM,
                           ir.E_BAD_GLOB, ir.E_BAD_WHEN, ir.E_DUP_RULE_ID, ir.E_BAD_URGENCY,
                           ir.E_BAD_DERIVE, ir.E_INPUT}
        covered_exits = {case["expected_exit"] for case in cases}
        missing_exits = sorted(expected_exits - covered_exits)
        coverage_ok = not missing_exits
        results.append(coverage_ok)
        print(f"selftest exit-code-coverage: exits {sorted(expected_exits)}; "
              f"missing={missing_exits or 'none'} [{'ok' if coverage_ok else 'MISMATCH'}]")

        used = set()
        for case in cases:
            for r in case["rules"]:
                used.add(pathlib.Path(r).name)
        fixture_files = {p.name for p in (FIXTURES / "validate").iterdir()
                          if p.name not in ("cases.json",) and p.suffix == ".toml"}
        unused_fixtures = sorted(fixture_files - used)
        fixture_coverage = not unused_fixtures
        results.append(fixture_coverage)
        print(f"selftest fixture-coverage: unused={unused_fixtures or 'none'} "
              f"[{'ok' if fixture_coverage else 'MISMATCH'}]")

        # Schema <-> validator drift guard for the closed, typed per-deriver parameter registry.
        # Both planted copies must be rejected, proving that parameter-set and bound drift are
        # observable rather than merely comparing the current live documents successfully.
        schema = json.loads((HERE.parent / "schema" / "impact-rule-0.1.0-draft.schema.json")
                            .read_text(encoding="utf-8"))
        registry_errors = _schema_deriver_param_registry_errors(schema)

        extra_param_schema = copy.deepcopy(schema)
        extra_param_schema["$defs"]["deriver_params_git_cochange"]["properties"][
            "__selftest_schema_only_param__"] = {"type": "boolean"}
        extra_param_control_fired = bool(_schema_deriver_param_registry_errors(extra_param_schema))

        changed_bound_schema = copy.deepcopy(schema)
        changed_bound_schema["$defs"]["deriver_params_git_cochange"]["properties"][
            "min_confidence"]["maximum"] = 2
        changed_bound_control_fired = bool(_schema_deriver_param_registry_errors(changed_bound_schema))

        required_param_schema = copy.deepcopy(schema)
        required_param_schema["$defs"]["deriver_params_git_cochange"]["required"] = ["advisory"]
        required_param_control_fired = bool(
            _schema_deriver_param_registry_errors(required_param_schema))

        registry_ok = (not registry_errors and extra_param_control_fired
                       and changed_bound_control_fired and required_param_control_fired)
        results.append(registry_ok)
        print(f"selftest schema-deriver-param-registry-drift: live-errors="
              f"{registry_errors or 'none'} "
              f"extra-schema-param-control-fired={extra_param_control_fired} "
              f"changed-bound-control-fired={changed_bound_control_fired} "
              f"required-param-control-fired={required_param_control_fired} "
              f"[{'ok' if registry_ok else 'MISMATCH'}]")

        helper_ok = _non_json_helper_assertions(ir._find_non_json_value)
        lambda_control_fired = not _non_json_helper_assertions(lambda _value: None)
        original_min = ir.MIN_DERIVE_INT
        try:
            ir.MIN_DERIVE_INT = -(2 ** 63) + 1
            min_bound_control_fired = not _non_json_helper_assertions(ir._find_non_json_value)
        finally:
            ir.MIN_DERIVE_INT = original_min
        helper_assertions_ok = helper_ok and lambda_control_fired and min_bound_control_fired
        results.append(helper_assertions_ok)
        print(f"selftest non-json-helper-direct-assertions: live={helper_ok} "
              f"lambda-control-fired={lambda_control_fired} "
              f"min-bound-control-fired={min_bound_control_fired} "
              f"[{'ok' if helper_assertions_ok else 'MISMATCH'}]")

        # `ir.safe_repr_for_error` bounds BOTH an adversarially DEEP container (no Python
        # recursion, so no stack overflow) and an adversarially WIDE one (no full materialization,
        # so no O(size) cost) -- bounded TOTAL work and TOTAL output length regardless of either
        # axis.
        import time as _time
        start = _time.monotonic()
        deep_container = []
        cur = deep_container
        for _ in range(200_000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        deep_text = ir.safe_repr_for_error(deep_container)
        wide_container = list(range(2_000_000))
        wide_text = ir.safe_repr_for_error(wide_container)
        nested_huge_int = {"a": [10 ** 5000, 1, {"b": 10 ** 5000}]}
        nested_text = ir.safe_repr_for_error(nested_huge_int)
        # A top-level scalar `str` is bounded the SAME way a huge value nested inside a container
        # is -- truncated to `_SAFE_REPR_STR_PREVIEW_CHARS` BEFORE repr() ever sees it, never repr'd
        # in full first and truncated after. This is the single most common call-site shape (an
        # enum value, a glob, an id), so it is checked independently of the container-walk cases
        # above, which would not catch a regression here.
        huge_scalar_text = ir.safe_repr_for_error("x" * 1_000_000)
        huge_scalar_nested_text = ir.safe_repr_for_error(["x" * 1_000_000])
        elapsed = _time.monotonic() - start
        bounded_ok = (len(deep_text) <= ir._SAFE_REPR_MAX_CHARS + 10
                      and len(wide_text) <= ir._SAFE_REPR_MAX_CHARS + 10
                      and "digit integer" in nested_text and "5001" in nested_text
                      and len(huge_scalar_text) <= ir._SAFE_REPR_MAX_CHARS + 10
                      and len(huge_scalar_nested_text) <= ir._SAFE_REPR_MAX_CHARS + 10
                      and elapsed < 5.0)
        results.append(bounded_ok)
        print(f"selftest safe-repr-for-error-bounds-deep-and-wide-containers: "
              f"deep_len={len(deep_text)} wide_len={len(wide_text)} "
              f"nested={nested_text!r} huge_scalar_len={len(huge_scalar_text)} "
              f"huge_scalar_nested_len={len(huge_scalar_nested_text)} "
              f"({elapsed:.3f}s) [{'ok' if bounded_ok else 'MISMATCH'}]")

        # Bounded diagnostics, END TO END: ONE hostile merged rules document carrying a
        # 1,000,000-character string (or key) in every distinct field family whose validation error
        # embeds the offending value -- a bad <repo> id, an unknown top-level key, a bad deriver
        # string, an unknown deriver key (both the one-key and the not-exactly-one-key table forms),
        # an unknown `when` key, a bad `when.event` enum, a bad `relation` enum, an unknown
        # `must_update[]` key, a bad `must_update[].action` enum, an unknown `decided_by` key, a bad
        # `decided_by.detect` enum, a bad `importance` enum, a bad `urgency` (both the
        # not-a-valid-duration and the too-many-digits branches), a bad `retrigger` enum, and a
        # duplicate rule `id` -- asserts every single issue's `reason` this produces is no longer
        # than `ir.DIAGNOSTIC_REASON_MAX_CHARS`, the documented diagnostic bound (see the README).
        # Dropping the pre-repr truncation in `ir._safe_repr_scalar` (or reverting any of the call
        # sites this fold routed through `safe_repr_for_error` back to a raw `f"{value!r}"`) makes
        # this fail: several of these reasons would otherwise be on the order of 1,000,000
        # characters long.
        HOSTILE = "x" * 1_000_000
        hostile_doc = {
            "impact_version": "v1",
            "repo": HOSTILE,
            HOSTILE: "unknown-top-level-key",
            "derive": [
                HOSTILE,
                {HOSTILE: {}},
                {HOSTILE: {}, "other": {}},
            ],
            "rules": [
                {
                    "id": HOSTILE,
                    "when": {"event": HOSTILE, HOSTILE: 1},
                    "relation": HOSTILE,
                    "must_update": [{"target": "t", "action": HOSTILE, "check": "c", HOSTILE: 1}],
                    "decided_by": {"detect": HOSTILE, "fix": "human", HOSTILE: 1},
                    "importance": HOSTILE,
                    "urgency": "within:" + HOSTILE,
                    "retrigger": HOSTILE,
                },
                {
                    "id": HOSTILE,  # duplicate of rules[0]'s id
                    "when": {"event": "first-src-commit"},
                    "relation": "depends-on",
                    "must_update": [{"target": "t", "action": "update"}],
                    "decided_by": {"detect": "program", "fix": "human"},
                    "importance": "normal",
                    "urgency": "within:" + ("9" * 1_000_000) + "d",
                },
            ],
        }
        hostile_errors = ir.validate_merged([("hostile.toml", hostile_doc)])
        hostile_lengths = [len(e["reason"]) for e in hostile_errors]
        hostile_ok = (len(hostile_errors) >= 14
                      and all(n <= ir.DIAGNOSTIC_REASON_MAX_CHARS for n in hostile_lengths))
        results.append(hostile_ok)
        print(f"selftest bounded-diagnostics-hostile-rules-document: "
              f"{len(hostile_errors)} errors max_reason_len={max(hostile_lengths, default=0)} "
              f"cap={ir.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if hostile_ok else 'MISMATCH'}]")

        # Two CLI-reachable oversized-value reproductions, run through the SAME `run_validate` path
        # `main()`'s own `validate` command uses (a real file on disk, loaded with
        # `ir.load_toml_file`, validated with `ir.validate_merged`, reported through the full CLI
        # report constructor) -- not merely the in-process `ir.validate_merged` call above, so this
        # also proves the bound holds end to end through the actual CLI entry point: a bad `repo`
        # id carrying a 1,000,000-character string, and a bad `relation` enum value doing the same.
        import re
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)

            repo_path = tmp_path / "cli-huge-repo-id.toml"
            repo_path.write_text(f'impact_version="1"\nrepo="{HOSTILE}"\nrules=[]\n',
                                  encoding="utf-8")
            repo_code, repo_report = _run_validate_via_cli([repo_path])
            repo_reasons = [e["reason"] for e in repo_report["errors"]]
            repo_ok = (repo_code == ir.E_BAD_TYPE and bool(repo_reasons)
                       and all(len(r) <= ir.DIAGNOSTIC_REASON_MAX_CHARS for r in repo_reasons))
            results.append(repo_ok)
            print(f"selftest cli-reachable-oversized-repo-id: exit={repo_code} "
                  f"max_reason_len={max((len(r) for r in repo_reasons), default=0)} "
                  f"cap={ir.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if repo_ok else 'MISMATCH'}]")

            relation_path = tmp_path / "cli-huge-relation-enum.toml"
            base_text = (FIXTURES / "validate" / "00-valid-minimal.toml").read_text(encoding="utf-8")
            relation_path.write_text(
                re.sub(r'relation\s*=\s*"[^"]+"', f'relation="{HOSTILE}"', base_text),
                encoding="utf-8")
            relation_code, relation_report = _run_validate_via_cli([relation_path])
            relation_reasons = [e["reason"] for e in relation_report["errors"]]
            relation_ok = (relation_code == ir.E_BAD_ENUM and bool(relation_reasons)
                           and all(len(r) <= ir.DIAGNOSTIC_REASON_MAX_CHARS for r in relation_reasons))
            results.append(relation_ok)
            print(f"selftest cli-reachable-oversized-relation-enum: exit={relation_code} "
                  f"max_reason_len={max((len(r) for r in relation_reasons), default=0)} "
                  f"cap={ir.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if relation_ok else 'MISMATCH'}]")

            # Regression: a `--rules` argument that is itself a hostile, 10,000-character path
            # (never written to disk — the failure is
            # `load_toml_file`'s own `OSError`, not a TOML parse error) used to make `cannot read
            # <path>: <exc>` embed the SAME oversized path TWICE (once interpolated raw, once again
            # inside the exception's own `str()`), well past the documented 2,000-character bound
            # (an earlier reproduction measured 20,047 characters). `ir.bound_path_preview` plus
            # the exception's CLASS name (never `str(exc)`) at the source, and `make_report`'s own
            # unconditional `ir.bound_reason` pass as the final backstop, now cap it. Reverting
            # either fix independently still passes (each is sufficient alone); removing BOTH makes
            # this fail.
            huge_rules_path = pathlib.Path("x" * 10_000)
            try:
                _run_validate_via_cli([huge_rules_path])
                huge_path_reasons = None
            except ir.RulesInputError as exc:
                huge_path_report = make_report("invalid", exc.code,
                                                [{"code": exc.code, "reason": exc.reason}])
                huge_path_reasons = [e["reason"] for e in huge_path_report["errors"]]
            huge_path_ok = (huge_path_reasons is not None and bool(huge_path_reasons)
                            and all(len(r) <= ir.DIAGNOSTIC_REASON_MAX_CHARS
                                    for r in huge_path_reasons))
            results.append(huge_path_ok)
            print(f"selftest cli-reachable-oversized-rules-path: "
                  f"max_reason_len={max((len(r) for r in (huge_path_reasons or [])), default=0)} "
                  f"cap={ir.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if huge_path_ok else 'MISMATCH'}]")

            # Regression: a `--rules` path that is not valid Unicode at all (the OS gives
            # Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a lone
            # UTF-16 surrogate in the decoded argv string) must still produce a report whose `reason`
            # text re-encodes as UTF-8 cleanly, with the exit code unchanged and the reason within
            # the documented cap. Mutation that turns this red: reverting `ir.bound_path_preview` to
            # `s = str(path)` with no escaping (the pre-fix code) makes `surrogate_utf8_ok` False.
            surrogate_path = pathlib.Path("missing-\udcff")
            try:
                _run_validate_via_cli([surrogate_path])
                surrogate_reason = None
                surrogate_code = None
            except ir.RulesInputError as exc:
                surrogate_code = exc.code
                surrogate_reason = exc.reason
            surrogate_utf8_ok = True
            if surrogate_reason is not None:
                try:
                    surrogate_reason.encode("utf-8")
                except UnicodeEncodeError:
                    surrogate_utf8_ok = False
            surrogate_ok = (surrogate_code == ir.E_INPUT and surrogate_reason is not None
                            and surrogate_utf8_ok
                            and len(surrogate_reason) <= ir.DIAGNOSTIC_REASON_MAX_CHARS
                            and "\udcff" not in surrogate_reason)
            results.append(surrogate_ok)
            print(f"selftest lone-surrogate-rules-path-escaped-in-report: exit={surrogate_code} "
                  f"reason={surrogate_reason!r} [{'ok' if surrogate_ok else 'MISMATCH'}]")

            # Regression: a `when.changed[]` glob carrying 1,000 repetitions of U+E0000 (a
            # non-printable "language tag" code point) whose `repr()` ESCAPES every occurrence to a
            # 10-character `\Uxxxxxxxx` sequence — even
            # after `_safe_repr_scalar`'s own 200-character PRE-repr preview, `repr()` of 200 such
            # characters is still ~2,000 characters, so the scalar formatter's own final cap-then-
            # append-marker step had to be EXACT, not "slice to the cap, then append a 4-character
            # marker" (an earlier reproduction measured 2,129 characters for a 2,000-character
            # bound — `_cap_with_marker`, not a bare slice, is what fixes this).
            escaped_glob = "\U000e0000" * 1000
            escaped_path = tmp_path / "cli-escaped-glob.toml"
            escaped_path.write_text(
                re.sub(r'changed\s*=\s*\[[^\]]*\]', f'changed=["{escaped_glob}"]', base_text),
                encoding="utf-8")
            escaped_code, escaped_report = _run_validate_via_cli([escaped_path])
            escaped_reasons = [e["reason"] for e in escaped_report["errors"]]
            escaped_ok = (escaped_code == ir.E_BAD_GLOB and bool(escaped_reasons)
                          and all(len(r) <= ir.DIAGNOSTIC_REASON_MAX_CHARS for r in escaped_reasons))
            results.append(escaped_ok)
            print(f"selftest cli-reachable-escaped-glob: exit={escaped_code} "
                  f"max_reason_len={max((len(r) for r in escaped_reasons), default=0)} "
                  f"cap={ir.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if escaped_ok else 'MISMATCH'}]")

            # By design: a raw, INVALID `repo`/`impact_version` echoed on an invalid report is
            # proportional reporting, not a diagnostic `reason`, and is correctly NOT size-capped
            # (only `reason` is). Checked here, at a million characters each, so a future change
            # cannot silently start capping either field without one of these two cases failing
            # first.
            # `repo` fails the <repo>-id grammar on a non-blank oversized string; `impact_version`
            # is only checked for "non-blank", so an oversized ALL-WHITESPACE string is what makes
            # IT invalid (a non-blank oversized string would pass `impact_version`'s own check and
            # exit 0, defeating this case's own purpose).
            for echo_field, echo_value in (("repo", "y" * 1_000_000), ("impact_version", " " * 1_000_000)):
                doc = {"impact_version": "1", "repo": "alpha", echo_field: echo_value}
                echo_text = "\n".join(f"{k}={json.dumps(v)}" for k, v in doc.items()) + "\nrules=[]\n"
                echo_path = tmp_path / f"cli-echo-{echo_field}.toml"
                echo_path.write_text(echo_text, encoding="utf-8")
                echo_code, echo_report = _run_validate_via_cli([echo_path])
                echoed = echo_report.get(echo_field)
                echo_ok = (echo_code == ir.E_BAD_TYPE and echoed == echo_value)
                results.append(echo_ok)
                print(f"selftest cli-reachable-echo-{echo_field}-not-size-capped: exit={echo_code} "
                      f"echoed_len={len(echoed) if isinstance(echoed, str) else 'n/a'} "
                      f"[{'ok' if echo_ok else 'MISMATCH'}]")

        # Direct, non-CLI exactness checks on the cap primitives themselves: `ir.bound_reason` and
        # `ir._cap_with_marker` must return a result no longer than the requested cap for ANY input,
        # including one already exactly at the cap (no marker should be added when none is needed)
        # and one with many repr-escaped characters (the same shape the escaped-glob case above
        # exercises end to end, checked here in isolation too). Bypassing `ir.bound_reason` in any
        # report constructor, or reverting `ir._cap_with_marker` to a bare `s[:n] + marker` (the
        # pre-fold defect), makes one of these fail.
        exact_cap_text = "z" * ir.DIAGNOSTIC_REASON_MAX_CHARS
        exact_cap_ok = (ir.bound_reason(exact_cap_text) == exact_cap_text)
        one_over_text = "z" * (ir.DIAGNOSTIC_REASON_MAX_CHARS + 1)
        one_over_result = ir.bound_reason(one_over_text)
        one_over_ok = len(one_over_result) == ir.DIAGNOSTIC_REASON_MAX_CHARS
        huge_reason_result = ir.bound_reason("w" * 5_000_000)
        huge_reason_ok = len(huge_reason_result) == ir.DIAGNOSTIC_REASON_MAX_CHARS
        many_escapes_result = ir.safe_repr_for_error("\\" * 5000)
        many_escapes_ok = len(many_escapes_result) <= ir._SAFE_REPR_MAX_CHARS
        cap_exactness_ok = exact_cap_ok and one_over_ok and huge_reason_ok and many_escapes_ok
        results.append(cap_exactness_ok)
        print(f"selftest bound-reason-and-cap-with-marker-exactness: "
              f"exact_cap_len={len(exact_cap_text)} one_over_result_len={len(one_over_result)} "
              f"huge_result_len={len(huge_reason_result)} "
              f"many_escapes_result_len={len(many_escapes_result)} "
              f"[{'ok' if cap_exactness_ok else 'MISMATCH'}]")

        print(f"selftest version-handshake: this validator implements {ir.IMPACT_RULES_VERSION!r}")

        # The regression that closes the class (argv fuzz).
        argv_fuzz_baselines = {
            "validate": ["validate", "--rules", str(FIXTURES / "validate" / "00-valid-minimal.toml")],
        }
        argv_fuzz_exits = {0, ir.E_MISSING_KEY, ir.E_UNKNOWN_KEY, ir.E_BAD_TYPE, ir.E_BAD_ENUM,
                            ir.E_BAD_GLOB, ir.E_BAD_WHEN, ir.E_DUP_RULE_ID, ir.E_BAD_URGENCY,
                            ir.E_BAD_DERIVE, ir.E_USAGE, ir.E_INPUT}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            HERE / "impact_validate.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_failures = _encoding_regression_failures(
            HERE / "impact_validate.py",
            [(argv_fuzz_baselines["validate"], {0}), (["validate"], {ir.E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = ir.output_boundary_selftest(
            HERE / "impact_validate.py", argv_fuzz_baselines["validate"], ["validate"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "impact_validate.py", "make_report", "run_validate",
            ["validate"], argv_fuzz_baselines["validate"])
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


def _main(argv=None) -> int:
    files = []
    try:
        parser = make_parser()
        args = parser.parse_args(argv)
        if args.selftest:
            if args.command is not None:
                parser.error("--selftest cannot be combined with a command")
            return run_selftest()
        if args.command != "validate":
            parser.error("the 'validate' command or --selftest is required")
        if not args.rules:
            parser.error("at least one --rules FILE is required")
        paths = [pathlib.Path(p) for p in args.rules]
        files = [str(p) for p in paths]
        code, report = run_validate(paths)
    except ir.UsageError as exc:
        code = exc.code
        report = make_report("invalid", code, [{"code": code, "reason": exc.reason}], files=files)
    except ir.RulesInputError as exc:
        code = exc.code
        report = make_report("invalid", code, [{"code": code, "reason": exc.reason}], files=files)
    except Exception as exc:
        reason = f"input/runtime failure ({type(exc).__name__}: {exc})"
        code = ir.E_INPUT
        report = make_report("invalid", code, [{"code": code, "reason": reason}], files=files)
    ir.write_report(ir.emit_report(report, indent=2, sort_keys=True, allow_nan=False))
    return code



def main(argv=None) -> int:
    return ir.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
