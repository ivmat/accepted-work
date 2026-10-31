#!/usr/bin/env python3
"""fill_observe.py — first cut, stdlib-only, READ-ONLY fact-gatherer for a `fill-contract`. For
every file named in a contract, gathers the facts a `filled_when`/`due_when` predicate tree needs
(markers present, not-contains checks, table data-row count, line count, stamps, frontmatter digest
presence, git last-modified date), plus repo-wide git maturity events (instantiated, first-src-commit,
first-test, first-spec, first-dependency, first-release, commit count/dates, days-since-setup).
Prints one OBSERVATIONS JSON object on stdout. NEVER decides a not-due/due/overdue/filled/unknown
state — see `fill_evaluate.py` for that.

This tool touches the filesystem and runs `git log`/`git tag`/`git rev-parse` (read-only: never
`git add`/`commit`/`checkout`/anything that mutates `--repo`). It is the one IMPURE tool of the
three; `--now` is still an explicit argument (no `datetime.now()`/wall-clock read) so a given
`--repo`+`--contract`+`--now` triple is deterministic modulo the repo's own git history, which does
not change under a read-only scan.

GIT ENVIRONMENT: every git subprocess call in this module (both the real `--repo PATH` scan and the
self-test's own throwaway git repos) runs with every inherited `GIT_*` environment variable stripped
first (`_scrubbed_git_env`, below). `-C <repo>` alone is NOT enough: git honors `GIT_DIR`/
`GIT_INDEX_FILE`/etc. from the environment even when `-C` is also given, so a leaked hook-exported
`GIT_DIR` (this tool running inside a pre-commit hook, for instance) would otherwise make every git
call here silently operate against the WRONG repository instead of the caller's own `--repo PATH`.
Every git call also has a wall-clock timeout (`GIT_TIMEOUT_SECONDS`); a timeout is a reported error
(exit 14), never a hang.

FILE SIZE: a file's content is not scanned for markers/stamps/table rows/digest/rule count once it
exceeds `MAX_SCAN_BYTES` — the observation instead carries `scan_skipped_reason` and every
per-content fact field this file's `filled_when`/`due_when` predicates would otherwise need is left
absent (fill_evaluate.py then resolves every one of them to Kleene UNKNOWN, never crashes and never
guesses). This is an independent ceiling on total work per file, not a remedy for a scanning
defect: `fill_common.find_markers`/`find_rule_count` (the FILL:/RULE: marker scanners) are a linear,
forward-only scan (`fill_common._iter_html_comments`), not a backtracking `.*?`-DOTALL regex, so
their own cost is already O(file size) even on an adversarial file with many unclosed `<!-- FILL:`
openers — MAX_SCAN_BYTES just keeps that O(file size) bounded to a fixed, generous ceiling
regardless of how large a file on disk actually is.

NON-UTF-8 FILES: an invalid byte sequence becomes U+FFFD substitution
characters; this format does not detect or flag the presence of a substitution
character (doing so would need a second full-file byte scan for no behavioral benefit: the text is
still scanned as-is, and a marker/stamp straddling an invalid byte simply is not matched — the same
outcome as an ordinary absent marker).

SYMLINK CONTAINMENT: before reading a contract entry's `file`, its resolved (symlinks followed)
absolute path is checked to be inside the resolved `--repo` directory. A path that escapes (a
symlink pointing outside `--repo`) is never read; the observation reports `exists: false` with an
`unreadable_reason` explaining why, never silently following the link.

Usage:
  python3 fill_observe.py observe --repo PATH --contract FILE --now T
      [--src-glob GLOB,...] [--test-glob GLOB,...] [--spec-glob GLOB,...] [--manifest NAME,...]
  python3 fill_observe.py --selftest

Exit codes (lowest-numbered class wins; 0 iff no error):
  0   observation completed (facts may individually be null/unknown — that is not a tool error)
  10  `--repo` does not exist or is not a directory
  11  `--repo` exists but is not usable as a git repository (`git rev-parse` failed), or is a
      SUBDIRECTORY of one (`--repo` must be the repository's own top level — see `--show-toplevel`)
  12  `--contract` file missing or unreadable, or not valid TOML syntax
  13  `--contract` is valid TOML but fails `fill_common.validate_contract` shape/vocabulary checks
      (observe refuses to scan against an invalid contract rather than guess what it means)
  14  a git subprocess call did not complete within GIT_TIMEOUT_SECONDS, could not start (e.g. no
      `git` binary on PATH), returned a calendar date `_to_date` could not parse, OR a git query made
      AFTER `--repo` was already accepted as the repository's own top level returned a nonzero exit
      (an infrastructure failure — a corrupted object, a permissions problem, disk exhaustion
      mid-read, ...) — reported, never a hang, never a crash, and never silently read as a genuine
      empty result (a FAILED history/tag/shallow-status query never becomes a `null` fact; see
      UNBORN REPOSITORIES below for the one case, an unborn repository's own history query, that is a
      genuinely EXPECTED "failure" and is therefore detected explicitly and never routed through this
      exit code at all)
  15  a `--src-glob`/`--test-glob`/`--spec-glob`/`--manifest` value fails the glob/path grammar
      (fill_common.validate_glob_grammar), or is an explicitly EMPTY list (e.g. `--src-glob ""`) —
      checked before any git call is made
  64  usage/argument error
  65  any other unreadable/malformed input (reserved catch-all)
  74  stdout unavailable; no report can be written
  99  selftest mismatch or selftest infrastructure failure

SHALLOW CLONES: `git rev-parse --is-shallow-repository` is checked once, before any history is read.
A shallow clone (e.g. `actions/checkout`'s own default of `fetch-depth: 1`) only sees commits back to
the grafted boundary, so any fact that depends on finding the OLDEST matching commit (the repo's own
first commit, the first commit touching `src/**`/`test/**`/`spec/**`/a manifest glob, the total commit
count/every commit's date, "days since instantiated") would silently report the grafted boundary as
if it were real history — a CONFIDENTLY WRONG date or count, not a missing one. None of those
maturity keys is emitted at all when the clone is shallow (not set to `null`: an ABSENT key is what
makes `fill_evaluate.py` resolve the corresponding `due_when` predicate to Kleene `unknown` — the
same rule already used for a hand-written observations document that omits a key, documented in
`fill_common.py`'s own OBSERVATIONS docstring) — no new top-level key, no new exit code. Fetch full
history (`fetch-depth: 0` in CI) to get real facts. The per-file fact `last_modified_date` is ALSO
omitted for every file when the clone is shallow: it names the most recent commit touching that one
file, and the grafted boundary itself looks exactly like a genuine change to any file present there —
a file last changed long before the boundary would otherwise be reported with a confidently WRONG
recent date, not a missing one. A shallow clone yields no history-derived facts at all, per-file or
repo-wide.

UNBORN REPOSITORIES: a git work tree that has never had a commit (straight out of `git init`, before
the first `git commit`; a staged-but-never-committed file still counts) is detected EXPLICITLY,
before any history is read, for the same class of reason the SHALLOW CLONES check runs first: every
`git log` call this module makes would otherwise itself fail outright on an unborn repo ("does not
have any commits yet", exit 128) — NOT succeed with empty output — and that failure must never be
mistaken for the ordinary "no commit touches this glob"/"no tags exist" success-with-empty case (see
exit code 14 above). `git rev-parse --verify --quiet HEAD` exiting `1` is only a CANDIDATE signal, not
proof: an UNREADABLE ref store (e.g. `.git/refs/heads` unreadable on a repo that already has a real
commit) exits `1` the same way, so `_is_unborn_repo` POSITIVELY confirms emptiness with a second,
ref-independent probe (`git cat-file --batch-all-objects`) before ever treating a repo as unborn — see
that function's own docstring for the full decision table; any outcome that is not clearly one or the
other is reported as exit 14, never guessed. Unlike a shallow clone, a genuinely unborn repository's
empty history is a genuinely KNOWN
fact, not an unknown one hidden by a truncated window: every repo-wide maturity date is reported
`null` (`commits_count` the true `0`, `commit_dates` the true `[]`, `days_since_setup` `null`
because there is no `instantiated_date` to subtract from) rather than omitted — "this has not happened
yet" IS a confirmed negative here, the same way `fill_evaluate.py` already treats a present `null`
differently from an absent key. `first_release_date` is still computed through the ordinary
`first_release_date` function (not hand-set to `null`): `git tag` does not depend on `HEAD` resolving
and already returns success-with-empty ("no tags") on an unborn repo, so routing it through the same
path keeps a GENUINELY failing `git tag` surfacing as exit 14 here too. The per-file fact
`last_modified_date` is OMITTED (not `null`) for every file on an unborn repo, the same shape (and
exit-14-not-null reasoning) a shallow clone's own per-file omission already uses: the file plainly
EXISTS on disk, so an absent key ("unknown/not yet computable") reads truer than a `null` ("known to
have no such date") would for something that unambiguously exists.

COMMIT DATES ARE UTC CALENDAR DAYS: `git log --format=%aI` reports each commit's AUTHOR-LOCAL offset
datetime; every commit date this module computes converts that offset to UTC before taking the
calendar day (`_to_date`), the same basis `fill_common.parse_now` already puts `--now` on — so a
subtraction between the two (`days_since_setup`) always compares like with like, never two
different calendars. A `days_since_setup` that would still come out negative (clock skew, a
future-dated commit, or `--now` genuinely earlier than the repository's first commit) is reported
`null`, never a guessed negative count `fill_common.validate_maturity` would itself reject.

COMMIT DATES ARE SORTED, NOT HISTORY ORDER: `commit_dates` is the list of commit author days
(UTC calendar days, see above) SORTED oldest-first — not `git log --reverse`'s own history order
(parent before child). The two orders usually coincide but are not the same thing: a cherry-pick, a
rebase, an amended old commit, or a contributor with a skewed clock can make a later-in-history
commit carry an earlier author date, and `fill_common.validate_maturity` requires the emitted list to
be non-decreasing (every evaluator caller indexes it assuming oldest-first) — so `compute_maturity`
sorts it before emitting, and `instantiated_date` is the EARLIEST day in that sorted list, never
`dates[0]` of the unsorted history order.

Stdlib only: argparse, json, os, pathlib, re, stat, subprocess, sys, time, datetime, fill_common
(this directory).
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
from datetime import datetime, timezone

import fill_common as fc
from schema_conformance import boundary_defect_controls, conformance_errors

REPORT_SCHEMA = ("schema/fill-contract-0.1.0-draft.schema.json", "observe_output")
fc.register_report_schema(__file__, REPORT_SCHEMA)

# Preserve readable content while substituting one U+FFFD per invalid UTF-8 sequence.
codecs.register_error("fill-substitute", lambda exc: ("\ufffd", exc.end))

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"

OBSERVER_VERSION = "fill-contract/first-cut-0.1.0-draft"

E_REPO_MISSING = 10
E_REPO_NOT_GIT = 11
E_CONTRACT_UNREADABLE = 12
E_CONTRACT_INVALID = 13
E_GIT_TIMEOUT = 14
E_BAD_GLOB = 15
E_USAGE = 64
E_INPUT = 65
E_SELFTEST = 99


def _output_schema_errors(report: dict) -> list[str]:
    schema = json.loads((HERE.parent / "schema" /
                         "fill-contract-0.1.0-draft.schema.json").read_text(encoding="utf-8"))
    return conformance_errors(report, schema, "#/$defs/observe_output", "observe_output")

DEFAULT_SRC_GLOBS = ["src/**"]
DEFAULT_TEST_GLOBS = ["test/**", "tests/**"]
DEFAULT_SPEC_GLOBS = ["spec/**"]
DEFAULT_MANIFESTS = ["rebar.config", "Cargo.toml", "pyproject.toml", "package.json"]

GIT_TIMEOUT_SECONDS = 30

# A file this large is not scanned for markers/stamps/table rows/digest/rule count (see the module
# docstring's FILE SIZE section). Generous for any real templated file this format tracks — a real
# templated file is typically a few kilobytes — while still bounding worst-case regex cost to a
# fixed ceiling.
MAX_SCAN_BYTES = 1_048_576


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise fc.FillError(E_USAGE, message)


def _split_csv(value) -> list:
    """Splits a `--src-glob`/`--test-glob`/`--spec-glob`/`--manifest` CSV argument on ',' only — NO
    `.strip()` on the individual tokens: each token is what `fc.validate_glob_grammar` checks next,
    and that check must see the exact candidate string, not a version already trimmed of a leading/
    trailing space that the grammar itself would (correctly) reject. Only an entirely empty token
    (`""`, from `",,"` or a trailing comma) is dropped — that is CSV splitting, not grammar
    leniency."""
    if value is None:
        return None
    return [v for v in value.split(",") if v]


# ---------------------------------------------------------------------------
# git helpers (read-only, env-scrubbed, timed)
# ---------------------------------------------------------------------------
def _scrubbed_git_env(extra: dict | None = None) -> dict:
    """A subprocess environment safe to hand to any git call this module makes: a copy of the
    REAL `os.environ` with every `GIT_*` variable stripped (the one exception, `GIT_TERMINAL_
    PROMPT`, only ever suppresses an interactive credential prompt — never a repo-routing var), then
    `extra` applied on top (e.g. a fixed author/committer identity for a self-test commit)."""
    env = dict(os.environ)
    for var in list(env):
        if var.startswith("GIT_") and var != "GIT_TERMINAL_PROMPT":
            del env[var]
    if extra:
        env.update(extra)
    return env


def _run_git(repo: pathlib.Path, args: list, timeout: float = GIT_TIMEOUT_SECONDS,
             env: dict | None = None) -> tuple:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            timeout=timeout, env=env if env is not None else _scrubbed_git_env(),
        )
    except subprocess.TimeoutExpired:
        raise fc.FillError(E_GIT_TIMEOUT,
                            f"git {' '.join(args)} (in {repo}) timed out after {timeout}s")
    except OSError as exc:
        raise fc.FillError(E_GIT_TIMEOUT, f"git {' '.join(args)} (in {repo}) failed to start: {exc}")
    return result.returncode, result.stdout, result.stderr


def is_git_repo(repo: pathlib.Path) -> bool:
    code, out, _ = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


def _git_toplevel(repo: pathlib.Path) -> pathlib.Path | None:
    code, out, _ = _run_git(repo, ["rev-parse", "--show-toplevel"])
    if code != 0 or not out.strip():
        return None
    return pathlib.Path(out.strip())


_ERROR_SNIPPET_MAX = 200


def _git_ok(repo: pathlib.Path, args: list, query_label: str) -> str:
    """Runs a git query that is ALWAYS expected to succeed once `--repo` has already been accepted
    as the git work-tree's own top level (every call site below except `is_git_repo`/
    `_git_toplevel`, which run the PRE-acceptance probe and keep their own "not a repo" reading of a
    nonzero exit — see those two functions). A nonzero exit here is NEVER success-with-empty-output
    — it is an infrastructure failure (a corrupted object, a permissions problem, a `git` binary too
    old for a flag used above, disk exhaustion mid-read, ...) and is reported as exactly that (exit
    `E_GIT_TIMEOUT`, the same class a timeout/could-not-start git call already uses), never silently
    read as "no matching commit"/"no tags"/"not shallow" and turned into a confirmed-negative `null`
    fact (the defect this function exists to close — see the module docstring's UNBORN REPOSITORIES
    section for the one case, an unborn repository's own `git log`, that IS a genuine, EXPECTED
    failure mode and is detected explicitly BEFORE it ever reaches this function, never routed
    through it). `query_label` is a short, bounded name for the query (`"git log"`, `"git tag"`,
    `"git rev-parse"`, ...) for the error message — never the full argv, which can carry a glob/
    pathspec drawn from `--src-glob`/a contract `file` entry and is already a candidate for
    `fc.bound_reason`'s own truncation if adversarially long. Raises `fc.FillError(E_GIT_TIMEOUT, …)`
    on a nonzero exit; returns stdout (success, possibly empty) otherwise."""
    code, out, err = _run_git(repo, args)
    if code != 0:
        stderr_preview = err.strip()[:_ERROR_SNIPPET_MAX]
        raise fc.FillError(E_GIT_TIMEOUT,
                            f"{query_label} (in {fc.bound_path_preview(repo)}) failed "
                            f"(exit {code}): {stderr_preview}")
    return out


def is_shallow_repo(repo: pathlib.Path) -> bool:
    out = _git_ok(repo, ["rev-parse", "--is-shallow-repository"], "git rev-parse")
    return out.strip() == "true"


def _is_unborn_repo(repo: pathlib.Path) -> bool:
    """True iff `repo` is a git work tree that has never had a commit (`HEAD` does not resolve to
    any commit yet — e.g. straight out of `git init`, before the first `git commit`; a staged
    (`git add`)-but-never-committed file is still unborn by this definition). `git rev-parse
    --verify --quiet HEAD` exits `1` for this — but, confirmed live on this module's own supported
    git version, it ALSO exits `1` for an unrelated reason: an UNREADABLE ref store (e.g.
    `.git/refs/heads` with its read permission removed on a repo that already has a real commit).
    That exit code alone does NOT prove emptiness — it was wrong to claim it "fails only for this
    reason" (an earlier version of this docstring did; a repo with real history but unreadable refs
    would otherwise have been misread as unborn and reported with confirmed-negative `null` facts
    for a repo that actually HAS history, the exact defect class `_git_ok` elsewhere in this module
    already exists to close). So a rev-parse exit of exactly `1` is only a CANDIDATE; this function
    positively confirms it before returning `True`: it lists every object in the repository's own
    object store, independent of any ref (`git cat-file --batch-all-objects
    --batch-check='%(objecttype)'` — this still succeeds even with every ref unreadable, since it
    never consults a ref at all), and requires that listing to both SUCCEED and name no `commit`
    object anywhere — a repo with real history still has its commit objects on disk even if its refs
    cannot be read, and this listing finds them; a staged-but-uncommitted blob (the ordinary shape of
    `git add` on a genuinely unborn repo) has no `commit` object to find and passes. This repo-wide
    object listing runs ONLY after `rev-parse` has already failed (an ordinary, non-unborn repo never
    pays its cost). Any outcome that is NOT "rev-parse exits exactly 1 AND cat-file succeeds AND
    lists no commit object" raises `fc.FillError(E_GIT_TIMEOUT, …)` — covering a rev-parse exit
    other than 0/1, a `cat-file` failure, or a `commit` object actually found — never guessed as
    either "unborn" or "has history", and never silently treated as a shallow-clone-style omission
    (`compute_maturity`'s own caller never gets a chance to misread this ambiguity as a confirmed
    negative)."""
    code, _, _ = _run_git(repo, ["rev-parse", "--verify", "--quiet", "HEAD"])
    if code == 0:
        return False
    if code != 1:
        # Not the canonical "unborn" exit at all (a corrupted ref store this probe cannot even
        # start on, a timeout already raised inside `_run_git` itself before reaching here, ...).
        raise fc.FillError(E_GIT_TIMEOUT,
                            "HEAD does not resolve and the repository is not provably empty "
                            f"(git rev-parse --verify --quiet HEAD exited {code}, not the "
                            "canonical unborn-repo exit 1)")
    cat_code, cat_out, cat_err = _run_git(
        repo, ["cat-file", "--batch-all-objects", "--batch-check=%(objecttype)"])
    if cat_code != 0:
        raise fc.FillError(E_GIT_TIMEOUT,
                            "HEAD does not resolve and the repository is not provably empty "
                            f"(git cat-file --batch-all-objects failed, exit {cat_code}: "
                            f"{cat_err.strip()[:_ERROR_SNIPPET_MAX]})")
    if any(line.strip() == "commit" for line in cat_out.splitlines()):
        raise fc.FillError(E_GIT_TIMEOUT,
                            "HEAD does not resolve and the repository is not provably empty "
                            "(a commit object exists in the object store; the ref(s) that would "
                            "resolve HEAD to it are most likely unreadable, not absent)")
    return True


def _log_args(args: list) -> list:
    """Every `git log` call this module makes runs with BOTH `--no-show-signature` and
    `-c log.showSignature=false`: a user's own global `log.showSignature = true` (a perfectly
    ordinary setting for someone who signs commits) would otherwise make git print signature
    verification lines ahead of `%aI`'s own date line on some git/gpg configurations, corrupting the
    one line `_to_date` expects to parse. `-c` only overrides this ONE config key for this one
    invocation — it does not disable the user's config wholesale (`safe.directory`, in particular,
    keeps working, since nothing here touches `-c safe.directory=...` or drops global config
    loading)."""
    return ["-c", "log.showSignature=false", "log", "--no-show-signature", *args]


def _to_date(iso: str) -> str:
    """git %aI is a full ISO-8601 offset datetime. Commit dates are treated as UTC CALENDAR DAYS:
    the commit's own author-local offset is converted to UTC before the date is taken, so every
    subtraction this format performs against `--now` (also UTC — see `fill_common.parse_now`)
    compares like with like, never two different calendars (see the module docstring's COMMIT DATES
    ARE UTC CALENDAR DAYS section)."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            raise ValueError("no UTC offset in git's own %aI output")
        return dt.astimezone(timezone.utc).date().isoformat()
    except (ValueError, OverflowError) as exc:
        # Both the quoted INPUT and the underlying exception's own message are truncated: Python's
        # own `datetime.fromisoformat` ValueError embeds the ENTIRE candidate string in its message
        # too, so truncating only `iso` and not `str(exc)` would still let an arbitrarily long
        # adversarial "date" make this report unboundedly long.
        snippet = iso[:_ERROR_SNIPPET_MAX]
        exc_snippet = str(exc)[:_ERROR_SNIPPET_MAX]
        raise fc.FillError(E_GIT_TIMEOUT,
                            f"git returned a non-canonical calendar date (read {snippet!r}): "
                            f"{exc_snippet}")


