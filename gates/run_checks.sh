#!/usr/bin/env bash
# run_checks.sh -- run every format tool self-test that exists in this tree.
#
# Needs: bash, python3 (3.11 or newer). This export's own tool does not need that floor; the
# runner sets one version for every format this family may add.
# Uses nothing outside this tree. Writes nothing into this tree (bytecode is disabled).
# GATE_JOBS defaults to 2; values 1..4 are capped by CPU count. Each tool has a 900s timeout.
# Some self-tests create and remove their own temporary directories. If a self-test in this tree
# ever needs git, its tool path goes in NEEDS_GIT below, and SKIP_GIT_SELFTESTS=1 skips it.
#
# Rule: a tool that supports --selftest is run with --selftest. It passes only if it exits 0 and,
# when its expected result line is pinned below, that exact line appears in its output. A pinned
# line catches a silent change of the number of test cases. The observer instead accounts for
# all 81 planned cases as executed or independently justified platform skips.
#
# Usage:  bash gates/run_checks.sh            # run all self-tests
#         SKIP_GIT_SELFTESTS=1 bash gates/run_checks.sh   # skip the self-tests that need git
#                                                          # (format_fill-contract/tools/fill_observe.py)
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
export PYTHONDONTWRITEBYTECODE=1

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) and sys.flags.optimize == 0 else 1)' || {
  echo "run_checks: unoptimized python3 3.11 or newer is required (no -O/PYTHONOPTIMIZE)"; exit 2; }

# tool path | expected result line ("" = not pinned: exit code only)
PINNED="
format_obligation/tools/obligation_fold.py|SELFTEST PASS: 130 cases
format_relation/tools/relation_validate.py|SELFTEST PASS: 72 cases
format_trigger/tools/trigger_validate.py|SELFTEST PASS: 67 cases
format_trigger/tools/trigger_fire.py|SELFTEST PASS: 70 cases
format_impact-rule/tools/impact_validate.py|SELFTEST PASS: 88 cases
format_impact-rule/tools/impact_evaluate.py|SELFTEST PASS: 66 cases
format_fill-contract/tools/fill_validate.py|SELFTEST PASS: 79 cases
format_fill-contract/tools/fill_observe.py|SELFTEST PASS: 81 cases
format_fill-contract/tools/fill_evaluate.py|SELFTEST PASS: 77 cases
"
# Literal space-separated roster paths; no shell expansion or inline comments.
NEEDS_GIT="format_fill-contract/tools/fill_observe.py"

pass=0; fail=0; skipped=0
if python3 -B gates/check_selftest_result.py --selftest; then
  pass=$((pass+1))
else
  echo "FAIL  observer accounting controls"; fail=$((fail+1))
fi

# MATURITY's inventories are derived from this exported tree and the PINNED self-test roster:
# cases may share files, and cases.json itself is also a fixture file. This check covers every row,
# so tool additions, fixture additions, and changed pinned case totals cannot leave prose stale.
maturity_file="MATURITY.md"
[ -f "$maturity_file" ] || maturity_file="MATURITY.md.in"
if python3 -B - "$maturity_file" "$ROOT/gates/run_checks.sh" <<'PY'
import pathlib, re, sys

maturity = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
runner = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
parts = {
    "Obligation": "format_obligation",
    "Relation": "format_relation",
    "Trigger": "format_trigger",
    "Impact rule": "format_impact-rule",
    "Fill contract": "format_fill-contract",
}
pins = {}
for tool, count in re.findall(
        r"^(format_[^|]+/tools/[^|]+\.py)\|SELFTEST PASS: ([0-9]+) cases$",
        runner, re.MULTILINE):
    pins.setdefault(tool.split("/", 1)[0], []).append(int(count))
