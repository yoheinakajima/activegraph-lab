"""OAuth 2.1 authorization server for the MCP surface (ADR-017) — STATELESS.

No token table, no client table, nothing written anywhere: every credential
this module issues (client_id, authorization code, access/refresh token) is
an HMAC-signed payload, verified later by recomputing the signature. The
module itself holds no secret — every function takes the signing key
(derived from LAB_MCP_TOKEN in server/lab_server.py, kernel) as an explicit
argument, so importing this module yields inert functions; the key never
leaves the kernel except per call (same posture as server/mcp.py: the
kernel authorizes, this module renders). Rotating LAB_MCP_TOKEN therefore
invalidates every client_id, code, and token at once — the same revocation
story as the legacy bearer/URL-token presentations, which remain (ADR-016).

Single operator, public clients only (no client_secret), PKCE S256
required. The authorize page is a password field: the operator pastes
LAB_MCP_TOKEN once; the token never rides in the connector URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from html import escape
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

CODE_TTL_SECONDS = 60
ACCESS_TTL_SECONDS = 24 * 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600
SCOPE = "mcp"

_ALLOWED_HOSTS = ("claude.ai", "claude.com")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


# ---------------------------------------------------------------- signing

def derive_key(mcp_token: str) -> bytes:
    """The one derivation: LAB_MCP_TOKEN → HMAC signing key. Called by the
    kernel (server/lab_server.py) per request; never cached here."""
    return hmac.new(mcp_token.encode(), b"lab-oauth-signing-v1",
                    hashlib.sha256).digest()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(key: bytes, payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return _b64u(body) + "." + _b64u(sig)


def _verify(key: bytes, token: str, typ: str) -> Optional[dict]:
    """Recompute-and-compare: returns the payload iff the signature, the
    type tag, and the expiry all check out. Constant-time on the signature."""
    try:
        body_s, sig_s = token.split(".", 1)
        body = _b64u_dec(body_s)
        sig = _b64u_dec(sig_s)
    except Exception:
        return None
    good = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        payload = json.loads(body)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("typ") != typ:
        return None
    try:
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    return payload


def looks_signed(supplied: str) -> bool:
    """Shape check only — needs no key. Routes /mcp bearers: a credential
    shaped like one of our signed blobs takes the OAuth verification path
    (failure → 401 invalid_token so clients know to refresh); anything else
    falls through to the legacy 403."""
    try:
        body_s, _sig_s = supplied.split(".", 1)
        payload = json.loads(_b64u_dec(body_s))
    except Exception:
        return False
    return isinstance(payload, dict) and "typ" in payload


def verify_access_token(key: bytes, supplied: str) -> bool:
    return _verify(key, supplied, "access") is not None


# ---------------------------------------------------------------- DCR (RFC 7591)

def _redirect_uri_ok(uri: str) -> bool:
    """https callbacks on claude.ai / claude.com, or localhost (any port,
    http allowed there only). Everything else is refused at registration,
    at authorize time, and again via the code binding at token time."""
    try:
        u = urlparse(uri)
        host = (u.hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if u.scheme == "https" and host in _ALLOWED_HOSTS:
        return True
    if u.scheme in ("http", "https") and host in _LOCAL_HOSTS:
        return True
    return False


def client_id_for(key: bytes, redirect_uris: list[str]) -> str:
    """Deterministic client_id: HMAC of the (sorted, deduped) redirect_uris.
    Stateless DCR — re-registration with the same URIs is idempotent."""
    canon = "\n".join(sorted(set(redirect_uris)))
    return "lab-" + hmac.new(key, b"client-id-v1\n" + canon.encode(),
                             hashlib.sha256).hexdigest()[:32]


def handle_register(key: bytes, raw: bytes) -> tuple[int, dict]:
    """POST /register. Fixed error bodies (RFC 7591 codes), nothing echoed."""
    try:
        body = json.loads(raw or b"")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": "invalid_client_metadata",
                     "error_description": "body must be JSON"}
    if not isinstance(body, dict):
        return 400, {"error": "invalid_client_metadata",
                     "error_description": "body must be a JSON object"}
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris \
            or not all(isinstance(u, str) for u in uris):
        return 400, {"error": "invalid_redirect_uri",
                     "error_description": "redirect_uris (non-empty list) is required"}
    if not all(_redirect_uri_ok(u) for u in uris):
        return 400, {"error": "invalid_redirect_uri",
                     "error_description": "redirect_uris must be https "
                                          "claude.ai/claude.com callbacks or localhost"}
    return 201, {
        "client_id": client_id_for(key, uris),
        "redirect_uris": sorted(set(uris)),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": SCOPE,
    }


# ---------------------------------------------------------------- /authorize

_AUTHORIZE_FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>activegraph-lab — authorize</title>
<style>body{{font:16px/1.5 system-ui;max-width:26rem;margin:4rem auto;padding:0 1rem}}
input[type=password]{{width:100%;padding:.5rem;font-size:1rem}}
button{{margin-top:.8rem;padding:.5rem 1.2rem;font-size:1rem}}</style></head>
<body>
<h1>activegraph-lab</h1>
<p>A client is requesting MCP access (read tools + send_chat — never
decisions, pause, or seams). Paste <code>LAB_MCP_TOKEN</code> to approve.</p>
<form method="post" action="/authorize">
{hidden}
<input type="password" name="token" autocomplete="off" autofocus
       placeholder="LAB_MCP_TOKEN">
<button type="submit">Authorize</button>
</form>
</body></html>
"""