def commit_dates(repo: pathlib.Path) -> list:
    out = _git_ok(repo, _log_args(["--format=%aI", "--reverse"]), "git log")
    if not out.strip():
        return []
    return [_to_date(line) for line in out.strip().splitlines()]


def first_commit_touching(repo: pathlib.Path, globs: list) -> str:
    """The EARLIEST UTC calendar day (see the module docstring's COMMIT DATES ARE UTC CALENDAR DAYS
    section) among EVERY commit touching any of `globs` -- not `git log --reverse`'s own
    HISTORY-order first touching commit. The two usually coincide but are not the same thing: a
    cherry-pick, a rebase, an amended old commit, or a contributor with a skewed clock can make a
    later-in-history commit touching this glob carry an EARLIER author date than an
    earlier-in-history commit touching the same glob (the exact shape that already drove
    `compute_maturity`'s own `commit_dates`/`instantiated_date` to be the SORTED list / its earliest
    entry, never history order's first one -- see COMMIT DATES ARE SORTED, NOT HISTORY ORDER above).
    `first_src_commit_date`/`first_test_date`/`first_spec_date`/`first_dependency_date` all derive
    from this function, and this format's own chronology is "author days, oldest first" throughout
    -- taking `min(...)` over every touching commit's date (not merely `[0]` of one glob's own
    history-order list) is what keeps this fact on that SAME chronology, so a
    `due_when = { all = [ {predicate="instantiated"}, {predicate="first-src-commit"} ] }` tree and a
    `commits-at-least` check of the same history agree on which day is earliest."""
    dates = []
    for glob in globs:
        out = _git_ok(repo, _log_args(["--format=%aI", "--reverse", "--", f":(glob){glob}"]),
                      "git log")
        if out.strip():
            dates.append(min(_to_date(line) for line in out.strip().splitlines()))
    return min(fc.parse_iso_date(d) for d in dates).isoformat() if dates else None


def first_release_date(repo: pathlib.Path) -> str:
    """The EARLIEST tag by `creatordate` (`git tag --sort=creatordate`'s own first line), then that
    ONE tag's own commit date. Unlike `first_commit_touching`, there is no history-order-vs-
    author-date-order ambiguity to resolve here: `--sort=creatordate` already selects the earliest
    tag directly (not a history-order first tag later re-sorted), and the `-1` on the second git
    call reads exactly one commit's date -- there is no multi-commit list to take a `min(...)` over.
    A later cherry-pick/rebase of the tagged commit itself cannot change which TAG was created
    first, only what that tag happens to point at today, which is exactly the fact this field
    reports."""
    out = _git_ok(repo, ["tag", "--sort=creatordate"], "git tag")
    if not out.strip():
        return None
    tag = out.strip().splitlines()[0]
    out = _git_ok(repo, _log_args(["-1", "--format=%aI", tag]), "git log")
    if not out.strip():
        return None
    return _to_date(out.strip())


def file_last_modified(repo: pathlib.Path, relpath: str) -> str:
    # `:(literal)<path>` — relpath is an exact, already-grammar-checked repo-relative FILE path
    # (fill_common.validate_path_grammar), never a glob; without the `:(literal)` prefix, git would
    # still read pathspec-magic characters INSIDE an otherwise-ordinary file name (a leading ':' is
    # already rejected by the grammar itself, but '*', '?', '[...]' are not, since they are entirely
    # legal filename characters) as glob/pathspec syntax, silently reporting another file's own
    # last-modified date instead of this one's.
    out = _git_ok(repo, _log_args(["-1", "--format=%aI", "--", f":(literal){relpath}"]), "git log")
    if not out.strip():
        return None
    return _to_date(out.strip())


def _validate_globs(label: str, globs: list) -> None:
    for g in globs:
        reason = fc.validate_glob_grammar(g)
        if reason is not None:
            raise fc.FillError(E_BAD_GLOB, f"--{label} value {fc.safe_repr_for_error(g)} is not a "
                                            f"valid glob: {reason}")


