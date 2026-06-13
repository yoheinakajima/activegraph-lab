"""GitHub read-only access — PLUMBING (ADR-022, rung 1).

tool_gateway local capabilities (provider "github"): get_tree, get_file,
list_commits, list_issues, list_pulls — stdlib HTTP against the GitHub REST
API, READ-ONLY by construction (every request is a GET; no write scope
exists in this module). Access is bounded by an allowlist
(GITHUB_REPO_ALLOWLIST, comma-separated; entries without an owner default
to yoheinakajima/<name>); a repo outside it is refused before any network
I/O. Public repos, unauthenticated — or GITHUB_TOKEN if present, used only
for rate limits: the token rides in a request header, never in any payload,
envelope, or error (sentinel-audited like every secret).

Handlers return the same {url, status, content, error?} envelope as
web.fetch_url, so the research worker consumes GitHub sources through its
existing tool_result path with zero special-casing — and the MCP READ tier
exposes the same handlers as the github_read passthrough (one endpoint,
one allowlist).

Rung 2 (decision kind=submit_pr — a write token behind TWO gates) is
designed in ADR-022 and ROADMAP.md, deliberately NOT implemented here.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_ALLOWLIST = ("yoheinakajima/activegraph-lab,activegraph,"
                     "activegraph-packs,ag-coder")
DEFAULT_OWNER = "yoheinakajima"

_API = "https://api.github.com"
_MAX_FILE_CHARS = 200_000
_MAX_LIST = 100
_UA = ("Mozilla/5.0 (compatible; activegraph-lab/0.1; "
       "+https://github.com/yoheinakajima/activegraph-lab)")

# Fixture seam: a transport(url) -> (status, parsed_json) injected in place
# of the live stdlib client. Never the token's concern — canned transports
# see no headers.
_TRANSPORT: dict[str, Optional[Callable]] = {"fn": None}


def set_transport(fn: Optional[Callable]) -> None:
    _TRANSPORT["fn"] = fn


def repo_allowlist() -> set[str]:
    raw = (os.environ.get("GITHUB_REPO_ALLOWLIST", "").strip()
           or DEFAULT_ALLOWLIST)
    out: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip().strip("/")
        if not entry:
            continue
        if "/" not in entry:
            entry = f"{DEFAULT_OWNER}/{entry}"
        out.add(entry.lower())
    return out


def _check_repo(repo: str) -> Optional[str]:
    repo = (repo or "").strip().strip("/")
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo):
        return f"invalid repo (want owner/name): {repo!r}"
    if repo.lower() not in repo_allowlist():
        return (f"repo '{repo}' is not in GITHUB_REPO_ALLOWLIST "
                f"(allowed: {', '.join(sorted(repo_allowlist()))})")
    return None


def _scrub(text: str) -> str:
    """Defense in depth: the token never enters payloads by construction;
    scrub it from any error text anyway."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and token in text:
        text = text.replace(token, "<GITHUB_TOKEN>")
    return text


