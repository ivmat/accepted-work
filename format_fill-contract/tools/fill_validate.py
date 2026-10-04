#!/usr/bin/env python3
"""fill_validate.py — first cut, stdlib-only shape + closed-vocabulary validator for a
`fill-contract` TOML file. Checks that a contract's top-level fields, each per-file entry, and
every `filled_when`/`due_when` predicate-expression tree are well-formed and use only the closed
predicate vocabulary. Does NOT evaluate the contract against any repo — that is `fill_observe.py`
(facts) + `fill_evaluate.py` (decision). `retrigger`/`adequacy` tables are shape-checked here (exit
16) but this first cut never semantically evaluates either.

Usage:
  python3 fill_validate.py validate --contract FILE
  python3 fill_validate.py --selftest

Exit codes (lowest-numbered class wins when several fire; 0 iff no error):
  0   the contract is well-formed and uses only the closed predicate vocabulary
  10  a top-level field (`fill_version`, `template`) is missing or not a non-blank string
  11  `contracts` is missing/not a non-empty array of tables, or an entry is not a table
  12  a contract entry is missing a required field (`file`/`importance`/`urgency_after_due`/
      `filled_when`/`due_when`), has a field of the wrong type, a `file` value violating the
      path grammar (see fill_common.validate_path_grammar), an `importance` outside the closed
      enum, an invalid `urgency_after_due` duration, or a duplicate `file` value across entries
  13  a `filled_when`/`due_when` predicate-expression node is malformed: unknown predicate name,
      missing/mistyped parameter, an unknown extra parameter, an empty/absent `any`/`all` list, a
      node with zero or more than one of {predicate, any, all, not} (a leaf carrying a combinator
      key is exactly this case), or a tree nesting deeper than fill_common.MAX_EXPR_DEPTH
  14  a `not` combinator is used inside `due_when` (restricted to `filled_when` only)
  15  `template_rows` is missing on an entry whose `filled_when` tree uses `rows-beyond-template` —
      a one-way rule, not an iff (see fill_common.py). A PRESENT but mistyped/out-of-range
      `template_rows` is a 12, not a 15: its type is checked whenever the key is present, regardless
      of whether `rows-beyond-template` is used at all.
  16  a present `retrigger` or `adequacy` table is shape-invalid (neither is semantically
      evaluated by any tool in this first cut)
  64  usage/argument error
  65  unreadable file or invalid TOML syntax
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

Stdlib only: argparse, json, pathlib, re, sys, time, fill_common (this directory).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import fill_common as fc
from schema_conformance import (boundary_defect_controls, conformance_errors,
                                rewrite_toml_integers_as_floats, schema_input_variants,
                                toml_inline_document)

REPORT_SCHEMA = ("schema/fill-contract-0.1.0-draft.schema.json", "validate_output")
fc.register_report_schema(__file__, REPORT_SCHEMA)

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"

VALIDATOR_VERSION = "fill-contract/first-cut-0.1.0-draft"
E_USAGE = 64
E_INPUT = 65
E_SELFTEST = 99


def _output_schema_errors(report: dict) -> list[str]:
    schema = json.loads((HERE.parent / "schema" /
                         "fill-contract-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/validate_output", "validate_output")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise fc.FillError(E_USAGE, message)


def _error_report(code: int, errors: list) -> dict:
    """The ONE report constructor: every exit path (a computed validation result, a FillError such
    as an unreadable file or a usage error, or the catch-all for an unexpected exception) builds its
    output through this function, so the documented shape (`status`/`exit_code`/`errors`/
    `validator_version`) cannot drift between one exit path and another — including
    `validator_version`, which an earlier version of this module's own usage-error/catch-all paths
    omitted even though every OTHER report carried it. Every entry's `reason` is passed through
    `fc.bound_reason` HERE, unconditionally — the final, structural enforcement of the documented
    ≤2,000-character `reason` bound, so it holds even for an entry built by a call site that forgot
    to self-bound, not only for ones that already used `fc.safe_repr_for_error`."""
    return {
        "status": "valid" if not errors else "invalid",
        "exit_code": code,
        "errors": [{**e, "reason": fc.bound_reason(e["reason"])} for e in errors],
        "validator_version": VALIDATOR_VERSION,
    }


def run_validate(contract_path: pathlib.Path) -> tuple:
    contract = fc.load_toml(contract_path)  # raises FillError(65, ...) on read/parse failure
    errors = fc.validate_contract(contract)
    reports = [{"code": c, "reason": r} for c, r in errors]
    code = min((e["code"] for e in reports), default=0)
    return code, _error_report(code, reports)


def case_matches(case: dict, code: int, report: dict) -> bool:
    if code != case["expected_exit"]:
        return False
    if "expected_error_codes" in case:
        got = sorted(e["code"] for e in report["errors"])
        if got != sorted(case["expected_error_codes"]):
            return False
    return True


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    validate_p = subparsers.add_parser("validate", allow_abbrev=False)
    validate_p.add_argument("--contract", required=True)
    # Internal: the self-test's own subprocess entry point for a bounded, programmatically-built
    # case (a very deep predicate-expression tree cannot be authored as a readable fixture file) —
    # not a documented user-facing command. See _run_deep_tree_case_bounded below.
    run_case_p = subparsers.add_parser("run-case", allow_abbrev=False)
    run_case_p.add_argument("name")
    return parser


def _run_case_file(case: dict) -> tuple:
    path = FIXTURES / "validate" / case["contract"]
    try:
        return run_validate(path)
    except fc.FillError as exc:
        return exc.code, _error_report(exc.code, [{"code": exc.code, "reason": str(exc)}])


def _run_case_via_cli(case: dict) -> tuple:
    captured = io.StringIO()
    argv = ["validate", "--contract", str(FIXTURES / "validate" / case["contract"])]
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    return code, json.loads(captured.getvalue())


def _run_validate_via_cli(path: pathlib.Path) -> tuple:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(["validate", "--contract", str(path)])
    report = json.loads(captured.getvalue())
    if code == E_INPUT:
        reasons = report.get("errors") or []
        reason = reasons[0].get("reason", "CLI validation failed") if reasons else "CLI validation failed"
        raise fc.FillError(code, reason)
    return code, report


def _integer_spelling_differential(cases: list[dict]) -> tuple[bool, int, list]:
    failures, rewritten = [], 0
    with tempfile.TemporaryDirectory(prefix="fill-contract-integral-floats-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(cases):
            source = FIXTURES / "validate" / case["contract"]
            if source.is_file():
                changed, count = rewrite_toml_integers_as_floats(
                    source.read_text(encoding="utf-8"))
                rewritten += count
                destination = tmp_path / f"{case_index}.toml"
                destination.write_text(changed, encoding="utf-8")
            else:
                destination = source
            original_code, original_report = _run_case_via_cli(case)
            try:
                changed_code, changed_report = _run_validate_via_cli(destination)
            except fc.FillError as exc:
                changed_code, changed_report = exc.code, {"status": "invalid"}
            verdict_same = changed_report.get("status") == original_report.get("status")
            exact_when_accepted = case["expected_exit"] != 0 or changed_code == original_code
            if not (verdict_same and exact_when_accepted):
                failures.append((case["name"], original_code, changed_code,
                                 original_report.get("status"), changed_report.get("status")))
    return not failures and rewritten > 0, rewritten, failures


def _old_integer_check_control() -> tuple[bool, int, str]:
    """Restore the rejected Python-int-only check and run the real self-test entry point."""
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, E_SELFTEST, "nested boundary control suppressed"
    if os.environ.get("FILL_INTEGER_CONTROL_ACTIVE") == "1":
        return True, E_SELFTEST, "nested control suppressed"
    old = 'elif kind_ == "int" and not is_schema_integer(value):'
    new = ('elif kind_ == "int" and '
           '(isinstance(value, bool) or not isinstance(value, int)):')
    with tempfile.TemporaryDirectory(prefix="fill-old-integer-check-control-") as tmp:
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
            return False, 0, "could not plant old isinstance(int) check"
        common.write_text(source.replace(old, new, 1), encoding="utf-8")
        env = dict(os.environ)
        env["FILL_INTEGER_CONTROL_ACTIVE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-B", str(copied / "tools/fill_validate.py"), "--selftest"],
            cwd=tmp_root, capture_output=True, text=True, env=env, timeout=240,
        )
    return proc.returncode == E_SELFTEST, proc.returncode, (proc.stdout + proc.stderr)[-500:]


def _input_schema_differential(cases: list[dict]) -> tuple[bool, int, int, list]:
    """Compare the contract schema with the real validator executable.

    The conditional requirement for ``template_rows`` is the sole documented exception (spec
    §9/§12.1; its property description and semantic rule ``template-rows-required`` in the schema).
    """
    if getattr(boundary_defect_controls, "__name__", "") == "<lambda>":
        return True, 0, 0, []
    schema = json.loads((HERE.parent / "schema" /
                         "fill-contract-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    failures, generated, semantic_rejections = [], 0, 0
    validation_exits = set(range(10, 17))
    with tempfile.TemporaryDirectory(prefix="fill-schema-differential-") as tmp:
        tmp_path = pathlib.Path(tmp)
        for case_index, case in enumerate(c for c in cases if c["expected_exit"] == 0):
            source = FIXTURES / "validate" / case["contract"]
            document = fc.normalize_schema_numbers(fc.load_toml(source))
            baseline_errors = conformance_errors(
                document, schema, "#/$defs/contract", "contract")
            if baseline_errors:
                failures.append((case["name"], "positive-fixture-schema", baseline_errors[:3]))
                continue
            for variant_index, (label, variant) in enumerate(schema_input_variants(
                    document, schema, "#/$defs/contract")):
                generated += 1
                schema_accepts = not conformance_errors(
                    variant, schema, "#/$defs/contract", "contract")
                destination = tmp_path / f"{case_index}-{variant_index}.toml"
                destination.write_text(toml_inline_document(variant), encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, "-B", str(HERE / "fill_validate.py"), "validate",
                     "--contract", str(destination)], capture_output=True, timeout=10)
                cli_accepts = proc.returncode == 0
                semantic = (schema_accepts and not cli_accepts and proc.returncode == 15
                            and label.startswith("remove-optional:")
                            and label.endswith(".template_rows"))
                if semantic:
                    semantic_rejections += 1
                elif schema_accepts != cli_accepts:
                    failures.append((case["name"], label, schema_accepts, proc.returncode))
                elif not cli_accepts and proc.returncode not in validation_exits:
                    failures.append((case["name"], label, "non-validation-exit", proc.returncode))
    return not failures and generated > 0, generated, semantic_rejections, failures


# ---------------------------------------------------------------------------
# A tiny, purpose-built JSON-Schema-SUBSET interpreter, used only by this selftest to give a real,
# structural (not merely prose) proof that (a) the schema ROOT selects $defs.contract — a bare `{}`
# is rejected, a real contract dict is accepted, and (b) $defs.predicate_leaf rejects a
# leaf carrying a combinator key. This repo is stdlib-only and there is no `jsonschema`
# package in the stdlib; this interprets exactly the constructs this ONE schema file uses
# (type/properties/required/additionalProperties/enum/pattern/minLength/minItems/items/oneOf/anyOf/
# allOf/not/$ref) — it is not a general-purpose validator. Bounded recursion: the SCHEMA document
# itself is small and fixed (this function's recursion follows $ref/oneOf/anyOf/allOf/not nesting in
# the schema, never the size of `value`), but a depth cap is still enforced defensively.
# ---------------------------------------------------------------------------
_SCHEMA_MAX_DEPTH = 64


def _schema_deref(schema_root: dict, node: dict) -> dict:
    if "$ref" in node and node["$ref"].startswith("#/$defs/"):
        return schema_root["$defs"][node["$ref"][len("#/$defs/"):]]
    return node


def _schema_check_type(schema_root: dict, node: dict, t, value, depth: int) -> bool:
    if isinstance(t, list):
        return any(_schema_check_type(schema_root, node, tt, value, depth) for tt in t)
    if t == "object":
        if not isinstance(value, dict):
            return False
        for key in node.get("required", []):
            if key not in value:
                return False
        props = node.get("properties", {})
        if node.get("additionalProperties") is False and (set(value) - set(props)):
            return False
        for key, sub in props.items():
            if key in value and not _schema_check(schema_root, sub, value[key], depth + 1):
                return False
        return True
    if t == "array":
        if not isinstance(value, list):
            return False
        if "minItems" in node and len(value) < node["minItems"]:
            return False
        items = node.get("items")
        return items is None or all(_schema_check(schema_root, items, v, depth + 1) for v in value)
    if t == "string":
        if not isinstance(value, str):
            return False
        if "minLength" in node and len(value) < node["minLength"]:
            return False
        if "maxLength" in node and len(value) > node["maxLength"]:
            return False
        if "pattern" in node and re.search(node["pattern"], value) is None:
            return False
        return True
    if t == "integer":
        if not fc.is_schema_integer(value):
            return False
        return not ("minimum" in node and value < node["minimum"])
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def _schema_check(schema_root: dict, node: dict, value, depth: int = 0) -> bool:
    if depth > _SCHEMA_MAX_DEPTH:
        return False
    node = _schema_deref(schema_root, node)
    if not isinstance(node, dict):
        return False
    checks = []
    if "$ref" in node:
        checks.append(_schema_check(schema_root, {"$ref": node["$ref"]}, value, depth + 1))
    if "oneOf" in node:
        checks.append(sum(1 for sub in node["oneOf"]
                           if _schema_check(schema_root, sub, value, depth + 1)) == 1)
    if "anyOf" in node:
        checks.append(any(_schema_check(schema_root, sub, value, depth + 1) for sub in node["anyOf"]))
    if "allOf" in node:
        checks.append(all(_schema_check(schema_root, sub, value, depth + 1) for sub in node["allOf"]))
    if "not" in node:
        checks.append(not _schema_check(schema_root, node["not"], value, depth + 1))
    if "enum" in node:
        checks.append(value in node["enum"])
    if "const" in node:
        checks.append(value == node["const"])
    if "required" in node and "type" not in node:
        checks.append(isinstance(value, dict) and all(k in value for k in node["required"]))
    t = node.get("type")
    if t is not None:
        checks.append(_schema_check_type(schema_root, node, t, value, depth))
    if not checks:
        return True  # an unconstrained schema fragment (e.g. bare {}) matches anything
    return all(checks)


def _schema_predicate_registry(schema: dict, def_name: str) -> dict:
    """Read one typed predicate registry from the schema into the Python registry's shape."""
    registry = {}
    for branch in schema["$defs"][def_name]["oneOf"]:
        if branch.get("type") != "object" or branch.get("additionalProperties") is not False:
            raise ValueError(f"{def_name} branch is not a closed object")
        props = branch.get("properties", {})
        name = props.get("predicate", {}).get("const")
        if not isinstance(name, str) or name in registry:
            raise ValueError(f"{def_name} branch has missing/duplicate predicate const")
        required = set(branch.get("required", []))
        if "predicate" not in required:
            raise ValueError(f"{def_name}.{name} does not require predicate")
        params = {}
        for param, param_schema in props.items():
            if param == "predicate":
                continue
            if param_schema.get("$ref") == "#/$defs/duration":
                kind = "duration"
            elif param_schema.get("type") == "string":
                kind = "str"
            elif (param_schema.get("type") == "integer"
                  and param_schema.get("minimum") == fc.PREDICATE_INT_MIN
                  and param_schema.get("maximum") == fc.PREDICATE_INT_MAX):
                kind = "int"
            else:
                raise ValueError(f"{def_name}.{name}.{param} has unknown kind/bounds")
            params[param] = (param in required, kind)
        registry[name] = {"required": required, "params": params}
    return registry


