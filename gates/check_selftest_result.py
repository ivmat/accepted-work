#!/usr/bin/env python3
"""Check the observer's executed and skipped self-test counts against its pinned roster."""
import argparse
import errno
import json
import os
from pathlib import Path
import re
import sys
import tempfile

# Only these existing platform-dependent probes may be skipped. Every other case
# remains mandatory. The runner independently checks these environment conditions.
SKIP_REASONS = {
    "repo-case-variant-alias-accepted": "filesystem is case-sensitive",
    "unreadable-file-permission-error": "root bypasses permission bits",
    "unreadable-parent-directory-chmod-000": "root bypasses permission bits",
    "unreadable-refs-with-real-commit-raises-not-null": "root bypasses permission bits",
    "symlink-escapes-repo": "symlink creation is unsupported or forbidden",
}
SYMLINK_UNAVAILABLE = {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP}


def unavailable_cases():
    """Probe the same temporary-filesystem and effective-user constraints as the test."""
    unavailable = {}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        for name in ("unreadable-file-permission-error",
                     "unreadable-parent-directory-chmod-000",
                     "unreadable-refs-with-real-commit-raises-not-null"):
            unavailable[name] = SKIP_REASONS[name]
    with tempfile.TemporaryDirectory(prefix="observer-capabilities-") as tmp:
        root = Path(tmp)
        probe = root / "repo"
        probe.mkdir()
        alias = root / "REPO"
        if not alias.is_dir() or not os.path.samefile(alias, probe):
            unavailable["repo-case-variant-alias-accepted"] = SKIP_REASONS[
                "repo-case-variant-alias-accepted"]
        target = root / "target"
        target.write_text("probe", encoding="utf-8")
        try:
            (probe / "link").symlink_to(target)
        except OSError as exc:
            if exc.errno not in SYMLINK_UNAVAILABLE:
                raise
            unavailable["symlink-escapes-repo"] = SKIP_REASONS["symlink-escapes-repo"]
    return unavailable


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate observer summary key: {key}")
        result[key] = value
    return result


def check_output(output, expected, unavailable):
    summaries = [line.removeprefix("SELFTEST SUMMARY: ") for line in output.splitlines()
                 if line.startswith("SELFTEST SUMMARY: ")]
    if len(summaries) != 1:
        raise ValueError("exactly one observer summary is required")
    summary = json.loads(summaries[0], object_pairs_hook=unique_pairs)
    if not isinstance(summary, dict) or set(summary) != {"passed", "failed", "skipped"}:
        raise ValueError("invalid observer summary fields")
    passed, failed, skips = summary["passed"], summary["failed"], summary["skipped"]
    if any(type(n) is not int or n < 0 for n in (passed, failed)) or not isinstance(skips, list):
        raise ValueError("invalid observer case counts")
    reported = {}
    for skip in skips:
        if not isinstance(skip, dict) or set(skip) != {"case", "reason"}:
            raise ValueError("invalid skip record")
        name, reason = skip["case"], skip["reason"]
        if not isinstance(name, str) or name in reported or SKIP_REASONS.get(name) != reason:
            raise ValueError("unknown, duplicate or unexplained skip")
        reported[name] = reason
    if reported != unavailable:
        raise ValueError("reported skips differ from independently probed platform limitations")
    if passed + failed + len(skips) != expected:
        raise ValueError(f"case roster changed: expected {expected}, got {passed + failed + len(skips)}")
    if failed or not passed or "[MISMATCH]" in output:
        raise ValueError("observer has failed cases")
    totals = re.findall(r"^SELFTEST (PASS|FAIL): ([0-9]+) cases$", output, re.MULTILINE)
    if totals != [("PASS", str(passed))]:
        raise ValueError("executed case line does not match summary")
    return f"{passed} passed, {failed} failed, {len(skips)} skipped; {expected} planned"


def selftest():
    alias = "repo-case-variant-alias-accepted"
    unavailable = {alias: SKIP_REASONS[alias]}
    root_unavailable = {name: reason for name, reason in SKIP_REASONS.items()
                        if reason == "root bypasses permission bits"}
    root_skips = [{"case": name, "reason": reason} for name, reason in root_unavailable.items()]
    def output(passed=80, failed=0, skipped=None, trailer=None):
        if skipped is None:
            skipped = [{"case": alias, "reason": SKIP_REASONS[alias]}]
        return ("SELFTEST SUMMARY: " + json.dumps(dict(passed=passed, failed=failed, skipped=skipped))
                + f"\nSELFTEST PASS: {passed if trailer is None else trailer} cases\n")
    controls = [
        ("all executed", output(81, skipped=[]), {}, True),
        ("one justified skip", output(), unavailable, True),
        ("root reports all three permission cases", output(78, skipped=root_skips), root_unavailable, True),
        ("root omits parent-directory case", output(79, skipped=[item for item in root_skips
            if item["case"] != "unreadable-parent-directory-chmod-000"]), root_unavailable, False),
        ("missing mandatory case", output(79), unavailable, False),
        ("unexpected extra case", output(81), unavailable, False),
        ("failed case", output(79, 1), unavailable, False),
        ("skip on capable platform", output(), {}, False),
        ("missing required skip report", output(81, skipped=[]), unavailable, False),
        ("unknown skipped test", output(skipped=[{"case": "mandatory", "reason": "optional"}]), unavailable, False),
        ("wrong skip reason", output(skipped=[{"case": alias, "reason": "unknown"}]), unavailable, False),
        ("duplicate skip", output(79, skipped=[{"case": alias, "reason": SKIP_REASONS[alias]}] * 2), unavailable, False),
        ("inflated executed count", output(trailer=81), unavailable, False),
        ("missing summary", "SELFTEST PASS: 81 cases\n", {}, False),
        ("duplicate summary", output() + output(), unavailable, False),
        ("duplicate summary key", output().replace('"passed": 80', '"passed": 80, "passed": 80', 1), unavailable, False),
        ("duplicate skip-record key", output().replace('"case":', '"case": "hidden", "case":', 1), unavailable, False),
        ("contradictory failure", "case [MISMATCH]\n" + output(), unavailable, False),
        ("boolean count", output(True), unavailable, False),
    ]
    for name, text, platform, should_pass in controls:
        try:
            check_output(text, 81, platform)
            accepted = True
        except (ValueError, TypeError):
            accepted = False
        if accepted != should_pass:
            raise AssertionError(name)
    print(f"SELFTEST PASS: {len(controls)} observer accounting controls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--expected-cases", type=int)
    args = parser.parse_args()
    try:
        if args.selftest:
            selftest()
        elif args.expected_cases is not None and args.expected_cases > 0:
            print(check_output(sys.stdin.read(), args.expected_cases, unavailable_cases()))
        else:
            parser.error("choose --selftest or --expected-cases N")
    except (ValueError, TypeError, OSError, AssertionError) as exc:
        print(f"observer accounting failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