def _http_get(url: str) -> tuple[int, Any]:
    fn = _TRANSPORT["fn"]
    if fn is not None:
        return fn(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:  # rate limits only — never echoed anywhere
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(
                resp.read(2_000_000).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"message": _scrub(f"HTTPError: {exc.code} {exc.reason}")}
    except Exception as exc:
        return 0, {"message": _scrub(f"{type(exc).__name__}: {exc}")}


def _envelope(url: str, status: int, content: str = "",
              error: Optional[str] = None) -> dict:
    out = {"url": url, "status": status, "content": content}
    if error:
        out["error"] = _scrub(error)
    return out


def _refused(repo: str, err: str) -> dict:
    return _envelope(f"https://github.com/{repo}", 0, error=err)


def _limit(n: Any, default: int = 30) -> int:
    try:
        return max(1, min(int(n or default), _MAX_LIST))
    except (TypeError, ValueError):
        return default


def gh_get_tree(repo: str, ref: str = "HEAD", recursive: bool = False, **_kw) -> dict:
    err = _check_repo(repo)
    if err:
        return _refused(repo, err)
    api = (f"{_API}/repos/{repo}/git/trees/{urllib.request.quote(str(ref))}"
           + ("?recursive=1" if recursive else ""))
    status, body = _http_get(api)
    url = f"https://github.com/{repo}/tree/{ref}"
    if status != 200:
        return _envelope(url, status, error=str((body or {}).get("message", status)))
    entries = [{"path": t.get("path"), "type": t.get("type"),
                "size": t.get("size")} for t in (body.get("tree") or [])][:500]
    return _envelope(url, 200, json.dumps({
        "repo": repo, "ref": ref, "truncated": bool(body.get("truncated")),
        "entries": entries}))


def gh_get_file(repo: str, path: str, ref: Optional[str] = None, **_kw) -> dict:
    err = _check_repo(repo)
    if err:
        return _refused(repo, err)
    api = f"{_API}/repos/{repo}/contents/{urllib.request.quote(str(path or ''))}"
    if ref:
        api += f"?ref={urllib.request.quote(str(ref))}"
    status, body = _http_get(api)
    url = f"https://github.com/{repo}/blob/{ref or 'HEAD'}/{path}"
    if status != 200:
        return _envelope(url, status, error=str((body or {}).get("message", status)))
    if isinstance(body, list):  # a directory — point at get_tree instead
        names = json.dumps([e.get("path") for e in body][:200])
        return _envelope(url, 200, names)
    try:
        text = base64.b64decode(body.get("content") or "").decode(
            "utf-8", errors="replace")[:_MAX_FILE_CHARS]
    except Exception as exc:
        return _envelope(url, 0, error=f"decode failed: {type(exc).__name__}")
    return _envelope(url, 200, text)


def gh_list_commits(repo: str, path: Optional[str] = None,
                    limit: Any = 30, **_kw) -> dict:
    err = _check_repo(repo)
    if err:
        return _refused(repo, err)
    api = f"{_API}/repos/{repo}/commits?per_page={_limit(limit)}"
    if path:
        api += f"&path={urllib.request.quote(str(path))}"
    status, body = _http_get(api)
    url = f"https://github.com/{repo}/commits"
    if status != 200:
        return _envelope(url, status, error=str((body or {}).get("message", status)))
    rows = [{"sha": (c.get("sha") or "")[:10],
             "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
             "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
             "message": str((c.get("commit") or {}).get("message") or ""
                            ).splitlines()[0][:120]}
            for c in (body or [])]
    return _envelope(url, 200, json.dumps({"repo": repo, "commits": rows}))


def gh_list_issues(repo: str, state: str = "open", limit: Any = 30, **_kw) -> dict:
    err = _check_repo(repo)
    if err:
        return _refused(repo, err)
    state = state if state in ("open", "closed", "all") else "open"
    api = f"{_API}/repos/{repo}/issues?state={state}&per_page={_limit(limit)}"
    status, body = _http_get(api)
    url = f"https://github.com/{repo}/issues"
    if status != 200:
        return _envelope(url, status, error=str((body or {}).get("message", status)))
    rows = [{"number": i.get("number"), "title": i.get("title"),
             "state": i.get("state"),
             "user": (i.get("user") or {}).get("login"),
             "is_pull_request": "pull_request" in (i or {}),
             "created_at": i.get("created_at")} for i in (body or [])]
    return _envelope(url, 200, json.dumps({"repo": repo, "state": state,
                                           "issues": rows}))


def gh_list_pulls(repo: str, state: str = "open", limit: Any = 30, **_kw) -> dict:
    err = _check_repo(repo)
    if err:
        return _refused(repo, err)
    state = state if state in ("open", "closed", "all") else "open"
    api = f"{_API}/repos/{repo}/pulls?state={state}&per_page={_limit(limit)}"
    status, body = _http_get(api)
    url = f"https://github.com/{repo}/pulls"
    if status != 200:
        return _envelope(url, status, error=str((body or {}).get("message", status)))
    rows = [{"number": p.get("number"), "title": p.get("title"),
             "state": p.get("state"),
             "user": (p.get("user") or {}).get("login"),
             "head": ((p.get("head") or {}).get("ref")),
             "created_at": p.get("created_at")} for p in (body or [])]
    return _envelope(url, 200, json.dumps({"repo": repo, "state": state,
                                           "pulls": rows}))


GITHUB_CAPABILITIES: dict[str, Callable] = {
    "get_tree": gh_get_tree,
    "get_file": gh_get_file,
    "list_commits": gh_list_commits,
    "list_issues": gh_list_issues,
    "list_pulls": gh_list_pulls,
}


def register_github_read(transport: Optional[Callable] = None,
                         overwrite: bool = False) -> bool:
    """Register the github.* capabilities with tool_gateway's local registry
    (idempotent, like register_web_fetch). `transport` injects a canned
    client for fixtures."""
    if transport is not None:
        set_transport(transport)
    try:
        from packs.tool_gateway.tools import (_LOCAL_REGISTRY,
                                              register_local_capability)
    except Exception:
        return False
    registered = False
    for cap, fn in GITHUB_CAPABILITIES.items():
        if overwrite or f"github.{cap}" not in _LOCAL_REGISTRY:
            register_local_capability("github", cap, fn)
            registered = True
    return registered


# ── URL routing for the research worker (ADR-020/022) ───────────────────────

_GH_BLOB_RE = re.compile(
    r"^https?://github\.com/([\w.\-]+/[\w.\-]+)/blob/([^/]+)/(.+)$")
_GH_TREE_RE = re.compile(
    r"^https?://github\.com/([\w.\-]+/[\w.\-]+)(?:/tree/([^/]+).*)?/?$")


def call_spec_for_url(url: str) -> tuple[str, str, dict]:
    """(provider, capability, input_data) for a research-worker source URL:
    github.com URLs route to the read tools, everything else to web.fetch_url."""
    m = _GH_BLOB_RE.match(url)
    if m:
        return "github", "get_file", {"repo": m.group(1), "ref": m.group(2),
                                      "path": m.group(3)}
    m = _GH_TREE_RE.match(url)
    if m:
        # Recursive so the research worker sees the full file list and can
        # fetch the CONTENTS of relevant implementation files, not just the
        # top-level tree (ADR-022/020; branch#62 closed having fetched only
        # the repo tree, never a file — the published verdict flagged it).
        spec: dict = {"repo": m.group(1), "recursive": True}
        if m.group(2):
            spec["ref"] = m.group(2)
        return "github", "get_tree", spec
    return "web", "fetch_url", {"url": url}