def _python_predicate_registry(registry: dict) -> dict:
    return {
        name: {
            "required": {"predicate"} | {
                param for param, (required, _kind) in params.items() if required
            },
            "params": {
                param: (required, "duration" if name == "fresh" and param == "max_age" else kind)
                for param, (required, kind) in params.items()
            },
        }
        for name, params in registry.items()
    }


def _run_selftest_schema_checks(schema: dict) -> list:
    """Returns a list of (label, ok) pairs for the schema-root-selection and leaf/combinator
    structural proofs, below."""
    results = []

    ok = _schema_check(schema, schema, {}) is False
    results.append(("schema-root-rejects-empty-object", ok))

    valid_contract = {
        "fill_version": "0.1.0", "template": "t",
        "contracts": [{
            "file": "X.md", "importance": "normal", "urgency_after_due": "7d",
            "filled_when": {"predicate": "no-marker"},
            "due_when": {"any": [{"predicate": "instantiated"}]},
        }],
    }
    ok = _schema_check(schema, schema, valid_contract) is True
    results.append(("schema-root-accepts-a-real-contract", ok))

    plain_leaf = {"predicate": "no-marker"}
    ok = _schema_check(schema, schema["$defs"]["filled_predicate_leaf"], plain_leaf) is True
    results.append(("predicate-leaf-accepts-a-plain-leaf", ok))

    combinator_leaf = {"predicate": "no-marker", "any": [{"predicate": "no-marker"}]}
    ok = _schema_check(schema, schema["$defs"]["filled_predicate_leaf"], combinator_leaf) is False
    results.append(("predicate-leaf-rejects-a-combinator-key", ok))

    return results