errors = []
for part, dirname in parts.items():
    row = next((line for line in maturity.splitlines() if line.startswith(f"| {part} |")), "")
    match = re.search(
        r"; ([0-9]+) tools(?: \([^;]+\))?; ([0-9]+) test-case files; "
        r"self-test cases ([0-9]+(?: \+ [0-9]+)*)", row)
    if not match:
        errors.append(f"{part}: inventory fields missing from MATURITY row")
        continue
    stated_tools = int(match.group(1))
    stated_fixtures = int(match.group(2))
    stated_cases = [int(value) for value in match.group(3).split(" + ")]
    base = pathlib.Path(dirname)
    actual_tools = len(list((base / "tools").glob("*.py")))
    actual_fixtures = sum(path.is_file() for path in (base / "fixtures").rglob("*"))
    actual_cases = pins.get(dirname, [])
    if (stated_tools, stated_fixtures, stated_cases) != \
            (actual_tools, actual_fixtures, actual_cases):
        errors.append(
            f"{part}: MATURITY {(stated_tools, stated_fixtures, stated_cases)!r} != "
            f"tree/runner {(actual_tools, actual_fixtures, actual_cases)!r}"
        )
for error in errors:
    print(error)
raise SystemExit(bool(errors))
PY
then
  echo "PASS  MATURITY tool/fixture/self-test counts agree with tree and runner pins"
  pass=$((pass+1))
else
  echo "FAIL  MATURITY tool/fixture/self-test inventory mismatch"
  fail=$((fail+1))
fi

# Each format README has one canonical Inventory line. Check every stated total against this tree
# and the same runner pins used above. Mutating the current count proves the rule can fail.
if python3 -B - "$ROOT/gates/run_checks.sh" <<'PY'
import pathlib, re, sys

root = pathlib.Path.cwd()
runner = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
pins = {}
for tool, count in re.findall(
        r"^(format_[^|]+/tools/[^|]+\.py)\|SELFTEST PASS: ([0-9]+) cases$",
        runner, re.MULTILINE):
    pins.setdefault(tool.split("/", 1)[0], []).append(int(count))

def inventory_errors(overrides=None):
    overrides = overrides or {}
    errors = []
    for dirname in ("format_obligation", "format_relation", "format_trigger",
                    "format_impact-rule", "format_fill-contract"):
        path = root / dirname / "README.md"
        if not path.exists():
            path = path.with_suffix(".md.in")
        text = overrides.get(dirname, path.read_text(encoding="utf-8"))
        match = re.search(
            r"^Inventory: ([0-9]+) files total, ([0-9]+) tools, "
            r"([0-9]+) test-case files, ([0-9]+(?: \+ [0-9]+)*) self-test cases\.$",
            text, re.MULTILINE)
        if not match:
            errors.append(f"{dirname}: canonical README Inventory line missing")
            continue
        stated = (int(match.group(1)), int(match.group(2)), int(match.group(3)),
                  [int(value) for value in match.group(4).split(" + ")])
        actual = (sum(p.is_file() for p in (root / dirname).rglob("*")),
                  len(list((root / dirname / "tools").glob("*.py"))),
                  sum(p.is_file() for p in (root / dirname / "fixtures").rglob("*")),
                  pins.get(dirname, []))
        if stated != actual:
            errors.append(f"{dirname}: README {stated!r} != tree/runner {actual!r}")
    return errors

errors = inventory_errors()
fill_path = root / "format_fill-contract" / "README.md"
if not fill_path.exists():
    fill_path = fill_path.with_suffix(".md.in")
fill = fill_path.read_text(encoding="utf-8")
count = re.search(r"^Inventory: ([0-9]+) files total,", fill, re.MULTILINE)
if count is None:
    errors.append("README file-count mutation target missing")
else:
    planted = fill[:count.start(1)] + str(int(count.group(1)) + 1) + fill[count.end(1):]
    if not inventory_errors({"format_fill-contract": planted}):
        errors.append("README current-count-plus-one planted control did not fire")
for error in errors:
    print(error)
raise SystemExit(bool(errors))
PY
then
  echo "PASS  README file/tool/fixture/self-test counts agree; current-count-plus-one control fired"
  pass=$((pass+1))
else
  echo "FAIL  README inventory mismatch or inert planted control"
  fail=$((fail+1))
fi

# The pinned roster remains authoritative. Audited independent suites use bounded
# subprocess parallelism; unknown suites run exclusively. No results are cached.
if python3 -B gates/selftest_jobs.py --selftest; then
  pass=$((pass+1))
else
  echo "FAIL  subprocess scheduling controls"; fail=$((fail+1))
fi
python3 -B gates/selftest_jobs.py --public "$ROOT" \
  --initial-passed "$pass" --initial-failed "$fail"
