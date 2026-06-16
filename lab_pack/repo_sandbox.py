"""Bounded repo-execution sandbox — PLUMBING (ADR-035 / ADR-022 rung 2).

The isolation foundation under the code_worker (ADR-035, Phase 2) and the
submit_pr decision (ADR-035, Phase 3): clone an ALLOWLISTED repo, run a
specified command (tests/build/eval) against it, optionally apply a proposed
diff and re-run to PROVE the fix, and capture stdout/stderr/exit/timing as
evidence.

Isolation, by construction:

  * CLEAN ENVIRONMENT — the sandbox subprocess never inherits a secret. Its
    env is built from scratch from a curated allowlist (PATH, HOME, locale,
    CA bundles — what git/pip need and nothing more); the lab's secrets
    (ANTHROPIC_API_KEY, LAB_OPERATOR_TOKEN, LAB_MCP_TOKEN, GITHUB_TOKEN,
    GITHUB_WRITE_TOKEN, DATABASE_URL / LAB_DATABASE_URL, …) are NEVER copied
    in, and a defensive assertion refuses to launch if one leaked into the
    allowlist. This is the property the Phase-1 sentinel test gates on.
  * WALL-CLOCK + RESOURCE LIMITS — every run is bounded by a wall-clock
    timeout (killed on expiry) and, on POSIX, CPU-time and address-space
    rlimits set in a preexec hook (best-effort; a platform that rejects them
    still runs under the wall-clock bound).
  * ALLOWLISTED REPOS ONLY — the repo is checked against
    GITHUB_REPO_ALLOWLIST (the same allowlist github_read enforces) BEFORE
    any network or filesystem I/O. A repo outside it is refused.
  * CAPTURED OUTPUT IS EVIDENCE — stdout, stderr (truncated to a cap),
    exit code, duration, and timed-out flag come back as a structured
    result the code_worker writes as attributed evidence.

Subprocess-hardened, deliberately. This runs the LAB'S OWN allowlisted repos
only — not arbitrary untrusted code — so a subprocess with a clean env,
wall-clock + rlimit bounds, and an allowlist is the right amount of isolation
for now. The future upgrade for running UNTRUSTED code is a hosted micro-VM
sandbox (E2B): a separate kernel and network namespace per run. That is NOT a
dependency this session takes; this module is the subprocess floor it would
sit on, and the public API (`run_repo_task`) would be the seam an E2B backend
slots behind.

Network: a subprocess shares the host network namespace, so this floor cannot
*enforce* "network only for clone + package install" — that enforcement is
exactly what the E2B upgrade buys. The allowlist (own repos only) plus the
clean env (no credentials to exfiltrate) are the mitigations that make the
subprocess floor acceptable until then.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Optional

from .github_read import _check_repo

# Wall-clock + resource bounds (generous defaults; the code_worker passes
# seam-tunable values from settings.sandbox_timeout_seconds).
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MEM_LIMIT_MB = 2048
_MAX_CAPTURE_CHARS = 20_000  # per stream, per run — evidence, not a transcript

# Secrets that must NEVER reach the sandbox env. The lab_server keeps the
# canonical list (server.lab_server._SECRET_ENV_KEYS); this is the sandbox's
# own copy so the module is import-safe with no server dependency. GitHub's
# read AND write tokens are both here — a sandboxed test run has no business
# touching either.
SECRET_ENV_KEYS = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "LAB_OPERATOR_TOKEN", "LAB_MCP_TOKEN",
    "GITHUB_TOKEN", "GITHUB_WRITE_TOKEN",
    "DATABASE_URL", "LAB_DATABASE_URL",
})

# The ONLY env vars the sandbox subprocess inherits — what git/pip/python need
# to function, and nothing that could carry a credential. Anything not here is
# absent from the child env. (No *_TOKEN, *_KEY, *_SECRET, *_URL, *_PASSWORD.)
_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO", "CURL_CA_BUNDLE",
    "SYSTEMROOT", "COMSPEC",  # Windows shells need these
)

# Fixture/sentinel seam: a clone hook (repo, ref, dest_dir) -> None|err that
# populates dest_dir WITHOUT touching the network, injected in place of the
# live `git clone`. The RUN step is never stubbed — the sentinel test must
# exercise the REAL subprocess env to prove no secret reaches it.
_CLONE_HOOK: dict[str, Optional[Callable]] = {"fn": None}


def set_clone_hook(fn: Optional[Callable]) -> None:
    _CLONE_HOOK["fn"] = fn


def clean_env() -> dict[str, str]:
    """Build the sandbox subprocess env from scratch: only the curated
    allowlist, never a wholesale copy of os.environ. Refuses to return an env
    carrying any known secret key (defense in depth — a leaked allowlist entry
    fails loud, it does not pass a credential to a test runner)."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", os.defpath)
    # Belt and suspenders: a secret key must never survive into the child env.
    leaked = SECRET_ENV_KEYS & set(env)
    if leaked:
        raise RuntimeError(
            f"sandbox env build leaked secret key(s) {sorted(leaked)} — "
            "refusing to launch the sandbox")
    # Keep the runner deterministic and quiet.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never block on a credential prompt
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    return env