# ---------------------------------------------------------------------------
# A very deep predicate-expression tree, run in a SEPARATE PROCESS with a hard wall-clock timeout
# (trigger_fire.py's run-case pattern): proves validate_expr/expr_has_predicate stay iterative and
# bounded (fc.MAX_EXPR_DEPTH) rather than crashing with a Python RecursionError or hanging, on an
# input far deeper than any real contract would ever nest.
# ---------------------------------------------------------------------------
_DEEP_TREE_DEPTH = 5000


def _build_deep_tree_contract(depth: int) -> dict:
    node = {"predicate": "no-marker"}
    for _ in range(depth):
        node = {"not": node}
    return {
        "fill_version": "0.1.0", "template": "t",
        "contracts": [{
            "file": "f.md", "importance": "normal", "urgency_after_due": "1d",
            "filled_when": node,
            "due_when": {"any": [{"predicate": "instantiated"}]},
        }],
    }


def _run_deep_tree_case() -> dict:
    contract = _build_deep_tree_contract(_DEEP_TREE_DEPTH)
    errors = fc.validate_contract(contract)
    leaves: list = []
    fc.walk_leaves(contract["contracts"][0]["filled_when"], leaves)
    has_pred = fc.expr_has_predicate(contract["contracts"][0]["filled_when"], "no-marker")
    return {
        "error_codes": sorted({c for c, _ in errors}),
        "depth_error_present": any("nests deeper than" in r for _, r in errors),
        "walk_leaves_count": len(leaves),
        "expr_has_predicate": has_pred,
    }


