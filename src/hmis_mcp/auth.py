"""Access control for the HTTP transport.

One shared secret, presented one of two ways:

* as a bearer token, for agents that can send an Authorization header;
* typed into a login page during an OAuth authorization-code flow, for agents
  such as ChatGPT that only speak OAuth.

The server is its own OAuth authorization server. Clients register themselves
(RFC 7591), the operator approves each one by entering the secret, and the
client receives short-lived access tokens with rotating refresh tokens. Only
token hashes are kept, in a 0600 state file so approvals survive a restart.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "hmis.read"
MIN_SECRET_LENGTH = 24
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 600
MAX_FAILURES = 10
FAILURE_WINDOW = 600
MAX_PENDING = 200
MAX_CLIENTS = 500
LOGIN_PATH = "/login"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AccessProvider:
    """OAuthAuthorizationServerProvider backed by one operator secret."""

    def __init__(self, secret: str, public_url: str, state_file: str | Path | None = None):
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"the access secret must be at least {MIN_SECRET_LENGTH} characters; "
                "generate one with `hmis-mcp secret`")
        self._secret = secret.encode()
        self.public_url = public_url.rstrip("/")
        self._path = Path(state_file).expanduser() if state_file else None
        self._clients: dict[str, dict[str, Any]] = {}
        self._access: dict[str, dict[str, Any]] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: deque[float] = deque()
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        data = json.loads(self._path.read_text())
        self._clients = data.get("clients", {})
        self._access = data.get("access", {})
        self._refresh = data.get("refresh", {})

    def _save(self) -> None:
        now = time.time()
        self._access = {k: v for k, v in self._access.items() if v["expires_at"] > now}
        self._refresh = {k: v for k, v in self._refresh.items() if v["expires_at"] > now}
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"clients": self._clients, "access": self._access,
                       "refresh": self._refresh}, f)
        os.replace(tmp, self._path)

    # -- secret checks -----------------------------------------------------

    def _is_secret(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._secret)

    def _locked_out(self) -> bool:
        cutoff = time.time() - FAILURE_WINDOW
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        return len(self._failures) >= MAX_FAILURES

    # -- OAuthAuthorizationServerProvider ----------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            return
        while len(self._clients) >= MAX_CLIENTS:  # registration is open; bound it
            self._clients.pop(next(iter(self._clients)))
        self._clients[client_info.client_id] = client_info.model_dump(mode="json")
        self._save()

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
        while len(self._pending) >= MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = (client.client_id or "", params, now + LOGIN_TTL)
        return f"{self.public_url}{LOGIN_PATH}?txn={txn}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code and (code.client_id != client.client_id or code.expires_at < time.time()):
            return None
        return code

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(authorization_code.client_id, authorization_code.scopes,
                           authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        rec = self._refresh.get(_hash(refresh_token))
        if not rec or rec["client_id"] != client.client_id or rec["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=rec["client_id"],
                            scopes=rec["scopes"], expires_at=int(rec["expires_at"]),
                            resource=rec.get("resource"))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if self._refresh.pop(_hash(refresh_token.token), None) is None:
            raise TokenError("invalid_grant", "refresh token already used")
        return self._issue(refresh_token.client_id, refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self._is_secret(token):
            return AccessToken(token=token, client_id="static-secret", scopes=[SCOPE])
        rec = self._access.get(_hash(token))
        if not rec or rec["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=rec["client_id"], scopes=rec["scopes"],
                           expires_at=int(rec["expires_at"]), resource=rec.get("resource"))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access.pop(_hash(token.token), None)
        self._refresh.pop(_hash(token.token), None)
        self._save()

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        base = {"client_id": client_id, "scopes": scopes, "resource": resource}
        self._access[_hash(access)] = {**base, "expires_at": now + ACCESS_TTL}
        self._refresh[_hash(refresh)] = {**base, "expires_at": now + REFRESH_TTL}
        self._save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    # -- login page --------------------------------------------------------

    async def login(self, request: Request) -> Response:
        """GET shows the approval form; POST checks the secret and returns to the client."""
        if request.method == "POST":
            form = await request.form()
            txn, supplied = str(form.get("txn", "")), str(form.get("secret", ""))
        else:
            txn, supplied = request.query_params.get("txn", ""), None
        pending = self._pending.get(txn)
        if not pending or pending[2] < time.time():
            return _page("This sign-in link has expired. Start the connection again from "
                         "your AI client.", status=400)
        client_id, params, _ = pending
        client_name = (self._clients.get(client_id) or {}).get("client_name") or "An AI client"
        redirect_host = params.redirect_uri.host or "unknown"

        error = ""
        if supplied is not None:
            if self._locked_out():
                return _page("Too many failed attempts. Wait ten minutes and try again.",
                             status=429)
            if self._is_secret(supplied):
                del self._pending[txn]
                code = secrets.token_urlsafe(32)
                self._codes[code] = AuthorizationCode(
                    code=code, scopes=params.scopes or [SCOPE],
                    expires_at=time.time() + CODE_TTL, client_id=client_id,
                    code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=params.resource)
                return RedirectResponse(
                    construct_redirect_uri(str(params.redirect_uri), code=code,
                                           state=params.state),
                    status_code=302, headers={"Cache-Control": "no-store"})
            self._failures.append(time.time())
            error = "<p class=err>That secret is not correct.</p>"

        body = f"""
            <p><b>{html.escape(client_name)}</b> is asking for access to this agency's
            HMIS statistics. After you approve, you will be sent back to
            <b>{html.escape(redirect_host)}</b>.</p>
            <p>It will be able to read aggregate counts. It will not be able to read
            client names, identifiers or individual records.</p>
            {error}
            <form method="post" action="{LOGIN_PATH}">
              <input type="hidden" name="txn" value="{html.escape(txn)}">
              <label>Access secret
                <input type="password" name="secret" autocomplete="off" autofocus required>
              </label>
              <button type="submit">Approve</button>
            </form>"""
        return _page(body, status=401 if error else 200)


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>HMIS statistics: approve access</title>
        <style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;
        padding:0 1rem}}input[type=password]{{display:block;width:100%;padding:.5rem;
        margin:.25rem 0 1rem;font:inherit;box-sizing:border-box}}button{{padding:.5rem 1.25rem;
        font:inherit}}.err{{color:#b00020}}</style></head>
        <body><h1>HMIS statistics</h1>{body}</body></html>""",
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                 # No form-action directive: browsers apply it to the redirect that
                 # follows the POST, which would block native clients' custom schemes.
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                            "frame-ancestors 'none'",
                 "Referrer-Policy": "no-referrer"},
    )
