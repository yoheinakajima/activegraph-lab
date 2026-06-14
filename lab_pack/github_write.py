"""GitHub WRITE access — PLUMBING (ADR-035, rung 2 of ADR-022).

The doubly-gated write path: the lab NEVER writes to GitHub on its own. A code
change it wants to land becomes an artifact (the diff + the sandbox proof);
the lab opens a decision kind=submit_pr, status=pending; and ONLY on operator
approval (the first gate) is a write-scoped token exercised to push a branch
and open a PR — which the operator then reviews and merges on GitHub (the
second gate, in a different system). No auto-merge, ever.

Token discipline (sentinel-audited like every secret):
  * GITHUB_WRITE_TOKEN is a SEPARATE secret from the read-only GITHUB_TOKEN
    (ADR-022 rung 1) — read rate-limit access must not carry write scope.
    Fine-grained, scoped to the lab repo only, and ABSENT from the deployment
    until the operator configures it: absent → the lab can draft + sandbox +
    propose, and the operator opens the PR manually from the diff. Its
    presence is never required.
  * The token rides ONLY in a request Authorization header. It is NEVER in any
    event payload, observation, artifact, decision, log line, or error body —
    `_scrub` redacts it from any error text as defense in depth, and the
    public-safety sentinel audit greps the whole corpus for it.

Allowlist: the same GITHUB_REPO_ALLOWLIST github_read enforces, checked BEFORE
any network I/O. A repo outside it is refused — there is no path to write to a
repo the lab is not allowed to read.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .github_read import _check_repo

_API = "https://api.github.com"
_UA = ("Mozilla/5.0 (compatible; activegraph-lab/0.1; "
       "+https://github.com/yoheinakajima/activegraph-lab)")

# Fixture/sentinel seam: a PR opener (repo, *, head_branch, base, title, body,
# files) -> dict injected in place of the live API client. The mock never sees
# the token; the live opener never returns it.
_PR_OPENER: dict[str, Optional[Callable]] = {"fn": None}


def set_pr_opener(fn: Optional[Callable]) -> None:
    _PR_OPENER["fn"] = fn


def write_token_configured() -> bool:
    """Is a write token present? Absent is the normal posture (the operator
    opens the PR by hand from the diff); present unlocks the approved-decision
    write path."""
    return bool(os.environ.get("GITHUB_WRITE_TOKEN", "").strip())


def _scrub(text: str) -> str:
    """Defense in depth: the write token never enters payloads by construction;
    scrub it (and the read token) from any error text anyway."""
    for key in ("GITHUB_WRITE_TOKEN", "GITHUB_TOKEN"):
        tok = os.environ.get(key, "").strip()
        if tok and tok in text:
            text = text.replace(tok, f"<{key}>")
    return text


def _api(method: str, url: str, body: Optional[dict]) -> tuple[int, Any]:
    """One authenticated GitHub API call. The write token rides in the header
    only; it is never returned or logged."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    token = os.environ.get("GITHUB_WRITE_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(2_000_000).decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = (exc.read() or b"").decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"message": raw[:300]}
        return exc.code, parsed
    except Exception as exc:
        return 0, {"message": _scrub(f"{type(exc).__name__}: {exc}")}


def _live_open_pr(repo: str, *, head_branch: str, base: str, title: str,
                  body: str, files: dict[str, str]) -> dict:
    """Push a branch with the changed files and open a PR (live GitHub API).

    `files` maps repo-relative path → full new file content (the proposed
    change as concrete file states — the diff is for the human reviewer; the
    API commits contents). Exercised ONLY on operator approval AND with a
    write token present. Never raises — failures come back as {error}."""
    if not write_token_configured():
        return {"error": "GITHUB_WRITE_TOKEN is not configured — the lab "
                         "cannot open the PR; open it manually from the diff"}
    if not files:
        return {"error": "no file contents to commit (the submit_pr artifact "
                         "must carry the changed files' new contents)"}

    # 1) base ref → its commit sha
    st, ref = _api("GET", f"{_API}/repos/{repo}/git/ref/heads/{base}", None)
    if st != 200:
        return {"error": _scrub(f"could not read base ref '{base}': "
                                f"{(ref or {}).get('message', st)}")}
    base_sha = ((ref or {}).get("object") or {}).get("sha")
    if not base_sha:
        return {"error": f"base ref '{base}' carried no sha"}

    # 2) create the head branch at base
    st, mk = _api("POST", f"{_API}/repos/{repo}/git/refs",
                  {"ref": f"refs/heads/{head_branch}", "sha": base_sha})
    if st not in (200, 201) and "already exists" not in str((mk or {}).get("message", "")):
        return {"error": _scrub(f"could not create branch '{head_branch}': "
                                f"{(mk or {}).get('message', st)}")}

    # 3) commit each changed file onto the head branch
    for path, content in files.items():
        st, cur = _api("GET", f"{_API}/repos/{repo}/contents/{path}?ref={head_branch}",
                       None)
        sha = (cur or {}).get("sha") if st == 200 else None
        payload = {"message": f"{title}\n\n(opened by the lab on operator approval)",
                   "content": base64.b64encode(content.encode()).decode(),
                   "branch": head_branch}
        if sha:
            payload["sha"] = sha
        st, put = _api("PUT", f"{_API}/repos/{repo}/contents/{path}", payload)
        if st not in (200, 201):
            return {"error": _scrub(f"could not commit '{path}': "
                                    f"{(put or {}).get('message', st)}")}

    # 4) open the pull request
    st, pr = _api("POST", f"{_API}/repos/{repo}/pulls",
                  {"title": title, "head": head_branch, "base": base, "body": body})
    if st not in (200, 201):
        return {"error": _scrub(f"could not open PR: {(pr or {}).get('message', st)}")}
    return {"url": pr.get("html_url"), "number": pr.get("number"),
            "head": head_branch, "base": base}


def open_pull_request(repo: str, *, head_branch: str, base: str = "main",
                      title: str, body: str = "",
                      files: Optional[dict[str, str]] = None) -> dict:
    """Open a PR for an approved submit_pr decision. Allowlist-checked BEFORE
    any I/O. Returns {url, number, head, base} on success or {error}. The
    write token is exercised here and ONLY here (the gate calls this only on an
    approved decision). Never raises; never returns or logs the token."""
    allow_err = _check_repo(repo)
    if allow_err:
        return {"error": allow_err}
    fn = _PR_OPENER["fn"] or _live_open_pr
    try:
        out = fn(repo, head_branch=head_branch, base=base, title=title,
                 body=body, files=files or {})
    except Exception as exc:  # an opener must never crash the gate
        return {"error": _scrub(f"{type(exc).__name__}: {exc}")}
    # Final scrub: even a mock opener's output passes through the redactor.
    if isinstance(out, dict) and out.get("error"):
        out["error"] = _scrub(str(out["error"]))
    return out