def _run_deep_tree_case_bounded(max_seconds: float) -> tuple:
    """Runs `_run_deep_tree_case` in a SEPARATE PROCESS with a hard wall-clock timeout — measuring
    elapsed time for an in-process call that never returns measures nothing; only a subprocess
    timeout can actually interrupt a genuine stall. Returns (result_dict_or_None, elapsed, timed_out)."""
    import subprocess
    import time
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "run-case", "deep-tree"],
            cwd=str(HERE), capture_output=True, text=True, timeout=max_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - start, True
    elapsed = time.monotonic() - start
    try:
        return json.loads(result.stdout), elapsed, False
    except json.JSONDecodeError:
        return None, elapsed, False




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
        return False, f"stdout violates $defs.validate_output: {schema_errors}"
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
        cases_data = json.loads((FIXTURES / "validate" / "cases.json").read_text(encoding="utf-8"))
        cases = cases_data.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases.json must contain a nonempty 'cases' array")

        results = []
        for case in cases:
            code, report = _run_case_via_cli(case)
            ok = case_matches(case, code, report)
            results.append(ok)
            print(f"selftest {case['name']}: expected exit={case['expected_exit']} got exit={code} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
            if not ok:
                print(fc.emit_report(report, indent=2, sort_keys=True, allow_nan=False))

        integer_diff_ok, integer_rewrites, integer_diff_failures = (
            _integer_spelling_differential(cases))
        old_check_control_fired, old_check_exit, old_check_detail = _old_integer_check_control()
        results.extend((integer_diff_ok, old_check_control_fired))
        print(f"selftest schema-integer-integral-float-differential: "
              f"{integer_rewrites} integer spelling(s), {len(integer_diff_failures)} failure(s) "
              f"{integer_diff_failures[:3]!r}; old-isinstance-int control "
              f"exit={old_check_exit} detail={old_check_detail!r} "
              f"[{'ok' if integer_diff_ok and old_check_control_fired else 'MISMATCH'}]")

        schema_diff_ok, schema_variants, schema_semantic, schema_diff_failures = (
            _input_schema_differential(cases))
        results.append(schema_diff_ok)
        print(f"selftest input-schema-cli-differential-contracts: "
              f"{schema_variants} generated variant(s), "
              f"{schema_semantic} documented semantic rejection(s), "
              f"{len(schema_diff_failures)} failure(s) {schema_diff_failures[:3]!r} "
              f"[{'ok' if schema_diff_ok else 'MISMATCH'}]")

        usage_output = io.StringIO()
        with contextlib.redirect_stdout(usage_output):
            usage_code = main(["validate"])
        usage_report = json.loads(usage_output.getvalue())
        usage_code = usage_code if usage_report.get("exit_code") == usage_code else -1
        results.append(usage_code == E_USAGE)
        print(f"selftest usage-error: expected exit=64 got exit={usage_code} "
              f"[{'ok' if usage_code == E_USAGE else 'MISMATCH'}]")

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
        exception_ok = (exception_code == E_INPUT and
                        exception_report["errors"][0]["reason"].startswith("input/runtime failure"))
        results.append(exception_ok)
        print(f"selftest exit-65-injected-exception-catch-all: got exit={exception_code} "
              f"[{'ok' if exception_ok else 'MISMATCH'}]")

        expected_exits = {0, 10, 11, 12, 13, 14, 15, 16, 65}
        covered_exits = {case["expected_exit"] for case in cases}
        missing_exits = sorted(expected_exits - covered_exits)
        coverage_ok = not missing_exits
        results.append(coverage_ok)
        print(f"selftest exit-code-coverage: exits {sorted(expected_exits)}; "
              f"missing={missing_exits or 'none'} [{'ok' if coverage_ok else 'MISMATCH'}]")

        used = {case["contract"] for case in cases}
        fixture_files = {p.name for p in (FIXTURES / "validate").iterdir()
                          if p.name != "cases.json" and p.suffix == ".toml"}
        unused_fixtures = sorted(fixture_files - used)
        fixture_coverage = not unused_fixtures
        results.append(fixture_coverage)
        print(f"selftest fixture-coverage: unused={unused_fixtures or 'none'} "
              f"[{'ok' if fixture_coverage else 'MISMATCH'}]")

        # schema<->validator drift guard: names, parameter names, requiredness, kinds, integer
        # bounds, and fresh.max_age's duration shape are all read from the schema and compared to
        # the executable registries. A planted extra schema parameter proves the check can fail.
        schema = json.loads((HERE.parent / "schema" / "fill-contract-0.1.0-draft.schema.json")
                             .read_text(encoding="utf-8"))
        filled_defs = schema.get("$defs", {}).get("filled_predicate_name", {}).get("enum", [])
        due_defs = schema.get("$defs", {}).get("due_predicate_name", {}).get("enum", [])
        importance_defs = schema.get("$defs", {}).get("importance", {}).get("enum", [])
        filled_schema_registry = _schema_predicate_registry(schema, "filled_predicate_leaf")
        due_schema_registry = _schema_predicate_registry(schema, "due_predicate_leaf")
        filled_python_registry = _python_predicate_registry(fc.FILLED_PREDICATES)
        due_python_registry = _python_predicate_registry(fc.DUE_PREDICATES)
        drift_ok = (filled_schema_registry == filled_python_registry
                    and due_schema_registry == due_python_registry
                    and set(filled_defs) == set(fc.FILLED_PREDICATES)
                    and set(due_defs) == set(fc.DUE_PREDICATES)
                    and set(importance_defs) == set(fc.IMPORTANCE_ENUM))
        results.append(drift_ok)
        print(f"selftest schema-predicate-registry-drift: filled-match="
              f"{filled_schema_registry == filled_python_registry} "
              f"due-match={due_schema_registry == due_python_registry} "
              f"importance-match={set(importance_defs) == set(fc.IMPORTANCE_ENUM)} "
              f"[{'ok' if drift_ok else 'MISMATCH'}]")

        planted_schema = copy.deepcopy(schema)
        planted_branch = planted_schema["$defs"]["filled_predicate_leaf"]["oneOf"][0]
        planted_branch["properties"]["__selftest_extra_param__"] = {"type": "string"}
        planted_registry = _schema_predicate_registry(planted_schema, "filled_predicate_leaf")
        drift_control_fired = planted_registry != filled_python_registry
        results.append(drift_control_fired)
        print(f"selftest schema-predicate-registry-drift-planted-extra-param: "
              f"control-fired={drift_control_fired} "
              f"[{'ok' if drift_control_fired else 'MISMATCH'}]")

        required_schema = copy.deepcopy(schema)
        required_schema["$defs"]["filled_predicate_leaf"]["oneOf"][0]["required"].append(
            "__selftest_undeclared_required__")
        required_registry = _schema_predicate_registry(
            required_schema, "filled_predicate_leaf")
        required_control_fired = required_registry != filled_python_registry
        results.append(required_control_fired)
        print(f"selftest schema-predicate-registry-drift-planted-undeclared-required: "
              f"control-fired={required_control_fired} "
              f"[{'ok' if required_control_fired else 'MISMATCH'}]")

        # the schema root selects $defs.contract (not a bare $defs library that
        # would let an arbitrary object, including {}, validate), and a typed leaf rejects a leaf
        # carrying a combinator key — proved structurally, not only asserted in prose.
        for label, ok in _run_selftest_schema_checks(schema):
            results.append(ok)
            print(f"selftest {label}: [{'ok' if ok else 'MISMATCH'}]")

        # a WITHIN-BOUND deep tree (well under fc.MAX_EXPR_DEPTH), complementing the beyond-bound
        # case below: proves walk_leaves/expr_has_predicate correctly find a leaf that legitimately
        # nests several levels down, not just that they safely give up beyond the bound.
        shallow_contract = _build_deep_tree_contract(10)
        shallow_errors = fc.validate_contract(shallow_contract)
        shallow_leaves: list = []
        fc.walk_leaves(shallow_contract["contracts"][0]["filled_when"], shallow_leaves)
        shallow_has_pred = fc.expr_has_predicate(shallow_contract["contracts"][0]["filled_when"], "no-marker")
        ok = shallow_errors == [] and len(shallow_leaves) == 1 and shallow_has_pred is True
        results.append(ok)
        print(f"selftest within-bound-nested-tree: errors={shallow_errors} "
              f"leaves={len(shallow_leaves)} has_pred={shallow_has_pred} [{'ok' if ok else 'MISMATCH'}]")

        # a predicate-expression tree 5000 levels deep: validate_expr/walk_leaves/
        # expr_has_predicate must stay iterative and terminate well within a small bound, and
        # validate_contract must report the depth-exceeded error (code 13), not silently truncate.
        deep_max_seconds = 10.0
        deep_result, elapsed, timed_out = _run_deep_tree_case_bounded(deep_max_seconds)
        if timed_out:
            results.append(False)
            print(f"selftest deep-expression-tree: killed after {elapsed:.3f}s, over the "
                  f"{deep_max_seconds}s bound (subprocess timeout) [MISMATCH]")
        else:
            # The leaf predicate sits 5000 levels down, past fc.MAX_EXPR_DEPTH (64): walk_leaves and
            # expr_has_predicate both stop descending at the bound (documented, defensive — a tree
            # this deep is already rejected by validate_expr before any tool trusts it), so neither
            # finds the leaf. That is the correct, bounded answer, not a bug in the walkers.
            ok = (deep_result is not None
                  and deep_result.get("error_codes") == [13]
                  and deep_result.get("depth_error_present") is True
                  and deep_result.get("walk_leaves_count") == 0
                  and deep_result.get("expr_has_predicate") is False)
            results.append(ok)
            print(f"selftest deep-expression-tree: depth={_DEEP_TREE_DEPTH} result={deep_result} "
                  f"({elapsed:.3f}s, subprocess-bounded) [{'ok' if ok else 'MISMATCH'}]")

        # Each duration field preserves its own error class before integer conversion.
        import tomllib
        base = tomllib.loads((FIXTURES / "validate" / "eval-due-state.toml").read_text())
        duration_ok = True
        for value in ("9" * 5000 + "d", "9" * 16 + "d", "36501d", "0d"):
            for field, expected in (("urgency", 12), ("fresh", 13), ("retrigger", 16)):
                doc = copy.deepcopy(base)
                entry = doc["contracts"][0]
                if field == "urgency":
                    entry["urgency_after_due"] = value
                elif field == "fresh":
                    entry["filled_when"] = {"predicate": "fresh", "max_age": value}
                else:
                    entry["retrigger"] = {"every": value}
                duration_ok &= [c for c, _ in fc.validate_contract(doc)] == [expected]
        for value, days in (("1d", 1), ("36500d", 36500), ("0" * 5000 + "1d", 1)):
            parsed = fc.parse_duration(value)
            duration_ok &= parsed is not None and parsed.days == days
        results.append(duration_ok)
        print(f"selftest duration-decimal-length-and-field-codes: "
              f"[{'ok' if duration_ok else 'MISMATCH'}]")

        # TOML permits these floats; no field in this contract permits them, including
        # carried adequacy/retrigger values and unknown fields at any nesting level.
        finite_ok = True
        paths = [("template",), ("fill_version",), ("extra",),
                 ("contracts", 0, "file"), ("contracts", 0, "importance"),
                 ("contracts", 0, "urgency_after_due"), ("contracts", 0, "template_rows"),
                 ("contracts", 0, "filled_when", "predicate"),
                 ("contracts", 0, "filled_when", "n"),
                 ("contracts", 0, "adequacy", "decided_by"),
                 ("contracts", 0, "adequacy", "when"),
                 ("contracts", 0, "adequacy", "extra"),
                 ("contracts", 0, "retrigger", "on"),
                 ("contracts", 0, "retrigger", "every"),
                 ("contracts", 0, "retrigger", "extra")]
        for literal in ("nan", "inf", "-inf", "1e999"):
            value = tomllib.loads("x=" + literal)["x"]
            for path in paths:
                doc = copy.deepcopy(base)
                entry = doc["contracts"][0]
                entry["adequacy"] = {"decided_by": "human", "when": "on-filled"}
                entry["retrigger"] = {"every": "1d"}
                node = doc
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                errors = fc.validate_contract(doc)
                report = _error_report(min((c for c, _ in errors), default=0),
                                       [{"code": c, "reason": r} for c, r in errors])
                finite_ok &= bool(errors) and fc.load_json_strict(
                    json.dumps(report, allow_nan=False))[1] is None
        results.append(finite_ok)
        print(f"selftest non-finite-carried-fields: [{'ok' if finite_ok else 'MISMATCH'}]")

        # Bounded diagnostics, END TO END: ONE hostile contract carrying a 1,000,000-character
        # string (or key) in every distinct field family whose validation error embeds the
        # offending value -- an enum violation (importance), a bad duration (urgency_after_due), an
        # unknown predicate name, an unknown predicate parameter KEY, a bad max_age, a bad retrigger
        # event name, a bad retrigger duration, a bad adequacy enum, an unknown top-level key, and
        # an unknown contract-entry key -- asserts every single (code, reason) this produces is no
        # longer than fc.DIAGNOSTIC_REASON_MAX_CHARS, the documented diagnostic bound (see the
        # README). Dropping the pre-repr truncation in fc._safe_repr_scalar (or reverting any of
        # the call sites below back to a raw f"{value!r}") makes this fail: several of these ten
        # reasons would otherwise be on the order of 1,000,000 characters long.
        HOSTILE = "x" * 1_000_000
        hostile_doc = copy.deepcopy(base)
        hostile_doc[HOSTILE] = "unknown-top-level-key"
        hostile_entry = hostile_doc["contracts"][0]
        hostile_entry["importance"] = HOSTILE
        hostile_entry["urgency_after_due"] = HOSTILE
        hostile_entry["filled_when"] = {"any": [
            {"predicate": HOSTILE},
            {"predicate": "no-marker", HOSTILE: 1},
            {"predicate": "fresh", "max_age": HOSTILE},
        ]}
        hostile_entry["retrigger"] = {"on": [HOSTILE], "every": HOSTILE}
        hostile_entry["adequacy"] = {"decided_by": "human", "when": HOSTILE}
        hostile_entry[HOSTILE] = "unknown-entry-key"
        hostile_errors = fc.validate_contract(hostile_doc)
        hostile_lengths = [len(reason) for _, reason in hostile_errors]
        hostile_ok = (len(hostile_errors) == 10
                      and all(n <= fc.DIAGNOSTIC_REASON_MAX_CHARS for n in hostile_lengths))
        results.append(hostile_ok)
        print(f"selftest bounded-diagnostics-hostile-contract-document: "
              f"{len(hostile_errors)} errors max_reason_len={max(hostile_lengths, default=0)} "
              f"cap={fc.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if hostile_ok else 'MISMATCH'}]")

        # A CLI-reachable oversized `importance` value (a 1,000,000-character string), validated
        # through the SAME `run_validate` path `main()`'s own `validate` command uses (load a real
        # fixture off disk, parse it, run it through the full CLI report construction) -- not
        # merely the in-process `fc.validate_contract` call above, so this also proves the bound
        # holds end to end through the actual CLI entry point, not only through the library call.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cli_huge_importance_path = pathlib.Path(tmp) / "cli-huge-importance.toml"
            base_text = (FIXTURES / "validate" / "eval-due-state.toml").read_text(encoding="utf-8")
            cli_huge_importance_path.write_text(
                re.sub(r'importance\s*=\s*"[^"]+"', f'importance="{HOSTILE}"', base_text),
                encoding="utf-8")
            cli_code, cli_report = _run_validate_via_cli(cli_huge_importance_path)
            cli_reasons = [e["reason"] for e in cli_report["errors"]]
        cli_ok = (cli_code == 12 and bool(cli_reasons)
                  and all(len(r) <= fc.DIAGNOSTIC_REASON_MAX_CHARS for r in cli_reasons))
        results.append(cli_ok)
        print(f"selftest cli-reachable-oversized-importance-value: exit={cli_code} "
              f"max_reason_len={max((len(r) for r in cli_reasons), default=0)} "
              f"cap={fc.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if cli_ok else 'MISMATCH'}]")

        # `fc.safe_repr_for_error` bounds BOTH an adversarially DEEP container (no Python
        # recursion, so no stack overflow) and an adversarially WIDE one (no full materialization,
        # so no O(size) cost) -- bounded TOTAL work and TOTAL output length regardless of either
        # axis, timed with a generous wall-clock bound (subprocess-independent: this is a pure,
        # in-process function call, not a git/filesystem operation).
        import time as _time
        start = _time.monotonic()
        deep_container = []
        cur = deep_container
        for _ in range(200_000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        deep_text = fc.safe_repr_for_error(deep_container)
        wide_container = list(range(2_000_000))
        wide_text = fc.safe_repr_for_error(wide_container)
        nested_huge_int = {"a": [10 ** 5000, 1, {"b": 10 ** 5000}]}
        nested_text = fc.safe_repr_for_error(nested_huge_int)
        # A top-level scalar `str` is bounded the SAME way a huge value nested inside a container
        # is -- truncated to `_SAFE_REPR_STR_PREVIEW_CHARS` BEFORE repr() ever sees it, never repr'd
        # in full first and truncated after (the early-return `if not _safe_repr_is_container(value)`
        # path in `safe_repr_for_error` is the single most common call-site shape: an enum value, a
        # glob, an id -- so this axis must be checked independently of the container-walk cases
        # above, which would not catch a regression here).
        huge_scalar_text = fc.safe_repr_for_error("x" * 1_000_000)
        huge_scalar_nested_text = fc.safe_repr_for_error(["x" * 1_000_000])
        elapsed = _time.monotonic() - start
        bounded_ok = (len(deep_text) <= fc._SAFE_REPR_MAX_CHARS + 10
                      and len(wide_text) <= fc._SAFE_REPR_MAX_CHARS + 10
                      and "digit integer" in nested_text and "5001" in nested_text
                      and len(huge_scalar_text) <= fc._SAFE_REPR_MAX_CHARS + 10
                      and len(huge_scalar_nested_text) <= fc._SAFE_REPR_MAX_CHARS + 10
                      and elapsed < 5.0)
        results.append(bounded_ok)
        print(f"selftest safe-repr-for-error-bounds-deep-and-wide-containers: "
              f"deep_len={len(deep_text)} wide_len={len(wide_text)} "
              f"nested={nested_text!r} huge_scalar_len={len(huge_scalar_text)} "
              f"huge_scalar_nested_len={len(huge_scalar_nested_text)} "
              f"({elapsed:.3f}s) [{'ok' if bounded_ok else 'MISMATCH'}]")

        # Regression: a `--contract` argument that is itself a hostile, 10,000-character path
        # (never written to disk — the failure is
        # `fc.load_toml`'s own `OSError`, not a TOML parse error) used to make `cannot read contract
        # file <path>: <exc>` embed the SAME oversized path TWICE (once interpolated raw, once again
        # inside the exception's own `str()`), well past the documented bound (an earlier
        # reproduction measured 20,061 characters). `fc.bound_path_preview` plus the exception's
        # CLASS name (never `str(exc)`) at the source, and `_error_report`'s own unconditional
        # `fc.bound_reason` pass as the final backstop, now cap it.
        huge_contract_path = pathlib.Path("x" * 10_000)
        try:
            _run_validate_via_cli(huge_contract_path)
            huge_path_reasons = None
        except fc.FillError as exc:
            huge_path_report = _error_report(exc.code, [{"code": exc.code, "reason": str(exc)}])
            huge_path_reasons = [e["reason"] for e in huge_path_report["errors"]]
        huge_path_ok = (huge_path_reasons is not None and bool(huge_path_reasons)
                        and all(len(r) <= fc.DIAGNOSTIC_REASON_MAX_CHARS for r in huge_path_reasons))
        results.append(huge_path_ok)
        print(f"selftest cli-reachable-oversized-contract-path: "
              f"max_reason_len={max((len(r) for r in (huge_path_reasons or [])), default=0)} "
              f"cap={fc.DIAGNOSTIC_REASON_MAX_CHARS} [{'ok' if huge_path_ok else 'MISMATCH'}]")

        # Regression: a `--contract` path that is not valid Unicode at all (the OS gives
        # Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a lone
        # UTF-16 surrogate in the decoded argv string) must still produce a report whose `reason`
        # text re-encodes as UTF-8 cleanly, with the exit code unchanged and the reason within the
        # documented cap. Mutation that turns this red: reverting `fc.bound_path_preview` to
        # `s = str(path)` with no escaping (the pre-fix code) makes `surrogate_utf8_ok` False.
        surrogate_contract_path = pathlib.Path("missing-\udcff")
        try:
            _run_validate_via_cli(surrogate_contract_path)
            surrogate_reason = None
            surrogate_code = None
        except fc.FillError as exc:
            surrogate_code = exc.code
            surrogate_reason = str(exc)
        surrogate_utf8_ok = True
        if surrogate_reason is not None:
            try:
                surrogate_reason.encode("utf-8")
            except UnicodeEncodeError:
                surrogate_utf8_ok = False
        surrogate_ok = (surrogate_code == 65 and surrogate_reason is not None and surrogate_utf8_ok
                        and len(surrogate_reason) <= fc.DIAGNOSTIC_REASON_MAX_CHARS
                        and "\udcff" not in surrogate_reason)
        results.append(surrogate_ok)
        print(f"selftest lone-surrogate-contract-path-escaped-in-report: exit={surrogate_code} "
              f"reason={surrogate_reason!r} [{'ok' if surrogate_ok else 'MISMATCH'}]")

        # Direct, non-CLI exactness checks on the cap primitives themselves: `fc.bound_reason` and
        # `fc._cap_with_marker` must return a result no longer than the requested cap for ANY input,
        # including one already exactly at the cap (no marker should be added when none is needed),
        # and `fc.safe_repr_for_error` on a heavily-escaped string stays within its own cap.
        # Bypassing `fc.bound_reason` in any report constructor, or reverting `fc._cap_with_marker`
        # to a bare `s[:n] + marker` (the pre-fold defect that returned 1,004 characters for a
        # 1,000-character cap), makes one of these fail.
        exact_cap_text = "z" * fc.DIAGNOSTIC_REASON_MAX_CHARS
        exact_cap_ok = (fc.bound_reason(exact_cap_text) == exact_cap_text)
        one_over_result = fc.bound_reason("z" * (fc.DIAGNOSTIC_REASON_MAX_CHARS + 1))
        one_over_ok = len(one_over_result) == fc.DIAGNOSTIC_REASON_MAX_CHARS
        huge_reason_result = fc.bound_reason("w" * 5_000_000)
        huge_reason_ok = len(huge_reason_result) == fc.DIAGNOSTIC_REASON_MAX_CHARS
        many_escapes_result = fc.safe_repr_for_error("\\" * 5000)
        many_escapes_ok = len(many_escapes_result) <= fc._SAFE_REPR_MAX_CHARS
        cap_exactness_ok = exact_cap_ok and one_over_ok and huge_reason_ok and many_escapes_ok
        results.append(cap_exactness_ok)
        print(f"selftest bound-reason-and-cap-with-marker-exactness: "
              f"exact_cap_len={len(exact_cap_text)} one_over_result_len={len(one_over_result)} "
              f"huge_result_len={len(huge_reason_result)} "
              f"many_escapes_result_len={len(many_escapes_result)} "
              f"[{'ok' if cap_exactness_ok else 'MISMATCH'}]")

        print(f"selftest version-handshake: this validator implements {VALIDATOR_VERSION!r}")

        # The regression that closes the class (argv fuzz).
        argv_fuzz_baselines = {
            "validate": ["validate", "--contract", str(FIXTURES / "validate" / "01-valid.toml")],
        }
        argv_fuzz_exits = set(expected_exits) | {E_USAGE}
        argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
            HERE / "fill_validate.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_failures = _encoding_regression_failures(
            HERE / "fill_validate.py",
            [(argv_fuzz_baselines["validate"], {0}), (["validate"], {E_USAGE})])
        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        output_checks = fc.output_boundary_selftest(
            HERE / "fill_validate.py", argv_fuzz_baselines["validate"], ["validate"])
        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "fill_validate.py", "_error_report", "run_validate",
            ["validate"], argv_fuzz_baselines["validate"])
        for name, (ok, detail) in boundary_controls.items():
            results.append(ok)
            print(f"selftest report-boundary-defect-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
        boundary_controls_ok = all(ok for ok, _ in boundary_controls.values())
        print(f"selftest report-schema-conformance: real boundary controls="
              f"{sorted(boundary_controls)} "
              f"[{'ok' if boundary_controls_ok else 'MISMATCH'}]")

        status = 0 if results and all(results) and boundary_controls_ok else E_SELFTEST
        print(f"SELFTEST {'PASS' if status == 0 else 'FAIL'}: {len(results)} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__}: {exc})")
        return E_SELFTEST


def _main(argv=None) -> int:
    try:
        parser = make_parser()
        args = parser.parse_args(argv)
        if args.selftest:
            if args.command is not None:
                parser.error("--selftest cannot be combined with a command")
            return run_selftest()
        if args.command == "run-case":
            if args.name != "deep-tree":
                parser.error(f"run-case: no case named {args.name!r}")
            report = _run_deep_tree_case()
            code = 0
            render_kwargs = {"allow_nan": False}
        elif args.command != "validate":
            parser.error("the 'validate' command or --selftest is required")
        else:
            code, report = run_validate(pathlib.Path(args.contract))
            render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except fc.FillError as exc:
        code = exc.code
        report = _error_report(code, [{"code": code, "reason": str(exc)}])
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except Exception as exc:
        code = E_INPUT
        report = _error_report(code, [{"code": code,
                                       "reason": f"input/runtime failure "
                                                 f"({type(exc).__name__})"}])
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    fc.write_report(fc.emit_report(report, **render_kwargs))
    return code



def main(argv=None) -> int:
    return fc.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
