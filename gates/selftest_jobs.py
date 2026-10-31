#!/usr/bin/env python3
"""Bounded subprocess scheduling for audited isolated self-tests; no cache or skipped work."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

# These suites mutate only fresh temporary copies/repositories. New suites are
# exclusive until their helpers and mutation controls have been checked too.
PARALLEL_SAFE = frozenset((path, ("--selftest",)) for path in {
    "format_obligation/tools/obligation_fold.py",
    "format_relation/tools/relation_validate.py",
    "format_trigger/tools/trigger_validate.py",
    "format_trigger/tools/trigger_fire.py",
    "format_impact-rule/tools/impact_validate.py",
    "format_impact-rule/tools/impact_evaluate.py",
    "format_fill-contract/tools/fill_validate.py",
    "format_fill-contract/tools/fill_observe.py",
    "format_fill-contract/tools/fill_evaluate.py",
})
TIMEOUT_SECONDS = 900
_ACTIVE = set()
_ACTIVE_LOCK = threading.RLock()
_CANCELLED = threading.Event()


def terminate(proc):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()


def cancel_jobs():
    _CANCELLED.set()
    with _ACTIVE_LOCK:
        for proc in list(_ACTIVE):
            terminate(proc)



@dataclass(frozen=True)
class Job:
    name: str
    command: tuple
    cwd: str
    parallel: bool = False
    timeout: float = TIMEOUT_SECONDS


@dataclass(frozen=True)
class Result:
    name: str
    returncode: int
    output: str
    elapsed: float
    error: str = ""


def require_unoptimized():
    if sys.flags.optimize > 0:
        raise ValueError("self-test scheduling requires unoptimized Python (no -O/PYTHONOPTIMIZE)")


def workers_from_environment():
    value = os.environ.get("GATE_JOBS", "2")
    if not re.fullmatch(r"[1-4]", value):
        raise ValueError("GATE_JOBS must be an integer from 1 to 4")
    return min(int(value), max(1, os.cpu_count() or 1))


def run_job(job):
    started = time.monotonic()
    if _CANCELLED.is_set():
        return Result(job.name, -1, "", 0, "runner cancelled")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONOPTIMIZE", None)
    try:
        proc = subprocess.Popen(job.command, cwd=job.cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=(os.name == "posix"))
    except OSError as exc:
        return Result(job.name, -1, "", time.monotonic() - started, f"could not start: {exc}")
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
        if _CANCELLED.is_set():
            terminate(proc)
    error = ""
    try:
        try:
            raw, _ = proc.communicate(timeout=job.timeout)
        except subprocess.TimeoutExpired:
            error = f"timed out after {job.timeout:g}s"
            terminate(proc)
            try:
                raw, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raw = exc.output or b""
                error += "; descendant output remained open after termination"
                proc.stdout.close()
                proc.wait(timeout=5)
    finally:
        # The leader may have exited while background children remain in its group.
        if os.name == "posix" or proc.poll() is None:
            terminate(proc)
        if proc.poll() is None:
            proc.wait(timeout=5)
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    try:
        output = raw.decode("utf-8")
    except UnicodeDecodeError:
        output = raw.decode("utf-8", "replace")
        error = error or "output is not UTF-8"
    return Result(job.name, proc.returncode, output, time.monotonic() - started, error)


def checked_results(jobs, results):
    names = [job.name for job in jobs]
    if len(results) != len(names) or any(not isinstance(r, Result) for r in results):
        raise RuntimeError("missing or malformed job result")
    for r in results:
        if (not isinstance(r.name, str) or type(r.returncode) is not int
                or not isinstance(r.output, str) or not isinstance(r.error, str)
                or type(r.elapsed) not in (int, float) or not math.isfinite(r.elapsed)
                or r.elapsed < 0):
            raise RuntimeError("malformed job result fields")
    if len({r.name for r in results}) != len(results) or {r.name for r in results} != set(names):
        raise RuntimeError("job/result roster mismatch")
    by_name = {r.name: r for r in results}
    return [by_name[name] for name in names]


def safe_run_job(job):
    try:
        return run_job(job)
    except Exception as exc:
        # A runner failure is a failed job, not a reason to drop remaining checks.
        return Result(job.name, -1, "", 0, f"runner failure: {type(exc).__name__}: {exc}")


def run_jobs(jobs, workers=None):
    """Run isolated jobs together, then exclusive jobs alone; return original roster order."""
    require_unoptimized()
    jobs = list(jobs)
    if not jobs or len({job.name for job in jobs}) != len(jobs):
        raise ValueError("job roster must be nonempty with unique names")
    if any(not job.command or type(job.timeout) not in (int, float)
           or not math.isfinite(job.timeout) or job.timeout <= 0 for job in jobs):
        raise ValueError("every job needs a command and positive timeout")
    workers = workers_from_environment() if workers is None else workers
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("worker count must be 1..4")
    parallel = [job for job in jobs if job.parallel]
    exclusive = [job for job in jobs if not job.parallel]
    results = []
    _CANCELLED.clear()
    previous = {}
    def interrupted(signum, frame):
        cancel_jobs()
        raise KeyboardInterrupt(f"signal {signum}")
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupted)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                # map yields in submission order even if subprocesses finish out of order.
                results.extend(pool.map(safe_run_job, parallel))
            except BaseException:
                cancel_jobs()
                raise
        for job in exclusive:
            results.append(safe_run_job(job))
        return checked_results(jobs, results)
    finally:
        if _ACTIVE:
            cancel_jobs()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def diagnostics(text):
    return text if len(text) <= 20000 else text[:16000] + "\n[... diagnostics truncated ...]\n" + text[-4000:]


def selftest():
    require_unoptimized()
    with tempfile.TemporaryDirectory(prefix="job-runner-test-") as tmp:
        root = Path(tmp)
        # Files prove each job ran even though completion order differs. Failed jobs
        # must not prevent later jobs; exclusives must see all parallel markers.
        def command(code):
            return (sys.executable, "-B", "-c", code)
        jobs = [
            Job("slow", command("import time,pathlib; time.sleep(.12); pathlib.Path('slow').touch(); print('slow')"), tmp, True),
            Job("failed", command("import pathlib; pathlib.Path('failed').touch(); print('failed'); raise SystemExit(7)"), tmp, True),
            Job("fast", command("import pathlib; pathlib.Path('fast').touch(); print('fast')"), tmp, True),
            Job("exclusive", command("import pathlib; assert all(pathlib.Path(p).exists() for p in ['slow','failed','fast']); print('exclusive')"), tmp),
        ]
        results = run_jobs(jobs, workers=2)
        assert [r.name for r in results] == [j.name for j in jobs]
        assert [r.returncode for r in results] == [0, 7, 0, 0]
        assert [r.output.strip() for r in results] == ["slow", "failed", "fast", "exclusive"]
        assert all((root / name).exists() for name in ["slow", "failed", "fast"])
        missing = run_jobs([Job("missing", (str(root / 'absent-executable'),), tmp)], 1)[0]
        assert missing.error and missing.returncode != 0
        timed = run_jobs([Job("timeout", command("import time; time.sleep(10)"), tmp, True, .05)], 1)[0]
        assert "timed out" in timed.error and timed.returncode != 0
        for bad in [[], [jobs[0], jobs[0]]]:
            try:
                run_jobs(bad, 2)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid roster accepted")
        for malformed in [results[:-1], results + [results[0]], [None] * len(jobs),
                          [results[0]] * len(jobs),
                          [Result("wrong", 0, "", 0)] + results[1:],
                          [Result("slow", True, "", 0)] + results[1:]]:
            try:
                checked_results(jobs, malformed)
            except RuntimeError:
                pass
            else:
                raise AssertionError("missing/malformed results accepted")
        from unittest.mock import patch
        with patch(__name__ + ".run_job", side_effect=[RuntimeError("planted"), results[1]]):
            caught = run_jobs(jobs[:2], workers=1)
        assert caught[0].error and caught[1] == results[1]
        # Measure the subprocess intervals, not just ThreadPoolExecutor settings.
        measured = [Job(str(i), command("import time,json; start=time.monotonic(); "
                        "time.sleep(.2); print(json.dumps([start,time.monotonic()]))"),
                        tmp, i < 3) for i in range(5)]
        intervals = [json.loads(r.output) for r in run_jobs(measured, 2)]
        events = sorted([(start, 1) for start, end in intervals[:3]] +
                        [(end, -1) for start, end in intervals[:3]])
        active = peak = 0
        for _, delta in events:
            active += delta
            peak = max(peak, active)
        assert peak == 2
        assert intervals[3][0] >= max(end for start, end in intervals[:3])
        assert intervals[4][0] >= intervals[3][1]
        # A successful leader may leave a child with closed output descriptors. It
        # must be gone before the exclusive phase begins.
        if os.name == "posix":
            code = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                    "\"import time,pathlib; time.sleep(.4); pathlib.Path('escaped-success').touch()\"],"
                    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)")
            barrier = "import time,pathlib; time.sleep(.6); assert not pathlib.Path('escaped-success').exists()"
            results = run_jobs([Job("success-parent", command(code), tmp, True),
                                Job("barrier", command(barrier), tmp)], 2)
            assert all(r.returncode == 0 and not r.error for r in results)
        # A child must not survive the timed-out job and write its delayed marker.
        if os.name == "posix":
            code = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
                    "\"import time,pathlib; time.sleep(.4); pathlib.Path('escaped').touch()\"]); time.sleep(10)")
            result = run_jobs([Job("tree-timeout", command(code), tmp, True, .1)], 1)[0]
            time.sleep(.5)
            assert result.error and not (root / 'escaped').exists()
    for command, env in [
            ([sys.executable, "-B", "-O", __file__, "--selftest"], dict(os.environ)),
            ([sys.executable, "-B", __file__, "--selftest"], dict(os.environ, PYTHONOPTIMIZE="1"))]:
        refused = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        assert refused.returncode != 0 and "requires unoptimized Python" in refused.stdout
        assert "SELFTEST PASS" not in refused.stdout
    configured_git_controls()
    print("SELFTEST PASS: job completeness, failure, malformed results, timeout/child cleanup, peak concurrency, ordering, exclusive barriers, optimized-mode refusal and configured Git-skip controls")


def configured_git_jobs(runner, roster):
    declarations = re.findall(r"^[ \t]*NEEDS_GIT[ \t]*=.*$", runner, re.MULTILINE)
    if len(declarations) != 1:
        raise ValueError("exactly one literal NEEDS_GIT declaration is required")
    match = re.fullmatch(r'NEEDS_GIT="([A-Za-z0-9_./ \-]*)"', declarations[0])
    if match is None:
        raise ValueError("NEEDS_GIT must be a double-quoted, space-separated list of literal tool paths")
    names = match.group(1).split()
    if len(names) != len(set(names)):
        raise ValueError("duplicate NEEDS_GIT tool")
    unknown = set(names) - set(roster)
    if unknown:
        raise ValueError(f"NEEDS_GIT names unknown self-tests: {sorted(unknown)}")
    return set(names)


def configured_git_controls():
    import contextlib
    import io
    from unittest.mock import patch
    first = "format_relation/tools/relation_validate.py"
    second = "format_trigger/tools/trigger_validate.py"
    ordinary = "format_impact-rule/tools/impact_validate.py"
    names = [first, second, ordinary]
    declaration = f'NEEDS_GIT="{first} {second}"'
    assert configured_git_jobs(declaration, names) == {first, second}
    for invalid in ["", declaration + "\n" + declaration, f"NEEDS_GIT={first}",
                    'NEEDS_GIT="$(false)"', declaration + " # comment",
                    f'NEEDS_GIT="{first} {first}"', 'NEEDS_GIT="format_missing/tools/x.py"']:
        try:
            configured_git_jobs(invalid, names)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid NEEDS_GIT declaration accepted")
    with tempfile.TemporaryDirectory(prefix="git-skip-roster-") as tmp:
        root = Path(tmp)
        (root / "gates").mkdir()
        (root / "gates/run_checks.sh").write_text(declaration + "\n" + "".join(
            name + "|SELFTEST PASS: 1 cases\n" for name in names), encoding="utf-8")
        for index, name in enumerate(names):
            path = root / name
            path.parent.mkdir(parents=True)
            path.write_text("# --selftest\nfrom pathlib import Path\n"
                            f"Path('ran{index}').touch()\nprint('SELFTEST PASS: 1 cases')\n", encoding="utf-8")
        def invoke(skip):
            output = io.StringIO()
            with patch.dict(os.environ, {"SKIP_GIT_SELFTESTS": skip, "GATE_JOBS": "1"}), \
                    contextlib.redirect_stdout(output):
                status = public_checks(root, 0, 0)
            return status, output.getvalue()
        status, output = invoke("1")
        assert status == 0 and "2 tools skipped" in output
        assert not (root / "ran0").exists() and not (root / "ran1").exists()
        assert (root / "ran2").exists()
        status, output = invoke("0")
        assert status == 0 and "0 tools skipped" in output
        assert all((root / f"ran{i}").exists() for i in range(3))
        (root / first).unlink()
        status, output = invoke("1")
        assert status != 0 and f"FAIL  {first}" in output
        assert f"PASS  {ordinary}" in output and "1 tools skipped" in output
        # Even an all-Git opt-out cannot hide a deleted pinned tool.
        declaration_all = 'NEEDS_GIT="' + ' '.join(names) + '"'
        manifest = root / "gates/run_checks.sh"
        manifest.write_text(manifest.read_text().replace(declaration, declaration_all), encoding="utf-8")
        status, output = invoke("1")
        assert status != 0 and f"FAIL  {first}" in output
        (root / first).write_text("# --selftest\nprint('SELFTEST PASS: 1 cases')\n", encoding="utf-8")
        output = io.StringIO()
        with patch.dict(os.environ, {"SKIP_GIT_SELFTESTS": "1", "GATE_JOBS": "1"}), \
                contextlib.redirect_stdout(output):
            status = public_checks(root, 1, 0)
        assert status == 0 and "3 tools skipped; 0 jobs" in output.getvalue()


def public_checks(root, initial_passed, initial_failed):
    from check_selftest_result import check_output, unavailable_cases
    runner = (root / "gates/run_checks.sh").read_text()
    rows = re.findall(r"^(format_[^|]+/tools/[^|]+\.py)\|([^\n]*)$", runner, re.MULTILINE)
    if len(dict(rows)) != len(rows):
        raise ValueError("duplicate pinned self-test")
    pins = dict(rows)
    discovered = {path.relative_to(root).as_posix() for path in root.glob("format_*/tools/*.py")
                  if "--selftest" in path.read_text()}
    # Pinned jobs remain required even if a file or --selftest token disappears.
    names = sorted(discovered | pins.keys())
    needs_git = configured_git_jobs(runner, names)
    skipped = []
    jobs = []
    for name in names:
        if (os.environ.get("SKIP_GIT_SELFTESTS") == "1" and name in needs_git
                and (root / name).is_file()):
            skipped.append(name)
        else:
            jobs.append(Job(name, (sys.executable, "-B", str(root / name), "--selftest"),
                            str(root), (name, ("--selftest",)) in PARALLEL_SAFE))
    started = time.monotonic()
    workers = workers_from_environment()
    # An explicit all-Git opt-out keeps the old shell contract: only the fast
    # checks count as passed, and every omitted tool is reported as skipped.
    results = [] if names and len(skipped) == len(names) else run_jobs(jobs, workers)
    passed, failed = initial_passed, initial_failed
    by_name = {r.name: r for r in results}
    for name in names:
        if name in skipped:
            print(f"SKIP  {name} (needs git; SKIP_GIT_SELFTESTS=1)")
            continue
        result = by_name[name]
        detail = result.error
        if not detail and result.returncode:
            detail = f"exit {result.returncode} (expected 0)"
        expected = pins.get(name, "")
        if not detail:
            try:
                if name == "format_fill-contract/tools/fill_observe.py":
                    count = int(expected.removeprefix("SELFTEST PASS: ").removesuffix(" cases"))
                    detail_ok = check_output(result.output, count, unavailable_cases())
                elif expected and expected not in result.output.splitlines():
                    raise ValueError(f"expected line {expected!r} not found")
                else:
                    detail_ok = expected or "exit 0; result line not pinned"
            except (ValueError, TypeError, OSError) as exc:
                detail = str(exc)
        if detail:
            failed += 1
            print(f"FAIL  {name}: {detail}\n{diagnostics(result.output)}")
        else:
            passed += 1
            print(f"PASS  {name}: {detail_ok} ({result.elapsed:.2f}s)")
            for line in result.output.splitlines():
                if "[skip]" in line:
                    print(line)
    print(f"run_checks: {passed} passed, {failed} failed, {len(skipped)} tools skipped; "
          f"{len(results)} jobs, {workers} workers, {time.monotonic()-started:.2f}s")
    return int(bool(failed) or not passed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--public", type=Path)
    parser.add_argument("--initial-passed", type=int, default=0)
    parser.add_argument("--initial-failed", type=int, default=0)
    args = parser.parse_args()
    try:
        require_unoptimized()
        if args.selftest:
            selftest()
            return 0
        if args.public is None:
            parser.error("choose --selftest or --public ROOT")
        if min(args.initial_passed, args.initial_failed) < 0:
            parser.error("initial counts must be nonnegative")
        return public_checks(args.public.resolve(), args.initial_passed, args.initial_failed)
    except (ValueError, OSError, RuntimeError, AssertionError) as exc:
        print(f"FAIL selftest_jobs: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