_AUTHORIZE_PARAMS = ("client_id", "redirect_uri", "code_challenge", "state")


def _validate_authorize(params: dict) -> Optional[str]:
    """Fixed validation messages only — no echo of any supplied value."""
    if params.get("response_type", "code") != "code":
        return "response_type must be code"
    if not params.get("client_id"):
        return "client_id is required"
    if not _redirect_uri_ok(params.get("redirect_uri") or ""):
        return "redirect_uri must be an https claude.ai/claude.com callback or localhost"
    if not params.get("code_challenge"):
        return "code_challenge is required (PKCE)"
    if params.get("code_challenge_method", "S256") != "S256":
        return "code_challenge_method must be S256"
    return None


def authorize_page(qs: dict) -> tuple[int, str]:
    """GET /authorize → the operator's one-field password page."""
    err = _validate_authorize(qs)
    if err:
        return 400, f"<h1>400</h1><p>{escape(err)}</p>"
    hidden = "\n".join(
        f'<input type="hidden" name="{name}" value="{escape(qs.get(name) or "", quote=True)}">'
        for name in _AUTHORIZE_PARAMS if qs.get(name))
    return 200, _AUTHORIZE_FORM.format(hidden=hidden)


def handle_authorize_post(key: bytes, mcp_token: str,
                          form: dict) -> tuple[int, Optional[str], str]:
    """POST /authorize (the form submit). Returns (status, location, html):
    302 + Location on success, else an HTML error page. Constant-time token
    compare; the 401 carries no oracle detail and echoes nothing."""
    err = _validate_authorize(form)  # re-validate: hidden fields are client data
    if err:
        return 400, None, f"<h1>400</h1><p>{escape(err)}</p>"
    supplied = (form.get("token") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, mcp_token):
        return 401, None, "<h1>401</h1><p>not authorized</p>"
    code = _sign(key, {
        "typ": "code",
        "cid": form["client_id"],
        "ruri": form["redirect_uri"],
        "chal": form["code_challenge"],
        "exp": int(time.time()) + CODE_TTL_SECONDS,
    })
    q = {"code": code}
    if form.get("state"):
        q["state"] = form["state"]
    sep = "&" if "?" in form["redirect_uri"] else "?"
    return 302, form["redirect_uri"] + sep + urlencode(q), ""


# ---------------------------------------------------------------- /token

def parse_form(raw: bytes) -> dict:
    """Token/authorize POST bodies: x-www-form-urlencoded, with a JSON
    fallback for clients that send it."""
    s = (raw or b"").decode("utf-8", "replace")
    if s.lstrip().startswith("{"):
        try:
            d = json.loads(s)
            return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {k: v[0] for k, v in parse_qs(s).items()}


def _mint_token_pair(key: bytes, client_id: str) -> dict:
    now = int(time.time())
    return {
        "access_token": _sign(key, {"typ": "access", "cid": client_id,
                                    "scope": SCOPE, "exp": now + ACCESS_TTL_SECONDS}),
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": _sign(key, {"typ": "refresh", "cid": client_id,
                                     "scope": SCOPE, "exp": now + REFRESH_TTL_SECONDS}),
        "scope": SCOPE,
    }


def handle_token(key: bytes, form: dict) -> tuple[int, dict]:
    """POST /token: authorization_code + PKCE, or refresh_token. Fixed RFC
    6749 error bodies only — no supplied value is ever echoed."""
    grant = form.get("grant_type")
    if grant == "authorization_code":
        payload = _verify(key, form.get("code") or "", "code")
        if payload is None:
            return 400, {"error": "invalid_grant"}
        verifier = form.get("code_verifier") or ""
        challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
        ok = hmac.compare_digest(challenge, str(payload.get("chal") or ""))
        ok &= hmac.compare_digest(form.get("client_id") or "",
                                  str(payload.get("cid") or ""))
        ok &= hmac.compare_digest(form.get("redirect_uri") or "",
                                  str(payload.get("ruri") or ""))
        if not verifier or not ok:
            return 400, {"error": "invalid_grant"}
        return 200, _mint_token_pair(key, payload["cid"])
    if grant == "refresh_token":
        payload = _verify(key, form.get("refresh_token") or "", "refresh")
        if payload is None:
            return 400, {"error": "invalid_grant"}
        return 200, _mint_token_pair(key, payload["cid"])
    return 400, {"error": "unsupported_grant_type"}


# ---------------------------------------------------------------- metadata

def metadata_authorization_server(base: str) -> dict:
    """RFC 8414 — advertised at /.well-known/oauth-authorization-server."""
    return {
        "issuer": base,
        "authorization_endpoint": base + "/authorize",
        "token_endpoint": base + "/token",
        "registration_endpoint": base + "/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
    }


def metadata_protected_resource(base: str) -> dict:
    """RFC 9728 — advertised at /.well-known/oauth-protected-resource."""
    return {
        "resource": base + "/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [SCOPE],
    }