def _rlimit_preexec(mem_limit_mb: int, timeout_seconds: int):
    """POSIX preexec hook: CPU-time and address-space rlimits. Best-effort —
    a platform that rejects a limit still runs under the wall-clock bound."""
    if os.name != "posix":
        return None

    def _apply():  # pragma: no cover - exercised in subprocess, not coverage
        try:
            import resource
            # CPU seconds: a little over the wall clock so the wall-clock kill
            # is the primary bound and the CPU limit is a runaway backstop.
            cpu = int(timeout_seconds) + 5
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        except Exception:
            pass
        try:
            import resource
            cap = int(mem_limit_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        except Exception:
            pass
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core dumps
        except Exception:
            pass

    return _apply


def _truncate(text: Optional[str]) -> str:
    text = text or ""
    if len(text) <= _MAX_CAPTURE_CHARS:
        return text
    return text[:_MAX_CAPTURE_CHARS] + f"\n…[truncated, {len(text)} chars total]"


def _exec(command: str, cwd: str, timeout_seconds: int,
          mem_limit_mb: int) -> dict[str, Any]:
    """Run one command in the sandbox: clean env, wall-clock + rlimit bounds,
    output captured. Never raises — a timeout or crash comes back as a
    structured result."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command] if os.name == "posix" else command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=clean_env(),
            shell=(os.name != "posix"),
            preexec_fn=_rlimit_preexec(mem_limit_mb, timeout_seconds)
            if os.name == "posix" else None,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": None,
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": _truncate(exc.stdout.decode("utf-8", "replace")
                                if isinstance(exc.stdout, bytes) else exc.stdout),
            "stderr": _truncate(exc.stderr.decode("utf-8", "replace")
                                if isinstance(exc.stderr, bytes) else exc.stderr),
            "error": f"wall-clock timeout after {timeout_seconds}s — killed",
        }
    except Exception as exc:
        return {
            "command": command,
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": "", "stderr": "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "command": command,
        "exit_code": proc.returncode,
        "timed_out": False,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": _truncate(proc.stdout),
        "stderr": _truncate(proc.stderr),
        "error": None,
    }


def _live_clone(repo: str, ref: Optional[str], dest: str) -> Optional[str]:
    """git clone an allowlisted repo (shallow). Returns an error string or
    None. Network only for the clone — the host network namespace is shared,
    so this is bounded by the allowlist + clean env, not by isolation (the
    E2B upgrade adds the namespace)."""
    url = f"https://github.com/{repo}.git"
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", str(ref)]
    cmd += [url, dest]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                              env=clean_env())
    except subprocess.TimeoutExpired:
        return "git clone timed out after 180s"
    except Exception as exc:
        return f"git clone failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        # stderr may name the repo URL but never a credential (clean env).
        tail = (proc.stderr or "clone failed").strip().splitlines()
        return f"git clone exited {proc.returncode}: {tail[-1][:200] if tail else ''}"
    return None


# A diff's target paths come from its `+++ b/<path>` hunks (the post-image
# side). The submit_pr write path commits CONCRETE file contents, not a diff
# (a diff cannot be applied server-side — propose_submit_pr_fn), so after a
# fix-task proves its diff in the sandbox the resulting file states are read
# back from these paths and ride into the PR. /dev/null targets (a pure
# deletion) carry no post-image content and are skipped.
_DIFF_TARGET_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)", re.M)


def _diff_target_paths(diff: str) -> list[str]:
    out: list[str] = []
    for path in _DIFF_TARGET_RE.findall(diff or ""):
        path = path.strip()
        if path and path != "/dev/null" and path not in out:
            out.append(path)
    return out


# Documentation/notes the suite never exercises. A self-repair fix that changes
# ONLY these — no source, no authored red-then-green test — proves nothing: the
# suite is green regardless of it (branch#1796 authored
# NOTE_TO_LAB_missing_source.md and nothing else, yet the proof reported green).
# Markdown/rst/txt and extensionless note files (NOTE_TO_LAB, README) are docs;
# everything with a real code/config extension counts as source.
_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".txt", ""})


def _is_source_path(path: str) -> bool:
    """A SOURCE/code/config file (.py, .yaml, .json, .toml, .sh, …) the suite
    can actually exercise — NOT pure documentation. Markdown/rst/txt and
    extensionless note files are documentation, never a proof of a code fix."""
    base = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1].lower()
    return bool(ext) and ext not in _DOC_EXTENSIONS


def _diff_changes_source(diff: str) -> bool:
    """Does the diff change at least one SOURCE file — not only docs/notes? The
    no-authored-test proof path (a source-only fix proven by a green suite) is
    valid ONLY for a real source change; a NOTE/markdown-only diff is green
    regardless and so proves nothing (branch#1796 — authoring_unproven by
    definition)."""
    return any(_is_source_path(p) for p in _diff_target_paths(diff))


def _read_changed_files(repo_dir: str, diff: str) -> dict[str, str]:
    """The post-fix contents of the files a proven diff touched, read from the
    cloned-and-patched working tree — the concrete states the PR commits.
    Bounded by the same per-file char cap as a captured stream; unreadable
    targets (a deletion, a binary) are simply omitted."""
    files: dict[str, str] = {}
    for path in _diff_target_paths(diff):
        # Stay inside the repo dir — never follow a traversal in a path.
        full = os.path.normpath(os.path.join(repo_dir, path))
        if not full.startswith(os.path.normpath(repo_dir) + os.sep):
            continue
        try:
            with open(full, "r", encoding="utf-8") as f:
                files[path] = _truncate(f.read())
        except (OSError, UnicodeDecodeError):
            continue
    return files


# ── deterministic patch construction (ADR-038) ──────────────────────────────
# The code_author step (ADR-037) emits the FULL new contents of each changed
# file, NOT a unified diff — the standard LLM diff-authoring failure is a
# hand-computed `@@` hunk header that `git apply` rejects (branch#1667,
# observation#1675/#1680). The lab builds the patch HERE, deterministically:
# read the cloned original, diff it against the model's new content with
# difflib, and `git apply` the result. The model supplies intent; the tooling
# supplies correct patch mechanics, so every authored patch applies by
# construction.


def _file_unified_diff(path: str, original: str, new: str) -> str:
    """A single file's unified diff (git-apply format) from its original and
    new contents, computed by difflib — never hand-written. Returns "" when the
    content is unchanged. A new file (original "") emits a `--- /dev/null` /
    `+++ b/<path>` fragment; an existing file emits `--- a/<path>` /
    `+++ b/<path>`. Lines are normalized to end with a newline so the last hunk
    line never runs into the next header (the classic difflib pitfall)."""
    if original == new:
        return ""
    a = original.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    if a and not a[-1].endswith("\n"):
        a[-1] += "\n"
    if b and not b[-1].endswith("\n"):
        b[-1] += "\n"
    fromfile = f"a/{path}" if original else "/dev/null"
    tofile = f"b/{path}"
    return "".join(difflib.unified_diff(a, b, fromfile=fromfile, tofile=tofile))


def build_diff_from_new_files(repo_dir: str,
                              new_files: dict[str, str]) -> tuple[str, Optional[str]]:
    """Construct one combined unified diff from authored full-file contents,
    diffing each against its cloned original (ADR-038). Returns
    (diff, error): a path escaping the repo dir is refused (error set); an
    authored file identical to the original contributes no fragment. The diff
    is git-apply-valid by construction — the caller applies it and the RUN
    proves it."""
    root = os.path.normpath(repo_dir)
    parts: list[str] = []
    for path, content in new_files.items():
        rel = (path or "").replace(os.sep, "/").lstrip("./")
        full = os.path.normpath(os.path.join(repo_dir, rel))
        if not (full == root or full.startswith(root + os.sep)):
            return "", f"refusing authored path outside the repo: {path}"
        try:
            with open(full, "r", encoding="utf-8") as f:
                original = f.read()
        except (OSError, UnicodeDecodeError):
            original = ""  # a new file the fix creates
        fragment = _file_unified_diff(rel, original, content or "")
        if fragment:
            parts.append(fragment)
    return "".join(parts), None


# ── search/replace materialization (ADR-042) ────────────────────────────────
# The code_author step (ADR-037/038) used to emit the FULL new contents of each
# changed file. For a large file that is unbounded output: branch#1837 hit the
# model's max_tokens emitting all 26k chars of research_worker.py to fix ten
# lines, the JSON truncated, and authoring produced "no usable output." ADR-042
# bounds the output to the CHANGE: the model emits search/replace edits for an
# existing file (and full content only for a NEW file). The lab MATERIALIZES the
# new file contents here — apply each edit to the cloned original — then the
# EXISTING deterministic-patch path (build_diff_from_new_files → difflib → git
# apply) and the red-then-green proof are unchanged. The model supplies intent;
# the tooling still owns patch mechanics.


def apply_search_replace(original: str, edits: list[dict],
                         path: str = "") -> tuple[str, Optional[str]]:
    """Apply ordered search/replace edits to `original`. Each edit's `search`
    block must appear EXACTLY ONCE in the progressively-edited text (an
    ambiguous or missing block is an error fed back to the authoring retry, not
    a silent mis-edit). Returns (new_text, error)."""
    text = original
    for i, ed in enumerate(edits or []):
        search = ed.get("search") or ""
        replace = ed.get("replace") or ""
        if not search:
            return "", (f"edit {i + 1} for {path}: empty search block — create "
                        "a new file with `content`, not an empty-search edit")
        count = text.count(search)
        if count == 0:
            return "", (f"edit {i + 1} for {path}: search block not found "
                        f"(it must match the file verbatim): {search[:160]!r}")
        if count > 1:
            return "", (f"edit {i + 1} for {path}: search block is ambiguous "
                        f"({count} matches) — include more surrounding context "
                        f"so it is unique: {search[:160]!r}")
        text = text.replace(search, replace, 1)
    return text, None


def materialize_authored_files(repo_dir: str,
                               authored: list[dict]) -> tuple[dict[str, str],
                                                              Optional[str]]:
    """Turn the model's authored files (ADR-042) into {path: full new content}
    against the cloned originals, ready for build_diff_from_new_files. Each
    entry is {path, edits?, content?}: `edits` (search/replace) apply to the
    cloned original (which must exist); `content` is used verbatim (a new file,
    or a whole-file rewrite when no edits are given). `edits` wins when both are
    present. Returns (new_files, error) — a path escaping the repo, a missing
    original for an edit, or a non-matching search block is an error."""
    root = os.path.normpath(repo_dir)
    new_files: dict[str, str] = {}
    for entry in authored or []:
        raw_path = entry.get("path")
        if not raw_path:
            continue
        rel = (raw_path or "").replace(os.sep, "/").lstrip("./")
        full = os.path.normpath(os.path.join(repo_dir, rel))
        if not (full == root or full.startswith(root + os.sep)):
            return {}, f"refusing authored path outside the repo: {raw_path}"
        edits = entry.get("edits") or []
        content = entry.get("content")
        if edits:
            try:
                with open(full, "r", encoding="utf-8") as f:
                    original = f.read()
            except (OSError, UnicodeDecodeError):
                return {}, (f"cannot edit {rel}: the file is not in the clone "
                            "(use `content` to create a new file)")
            new_text, err = apply_search_replace(original, edits, rel)
            if err:
                return {}, err
            new_files[rel] = new_text
        elif content is not None:
            new_files[rel] = content
        else:
            return {}, (f"authored file {rel} carries neither edits nor "
                        "content — nothing to apply")
    return new_files, None


def _apply_diff(repo_dir: str, diff: str) -> Optional[str]:
    """git apply a unified diff inside the cloned repo. Returns an error
    string or None."""
    patch_path = os.path.join(repo_dir, ".lab_proposed.patch")
    try:
        with open(patch_path, "w") as f:
            f.write(diff if diff.endswith("\n") else diff + "\n")
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", patch_path],
            cwd=repo_dir, capture_output=True, text=True, timeout=60,
            env=clean_env())
    except Exception as exc:
        return f"git apply failed: {type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(patch_path)
        except OSError:
            pass
    if proc.returncode != 0:
        tail = (proc.stderr or "apply failed").strip().splitlines()
        return f"git apply exited {proc.returncode}: {tail[-1][:200] if tail else ''}"
    return None


# ── proof integrity: red-then-green an authored regression test (ADR-039/040) ─
# The self-repair proof command is the lab's OWN harness
# (`python -m lab_pack.fixtures.run_fixtures`), which does NOT run pytest files
# under lab_pack/tests/ but DOES run fixture .yaml scenarios. Two false-greens
# followed: branch#1704 added a pytest test the proof never ran; branch#1751
# added a fixture .yaml the proof DID run but that passed VACUOUSLY against the
# unfixed code (36/36 — yet the source fix was never made). The integrity rule
# (ADR-039, hardened by ADR-040): when an authored change adds or modifies a
# test — pytest file OR fixture .yaml — the proof is RED-THEN-GREEN. The test
# must FAIL against the unfixed baseline (red) and PASS after the fix (green),
# with the full suite green after. A test green on the baseline proves nothing,
# whether it is unexecuted, vacuous, or simply does not exercise the defect.


def _pytest_command(test_paths: list[str]) -> str:
    """The command that runs the authored regression test(s) directly. `python
    -m pytest` (not bare `pytest`) so the repo root (cwd) is on sys.path — a
    test importing a top-level module of the cloned repo resolves. The cache
    provider is disabled so no .pytest_cache is written into the clone."""
    joined = " ".join(shlex.quote(p) for p in test_paths)
    return f"python -m pytest {joined} -p no:cacheprovider -q"


def _regression_executed_and_passed(run: dict) -> bool:
    """Was the authored regression test ACTUALLY executed and green? pytest
    exits 0 only when ≥1 test was collected and all passed; it exits 5 when no
    test was collected (the unexecuted-test case) and nonzero on failure. The
    'no tests ran' string is a belt-and-suspenders guard against an all-skipped
    or all-deselected run that still exits 0."""
    if not run:
        return False
    if run.get("exit_code") != 0:
        return False
    out = ((run.get("stdout") or "") + "\n" + (run.get("stderr") or "")).lower()
    if "no tests ran" in out or "no tests collected" in out:
        return False
    return True


def _regression_is_red(run: dict) -> bool:
    """Did the authored test RUN and FAIL — a genuine RED against the unfixed
    baseline (ADR-040)? Distinguished from two non-reds: a test that PASSES
    (exit 0) proves nothing (branch#1751's vacuous fixture — green regardless
    of the fix), and a test that never RAN ('no tests collected' / pytest exit
    5) is an absent test, not a caught defect. Only a run that executed and
    reported a failure is a real red."""
    if not run or run.get("exit_code") in (0, None):
        return False
    if run.get("exit_code") == 5:  # pytest: nothing collected
        return False
    out = ((run.get("stdout") or "") + "\n" + (run.get("stderr") or "")).lower()
    if "no tests ran" in out or "no tests collected" in out:
        return False
    return True


# ── authored-test detection: BOTH forms (ADR-040) ───────────────────────────
# The red-then-green proof covers any authored test, in either form the loop
# can write: a pytest file the proof command would NOT run on its own, AND a
# fixture .yaml the suite runner DOES run (which is exactly why a vacuous one
# slipped through green — branch#1751). Both are subject to red-then-green.


def is_pytest_test_path(path: str) -> bool:
    """A pytest file: a `.py` named `test_*`/`*_test`, or any `.py` under a
    tests/ or test/ directory. conftest.py is fixture wiring, not a test."""
    p = (path or "").replace("\\", "/").lower()
    if not p.endswith(".py"):
        return False
    base = p.rsplit("/", 1)[-1]
    if base == "conftest.py":
        return False
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    return any(seg in ("tests", "test") for seg in p.split("/")[:-1])


def is_fixture_test_path(path: str) -> bool:
    """A fixture test: a `.yaml`/`.yml` scenario under a fixtures/ directory —
    the form branch#1751 authored (research_worker_source_order.yaml) that the
    suite runner executes and that passed VACUOUSLY against the unfixed code."""
    p = (path or "").replace("\\", "/").lower()
    if not (p.endswith(".yaml") or p.endswith(".yml")):
        return False
    return any(seg in ("fixtures", "fixture") for seg in p.split("/")[:-1])


def is_authored_test_path(path: str) -> bool:
    """Either authored-test form — a pytest file OR a fixture .yaml."""
    return is_pytest_test_path(path) or is_fixture_test_path(path)


# ── split a combined unified diff into per-file fragments ────────────────────
# To run the authored test against the UNFIXED baseline (the RED phase) we apply
# ONLY the test-file fragments of the diff — never the source fix. A unified
# diff is a concatenation of per-file fragments, each opening with a `--- ` line.


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """[(target_path, fragment), …] — split a combined unified diff at each
    per-file `--- ` boundary; the target is the fragment's `+++ b/<path>`."""
    text = diff or ""
    starts = [m.start() for m in re.finditer(r"(?m)^--- ", text)]
    out: list[tuple[str, str]] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        frag = text[s:e]
        tm = _DIFF_TARGET_RE.search(frag)
        out.append(((tm.group(1).strip() if tm else ""), frag))
    return out


def _norm_rel(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./")


def _filter_diff_to_paths(diff: str, paths: list[str]) -> str:
    """The sub-diff touching ONLY the given paths (the authored test files) —
    applied to a pristine clone for the RED phase."""
    want = {_norm_rel(p) for p in (paths or [])}
    return "".join(frag for target, frag in _split_diff_by_file(diff)
                   if _norm_rel(target) in want)


def _regression_commands(test_paths: list[str], proof_command: str) -> list[str]:
    """The command(s) that exercise the authored test(s) directly, by form: a
    pytest run over the pytest files, and — for a fixture .yaml — the PROOF
    command itself (the suite runner is what executes a fixture). Either form's
    command must go RED on the unfixed baseline and GREEN after the fix."""
    pytest_paths = [p for p in (test_paths or []) if is_pytest_test_path(p)]
    fixture_paths = [p for p in (test_paths or []) if is_fixture_test_path(p)]
    cmds: list[str] = []
    if pytest_paths:
        cmds.append(_pytest_command(pytest_paths))
    if fixture_paths and proof_command not in cmds:
        cmds.append(proof_command)
    return cmds


def _run_regression(repo_dir: str, reg_commands: list[str], proof_command: str,
                    after_run: Optional[dict], timeout_seconds: int,
                    mem_limit_mb: int) -> list[dict]:
    """Run each regression command, reusing an already-executed proof-command
    run (the fixture form: the suite IS the regression, no second run needed)."""
    runs: list[dict] = []
    for cmd in reg_commands:
        if after_run is not None and cmd == proof_command:
            runs.append(after_run)
        else:
            runs.append(_exec(cmd, repo_dir, timeout_seconds, mem_limit_mb))
    return runs


def _red_baseline_phase(clone_fn, repo: str, ref: Optional[str],
                        test_only_diff: str, reg_commands: list[str],
                        proof_command: str, timeout_seconds: int,
                        mem_limit_mb: int) -> tuple[bool, list[dict], Optional[str]]:
    """RED phase (ADR-040): clone a PRISTINE copy, apply ONLY the authored test
    files (no source fix), and run the regression command(s). They MUST FAIL
    (red) — a test green here, or one that never runs, is no proof. Returns
    (baseline_red, runs, error). A separate clone (not a git reset) keeps this
    working against the canned fixture clones, which are not git repos."""
    if not (test_only_diff or "").strip():
        return False, [], ("no authored test fragment to run against the "
                           "unfixed baseline")
    workdir = tempfile.mkdtemp(prefix="lab-sandbox-red-")
    repo_dir = os.path.join(workdir, "repo")
    try:
        clone_err = clone_fn(repo, ref, repo_dir)
        if clone_err:
            return False, [], clone_err
        if not os.path.isdir(repo_dir):
            return False, [], "clone produced no repo directory"
        apply_err = _apply_diff(repo_dir, test_only_diff)
        if apply_err:
            return False, [], f"applying the test-only change failed: {apply_err}"
        runs = _run_regression(repo_dir, reg_commands, proof_command, None,
                               timeout_seconds, mem_limit_mb)
        baseline_red = bool(runs) and all(_regression_is_red(r) for r in runs)
        return baseline_red, runs, None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_repo_task(
    repo: str,
    command: str,
    *,
    ref: Optional[str] = None,
    diff: Optional[str] = None,
    new_files: Optional[dict[str, str]] = None,
    authored_files: Optional[list[dict]] = None,
    test_paths: Optional[list[str]] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    mem_limit_mb: int = DEFAULT_MEM_LIMIT_MB,
) -> dict[str, Any]:
    """Clone an allowlisted repo, run `command`, and (for a fix-task) apply a
    patch and re-run to prove it. The patch comes one of three ways:

      * `diff`           — a candidate unified diff supplied with the task
                           (ADR-036).
      * `authored_files` — the code_author step's search/replace output
                           (ADR-042): [{path, edits?, content?}]. The lab
                           MATERIALIZES the new file contents against the cloned
                           originals (apply each edit), then builds the patch.
      * `new_files`      — {path: full new content} (ADR-038, still accepted);
                           the lab builds the patch DETERMINISTICALLY (difflib
                           over the cloned original) so no hand-computed hunk
                           header can be wrong.

    Precedence when several are given: `new_files` (already materialized) wins,
    then `authored_files`, then `diff`.

    `test_paths` (ADR-039/040): repo-relative paths of authored regression
    tests — pytest files the proof command does NOT itself run, AND/OR fixture
    .yaml scenarios the suite runner DOES run. The proof is RED-THEN-GREEN: the
    authored test must FAIL against the unfixed baseline (red) and PASS after
    the fix (green), with the full suite green after. A test green on the
    baseline proves nothing (branch#1751); a test that never runs is no proof
    (branch#1704). When `test_paths` is empty the authored-test files are
    derived from the diff's own targets, so a supplied diff that adds a test is
    held to the same standard.

    Returns a structured result:

        {
          "repo", "ref", "command",
          "baseline":   {command, exit_code, timed_out, duration_seconds,
                         stdout, stderr, error},
          "after_diff": {...} | None,     # present only for a fix-task
          "regression": {...} | None,     # the authored test, AFTER the fix
          "regression_baseline": {...} | None,  # the SAME test on the unfixed
                         # baseline — it MUST be red (ADR-040)
          "baseline_red": bool,           # the authored test failed on the
                         # unfixed baseline (a real red-then-green regression)
          "regression_paths": [str] | None,
          "diff":       str | None,       # the patch actually applied — the
                         # git-apply-valid one BUILT from new_files, or the
                         # supplied diff; this is what an approved PR carries
          "proven":     bool,             # the deciding run(s) passed: the proof
                         # command exited 0 AND every authored test executed+green
          "changed_files": {path: content} | None,  # a PROVEN fix-task only:
                         # the post-fix file states the PR commits (ADR-036)
          "error":      str | None,       # refusal / clone / apply failure
        }

    Never raises — refusals (non-allowlisted repo), clone failures, apply
    failures, timeouts, and crashes all come back in the result. The repo
    allowlist is checked BEFORE any I/O.
    """
    base_result: dict[str, Any] = {
        "repo": repo, "ref": ref, "command": command,
        "baseline": None, "after_diff": None, "regression": None,
        "regression_paths": None, "diff": None, "proven": False,
        "changed_files": None, "error": None,
    }
    allow_err = _check_repo(repo)
    if allow_err:
        base_result["error"] = allow_err
        return base_result
    if not (command or "").strip():
        base_result["error"] = "no command specified for the sandbox run"
        return base_result

    workdir = tempfile.mkdtemp(prefix="lab-sandbox-")
    repo_dir = os.path.join(workdir, "repo")
    try:
        clone_fn = _CLONE_HOOK["fn"] or _live_clone
        clone_err = clone_fn(repo, ref, repo_dir)
        if clone_err:
            base_result["error"] = clone_err
            return base_result
        if not os.path.isdir(repo_dir):
            base_result["error"] = "clone produced no repo directory"
            return base_result

        baseline = _exec(command, repo_dir, timeout_seconds, mem_limit_mb)
        base_result["baseline"] = baseline

        # ADR-042: materialize the model's search/replace edits into full new
        # file contents against the cloned originals, then fall into the SAME
        # deterministic-patch path the full-content case (ADR-038) uses.
        if authored_files and not new_files and not diff:
            new_files, mat_err = materialize_authored_files(repo_dir,
                                                            authored_files)
            if mat_err:
                base_result["error"] = mat_err
                return base_result

        # ADR-038: build the patch deterministically from authored full-file
        # contents (diffing the cloned original) — never trust a model-written
        # hunk header. The git-generated patch then flows through the SAME
        # apply-and-prove path the supplied-diff case uses.
        if new_files and not diff:
            diff, build_err = build_diff_from_new_files(repo_dir, new_files)
            if build_err:
                base_result["error"] = build_err
                return base_result
            if not diff:
                base_result["error"] = (
                    "authored content matches the repo — no change to prove")
                return base_result
        base_result["diff"] = diff or None

        if diff:
            apply_err = _apply_diff(repo_dir, diff)
            if apply_err:
                base_result["error"] = apply_err
                base_result["proven"] = False
                return base_result
            after = _exec(command, repo_dir, timeout_seconds, mem_limit_mb)
            base_result["after_diff"] = after
            green_suite = (after.get("exit_code") == 0)

            # The authored test(s) the proof must red-then-green. Either the
            # caller named them (the authoring path) or we derive them from the
            # diff's targets (a supplied diff that adds a test) — both forms,
            # pytest AND fixture .yaml.
            tests = [_norm_rel(p) for p in (test_paths or [])] or [
                _norm_rel(t) for t in _diff_target_paths(diff)
                if is_authored_test_path(t)]

            if tests:
                base_result["regression_paths"] = list(tests)
                reg_commands = _regression_commands(tests, command)
                # ── GREEN phase: the authored test(s) must PASS after the fix.
                # (Reuse the proof-command run for the fixture form.) ──────────
                green_runs = _run_regression(repo_dir, reg_commands, command,
                                             after, timeout_seconds, mem_limit_mb)
                base_result["regression"] = green_runs[0] if green_runs else None
                green_test = bool(green_runs) and all(
                    _regression_executed_and_passed(r) for r in green_runs)
                # ── RED phase: the SAME test(s) on a PRISTINE clone with NO
                # source fix MUST FAIL (ADR-040). A test green on the unfixed
                # baseline proves nothing (branch#1751); a test that never runs
                # is not a red (branch#1704). ─────────────────────────────────
                test_only_diff = _filter_diff_to_paths(diff, tests)
                baseline_red, red_runs, red_err = _red_baseline_phase(
                    clone_fn, repo, ref, test_only_diff, reg_commands, command,
                    timeout_seconds, mem_limit_mb)
                base_result["regression_baseline"] = (
                    red_runs[0] if red_runs else None)
                base_result["baseline_red"] = baseline_red
                if red_err:
                    base_result["regression_baseline_error"] = red_err
                # proven = red-on-baseline AND green-after AND full suite green.
                proven = green_suite and green_test and baseline_red
            else:
                # A source-only fix (no authored test): the existing suite must
                # be green AND the diff must change real SOURCE. A diff that
                # touches only docs/notes (no source change, no red-then-green
                # test) proves nothing — the suite is green regardless of it
                # (branch#1796's NOTE_TO_LAB no-op: proven=true while
                # baseline_red was never even computed). (A DEFECT brief that
                # authors no regression test therefore cannot prove
                # green-without-red — it has no test to go red, and a doc-only
                # change has no source to exercise.)
                proven = green_suite and _diff_changes_source(diff)
            base_result["proven"] = proven
            if proven:
                # The fix is proven — read back the patched file states so the
                # PR can commit concrete contents (ADR-036). Only on success:
                # an unproven diff is not a fix to land.
                base_result["changed_files"] = _read_changed_files(repo_dir, diff)
        else:
            base_result["proven"] = (baseline.get("exit_code") == 0)
        return base_result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Files the authoring-context walk never reads (noise, binaries, or huge): the
# diff author reasons over source, not build artifacts or VCS internals.
_CONTEXT_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "data",
})
_CONTEXT_TEXT_EXT = frozenset({
    ".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".json",
    ".js", ".mjs", ".ts", ".tsx", ".sh", ".html", ".css", ".rst",
})