def compute_maturity(repo: pathlib.Path, now_dt: datetime, src_globs, test_globs, spec_globs,
                      manifests, shallow: bool) -> dict:
    _validate_globs("src-glob", src_globs)
    _validate_globs("test-glob", test_globs)
    _validate_globs("spec-glob", spec_globs)
    for m in manifests:
        reason = fc.validate_glob_grammar(m)
        if reason is not None:
            raise fc.FillError(E_BAD_GLOB, f"--manifest value {fc.safe_repr_for_error(m)} is not a "
                                            f"valid glob: {reason}")
    manifest_globs = [f"**/{m}" for m in manifests] + list(manifests)
    _validate_globs("manifest", manifest_globs)

    if shallow:
        # See the module docstring's SHALLOW CLONES section: every history-derived fact below is
        # OMITTED (not set to null) so fill_evaluate.py resolves each one to Kleene `unknown`,
        # exactly as it already does for a hand-written observations document that never set the
        # key — never a confidently wrong date computed from a truncated history window.
        return {}

    if _is_unborn_repo(repo):
        # UNBORN REPOSITORY (see the module docstring's own section): `HEAD` does not resolve to any
        # commit yet, so every `git log` call below would itself fail (exit 128, "does not have any
        # commits yet") -- NOT a success-with-empty-output the way "no commit touches this glob" or
        # "no tags exist" is. That failure is detected explicitly, here, BEFORE any of those calls is
        # ever made, and is reported as the genuine (and genuinely KNOWN, not unknown) empty history
        # it is: every date field is `null` -- "this has not happened" IS a confirmed negative for a
        # repository that provably has zero commits, unlike a SHALLOW clone's truncated window, where
        # the same `null` would be a confidently WRONG guess about history outside the visible
        # boundary (hence shallow omits these keys entirely instead -- see above). `commits_count` is
        # the true `0`, `commit_dates` the true `[]`. `first_release_date` is NOT hand-emitted `null`
        # here: an unborn repo can have no commit for a tag to point at, but the query itself
        # (`git tag --sort=creatordate`) does not depend on `HEAD` resolving and still succeeds
        # (empty output, the ordinary "no tags" case already handled by `first_release_date` itself)
        # -- routing it through the SAME function used in the ordinary case is what keeps "git tag
        # genuinely fails" surfacing as exit 14 here too, rather than being hand-waved to `null`
        # alongside the dates that truly cannot be computed without `HEAD`.
        return {
            "instantiated_date": None,
            "first_src_commit_date": None,
            "first_test_date": None,
            "first_spec_date": None,
            "first_dependency_date": None,
            "first_release_date": first_release_date(repo),
            "commits_count": 0,
            "commit_dates": [],
            "days_since_setup": None,
        }

    # `git log --reverse` is HISTORY order (parent before child), not author-date order: a
    # cherry-pick, a rebase, an amended old commit, or a contributor with a skewed clock can all
    # make a later-in-history commit carry an EARLIER author date. `commit_dates` is documented
    # (and validated by `fill_common.validate_maturity`) as the sorted list of commit author days,
    # oldest first -- sorting here, once, is what keeps every emitted observation passing its own
    # evaluator's ordering check regardless of the repository's actual history shape; canonical
    # `YYYY-MM-DD` strings sort chronologically as plain strings, so no date parsing is needed.
    dates = sorted(commit_dates(repo))
    instantiated = dates[0] if dates else None
    days_since = None
    if instantiated:
        # See the module docstring's COMMIT DATES ARE UTC CALENDAR DAYS section: both sides of this
        # subtraction are UTC calendar days, but clock skew, a future-dated commit, or a `--now`
        # genuinely earlier than the repo's first commit can still make the difference negative — a
        # negative count is never guessed/clamped to 0; it is reported `null` (fill_common's own
        # OBS_COUNT_MIN..OBS_COUNT_MAX bound on `days_since_setup` would reject a negative value
        # outright, and a shape error here is exactly the wrong signal for "no evidence either way").
        delta = (now_dt.date() - fc.parse_iso_date(instantiated)).days
        days_since = delta if delta >= 0 else None
    return {
        "instantiated_date": instantiated,
        "first_src_commit_date": first_commit_touching(repo, src_globs),
        "first_test_date": first_commit_touching(repo, test_globs),
        "first_spec_date": first_commit_touching(repo, spec_globs),
        "first_dependency_date": first_commit_touching(repo, manifest_globs),
        "first_release_date": first_release_date(repo),
        "commits_count": len(dates),
        "commit_dates": dates,
        "days_since_setup": days_since,
    }


# ---------------------------------------------------------------------------
# per-file observation
# ---------------------------------------------------------------------------
def _resolve_within_repo(repo: pathlib.Path, relpath: str) -> tuple:
    """Returns (resolved_path_or_None, unreadable_reason_or_None). Resolves BOTH `repo` and the
    candidate path (following symlinks) and checks containment — a `file` entry whose path escapes
    `--repo` via a symlink is never read; its content is treated the same as a missing file, with a
    reason recorded instead of guessed."""
    repo_resolved = repo.resolve()
    candidate = repo / relpath
    try:
        candidate_resolved = candidate.resolve()
    except OSError as exc:
        # The exception's own CLASS name plus a bounded preview of `candidate` — never `str(exc)`,
        # which embeds the full (unbounded, if `--repo` is adversarially long) path again.
        return None, f"cannot resolve path ({type(exc).__name__}: {fc.bound_path_preview(candidate)})"
    try:
        candidate_resolved.relative_to(repo_resolved)
    except ValueError:
        return None, "path resolves outside --repo (a symlink escape, or '..'-style traversal)"
    return candidate_resolved, None


def observe_file(repo: pathlib.Path, entry: dict, shallow: bool, unborn: bool = False) -> dict:
    relpath = entry["file"]
    resolved, escape_reason = _resolve_within_repo(repo, relpath)
    if escape_reason is not None:
        return {"file": relpath, "exists": False, "unreadable_reason": escape_reason}

    # EVERY filesystem probe on `resolved` -- not just `read_text()` -- runs inside this same
    # OSError guard, the file-type check included. `stat.S_ISREG(resolved.stat().st_mode)` is used
    # here INSTEAD of `pathlib.Path.is_file()`: `is_file()`'s own internal OSError handling is
    # Python-version-dependent (this README's 3.11/3.12 floor only swallows an OSError whose errno
    # is ENOENT/ENOTDIR/EBADF/ELOOP, re-raising anything else -- EACCES from a non-traversable
    # parent directory, for instance -- while 3.13+ swallows EVERY OSError internally and simply
    # returns False), so relying on it would make this guard's own `except OSError` branch below
    # either fire (3.11/3.12, landing correctly as `exists: false` + `unreadable_reason`) or never
    # fire at all (3.13+, silently losing the reason and leaving a bare `exists: false`
    # indistinguishable from a genuinely absent file). Calling `.stat()` directly and classifying
    # the outcome ourselves makes the behavior the SAME on every Python version this README
    # supports: `FileNotFoundError`/`NotADirectoryError` (the path, or a component of it, genuinely
    # does not exist) is a plain `exists: false`; any OTHER `OSError` (`PermissionError` from a
    # non-traversable parent directory, for instance) is `exists: false` WITH an `unreadable_reason`
    # explaining why -- the same shape the symlink-escape case above already uses. The one `stat()`
    # call's result is also reused for the file-size check right below, instead of calling `.stat()`
    # a second time.
    try:
        st = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        return {"file": relpath, "exists": False}
    except OSError as exc:
        # The exception's own CLASS name plus a bounded preview of `resolved` — never `str(exc)`,
        # which embeds the full (unbounded, if `--repo` is adversarially long) path again.
        return {"file": relpath, "exists": False,
                "unreadable_reason": fc.bound_reason(
                    f"cannot read ({type(exc).__name__}: {fc.bound_path_preview(resolved)})")}
    if not stat.S_ISREG(st.st_mode):
        return {"file": relpath, "exists": False}

    size = st.st_size
    obs = {"file": relpath, "exists": True}

    # SHALLOW CLONES: `last_modified_date` is a history-derived fact (the most recent commit
    # touching this one file) exactly like every `maturity` date field, and a shallow clone's
    # grafted boundary looks like a genuine change to any file that happens to be present at that
    # boundary commit, even one last touched long before it — a CONFIDENTLY WRONG recent date, not
    # a missing one. So a shallow clone yields NO history-derived facts at all: this key is OMITTED
    # (never set to `null`) here, the same way every `maturity` key already is (see
    # `compute_maturity` and the module docstring's SHALLOW CLONES section).
    # UNBORN REPOSITORIES: the same key is OMITTED here too, for a different reason — the file
    # itself has never been committed (there is no history to ask `git log` about at all, and an
    # unborn repo's own `git log` call fails outright rather than returning success-with-empty — see
    # the module docstring's UNBORN REPOSITORIES section and `compute_maturity`). Absent, never
    # `null`: `null` would claim "known to have no last-modified date", which is a stranger claim
    # for a file that plainly EXISTS on disk than "unknown/not yet computable" is.
    if size > MAX_SCAN_BYTES:
        obs["scan_skipped_reason"] = (f"file exceeds the {MAX_SCAN_BYTES}-byte scan limit "
                                       f"({size} bytes)")
        if not shallow and not unborn:
            obs["last_modified_date"] = file_last_modified(repo, relpath)
        return obs

    try:
        text = resolved.read_text(encoding="utf-8", errors="fill-substitute")
    except OSError as exc:
        return {"file": relpath, "exists": False,
                "unreadable_reason": fc.bound_reason(
                    f"cannot read ({type(exc).__name__}: {fc.bound_path_preview(resolved)})")}

    filled_when = entry.get("filled_when") or {}
    leaves = []
    fc.walk_leaves(filled_when, leaves)

    not_contains_texts = sorted({leaf["text"] for leaf in leaves
                                  if leaf.get("predicate") == "not-contains" and "text" in leaf})
    stamp_keys = sorted({leaf["key"] for leaf in leaves
                          if leaf.get("predicate") == "stamp" and "key" in leaf}
                         | {leaf["key"] for leaf in leaves
                            if leaf.get("predicate") == "fresh" and "key" in leaf})
    has_command = any(leaf.get("predicate") == "command" for leaf in leaves)

    obs["markers_present"] = fc.find_markers(text)
    obs["contains_text"] = {t: (t in text) for t in not_contains_texts}
    obs["table_rows_actual"] = fc.count_table_data_rows(text)
    obs["line_count"] = len(text.splitlines())
    obs["stamps"] = fc.find_stamps(text, set(stamp_keys))
    obs["digest_present"] = fc.frontmatter_digest_present(text)
    obs["declared_rules_count"] = fc.find_rule_count(text)
    if not shallow and not unborn:
        obs["last_modified_date"] = file_last_modified(repo, relpath)
    if has_command:
        obs["command_result"] = "unknown"
    return obs


def _resolve_globs(value, defaults: list, label: str) -> list:
    """`value` is `None` (the flag was never given — use the defaults), or the already-`_split_csv`'d
    list for one `--src-glob`/`--test-glob`/`--spec-glob`/`--manifest` argument. Checked by IDENTITY
    (`is not None`), never truthiness: an explicitly EMPTY list (`--src-glob ""`, or `--src-glob ","`
    — both split to `[]`) means the caller asked for NO globs at all, which is itself a usage error
    (exit 15) — not a request to silently fall back to the defaults, which `value or defaults` would
    have done by treating `[]` the same as `None`."""
    if value is None:
        return defaults
    if not value:
        raise fc.FillError(E_BAD_GLOB,
                            f"--{label} must name at least one glob (got an empty list)")
    return value


def run_observe(repo_arg: str, contract_arg: str, now_arg: str, src_glob=None, test_glob=None,
                 spec_glob=None, manifest=None) -> dict:
    repo = pathlib.Path(repo_arg)
    if not repo.is_dir():
        raise fc.FillError(E_REPO_MISSING, f"--repo does not exist or is not a directory: "
                            f"{fc.bound_path_preview(repo)}")
    if not is_git_repo(repo):
        raise fc.FillError(E_REPO_NOT_GIT, f"--repo is not usable as a git repository: "
                            f"{fc.bound_path_preview(repo)}")
    toplevel = _git_toplevel(repo)
    # `os.path.samefile` checks inode/device identity, not spelling: two paths that `resolve()`
    # would leave UNEQUAL as strings (a symlinked alias, differing case on a case-insensitive
    # filesystem like APFS/NTFS, or a platform's own symlinked temp-directory realpath) can still
    # name the SAME directory on disk. A string comparison of two `resolve()`d paths would wrongly
    # reject such an alias spelling of `--repo` as "a subdirectory of" its own top level.
    try:
        is_toplevel = toplevel is not None and os.path.samefile(toplevel, repo)
    except OSError:
        # `samefile` itself needs to `stat()` both paths; a path that vanishes/becomes
        # unreadable between `_git_toplevel`'s own git call and this check is reported the same
        # way as any other "not usable as the repository's own top level" case, never a crash.
        is_toplevel = False
    if not is_toplevel:
        raise fc.FillError(E_REPO_NOT_GIT,
                            f"--repo must be the git repository's own top level, not a "
                            f"subdirectory of one (--repo resolves to "
                            f"{fc.bound_path_preview(repo.resolve())}; the repository's top level "
                            f"is {fc.bound_path_preview(toplevel.resolve()) if toplevel is not None else 'unknown'})")

    contract_path = pathlib.Path(contract_arg)
    try:
        contract = fc.load_toml(contract_path)
    except fc.FillError:
        raise fc.FillError(E_CONTRACT_UNREADABLE, f"cannot read/parse --contract: "
                            f"{fc.bound_path_preview(contract_path)}")
    errors = fc.validate_contract(contract)
    if errors:
        raise fc.FillError(E_CONTRACT_INVALID,
                            f"--contract fails shape/vocabulary validation ({len(errors)} error(s)); "
                            f"run fill_validate.py for the full list")

    now_dt = fc.parse_now(now_arg)
    shallow = is_shallow_repo(repo)
    # See `compute_maturity`'s own UNBORN REPOSITORIES branch (it detects this independently, the
    # same way, for the SAME reason: avoiding every history-derived `git log` call rather than
    # letting one fail and reporting it as exit 14). A shallow clone always has at least one
    # (grafted-boundary) commit, so the two conditions never both hold; `unborn` is only meaningful,
    # and only checked, when `shallow` is already False.
    unborn = (not shallow) and _is_unborn_repo(repo)

    maturity = compute_maturity(
        repo, now_dt,
        _resolve_globs(src_glob, DEFAULT_SRC_GLOBS, "src-glob"),
        _resolve_globs(test_glob, DEFAULT_TEST_GLOBS, "test-glob"),
        _resolve_globs(spec_glob, DEFAULT_SPEC_GLOBS, "spec-glob"),
        _resolve_globs(manifest, DEFAULT_MANIFESTS, "manifest"),
        shallow=shallow)

    files = [observe_file(repo, entry, shallow, unborn) for entry in contract["contracts"]]

    return {
        "repo": str(repo),
        "contract": str(contract_path),
        "now": now_arg,
        "observer_version": OBSERVER_VERSION,
        "maturity": maturity,
        "files": files,
    }


