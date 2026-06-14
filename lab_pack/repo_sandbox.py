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

import json
import os
import re
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


def run_repo_task(
    repo: str,
    command: str,
    *,
    ref: Optional[str] = None,
    diff: Optional[str] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    mem_limit_mb: int = DEFAULT_MEM_LIMIT_MB,
) -> dict[str, Any]:
    """Clone an allowlisted repo, run `command`, and (for a fix-task) apply
    `diff` and re-run to prove it. Returns a structured result:

        {
          "repo", "ref", "command",
          "baseline":   {command, exit_code, timed_out, duration_seconds,
                         stdout, stderr, error},
          "after_diff": {...} | None,     # present only for a fix-task
          "proven":     bool,             # the run that decides success exited 0
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
        "baseline": None, "after_diff": None, "proven": False,
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

        if diff:
            apply_err = _apply_diff(repo_dir, diff)
            if apply_err:
                base_result["error"] = apply_err
                base_result["proven"] = False
                return base_result
            after = _exec(command, repo_dir, timeout_seconds, mem_limit_mb)
            base_result["after_diff"] = after
            base_result["proven"] = (after.get("exit_code") == 0)
            if base_result["proven"]:
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

        # Read hint files first, then the rest, smallest-readable first, until
        # the file-count or total-char budget is spent.
        ordered: list[str] = []
        hints = [h.replace(os.sep, "/").lstrip("./") for h in (hint_paths or [])]
        for h in hints:
            if h in rel_paths and h not in ordered:
                ordered.append(h)
        rest = [p for p in rel_paths
                if p not in ordered
                and os.path.splitext(p)[1].lower() in _CONTEXT_TEXT_EXT]

        def _size(p: str) -> int:
            try:
                return os.path.getsize(os.path.normpath(os.path.join(repo_dir, p)))
            except OSError:
                return 1 << 30
        rest.sort(key=_size)
        ordered += rest

        total = 0
        for rel in ordered:
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
    verdict = "PROVEN (green)" if result.get("proven") else "not proven"
    return (f"sandbox: {result['repo']} — "
            + "; ".join(parts) + f" → {verdict}")


if __name__ == "__main__":  # tiny manual smoke: run a command in this repo dir
    print(json.dumps(run_repo_task(
        sys.argv[1] if len(sys.argv) > 1 else "yoheinakajima/activegraph-lab",
        sys.argv[2] if len(sys.argv) > 2 else "echo hello"), indent=2,
        default=str))