def _resolve_hint_paths(hint_paths: list[str], rel_paths: list[str]) -> list[str]:
    """Resolve each hint (a path the brief/intent named) to the ACTUAL
    repo-relative path(s) in the cloned tree.

    A brief names a file the way a human does — usually the BARE basename
    ('research_worker.py'), not the full tree path ('lab_pack/research_worker.py').
    An exact tree match wins; otherwise a bare basename matches any tree path
    with that basename, and a partial path matches any tree path it is a suffix
    of. This is the branch#1796 fix: the target file the brief named never
    reached the authoring context because the bare name did not equal the full
    tree path, so it fell through to the budget-bounded context fill — where it
    was truncated or dropped entirely, and the worker 'could not see the file it
    was told to fix' (it wrote NOTE_TO_LAB_missing_source.md instead). Resolved
    hints are read in FULL, so the named target reaches the model uncut."""
    rel_set = set(rel_paths)
    resolved: list[str] = []

    def _add(p: str) -> None:
        if p not in resolved:
            resolved.append(p)

    for raw in (hint_paths or []):
        h = _norm_rel(raw)
        if not h:
            continue
        if h in rel_set:
            _add(h)
            continue
        if "/" in h:
            matches = [p for p in rel_paths if p == h or p.endswith("/" + h)]
        else:
            matches = [p for p in rel_paths if p.rsplit("/", 1)[-1] == h]
        # A precise hint resolves to one or a few files; an over-broad basename
        # (e.g. __init__.py across many packages) is not worth flooding the
        # full-read context with — cap it and let the budget fill cover the rest.
        if matches and len(matches) <= 4:
            for p in matches:
                _add(p)
    return resolved