def make_parser() -> UsageParser:
    parser = UsageParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--selftest", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    observe_p = subparsers.add_parser("observe", allow_abbrev=False)
    observe_p.add_argument("--repo", required=True)
    observe_p.add_argument("--contract", required=True)
    observe_p.add_argument("--now", required=True)
    observe_p.add_argument("--src-glob", default=None)
    observe_p.add_argument("--test-glob", default=None)
    observe_p.add_argument("--spec-glob", default=None)
    observe_p.add_argument("--manifest", default=None)
    return parser


def _run_observe_via_cli(repo_arg: str, contract_arg: str, now_arg: str, src_glob=None,
                         test_glob=None, spec_glob=None, manifest=None) -> dict:
    """Preserve run_observe's self-test interface while exercising main and the writer."""
    argv = ["observe", "--repo", repo_arg, "--contract", contract_arg, "--now", now_arg]
    for option, values in (("--src-glob", src_glob), ("--test-glob", test_glob),
                           ("--spec-glob", spec_glob), ("--manifest", manifest)):
        if values is not None:
            argv.extend((option, ",".join(values)))
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = main(argv)
    report = json.loads(captured.getvalue())
    if code != 0:
        reasons = report.get("errors") or []
        reason = reasons[0].get("reason", "CLI observation failed") if reasons else "CLI observation failed"
        raise fc.FillError(code, reason)
    return report


# ---------------------------------------------------------------------------
# selftest: builds tiny throwaway git repos under a tempfile dir. Every such repo is HERMETIC: env
# is `_scrubbed_git_env` plus `GIT_CONFIG_NOSYSTEM`/`HOME`/`XDG_CONFIG_HOME` pointed at the temp
# root (no global/system gitconfig can influence it), `core.hooksPath` pointed at a directory that
# is never populated (no hook can fire), `commit.gpgsign`/`tag.gpgsign` off (no signing prompt/
# failure), and a fixed author/committer identity and commit date (deterministic history).
# ---------------------------------------------------------------------------
def _hermetic_selftest_env(root: pathlib.Path, extra: dict | None = None) -> dict:
    env = _scrubbed_git_env(extra={
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root),
    })
    if extra:
        env.update(extra)
    return env


def _git_commit(repo: pathlib.Path, root: pathlib.Path, files: dict, message: str, date: str,
                 tag: str = None) -> None:
    for relpath, content in files.items():
        p = repo / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    env = _hermetic_selftest_env(root, extra={
        "GIT_AUTHOR_NAME": "selftest", "GIT_AUTHOR_EMAIL": "selftest@example.invalid",
        "GIT_COMMITTER_NAME": "selftest", "GIT_COMMITTER_EMAIL": "selftest@example.invalid",
        "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
    })
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True,
                    env=env, timeout=GIT_TIMEOUT_SECONDS)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message, "--no-verify"],
                    check=True, capture_output=True, env=env, timeout=GIT_TIMEOUT_SECONDS)
    if tag:
        subprocess.run(["git", "-C", str(repo), "tag", tag], check=True, capture_output=True,
                        env=env, timeout=GIT_TIMEOUT_SECONDS)


def _build_selftest_repo(root: pathlib.Path) -> pathlib.Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    env = _hermetic_selftest_env(root)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True,
                    env=env, timeout=GIT_TIMEOUT_SECONDS)
    no_hooks_dir = root / "no-hooks"
    no_hooks_dir.mkdir(exist_ok=True)
    for key, value in (
        ("user.name", "selftest"), ("user.email", "selftest@example.invalid"),
        ("core.hooksPath", str(no_hooks_dir)),
        ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True,
                        capture_output=True, env=env, timeout=GIT_TIMEOUT_SECONDS)

    notes_filled = (
        "---\nkind: notes\ndigest: x\n---\n# NOTES\n\n"
        "| Dependency | Gate | Residual |\n|---|---|---|\n"
        "| Python 3 stdlib | CPython CI | trust CPython |\n"
        "| widget-lib | widget-lib CI | pinned |\n"
    )
    plan_md = "---\nkind: plan\n---\n# PLAN\n\n- [ ] do the thing\n"
    _git_commit(repo, root, {"NOTES.md": notes_filled, "PLAN.md": plan_md},
                "initial template", "2026-09-01T09:00:00+00:00")
    _git_commit(repo, root, {"src/main.py": "print('hi')\n"}, "first src commit",
                "2026-09-03T09:00:00+00:00")
    _git_commit(repo, root, {"tests/test_main.py": "def test_x(): pass\n"}, "first test",
                "2026-09-04T09:00:00+00:00")
    _git_commit(repo, root, {"spec/first-cut.md": "# spec\n"}, "first spec",
                "2026-09-05T09:00:00+00:00")
    _git_commit(repo, root, {"pyproject.toml": "[project]\nname='x'\n"}, "add dependency manifest",
                "2026-09-06T09:00:00+00:00")
    _git_commit(repo, root, {"README.md": "# readme\n"}, "release", "2026-09-07T09:00:00+00:00",
                tag="v0.1.0")
    return repo