def clone_and_read(
    repo: str,
    *,
    ref: Optional[str] = None,
    hint_paths: Optional[list[str]] = None,
    max_files: int = 24,
    max_total_chars: int = 40_000,
) -> dict[str, Any]:
    """Clone an ALLOWLISTED repo and read a bounded set of its files — the
    relevant-file context the diff-authoring step (ADR-037) reasons over.

        {"repo", "ref", "tree": [path, …], "files": {path: content},
         "error": str | None}

    No command is run: the only subprocess is the clean-env `git clone` (the
    same isolation the rest of the sandbox uses), so this consumes no run
    budget and exercises no command env. The allowlist is checked BEFORE any
    I/O. `hint_paths` (e.g. files the brief names) are read first; the rest of
    the tree fills the remaining budget, smallest text source first. Never
    raises — refusals and clone failures come back in `error`.
    """
    out: dict[str, Any] = {"repo": repo, "ref": ref, "tree": [], "files": {},
                           "error": None}
    allow_err = _check_repo(repo)
    if allow_err:
        out["error"] = allow_err
        return out

    workdir = tempfile.mkdtemp(prefix="lab-sandbox-read-")
    repo_dir = os.path.join(workdir, "repo")
    try:
        clone_fn = _CLONE_HOOK["fn"] or _live_clone
        clone_err = clone_fn(repo, ref, repo_dir)
        if clone_err:
            out["error"] = clone_err
            return out
        if not os.path.isdir(repo_dir):
            out["error"] = "clone produced no repo directory"
            return out

        root = os.path.normpath(repo_dir)
        rel_paths: list[str] = []
        for dirpath, dirnames, filenames in os.walk(repo_dir):
            dirnames[:] = [d for d in dirnames if d not in _CONTEXT_SKIP_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, repo_dir)
                rel_paths.append(rel.replace(os.sep, "/"))
        rel_paths.sort()
        out["tree"] = rel_paths[:500]

        # Hint (target) files first, read in FULL — PHASE 2 (ADR-039): the
        # authoring step EDITS these, so it must see each one ENTIRE; a
        # truncated target file is the branch#1704 failure ("the source was
        # truncated in the request" → the worker could not author the real fix
        # and fell back to a test-only no-op). Hint files are ALWAYS included
        # and never truncated; the budget below bounds only the extra context.
        # Resolve the hint (target) files the brief/intent named to their ACTUAL
        # tree paths — a brief usually names a bare basename
        # ('research_worker.py'), not the full path ('lab_pack/research_worker.py').
        # Without this the named target never matched the tree, fell through to
        # the budget-bounded context fill, and was truncated or dropped — the
        # worker could not see the file it was told to fix (branch#1796).
        hint_present = _resolve_hint_paths(hint_paths or [], rel_paths)
        hint_set = set(hint_present)

        total = 0
        for rel in hint_present:
            full = os.path.normpath(os.path.join(repo_dir, rel))
            if not full.startswith(root + os.sep):
                continue  # never follow a traversal out of the repo
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read()  # FULL — no per-file cap, no truncation
            except (OSError, UnicodeDecodeError):
                continue
            out["files"][rel] = content
            total += len(content)

        # Then fill the remaining budget with other text sources, smallest
        # first. These are CONTEXT, not the targets, so they stay bounded.
        rest = [p for p in rel_paths
                if p not in hint_set
                and os.path.splitext(p)[1].lower() in _CONTEXT_TEXT_EXT]

        def _size(p: str) -> int:
            try:
                return os.path.getsize(os.path.normpath(os.path.join(repo_dir, p)))
            except OSError:
                return 1 << 30
        rest.sort(key=_size)

        for rel in rest:
            if len(out["files"]) >= max_files or total >= max_total_chars:
                break
            full = os.path.normpath(os.path.join(repo_dir, rel))
            if not full.startswith(root + os.sep):
                continue  # never follow a traversal out of the repo
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read(max_total_chars)
            except (OSError, UnicodeDecodeError):
                continue
            out["files"][rel] = _truncate(content)
            total += len(content)
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def evidence_summary(result: dict[str, Any]) -> str:
    """A one-line, secret-free summary of a run result for an observation's
    text (the full streams ride in metadata)."""
    if result.get("error"):
        return (f"sandbox: {result['repo']} — could not run: "
                f"{result['error']}")
    parts = []
    base = result.get("baseline") or {}
    parts.append(f"command exit={base.get('exit_code')} "
                 f"({base.get('duration_seconds')}s"
                 f"{', TIMED OUT' if base.get('timed_out') else ''})")
    after = result.get("after_diff")
    if after is not None:
        parts.append(f"after diff exit={after.get('exit_code')} "
                     f"({after.get('duration_seconds')}s"
                     f"{', TIMED OUT' if after.get('timed_out') else ''})")
    reg = result.get("regression")
    if reg is not None:
        executed = _regression_executed_and_passed(reg)
        parts.append(f"authored test after fix exit={reg.get('exit_code')} "
                     f"({'executed+passed' if executed else 'NOT executed/passed'})")
    if result.get("regression_baseline") is not None or "baseline_red" in result:
        parts.append("authored test on unfixed baseline: "
                     + ("RED (fails → a real regression test)"
                        if result.get("baseline_red")
                        else "NOT red (passes/never runs → proves nothing)"))
    verdict = "PROVEN (red-then-green)" if result.get("proven") else "not proven"
    return (f"sandbox: {result['repo']} — "
            + "; ".join(parts) + f" → {verdict}")


if __name__ == "__main__":  # tiny manual smoke: run a command in this repo dir
    print(json.dumps(run_repo_task(
        sys.argv[1] if len(sys.argv) > 1 else "yoheinakajima/activegraph-lab",
        sys.argv[2] if len(sys.argv) > 2 else "echo hello"), indent=2,
        default=str))