def _standard_contract_text() -> str:
    return (
        'fill_version = "0.1.0"\n'
        'template = "template-kit:full@v5"\n\n'
        '[[contracts]]\n'
        'file = "NOTES.md"\n'
        'importance = "high"\n'
        'urgency_after_due = "7d"\n'
        'template_rows = 1\n'
        'filled_when = { any = [ { predicate = "rows-beyond-template", min = 1 } ] }\n'
        'due_when = { any = [ { predicate = "first-src-commit" } ] }\n\n'
        '[[contracts]]\n'
        'file = "MISSING.md"\n'
        'importance = "normal"\n'
        'urgency_after_due = "3d"\n'
        'filled_when = { all = [ { predicate = "no-marker" } ] }\n'
        'due_when = { any = [ { predicate = "days-since-setup", n = 5 } ] }\n'
    )




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
        return False, f"stdout violates $defs.observe_output: {schema_errors}"
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
    import tempfile
    import errno
    results = []
    skipped = []
    try:
        with tempfile.TemporaryDirectory(prefix="fill_observe_selftest_") as tmp:
            root = pathlib.Path(tmp)
            repo = _build_selftest_repo(root)

            contract_path = root / "contract.toml"
            contract_path.write_text(_standard_contract_text(), encoding="utf-8")

            valid_argv = ["observe", "--repo", str(repo), "--contract", str(contract_path),
                          "--now", "2026-09-10T00:00:00+00:00"]
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                valid_code = main(valid_argv)
            report = json.loads(captured.getvalue())
            results.append(valid_code == 0)
            print(f"selftest fixture-through-cli-boundary: got exit={valid_code} "
                  f"[{'ok' if valid_code == 0 else 'MISMATCH'}]")

            original_run_observe = globals()["run_observe"]
            def _boom(*_args, **_kwargs):
                raise RuntimeError("selftest-injected catch-all exception")
            globals()["run_observe"] = _boom
            try:
                exception_output = io.StringIO()
                with contextlib.redirect_stdout(exception_output):
                    exception_code = main(valid_argv)
                exception_report = json.loads(exception_output.getvalue())
            finally:
                globals()["run_observe"] = original_run_observe
            exception_ok = (exception_code == E_INPUT and
                            exception_report["errors"][0]["reason"].startswith(
                                "input/runtime failure"))
            results.append(exception_ok)
            print(f"selftest exit-65-injected-exception-catch-all: got exit={exception_code} "
                  f"[{'ok' if exception_ok else 'MISMATCH'}]")

            m = report["maturity"]
            checks = [
                ("instantiated_date", m["instantiated_date"] == "2026-09-01"),
                ("first_src_commit_date", m["first_src_commit_date"] == "2026-09-03"),
                ("first_test_date", m["first_test_date"] == "2026-09-04"),
                ("first_spec_date", m["first_spec_date"] == "2026-09-05"),
                ("first_dependency_date", m["first_dependency_date"] == "2026-09-06"),
                ("first_release_date", m["first_release_date"] == "2026-09-07"),
                ("commits_count", m["commits_count"] == 6),
                ("days_since_setup", m["days_since_setup"] == 9),
            ]
            for name, ok in checks:
                results.append(ok)
                print(f"selftest maturity.{name}: [{'ok' if ok else 'MISMATCH'}] (got {m.get(name)!r})")

            f0 = report["files"][0]
            ok = (f0["exists"] is True and f0["table_rows_actual"] == 2
                  and "command_result" not in f0 and "scan_skipped_reason" not in f0)
            results.append(ok)
            print(f"selftest NOTES.md table_rows_actual==2, no command_result/"
                  f"scan_skipped_reason (one-way exists=true rule): [{'ok' if ok else 'MISMATCH'}] "
                  f"(got {f0.get('table_rows_actual')!r})")

            f1 = report["files"][1]
            ok = (f1["exists"] is False and "markers_present" not in f1
                  and "unreadable_reason" not in f1)
            results.append(ok)
            print(f"selftest MISSING.md exists=False, no further facts: [{'ok' if ok else 'MISMATCH'}]")

            # not-a-directory --repo
            try:
                _run_observe_via_cli(str(root / "nope"), str(contract_path), "2026-09-10T00:00:00+00:00")
                results.append(False)
                print("selftest bad-repo-path: expected FillError(10), got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_REPO_MISSING
                results.append(ok)
                print(f"selftest bad-repo-path: expected exit=10 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # not a git repo
            not_git = root / "not_a_repo"
            not_git.mkdir()
            try:
                _run_observe_via_cli(str(not_git), str(contract_path), "2026-09-10T00:00:00+00:00")
                results.append(False)
                print("selftest not-a-git-repo: expected FillError(11), got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_REPO_NOT_GIT
                results.append(ok)
                print(f"selftest not-a-git-repo: expected exit=11 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # unreadable contract file
            missing_contract = root / "nope.toml"
            try:
                _run_observe_via_cli(str(repo), str(missing_contract), "2026-09-10T00:00:00+00:00")
                results.append(False)
                print("selftest missing-contract: expected FillError(12), got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_CONTRACT_UNREADABLE
                results.append(ok)
                print(f"selftest missing-contract: expected exit=12 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # invalid contract (shape)
            bad_contract = root / "bad.toml"
            bad_contract.write_text('fill_version = "0.1.0"\ntemplate = "t"\n', encoding="utf-8")
            try:
                _run_observe_via_cli(str(repo), str(bad_contract), "2026-09-10T00:00:00+00:00")
                results.append(False)
                print("selftest invalid-contract-shape: expected FillError(13), got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_CONTRACT_INVALID
                results.append(ok)
                print(f"selftest invalid-contract-shape: expected exit=13 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # usage error
            usage_output = io.StringIO()
            with contextlib.redirect_stdout(usage_output):
                usage_code = main(["observe"])
            usage_report = json.loads(usage_output.getvalue())
            ok = usage_code == E_USAGE and usage_report.get("exit_code") == E_USAGE
            results.append(ok)
            print(f"selftest usage-error: expected exit=64 got exit={usage_code} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # bad --src-glob (pathspec-magic leading ':') is rejected before any git call is made
            try:
                _run_observe_via_cli(str(repo), str(contract_path), "2026-09-10T00:00:00+00:00",
                            src_glob=[":(exclude)**"])
                results.append(False)
                print("selftest bad-src-glob: expected FillError(15), got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_BAD_GLOB
                results.append(ok)
                print(f"selftest bad-src-glob: expected exit=15 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # an explicitly EMPTY --src-glob (what `--src-glob ""` or `--src-glob ","` splits to)
            # is a usage error, not a silent fallback to the defaults (checked by identity, not
            # truthiness — see _resolve_globs).
            try:
                _run_observe_via_cli(str(repo), str(contract_path), "2026-09-10T00:00:00+00:00", src_glob=[])
                results.append(False)
                print("selftest empty-src-glob-list: expected FillError(15), got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_BAD_GLOB
                results.append(ok)
                print(f"selftest empty-src-glob-list: expected exit=15 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # --repo pointing at a SUBDIRECTORY of a repository (not its own top level) is rejected
            # -- otherwise commits_count/instantiated-style facts would describe the WHOLE
            # repository while per-file reads are relative to the subdirectory.
            subdir = repo / "src"
            try:
                _run_observe_via_cli(str(subdir), str(contract_path), "2026-09-10T00:00:00+00:00")
                results.append(False)
                print("selftest repo-is-a-subdirectory: expected FillError(11), got none [MISMATCH]")
            except fc.FillError as exc:
                ok = exc.code == E_REPO_NOT_GIT
                results.append(ok)
                print(f"selftest repo-is-a-subdirectory: expected exit=11 got exit={exc.code} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # an ALIAS SPELLING of `--repo` -- same directory on disk (same inode/device), a
            # DIFFERENT string -- must be ACCEPTED, not rejected as "a subdirectory of" its own
            # top level. A plain symlink does not exercise this: both `Path.resolve()` (Python) and
            # `git rev-parse --show-toplevel` already follow it down to the identical physical path
            # string, so a bare string comparison of the two already agreed before this fix. A
            # CASE-VARIANT spelling of the same directory (accepted by APFS/HFS+ on macOS and NTFS
            # on Windows — both case-INSENSITIVE but case-PRESERVING by default) is the spelling
            # that actually differs as a string while still naming one directory: `resolve()` does
            # not normalize case, so the old string comparison rejected it; `os.path.samefile`
            # (inode/device identity) accepts it.
            alias_dir = repo.parent / (repo.name.upper())
            if alias_dir != repo and alias_dir.is_dir() and os.path.samefile(alias_dir, repo):
                try:
                    alias_report = _run_observe_via_cli(str(alias_dir), str(contract_path),
                                                "2026-09-10T00:00:00+00:00")
                    ok = alias_report["maturity"]["instantiated_date"] == "2026-09-01"
                    results.append(ok)
                    print(f"selftest repo-case-variant-alias-accepted: instantiated_date="
                          f"{alias_report['maturity'].get('instantiated_date')!r} "
                          f"[{'ok' if ok else 'MISMATCH'}]")
                except fc.FillError as exc:
                    results.append(False)
                    print(f"selftest repo-case-variant-alias-accepted: unexpected "
                          f"FillError({exc.code}) [MISMATCH]")
            else:
                skipped.append({"case": "repo-case-variant-alias-accepted",
                                "reason": "filesystem is case-sensitive"})
                print("selftest repo-case-variant-alias-accepted: [skip] (filesystem is case-sensitive)")

            # a tracked file whose NAME is a glob/pathspec-magic character (but not a leading ':',
            # which validate_path_grammar now rejects outright) must get its OWN last-modified date,
            # never another file's -- proves file_last_modified's `:(literal)` pathspec actually
            # suppresses glob interpretation of the file's own name. A SEPARATE repo (never `repo`
            # itself): mutating the shared `repo`'s own commit history here would change the
            # maturity facts the later hostile-GIT_DIR check compares against the original `report`.
            globname_dir = root / "globnamerepo"
            globname_repo = globname_dir / "repo"
            globname_repo.mkdir(parents=True)
            globname_env = _hermetic_selftest_env(globname_dir)
            subprocess.run(["git", "-C", str(globname_repo), "init", "-q"], check=True,
                            capture_output=True, env=globname_env, timeout=GIT_TIMEOUT_SECONDS)
            for key, value in (("user.name", "selftest"), ("user.email", "selftest@example.invalid"),
                                ("commit.gpgsign", "false")):
                subprocess.run(["git", "-C", str(globname_repo), "config", key, value], check=True,
                                capture_output=True, env=globname_env, timeout=GIT_TIMEOUT_SECONDS)
            _git_commit(globname_repo, globname_dir, {"a*b.md": "# a*b\n"}, "add glob-named file",
                        "2026-09-08T09:00:00+00:00")
            _git_commit(globname_repo, globname_dir, {"azzb.md": "# azzb, touched later\n"},
                        "add glob-match file", "2026-09-09T09:00:00+00:00")
            glob_name_contract = globname_dir / "globname.toml"
            glob_name_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "a*b.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            glob_name_report = _run_observe_via_cli(str(globname_repo), str(glob_name_contract),
                                            "2026-09-10T00:00:00+00:00")
            gobs = glob_name_report["files"][0]
            ok = gobs.get("last_modified_date") == "2026-09-08"
            results.append(ok)
            print(f"selftest literal-pathspec-for-glob-shaped-filename: "
                  f"last_modified_date={gobs.get('last_modified_date')!r} (want 2026-09-08, not "
                  f"2026-09-09 which belongs to azzb.md) [{'ok' if ok else 'MISMATCH'}]")

            # a leading ':' in a contract's own `file` value is now rejected at validation time
            # (git pathspec magic, same reason an observer glob already forbids it) -- never
            # reaches file_last_modified at all.
            colon_reason = fc.validate_path_grammar(":README.md")
            ok = colon_reason is not None
            results.append(ok)
            print(f"selftest leading-colon-path-rejected: reason={colon_reason!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # a tracked file that exists but cannot be READ (permission error) is `exists: false` +
            # `unreadable_reason`, the same shape as a symlink escape -- never the reserved
            # catch-all aborting every OTHER file's observation in the same run.
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                skipped.append({"case": "unreadable-file-permission-error",
                                "reason": "root bypasses permission bits"})
                skipped.append({"case": "unreadable-parent-directory-chmod-000",
                                "reason": "root bypasses permission bits"})
                print("selftest unreadable-file-permission-error: [skip] (root bypasses permission bits)")
                print("selftest unreadable-parent-directory-chmod-000: [skip] (root bypasses permission bits)")
            else:
                chmod_dir = root / "chmodrepo"
                chmod_repo = _build_selftest_repo(chmod_dir)
                unreadable_path = chmod_repo / "README.md"
                unreadable_path.chmod(0o000)
                try:
                    unreadable_contract = root / "unreadable.toml"
                    unreadable_contract.write_text(
                        'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                        '[[contracts]]\nfile = "README.md"\nimportance = "normal"\n'
                        'urgency_after_due = "1d"\n'
                        'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                        'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                        encoding="utf-8")
                    unreadable_report = _run_observe_via_cli(str(chmod_repo), str(unreadable_contract),
                                                     "2026-09-10T00:00:00+00:00")
                    uobs = unreadable_report["files"][0]
                    ok = (uobs["exists"] is False
                          and is_nonblank(uobs.get("unreadable_reason"))
                          and "PermissionError" in uobs.get("unreadable_reason", "")
                          and not fc.validate_observations(unreadable_report))
                    results.append(ok)
                    print(f"selftest unreadable-file-permission-error: exists={uobs['exists']} "
                          f"reason={uobs.get('unreadable_reason')!r} [{'ok' if ok else 'MISMATCH'}]")
                finally:
                    unreadable_path.chmod(0o644)  # restore before TemporaryDirectory cleanup

                # a NON-TRAVERSABLE PARENT DIRECTORY (chmod 000, not just the file itself): on
                # this interpreter `Path.is_file()`/`resolve()` both swallow the resulting
                # PermissionError, so this real-OS reproduction only proves "no crash, exists:
                # false" here -- the interpreter-version-specific regression (3.11/3.12 re-raising
                # it) is covered by the patched case directly below, which does not depend on which
                # Python this suite happens to run under.
                dirchmod_dir = root / "dirchmodrepo"
                dirchmod_repo = _build_selftest_repo(dirchmod_dir)
                nested_dir = dirchmod_repo / "nested"
                nested_dir.mkdir()
                (nested_dir / "x.md").write_text("# x\n", encoding="utf-8")
                nested_dir.chmod(0o000)
                try:
                    dirchmod_contract = root / "dirchmod.toml"
                    dirchmod_contract.write_text(
                        'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                        '[[contracts]]\nfile = "nested/x.md"\nimportance = "normal"\n'
                        'urgency_after_due = "1d"\n'
                        'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                        'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                        encoding="utf-8")
                    dirchmod_report = _run_observe_via_cli(str(dirchmod_repo), str(dirchmod_contract),
                                                   "2026-09-10T00:00:00+00:00")
                    dobs = dirchmod_report["files"][0]
                    ok = (dobs["exists"] is False
                          and is_nonblank(dobs.get("unreadable_reason"))
                          and "PermissionError" in dobs.get("unreadable_reason", "")
                          and not fc.validate_observations(dirchmod_report))
                    results.append(ok)
                    print(f"selftest unreadable-parent-directory-chmod-000: "
                          f"exists={dobs['exists']} reason={dobs.get('unreadable_reason')!r} "
                          f"[{'ok' if ok else 'MISMATCH'}]")
                finally:
                    nested_dir.chmod(0o755)  # restore before TemporaryDirectory cleanup

            # `observe_file` classifies `resolved.stat()`'s own outcome itself
            # (`stat.S_ISREG(st.st_mode)`) rather than calling `pathlib.Path.is_file()`, specifically
            # because `is_file()`'s OWN internal OSError handling is Python-version-dependent (this
            # README's 3.11/3.12 floor re-raises any errno outside ENOENT/ENOTDIR/EBADF/ELOOP --
            # EACCES from a non-traversable parent directory, for instance -- while 3.13+ swallows
            # EVERY OSError internally and returns False, silently losing the reason). Calling
            # `.stat()` directly sidesteps that version split entirely: `Path.stat()` has no such
            # internal swallowing on ANY supported version, so a `PermissionError` it raises always
            # reaches this function's own `except OSError` guard and becomes `exists: false` +
            # `unreadable_reason`, never escaping to `main`'s catch-all and aborting every other
            # file's observation (the same class of defect a permission error on the file itself is
            # already guarded against, one directory level up). Simulated here by patching
            # `Path.stat` to always raise `PermissionError`, so this proves the guard regardless of
            # which Python version this suite happens to run under (real-OS chmod-000-parent
            # coverage is the test directly above, on whichever Python this suite's own interpreter
            # happens to be).
            def _stat_raises_permission_error(self, *, follow_symlinks=True):
                raise PermissionError(13, "Permission denied (simulated stat() OSError)")

            from unittest.mock import patch as _patch
            is_file_dir = root / "isfilerepo"
            is_file_repo = _build_selftest_repo(is_file_dir)
            is_file_contract = root / "isfile.toml"
            is_file_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "README.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            with _patch.object(pathlib.Path, "stat", _stat_raises_permission_error):
                is_file_report = _run_observe_via_cli(str(is_file_repo), str(is_file_contract),
                                              "2026-09-10T00:00:00+00:00")
            ifobs = is_file_report["files"][0]
            ok = (ifobs["exists"] is False
                  and is_nonblank(ifobs.get("unreadable_reason"))
                  and "PermissionError" in ifobs.get("unreadable_reason", "")
                  and not fc.validate_observations(is_file_report))
            results.append(ok)
            print(f"selftest stat-oserror-caught-not-propagated: exists={ifobs['exists']} "
                  f"reason={ifobs.get('unreadable_reason')!r} [{'ok' if ok else 'MISMATCH'}]")

            # days_since_setup must never be negative (fill_common.validate_maturity itself
            # rejects a negative value) -- clock skew / a future-dated commit / a --now genuinely
            # earlier than the repo's own first commit all null it instead of guessing.
            skew_dir = root / "skewrepo"
            skew_repo = skew_dir / "repo"
            skew_repo.mkdir(parents=True)
            skew_env = _hermetic_selftest_env(skew_dir)
            subprocess.run(["git", "-C", str(skew_repo), "init", "-q"], check=True,
                            capture_output=True, env=skew_env, timeout=GIT_TIMEOUT_SECONDS)
            for key, value in (("user.name", "selftest"), ("user.email", "selftest@example.invalid"),
                                ("commit.gpgsign", "false")):
                subprocess.run(["git", "-C", str(skew_repo), "config", key, value], check=True,
                                capture_output=True, env=skew_env, timeout=GIT_TIMEOUT_SECONDS)
            _git_commit(skew_repo, skew_dir, {"README.md": "# readme\n"}, "future commit",
                        "2026-09-01T09:00:00+00:00")
            skew_contract = skew_dir / "skew.toml"
            skew_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "README.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            skew_report = _run_observe_via_cli(str(skew_repo), str(skew_contract), "2026-08-25T00:00:00+00:00")
            skew_dss = skew_report["maturity"]["days_since_setup"]
            ok = skew_dss is None and not fc.validate_observations(skew_report)
            results.append(ok)
            print(f"selftest days-since-setup-never-negative-future-now: "
                  f"days_since_setup={skew_dss!r} [{'ok' if ok else 'MISMATCH'}]")

            # a commit authored with a non-zero offset whose UTC calendar day is EARLIER
            # than its own author-local day, observed with a --now that is on that earlier UTC day
            # (but AFTER the commit in real wall-clock time) -- the naive "take the author-local
            # date, compare against a UTC --now" arithmetic from before this fix would report a
            # negative days_since_setup here (now's UTC date = 2026-08-31, author-local date =
            # 2026-09-01); the fix (compare UTC day to UTC day) reports 0, not a negative number,
            # and the observation still passes validate_observations either way.
            utcday_dir = root / "utcdayrepo"
            utcday_repo = utcday_dir / "repo"
            utcday_repo.mkdir(parents=True)
            utcday_env = _hermetic_selftest_env(utcday_dir)
            subprocess.run(["git", "-C", str(utcday_repo), "init", "-q"], check=True,
                            capture_output=True, env=utcday_env, timeout=GIT_TIMEOUT_SECONDS)
            for key, value in (("user.name", "selftest"), ("user.email", "selftest@example.invalid"),
                                ("commit.gpgsign", "false")):
                subprocess.run(["git", "-C", str(utcday_repo), "config", key, value], check=True,
                                capture_output=True, env=utcday_env, timeout=GIT_TIMEOUT_SECONDS)
            _git_commit(utcday_repo, utcday_dir, {"README.md": "# readme\n"}, "offset commit",
                        "2026-09-01T00:30:00+02:00")
            utcday_contract = utcday_dir / "utcday.toml"
            utcday_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "README.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            utcday_report = _run_observe_via_cli(str(utcday_repo), str(utcday_contract),
                                         "2026-08-31T23:00:00+00:00")
            utcday_m = utcday_report["maturity"]
            ok = (utcday_m["instantiated_date"] == "2026-08-31" and utcday_m["days_since_setup"] == 0
                  and not fc.validate_observations(utcday_report))
            results.append(ok)
            print(f"selftest commit-dates-are-utc-calendar-days: instantiated_date="
                  f"{utcday_m['instantiated_date']!r} days_since_setup="
                  f"{utcday_m['days_since_setup']!r} [{'ok' if ok else 'MISMATCH'}]")

            # a cherry-picked (or rebased, or amended, or clock-skewed) commit can be AUTHORED
            # before its own parent in `git log --reverse`'s history order -- `commit_dates` must
            # still come back sorted oldest-first (what `fill_common.validate_maturity` requires
            # and every evaluator indexes by), and `instantiated_date` must be the EARLIEST day, not
            # history order's first entry. BOTH commits touch `src/**` (the default `--src-glob`),
            # so this same history also proves `first_src_commit_date` follows the EARLIEST-touching
            # author day, never merely the first-in-history-order touching commit: the
            # second-in-history commit is the one authored first, so `first_src_commit_date` must
            # equal `instantiated_date` here, not the first-in-history commit's own later date.
            cherry_dir = root / "cherryrepo"
            cherry_repo = cherry_dir / "repo"
            cherry_repo.mkdir(parents=True)
            cherry_env = _hermetic_selftest_env(cherry_dir)
            subprocess.run(["git", "-C", str(cherry_repo), "init", "-q"], check=True,
                            capture_output=True, env=cherry_env, timeout=GIT_TIMEOUT_SECONDS)
            for key, value in (("user.name", "selftest"), ("user.email", "selftest@example.invalid"),
                                ("commit.gpgsign", "false")):
                subprocess.run(["git", "-C", str(cherry_repo), "config", key, value], check=True,
                                capture_output=True, env=cherry_env, timeout=GIT_TIMEOUT_SECONDS)
            _git_commit(cherry_repo, cherry_dir, {"src/a.py": "# a\n"},
                        "first commit in history order, touches src/**",
                        "2026-09-05T09:00:00+00:00")
            _git_commit(cherry_repo, cherry_dir, {"src/b.py": "# b\n"},
                        "second commit in history order, authored BEFORE the first "
                        "(cherry-pick/rebase/amend/skewed clock), touches src/**",
                        "2026-09-01T09:00:00+00:00")
            cherry_contract = cherry_dir / "cherry.toml"
            cherry_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "src/a.py"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            cherry_report = _run_observe_via_cli(str(cherry_repo), str(cherry_contract),
                                         "2026-09-10T00:00:00+00:00")
            cherry_m = cherry_report["maturity"]
            ok = (cherry_m["commit_dates"] == ["2026-09-01", "2026-09-05"]
                  and cherry_m["instantiated_date"] == "2026-09-01"
                  and cherry_m["first_src_commit_date"] == "2026-09-01"
                  and not fc.validate_observations(cherry_report))
            results.append(ok)
            print(f"selftest commit-dates-sorted-oldest-first-not-history-order: "
                  f"commit_dates={cherry_m['commit_dates']!r} "
                  f"instantiated_date={cherry_m['instantiated_date']!r} "
                  f"first_src_commit_date={cherry_m['first_src_commit_date']!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # a shallow clone (actions/checkout's own default) must never report a confidently
            # wrong instantiated/first-*-commit date or commit count computed from a truncated
            # history window -- every history-derived maturity key is simply ABSENT (never a
            # guessed value), so fill_evaluate.py resolves each one Kleene unknown.
            shallow_dir = root / "shallow"
            clone_result = subprocess.run(
                ["git", "clone", "--depth", "1", f"file://{repo}", str(shallow_dir)],
                capture_output=True, text=True, env=_hermetic_selftest_env(root),
                timeout=GIT_TIMEOUT_SECONDS)
            shallow_ok = (clone_result.returncode == 0 and is_git_repo(shallow_dir)
                          and is_shallow_repo(shallow_dir))
            shallow_report = _run_observe_via_cli(str(shallow_dir), str(contract_path),
                                          "2026-09-10T00:00:00+00:00")
            sm = shallow_report["maturity"]
            history_fields = ("instantiated_date", "first_src_commit_date", "first_test_date",
                               "first_spec_date", "first_dependency_date", "first_release_date",
                               "commits_count", "commit_dates", "days_since_setup")
            ok = (shallow_ok and sm == {}
                  and all(f not in sm for f in history_fields)
                  and not fc.validate_observations(shallow_report))
            results.append(ok)
            print(f"selftest shallow-clone-history-facts-are-unknown-not-wrong: "
                  f"is_shallow={shallow_ok} maturity={sm!r} [{'ok' if ok else 'MISMATCH'}]")

            # the PER-FILE fact `last_modified_date` is equally at risk from the graft boundary:
            # NOTES.md was last really changed in the repository's own first commit (2026-09-01),
            # long before the shallow clone's one visible commit (the 2026-09-07 release) -- but
            # that boundary commit LOOKS like a genuine change to every file present in its tree, so
            # an unguarded `git log -1 -- NOTES.md` on the shallow clone reports 2026-09-07, a
            # confidently WRONG recent date. The key must be ABSENT instead, and a bare `fresh`
            # predicate (which reads that key) must then resolve to unknown, never true.
            notes_full = file_last_modified(repo, "NOTES.md")
            notes_shallow_raw = file_last_modified(shallow_dir, "NOTES.md")
            snobs = shallow_report["files"][0]
            import fill_evaluate as fe
            fresh_state = fe.eval_filled_leaf({"predicate": "fresh", "max_age": "1d"}, snobs, {},
                                               fc.parse_now("2026-09-10"))
            ok = (snobs.get("file") == "NOTES.md" and notes_full == "2026-09-01"
                  and notes_shallow_raw == "2026-09-07"  # the graft-boundary illusion, unguarded
                  and "last_modified_date" not in snobs and fresh_state == fc.UNKNOWN)
            results.append(ok)
            print(f"selftest shallow-clone-per-file-last-modified-is-unknown-not-wrong: "
                  f"full={notes_full!r} raw-shallow-git-log={notes_shallow_raw!r} "
                  f"key-present={'last_modified_date' in snobs} fresh={fresh_state!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # a real repository with no tags at all: `git tag` itself succeeds (empty output) —
            # this is success-with-empty, never a failure, so `first_release_date` is `null`
            # (confirmed: no release) and the whole run still exits 0 (`cherry_report` above already
            # proves that by having completed without raising).
            ok = cherry_m.get("first_release_date") is None
            results.append(ok)
            print(f"selftest repo-with-no-tags-first-release-date-null-not-error: "
                  f"first_release_date={cherry_m.get('first_release_date')!r} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # UNBORN REPOSITORY: `git init`, a file present on disk, but NO commit at all yet.
            # `commit_dates`/`first_commit_touching`'s own `git log` would fail outright here (exit
            # 128, "does not have any commits yet") -- the point of this case is that `compute_
            # maturity`'s explicit `_is_unborn_repo` check keeps that genuine, EXPECTED absence of
            # history from ever reaching (and being misread by) those functions at all: the run
            # succeeds (exit 0), with the documented genuine-empty-history values, not exit 14.
            unborn_dir = root / "unbornrepo"
            unborn_repo = unborn_dir / "repo"
            unborn_repo.mkdir(parents=True)
            unborn_env = _hermetic_selftest_env(unborn_dir)
            subprocess.run(["git", "-C", str(unborn_repo), "init", "-q"], check=True,
                            capture_output=True, env=unborn_env, timeout=GIT_TIMEOUT_SECONDS)
            for key, value in (("user.name", "selftest"), ("user.email", "selftest@example.invalid"),
                                ("commit.gpgsign", "false")):
                subprocess.run(["git", "-C", str(unborn_repo), "config", key, value], check=True,
                                capture_output=True, env=unborn_env, timeout=GIT_TIMEOUT_SECONDS)
            (unborn_repo / "README.md").write_text("# readme, never committed\n", encoding="utf-8")
            unborn_contract = unborn_dir / "unborn.toml"
            unborn_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "README.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            unborn_report = _run_observe_via_cli(str(unborn_repo), str(unborn_contract),
                                         "2026-09-10T00:00:00+00:00")
            um = unborn_report["maturity"]
            uf0 = unborn_report["files"][0]
            ok = (um.get("commits_count") == 0 and um.get("commit_dates") == []
                  and um.get("instantiated_date") is None and um.get("first_src_commit_date") is None
                  and um.get("first_test_date") is None and um.get("first_spec_date") is None
                  and um.get("first_dependency_date") is None
                  and um.get("first_release_date") is None and um.get("days_since_setup") is None
                  and uf0.get("exists") is True and "last_modified_date" not in uf0
                  and not fc.validate_observations(unborn_report))
            results.append(ok)
            print(f"selftest unborn-repo-genuine-empty-history-not-failure: maturity={um!r} "
                  f"file-last-modified-present={'last_modified_date' in uf0} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # UNBORN REPOSITORY, with a STAGED (`git add`) file: `rev-parse --verify --quiet HEAD`
            # still exits 1 (no commit yet), and `_is_unborn_repo`'s own confirming `cat-file
            # --batch-all-objects` listing now finds exactly one object -- a `blob` (the staged
            # file's own content), never a `commit` -- so this must still resolve to "genuinely
            # unborn", not be mistaken for the unreadable-refs case the next case below covers.
            staged_dir = root / "stagedunbornrepo"
            staged_repo = staged_dir / "repo"
            staged_repo.mkdir(parents=True)
            staged_env = _hermetic_selftest_env(staged_dir)
            subprocess.run(["git", "-C", str(staged_repo), "init", "-q"], check=True,
                            capture_output=True, env=staged_env, timeout=GIT_TIMEOUT_SECONDS)
            for key, value in (("user.name", "selftest"), ("user.email", "selftest@example.invalid"),
                                ("commit.gpgsign", "false")):
                subprocess.run(["git", "-C", str(staged_repo), "config", key, value], check=True,
                                capture_output=True, env=staged_env, timeout=GIT_TIMEOUT_SECONDS)
            (staged_repo / "README.md").write_text("# readme, staged but never committed\n",
                                                     encoding="utf-8")
            subprocess.run(["git", "-C", str(staged_repo), "add", "-A"], check=True,
                            capture_output=True, env=staged_env, timeout=GIT_TIMEOUT_SECONDS)
            staged_contract = staged_dir / "staged.toml"
            staged_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "README.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            staged_ok = True
            staged_report = None
            try:
                staged_report = _run_observe_via_cli(str(staged_repo), str(staged_contract),
                                             "2026-09-10T00:00:00+00:00")
            except fc.FillError:
                staged_ok = False
            if staged_report is not None:
                sm2 = staged_report["maturity"]
                staged_ok = (staged_ok and sm2.get("commits_count") == 0
                             and sm2.get("instantiated_date") is None
                             and not fc.validate_observations(staged_report))
            results.append(staged_ok)
            print(f"selftest unborn-repo-with-staged-file-still-unborn-and-valid: "
                  f"maturity={staged_report['maturity'] if staged_report else None!r} "
                  f"[{'ok' if staged_ok else 'MISMATCH'}]")

            # UNREADABLE REFS, with a REAL commit: `rev-parse --verify --quiet HEAD` ALSO exits 1
            # here (confirmed live on this module's own supported git version) -- indistinguishable
            # from genuine unborn at that one call alone. `_is_unborn_repo`'s confirming check must
            # catch this: the repo's object store still has a `commit` object on disk even though
            # its ref cannot be read, so this must raise exit 14, never silently report
            # confirmed-negative `null` facts for a repo that actually has history. Skipped under
            # root (permission bits do not block root's own reads, same exception used elsewhere in
            # this selftest); the permission is always restored in a `finally`, even on failure, so
            # `tempfile.TemporaryDirectory`'s own cleanup can still remove the directory tree.
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                skipped.append({"case": "unreadable-refs-with-real-commit-raises-not-null",
                                "reason": "root bypasses permission bits"})
                print("selftest unreadable-refs-with-real-commit-raises-not-null: [skip] "
                      "(root bypasses permission bits)")
            else:
                unreadable_refs_dir = root / "unreadablerefsrepo"
                unreadable_refs_repo = _build_selftest_repo(unreadable_refs_dir)
                refs_heads = unreadable_refs_repo / ".git" / "refs" / "heads"
                refs_heads.chmod(0o000)
                try:
                    import contextlib as _contextlib_refs
                    import io as _io_refs
                    unreadable_refs_contract = root / "unreadablerefs.toml"
                    unreadable_refs_contract.write_text(_standard_contract_text(), encoding="utf-8")
                    _output = _io_refs.StringIO()
                    with _contextlib_refs.redirect_stdout(_output):
                        _code = main(["observe", "--repo", str(unreadable_refs_repo), "--contract",
                                      str(unreadable_refs_contract), "--now",
                                      "2026-09-10T00:00:00+00:00"])
                    try:
                        _payload = json.loads(_output.getvalue())
                    except json.JSONDecodeError:
                        _payload = {}
                    ok = (_code == E_GIT_TIMEOUT and _payload.get("exit_code") == E_GIT_TIMEOUT
                          and "not provably empty" in
                          (_payload.get("errors") or [{}])[0].get("reason", ""))
                    results.append(ok)
                    print(f"selftest unreadable-refs-with-real-commit-raises-not-null: exit={_code} "
                          f"reason={(_payload.get('errors') or [{}])[0].get('reason', '')!r} "
                          f"[{'ok' if ok else 'MISMATCH'}]")
                finally:
                    refs_heads.chmod(0o755)  # restore before TemporaryDirectory cleanup

            # Six fault-injection cases, one per `_run_git` call site that must distinguish a
            # genuine infrastructure FAILURE (nonzero exit) from success-with-empty-output: the
            # shallow-status probe, the repo-wide commit-dates query, the src/**-glob history-touch
            # query (run here through the full CLI boundary; a direct-`compute_maturity` check of the
            # same scenario follows right below), the tag-list query, a found tag's own
            # `git log -1` (tag-log), and a per-file `last_modified_date` query. Every one must
            # surface as CLI exit 14
            # (E_GIT_TIMEOUT), never a `null`/absent fact a confirmed-negative evaluation could read
            # as "not-due". `repo`/`contract_path` (built earlier in this function) already has a
            # tag, commits touching every default glob, and a file (`NOTES.md`) with real history —
            # every query this loop targets is actually exercised on a real, successful run first.
            from unittest.mock import patch as _patch_git_failures
            import contextlib as _contextlib_gf
            import io as _io_gf
            _real_run_git_for_failures = _run_git

            def _git_fails_on(matcher):
                def _patched(repo_arg, args, timeout=GIT_TIMEOUT_SECONDS, env=None):
                    if matcher(args):
                        return 128, "", "fatal: simulated git failure for regression coverage"
                    return _real_run_git_for_failures(repo_arg, args, timeout=timeout, env=env)
                return _patched

            def _is_shallow_status_query(args):
                return args[:2] == ["rev-parse", "--is-shallow-repository"]

            def _is_commit_dates_query(args):
                return "log" in args and "-1" not in args and "--" not in args

            def _is_src_glob_touch_query(args):
                return any(":(glob)src/**" in str(a) for a in args)

            def _is_tag_list_query(args):
                return args[:1] == ["tag"]

            def _is_tag_log_query(args):
                return ("log" in args and "-1" in args
                        and not any(str(a).startswith(":(") for a in args))

            def _is_per_file_last_modified_query(args):
                return any(str(a).startswith(":(literal)") for a in args)

            git_failure_cases = [
                ("shallow-status-query-failure", _is_shallow_status_query),
                ("commit-dates-query-failure", _is_commit_dates_query),
                ("src-glob-history-query-failure", _is_src_glob_touch_query),
                ("tag-list-query-failure", _is_tag_list_query),
                ("tag-log-query-failure", _is_tag_log_query),
                ("per-file-last-modified-query-failure", _is_per_file_last_modified_query),
            ]
            for _case_name, _matcher in git_failure_cases:
                _output = _io_gf.StringIO()
                with _patch_git_failures.object(sys.modules[__name__], "_run_git",
                                                 side_effect=_git_fails_on(_matcher)):
                    with _contextlib_gf.redirect_stdout(_output):
                        _code = main(["observe", "--repo", str(repo), "--contract",
                                      str(contract_path), "--now", "2026-09-10T00:00:00+00:00"])
                try:
                    _payload = json.loads(_output.getvalue())
                except json.JSONDecodeError:
                    _payload = {}
                ok = _code == E_GIT_TIMEOUT and _payload.get("exit_code") == E_GIT_TIMEOUT
                results.append(ok)
                print(f"selftest failed-git-query-never-becomes-empty-fact[{_case_name}]: "
                      f"exit={_code} reason={(_payload.get('errors') or [{}])[0].get('reason', '')!r} "
                      f"[{'ok' if ok else 'MISMATCH'}]")

            # The same failure checked directly against `compute_maturity` (not only at the CLI
            # boundary the loop above covers): a failed src/** history query must raise
            # `FillError(14)` out of `compute_maturity` itself, never return a `None`
            # `first_src_commit_date` that downstream evaluation would read as a confirmed negative
            # ("not-due").
            def _history_failure_repro_git(repo_arg, args):
                if any(":(glob)src/**" in str(a) for a in args):
                    return 128, "", "fatal: failed to read history"
                if "log" in args and "--" not in args:
                    return 0, "2026-09-01T00:00:00Z\n", ""
                return 0, "", ""

            repro_ok = True
            repro_reason = None
            with _patch_git_failures.object(sys.modules[__name__], "_run_git",
                                             side_effect=_history_failure_repro_git):
                try:
                    compute_maturity(repo, fc.parse_now("2026-10-01T00:00:00Z"), ["src/**"],
                                      ["test/**"], ["spec/**"], ["package.json"], False)
                    repro_ok = False
                except fc.FillError as exc:
                    repro_ok = exc.code == E_GIT_TIMEOUT
                    repro_reason = str(exc)
            results.append(repro_ok)
            print(f"selftest failed-history-query-raises-inside-compute-maturity-not-null: "
                  f"raised={repro_ok} reason={repro_reason!r} [{'ok' if repro_ok else 'MISMATCH'}]")

            # every `git log` call carries BOTH --no-show-signature and
            # -c log.showSignature=false (captured directly, not simulated via real gpg/ssh signing
            # infrastructure this self-test would otherwise have to depend on).
            captured_argvs = []
            _real_subprocess_run = subprocess.run

            def _capture_git_calls(cmd, *a, **kw):
                if isinstance(cmd, list) and cmd[:1] == ["git"]:
                    captured_argvs.append(cmd)
                return _real_subprocess_run(cmd, *a, **kw)

            from unittest.mock import patch
            with patch("subprocess.run", side_effect=_capture_git_calls):
                commit_dates(repo)
                first_commit_touching(repo, ["src/**"])
                first_release_date(repo)
                file_last_modified(repo, "README.md")

            def _has_log_hygiene(argv: list) -> bool:
                if "log" not in argv:
                    return False
                i = argv.index("log")
                return (argv[max(i - 2, 0):i] == ["-c", "log.showSignature=false"]
                        and i + 1 < len(argv) and argv[i + 1] == "--no-show-signature")

            log_argvs = [a for a in captured_argvs if "log" in a]
            log_hygiene_ok = bool(log_argvs) and all(_has_log_hygiene(a) for a in log_argvs)
            results.append(log_hygiene_ok)
            print(f"selftest git-log-signature-hygiene: calls={len(log_argvs)} "
                  f"[{'ok' if log_hygiene_ok else 'MISMATCH'}]")

            # _to_date's own error message quotes (truncated, repr) what it actually read,
            # instead of only saying "non-canonical" with no clue what the bad input was.
            to_date_ok = True
            try:
                _to_date("not-a-date-at-all")
                to_date_ok = False
            except fc.FillError as exc:
                to_date_ok &= exc.code == E_GIT_TIMEOUT and "not-a-date-at-all" in str(exc)
            try:
                _to_date("x" * 10000)
                to_date_ok = False
            except fc.FillError as exc:
                to_date_ok &= exc.code == E_GIT_TIMEOUT and len(str(exc)) < 1000
            results.append(to_date_ok)
            print(f"selftest to-date-error-quotes-truncated-input: [{'ok' if to_date_ok else 'MISMATCH'}]")

            # a file entry naming a symlink that escapes --repo is never read
            outside_dir = root / "outside"
            outside_dir.mkdir()
            secret = outside_dir / "SECRET.md"
            secret.write_text("outside content\n", encoding="utf-8")
            escape_link = repo / "ESCAPE.md"
            try:
                escape_link.symlink_to(secret)
                have_symlink = True
            except OSError as exc:
                if exc.errno not in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP}:
                    raise  # disk exhaustion and unexpected errors are test infrastructure failures
                have_symlink = False
                skipped.append({"case": "symlink-escapes-repo",
                                "reason": "symlink creation is unsupported or forbidden"})
                print("selftest symlink-escapes-repo: [skip] "
                      "(symlink creation is unsupported or forbidden)")
            if have_symlink:
                escape_contract = root / "escape.toml"
                escape_contract.write_text(
                    'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                    '[[contracts]]\nfile = "ESCAPE.md"\nimportance = "normal"\n'
                    'urgency_after_due = "1d"\n'
                    'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                    'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                    encoding="utf-8")
                escape_report = _run_observe_via_cli(str(repo), str(escape_contract),
                                             "2026-09-10T00:00:00+00:00")
                fobs = escape_report["files"][0]
                ok = (fobs["exists"] is False
                      and is_nonblank(fobs.get("unreadable_reason"))
                      and "markers_present" not in fobs)
                results.append(ok)
                print(f"selftest symlink-escapes-repo: exists={fobs['exists']} "
                      f"reason={fobs.get('unreadable_reason')!r} [{'ok' if ok else 'MISMATCH'}]")

            # a file larger than MAX_SCAN_BYTES is not content-scanned
            big_dir = root / "bigrepo"
            big_repo = _build_selftest_repo(big_dir)  # reuse builder shape under a fresh root
            _git_commit(big_repo, big_dir, {"BIG.md": "x" * (MAX_SCAN_BYTES + 1)}, "add big file",
                        "2026-09-08T09:00:00+00:00")
            big_contract = big_dir / "big.toml"
            big_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "BIG.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            big_report = _run_observe_via_cli(str(big_repo), str(big_contract), "2026-09-10T00:00:00+00:00")
            bobs = big_report["files"][0]
            ok = (bobs["exists"] is True and is_nonblank(bobs.get("scan_skipped_reason"))
                  and "markers_present" not in bobs and bobs.get("last_modified_date") is not None)
            results.append(ok)
            print(f"selftest oversized-file-not-scanned: exists={bobs['exists']} "
                  f"reason={bobs.get('scan_skipped_reason')!r} [{'ok' if ok else 'MISMATCH'}]")

            # a non-UTF-8 file never crashes the scan (errors="fill-substitute")
            badenc_path = repo / "BADENC.md"
            badenc_path.write_bytes(b"<!-- FILL: x -->\xff\xfe more text\n")
            badenc_contract = root / "badenc.toml"
            badenc_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "BADENC.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            badenc_report = _run_observe_via_cli(str(repo), str(badenc_contract), "2026-09-10T00:00:00+00:00")
            eobs = badenc_report["files"][0]
            ok = eobs["exists"] is True and eobs.get("markers_present") == ["x"]
            results.append(ok)
            print(f"selftest non-utf8-file-no-crash: markers={eobs.get('markers_present')} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

            # An adversarial stamp date (syntactically digit-shaped but not a real calendar day —
            # STAMP_RE's own capture group only checks shape, not validity) must never survive into
            # the observation: a successful observer run must ALWAYS produce a document
            # fill_common.validate_observations itself accepts, since fill_evaluate.py trusts
            # exactly that contract. Cross-checked against the real validator, not just eyeballed.
            # Case (a): ONLY an impossible date present (no valid alternative that could win by
            # coincidence) — proves the impossible date is dropped, not merely out-competed.
            adv_stamp_path = repo / "ADVSTAMP.md"
            adv_stamp_path.write_text("<!-- STAMP: checked 2026-02-30 -->\n", encoding="utf-8")
            adv_stamp_contract = root / "advstamp.toml"
            adv_stamp_contract.write_text(
                'fill_version = "0.1.0"\ntemplate = "t"\n\n'
                '[[contracts]]\nfile = "ADVSTAMP.md"\nimportance = "normal"\n'
                'urgency_after_due = "1d"\n'
                'filled_when = { any = [ { predicate = "stamp", key = "checked" } ] }\n'
                'due_when = { any = [ { predicate = "instantiated" } ] }\n',
                encoding="utf-8")
            adv_stamp_report = _run_observe_via_cli(str(repo), str(adv_stamp_contract),
                                            "2026-09-10T00:00:00+00:00")
            adv_stamps = adv_stamp_report["files"][0].get("stamps", {})
            validation_errors = fc.validate_observations(adv_stamp_report)
            ok = adv_stamps.get("checked") is None and not validation_errors
            results.append(ok)
            print(f"selftest observer-drops-impossible-stamp-date: stamps={adv_stamps!r} "
                  f"errors={validation_errors!r} [{'ok' if ok else 'MISMATCH'}]")

            now_ok = True
            for value in ("20260930", "2026-09-30\n", "2026-09-30T00:00:00Z\n",
                          "0001-01-01T00:00:00+01:00", "9999-12-31T23:00:00-02:00",
                          "2026-09-30T00:00:00+00:60", "2026-02-30",
                          "2026-09-30T24:00:00"):  # not a second spelling of next-day midnight
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["observe", "--repo", str(repo), "--contract", str(adv_stamp_contract),
                                 "--now", value])
                now_ok &= code == E_USAGE and json.loads(output.getvalue())["exit_code"] == E_USAGE
            results.append(now_ok)
            print(f"selftest canonical-now-cli-usage-errors: [{'ok' if now_ok else 'MISMATCH'}]")

            # Multiple dates: invalid spellings cannot displace the latest valid date.
            adv_stamp_path.write_text(
                "<!-- STAMP: checked 2026-01-15 -->\n<!-- STAMP: checked 2026-02-30 -->\n"
                "<!-- STAMP: checked 20260930 -->\n<!-- STAMP: checked 0000-01-01 -->\n"
                "<!-- STAMP: checked 2026-1-31 -->\n",
                encoding="utf-8")
            adv_stamp_report2 = _run_observe_via_cli(str(repo), str(adv_stamp_contract),
                                             "2026-09-10T00:00:00+00:00")
            adv_stamps2 = adv_stamp_report2["files"][0].get("stamps", {})
            validation_errors2 = fc.validate_observations(adv_stamp_report2)
            ok2 = adv_stamps2.get("checked") == "2026-01-15" and not validation_errors2
            results.append(ok2)
            print(f"selftest observer-output-with-adversarial-stamp-passes-validate_observations: "
                  f"stamps={adv_stamps2!r} errors={validation_errors2!r} "
                  f"[{'ok' if ok2 else 'MISMATCH'}]")

            # a hostile ambient GIT_DIR/GIT_INDEX_FILE/GIT_WORK_TREE (as a leaked pre-commit hook
            # environment would set) must not change the observation: this proves the env scrub.
            hostile_root = root / "hostile-not-a-repo"
            hostile_git_dir = hostile_root / ".git"
            saved = {k: os.environ.get(k) for k in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
            os.environ["GIT_DIR"] = str(hostile_git_dir)
            os.environ["GIT_INDEX_FILE"] = str(hostile_git_dir / "index")
            os.environ["GIT_WORK_TREE"] = str(hostile_root)
            try:
                hostile_report = _run_observe_via_cli(str(repo), str(contract_path),
                                              "2026-09-10T00:00:00+00:00")
                hostile_ok = hostile_report["maturity"] == report["maturity"]
                hostile_error = None
            except fc.FillError as exc:
                hostile_ok = False
                hostile_error = exc
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            results.append(hostile_ok)
            print(f"selftest git-env-scrub-under-hostile-GIT_DIR: matches-baseline={hostile_ok} "
                  f"error={hostile_error} [{'ok' if hostile_ok else 'MISMATCH'}]")

            # a git call that cannot complete in time is reported (exit 14), not hung
            timeout_ok, timeout_elapsed = _run_git_timeout_check(root, repo)
            results.append(timeout_ok)
            print(f"selftest git-call-timeout-reported: ok={timeout_ok} "
                  f"({timeout_elapsed:.3f}s, subprocess-bounded) [{'ok' if timeout_ok else 'MISMATCH'}]")

            # an adversarial file (many unclosed FILL markers, right at the size ceiling) still
            # scans within a generous wall-clock bound — proves MAX_SCAN_BYTES actually bounds the
            # regex's worst case, not merely that oversized files are skipped. 10s (not 2s): the
            # point of this case is LINEARITY, not absolute speed, and a 2s ceiling around a
            # subprocess startup plus a 6-commit git repo build is a flake risk on a slow/loaded CI
            # runner — 10s matches the deep-expression-tree case's own bound in fill_validate.py.
            adversarial_ok, adversarial_elapsed = _run_adversarial_markers_case_bounded(10.0)
            results.append(adversarial_ok)
            print(f"selftest adversarial-unclosed-markers-bounded: ok={adversarial_ok} "
                  f"({adversarial_elapsed:.3f}s, subprocess-bounded) [{'ok' if adversarial_ok else 'MISMATCH'}]")

        from unittest.mock import patch
        output = io.StringIO()
        with patch("tempfile.TemporaryDirectory", side_effect=OSError("temp directory unavailable")):
            with contextlib.redirect_stdout(output):
                infra_code = run_selftest()
        infra_ok = (infra_code == E_SELFTEST and "selftest infrastructure failure" in output.getvalue()
                    and "temp directory unavailable" in output.getvalue())
        results.append(infra_ok)
        print(f"selftest temp-directory-infrastructure-error: [{'ok' if infra_ok else 'MISMATCH'}]")

        # Regression: a `--repo` argument that is itself a hostile, 10,000-character string used
        # to make `--repo does not exist or is not a directory: <repo>` interpolate the raw
        # argument in full. `fc.bound_path_preview` at
        # the source, and `_error_report`'s own unconditional `fc.bound_reason` pass as the final
        # backstop, now keep the top-level `reason` no longer than `fc.DIAGNOSTIC_REASON_MAX_CHARS`.
        huge_repo_output = io.StringIO()
        with contextlib.redirect_stdout(huge_repo_output):
            huge_repo_code = main(["observe", "--repo", "r" * 10_000, "--contract",
                                    "nonexistent-selftest-contract.toml",
                                    "--now", "2026-09-10T00:00:00Z"])
        huge_repo_report = json.loads(huge_repo_output.getvalue())
        huge_repo_reason = (huge_repo_report.get("errors") or [{}])[0].get("reason", "")
        huge_repo_ok = (huge_repo_code == E_REPO_MISSING
                         and len(huge_repo_reason) <= fc.DIAGNOSTIC_REASON_MAX_CHARS)
        results.append(huge_repo_ok)
        print(f"selftest cli-reachable-oversized-repo-argument: exit={huge_repo_code} "
              f"reason_len={len(huge_repo_reason)} cap={fc.DIAGNOSTIC_REASON_MAX_CHARS} "
              f"[{'ok' if huge_repo_ok else 'MISMATCH'}]")

        # Regression: a `--repo` argument that is not valid Unicode at all (the OS gives
        # Python a raw byte with no UTF-8 interpretation; `surrogateescape` turns it into a lone
        # UTF-16 surrogate in the decoded argv string) must still produce a report whose `reason`
        # text re-encodes as UTF-8 cleanly. Mutation that turns this red: reverting
        # `fc.bound_path_preview` to `s = str(path)` with no escaping (the pre-fix code) makes
        # `surrogate_utf8_ok` False.
        surrogate_repo_output = io.StringIO()
        with contextlib.redirect_stdout(surrogate_repo_output):
            surrogate_repo_code = main(["observe", "--repo", "missing-\udcff", "--contract",
                                         "nonexistent-selftest-contract.toml",
                                         "--now", "2026-09-10T00:00:00Z"])
        surrogate_repo_report = json.loads(surrogate_repo_output.getvalue())
        surrogate_repo_reason = (surrogate_repo_report.get("errors") or [{}])[0].get("reason", "")
        surrogate_repo_utf8_ok = True
        try:
            surrogate_repo_reason.encode("utf-8")
        except UnicodeEncodeError:
            surrogate_repo_utf8_ok = False
        surrogate_repo_ok = (surrogate_repo_code == E_REPO_MISSING and surrogate_repo_utf8_ok
                             and len(surrogate_repo_reason) <= fc.DIAGNOSTIC_REASON_MAX_CHARS
                             and "\udcff" not in surrogate_repo_reason)
        results.append(surrogate_repo_ok)
        print(f"selftest lone-surrogate-repo-argument-escaped-in-report: exit={surrogate_repo_code} "
              f"reason={surrogate_repo_reason!r} [{'ok' if surrogate_repo_ok else 'MISMATCH'}]")

        print(f"selftest version-handshake: this observer implements {OBSERVER_VERSION!r}")

        # The regression that closes the class (argv fuzz). A fresh temp git repo + contract (the
        # SAME helpers the main self-test above builds its own from), so every value-taking option
        # has a known-valid baseline to fuzz from.
        with tempfile.TemporaryDirectory(prefix="fill_observe_argv_fuzz_") as fuzz_tmp:
            fuzz_root = pathlib.Path(fuzz_tmp)
            fuzz_repo = _build_selftest_repo(fuzz_root)
            fuzz_contract_path = fuzz_root / "contract.toml"
            fuzz_contract_path.write_text(_standard_contract_text(), encoding="utf-8")
            argv_fuzz_baselines = {
                "observe": ["observe", "--repo", str(fuzz_repo), "--contract", str(fuzz_contract_path),
                            "--now", "2026-09-10T00:00:00+00:00"],
            }
            argv_fuzz_exits = {0, E_REPO_MISSING, E_REPO_NOT_GIT, E_CONTRACT_UNREADABLE,
                               E_CONTRACT_INVALID, E_GIT_TIMEOUT, E_BAD_GLOB, E_USAGE, E_INPUT}
            argv_fuzz_covered, argv_fuzz_failures = _run_argv_fuzz(
                HERE / "fill_observe.py", make_parser(), argv_fuzz_baselines, argv_fuzz_exits)
            encoding_failures = _encoding_regression_failures(
                HERE / "fill_observe.py",
                [(argv_fuzz_baselines["observe"], {0}), (["observe"], {E_USAGE})])
            output_checks = fc.output_boundary_selftest(
                HERE / "fill_observe.py", argv_fuzz_baselines["observe"], ["observe"])
        argv_fuzz_ok = not argv_fuzz_failures
        results.append(argv_fuzz_ok)
        print(f"selftest argv-fuzz-every-option-produces-a-clean-report: "
              f"{argv_fuzz_covered} position(s) covered, {len(argv_fuzz_failures)} failure(s) "
              f"{argv_fuzz_failures[:3]!r} [{'ok' if argv_fuzz_ok else 'MISMATCH'}]")

        encoding_ok = not encoding_failures
        results.append(encoding_ok)
        print(f"selftest pythonioencoding-does-not-corrupt-report: "
              f"{len(encoding_failures)} failure(s) {encoding_failures[:3]!r} "
              f"[{'ok' if encoding_ok else 'MISMATCH'}]")

        for name, ok, detail in output_checks:
            results.append(ok)
            print(f"selftest output-boundary-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")

        boundary_controls = boundary_defect_controls(
            HERE / "fill_observe.py", "_error_report", "run_observe",
            ["observe"], argv_fuzz_baselines["observe"])
        for name, (ok, detail) in boundary_controls.items():
            results.append(ok)
            print(f"selftest report-boundary-defect-{name}: {detail} "
                  f"[{'ok' if ok else 'MISMATCH'}]")
        boundary_controls_ok = all(ok for ok, _ in boundary_controls.values())
        print(f"selftest report-schema-conformance: real boundary controls="
              f"{sorted(boundary_controls)} "
              f"[{'ok' if boundary_controls_ok else 'MISMATCH'}]")

        status = 0 if results and all(results) and boundary_controls_ok else E_SELFTEST
        print("SELFTEST SUMMARY: " + json.dumps({
            "passed": sum(bool(result) for result in results),
            "failed": sum(not result for result in results),
            "skipped": skipped,
        }, sort_keys=True))
        print(f"SELFTEST {'PASS' if status == 0 else 'FAIL'}: {len(results)} cases")
        return status
    except Exception as exc:
        print(f"FAIL exit=99: selftest infrastructure failure ({type(exc).__name__}: {exc})")
        return E_SELFTEST


def is_nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _run_git_timeout_check(root: pathlib.Path, repo: pathlib.Path) -> tuple:
    """Points PATH at a fake `git` that only ever sleeps, so `_run_git`'s own timeout fires
    deterministically (never flaky, unlike racing a real git call against a very short timeout).
    Runs in-process but bounds itself with a short timeout argument to `_run_git`, and separately
    measures wall-clock elapsed time to prove the call did NOT wait for the fake git's full sleep."""
    import time
    slow_bin = root / "slow-git-bin"
    slow_bin.mkdir(exist_ok=True)
    script = slow_bin / "git"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(0o755)
    env = _scrubbed_git_env(extra={"PATH": f"{slow_bin}{os.pathsep}{os.environ.get('PATH', '')}"})
    start = time.monotonic()
    try:
        _run_git(repo, ["rev-parse", "--is-inside-work-tree"], timeout=1.0, env=env)
        return False, time.monotonic() - start  # should have raised
    except fc.FillError as exc:
        elapsed = time.monotonic() - start
        return exc.code == E_GIT_TIMEOUT and elapsed < 4.0, elapsed


def _adversarial_markers_text() -> str:
    """Many unclosed `<!-- FILL:` openers (no matching `-->` anywhere), sized to stay just under
    MAX_SCAN_BYTES (now 1 MiB): the shape that made the old `.*?`-DOTALL regex scan quadratic. The
    current linear scanner (`fill_common._iter_html_comments`) finds the FIRST unclosed `<!--` and
    stops — one O(file size) `str.find` call, not thousands of them — so this proves the scan
    completes in comfortably sub-second time even at the full 1 MiB ceiling, not merely that the
    ceiling itself is never exceeded."""
    opener = "<!-- FILL: unclosed marker with no closing tag at all here "
    count = (MAX_SCAN_BYTES - 1024) // len(opener)
    return opener * count


def _run_adversarial_markers_case() -> dict:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="fill_observe_adversarial_") as tmp:
        root = pathlib.Path(tmp)
        repo = _build_selftest_repo(root)
        (repo / "ADV.md").write_text(_adversarial_markers_text(), encoding="utf-8")
        contract_path = root / "adv.toml"
        contract_path.write_text(
            'fill_version = "0.1.0"\ntemplate = "t"\n\n'
            '[[contracts]]\nfile = "ADV.md"\nimportance = "normal"\n'
            'urgency_after_due = "1d"\n'
            'filled_when = { any = [ { predicate = "no-marker" } ] }\n'
            'due_when = { any = [ { predicate = "instantiated" } ] }\n',
            encoding="utf-8")
        report = _run_observe_via_cli(str(repo), str(contract_path), "2026-09-10T00:00:00+00:00")
        fobs = report["files"][0]
        return {"exists": fobs["exists"], "scan_skipped": "scan_skipped_reason" in fobs,
                "markers_present": fobs.get("markers_present")}


def _run_adversarial_markers_case_bounded(max_seconds: float) -> tuple:
    import subprocess
    import time
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "run-case",
             "adversarial-markers"],
            cwd=str(HERE), capture_output=True, text=True, timeout=max_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, time.monotonic() - start
    elapsed = time.monotonic() - start
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, elapsed
    ok = (payload.get("exists") is True and payload.get("scan_skipped") is False
          and payload.get("markers_present") == [])
    return ok, elapsed


def _error_report(code: int, reason: str) -> dict:
    """The ONE error-report constructor: every exit path that is not a computed observation report
    (a FillError, or the catch-all for an unexpected exception) builds its output through this
    function, so the documented shape (`status`/`exit_code`/`errors`) cannot drift between one exit
    path and another. `reason` is passed through `fc.bound_reason` HERE, unconditionally — the
    final, structural enforcement of the documented ≤2,000-character `reason` bound, so it holds
    even for a `reason` built by a call site that forgot to self-bound."""
    return {"status": "unknown", "exit_code": code,
            "errors": [{"code": code, "reason": fc.bound_reason(reason)}]}


def _main(argv=None) -> int:
    try:
        parser = make_parser()
        if argv is None:
            argv = sys.argv[1:]
        if argv[:1] == ["run-case"]:
            # Internal: the self-test's own subprocess entry point for the bounded adversarial-
            # markers case — not a documented user-facing command.
            if len(argv) == 2 and argv[1] == "adversarial-markers":
                report = _run_adversarial_markers_case()
                code = 0
                render_kwargs = {"allow_nan": False}
            else:
                raise fc.FillError(E_USAGE, f"run-case: no case named {argv[1:]!r}")
        else:
            args = parser.parse_args(argv)
            if args.selftest:
                if args.command is not None:
                    parser.error("--selftest cannot be combined with a command")
                return run_selftest()
            if args.command != "observe":
                parser.error("the 'observe' command or --selftest is required")
            report = run_observe(
                args.repo, args.contract, args.now, src_glob=_split_csv(args.src_glob),
                test_glob=_split_csv(args.test_glob), spec_glob=_split_csv(args.spec_glob),
                manifest=_split_csv(args.manifest))
            code = 0
            render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except fc.FillError as exc:
        code = exc.code
        report = _error_report(code, str(exc))
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    except Exception as exc:
        code = E_INPUT
        report = _error_report(code, f"input/runtime failure ({type(exc).__name__}: {exc})")
        render_kwargs = {"indent": 2, "sort_keys": True, "allow_nan": False}
    fc.write_report(fc.emit_report(report, **render_kwargs))
    return code



def main(argv=None) -> int:
    return fc.run_cli(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
