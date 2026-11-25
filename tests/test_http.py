"""The HTTP transport, driven the way a remote agent such as ChatGPT drives it."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

SECRET = "test-secret-" + "x" * 24
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(export_dir, tmp_path, *extra, env=None):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "hmis_mcp.server", "--export", str(export_dir),
         "--transport", "http", "--port", str(port),
         "--state-file", str(tmp_path / "state.json"), *extra],
        env={**os.environ, **(env or {})}, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if proc.poll() is not None:
            return proc, base
        try:
            httpx2.get(f"{base}/.well-known/oauth-protected-resource/mcp", timeout=1)
            break
        except httpx2.TransportError:
            time.sleep(0.1)
    return proc, base


@pytest.fixture
def server(export_dir, tmp_path):
    proc, base = _start(export_dir, tmp_path, env={"HMIS_MCP_SECRET": SECRET})
    yield base, tmp_path
    proc.terminate()
    proc.wait(5)


def _mcp(base, token, fn):
    async def run():
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
            async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as c:
                return await fn(c)
    return asyncio.run(run())


def _oauth_login(base, secret=SECRET):
    """Register, authorize, log in, exchange. Returns (client_id, token response)."""
    http = httpx2.Client(base_url=base, follow_redirects=False)
    meta = http.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"] and "S256" in meta["code_challenge_methods_supported"]
    client = http.post(urlparse(meta["registration_endpoint"]).path, json={
        "client_name": "ChatGPT <script>", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code",
                                                              "refresh_token"],
        "response_types": ["code"]}).json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = http.get(urlparse(meta["authorization_endpoint"]).path, params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st4te",
        "scope": "hmis.read"})
    assert r.status_code == 302, r.text
    login = urlparse(r.headers["location"])
    assert login.path == "/login"
    txn = parse_qs(login.query)["txn"][0]
    page = http.get("/login", params={"txn": txn})
    assert page.status_code == 200 and "&lt;script&gt;" in page.text
    assert "<script>" not in page.text

    wrong = http.post("/login", data={"txn": txn, "secret": "not-the-secret"})
    assert wrong.status_code == 401
    r = http.post("/login", data={"txn": txn, "secret": secret})
    assert r.status_code == 302, r.text
    back = urlparse(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    q = parse_qs(back.query)
    assert q["state"] == ["st4te"]
    # The login link is single use.
    assert http.post("/login", data={"txn": txn, "secret": secret}).status_code == 400

    form = {"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": client["client_id"], "code_verifier": verifier}
    token_path = urlparse(meta["token_endpoint"]).path
    bad = http.post(token_path, data={**form, "code_verifier": "wrong" * 12})
    assert bad.status_code == 400
    tok = http.post(token_path, data=form)
    assert tok.status_code == 200, tok.text
    assert http.post(token_path, data=form).status_code == 400  # code is single use
    return http, token_path, client["client_id"], tok.json()


def test_unauthenticated_requests_are_refused_and_point_at_oauth(server):
    base, _ = server
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}
    r = httpx2.post(f"{base}/mcp", json=body, headers=headers)
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]
    r = httpx2.post(f"{base}/mcp", json=body,
                    headers={**headers, "Authorization": "Bearer nope"})
    assert r.status_code == 401
    prm = httpx2.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"].endswith("/mcp") and prm["authorization_servers"]


def test_oauth_flow_then_tool_calls(server):
    base, tmp = server
    http, token_path, client_id, tok = _oauth_login(base)
    assert tok["token_type"].lower() == "bearer" and tok["refresh_token"]

    names = _mcp(base, tok["access_token"], lambda c: c.list_tools())
    assert len(names.tools) == 4
    result = _mcp(base, tok["access_token"], lambda c: c.call_tool(
        "query_aggregate", {"measures": ["clients"], "group_by": ["project_type"]}))
    assert result.structured_content["rows"]

    # Refresh tokens rotate; a used one is dead.
    form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": client_id}
    fresh = http.post(token_path, data=form)
    assert fresh.status_code == 200, fresh.text
    assert http.post(token_path, data=form).status_code == 400
    assert _mcp(base, fresh.json()["access_token"], lambda c: c.list_tools()).tools

    # Nothing usable is written to disk: only hashes.
    state = (tmp / "state.json").read_text()
    assert oct((tmp / "state.json").stat().st_mode & 0o777) == "0o600"
    for value in (SECRET, tok["access_token"], tok["refresh_token"],
                  fresh.json()["access_token"], fresh.json()["refresh_token"]):
        assert value not in state
    assert client_id in json.loads(state)["clients"]


def test_static_secret_works_as_a_bearer_token(server):
    base, _ = server
    out = _mcp(base, SECRET, lambda c: c.call_tool("describe_dataset", {}))
    assert out.structured_content["privacy"]["min_cell_size"] == 11


def test_approvals_survive_a_restart(export_dir, tmp_path):
    proc, base = _start(export_dir, tmp_path, env={"HMIS_MCP_SECRET": SECRET})
    try:
        _, _, _, tok = _oauth_login(base)
    finally:
        proc.terminate()
        proc.wait(5)
    proc, base = _start(export_dir, tmp_path, env={"HMIS_MCP_SECRET": SECRET})
    try:
        assert _mcp(base, tok["access_token"], lambda c: c.list_tools()).tools
    finally:
        proc.terminate()
        proc.wait(5)


def test_login_locks_out_after_repeated_failures(server):
    base, _ = server
    http = httpx2.Client(base_url=base)
    client = http.post("/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}).json()
    r = http.get("/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": "c" * 43, "code_challenge_method": "S256"})
    txn = parse_qs(urlparse(r.headers["location"]).query)["txn"][0]
    codes = [http.post("/login", data={"txn": txn, "secret": f"guess-{i}"}).status_code
             for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    # Even the right secret is refused during the lockout.
    assert http.post("/login", data={"txn": txn, "secret": SECRET}).status_code == 429


@pytest.mark.parametrize("extra, env", [
    ((), {}),                                              # no secret at all
    (("--no-auth", "--host", "0.0.0.0"), {}),              # open on every interface
    (("--no-auth", "--public-url", "https://x.example"), {}),
    ((), {"HMIS_MCP_SECRET": "short"}),
])
def test_refuses_to_start_unprotected(export_dir, tmp_path, extra, env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("HMIS_")}
    proc = subprocess.run(
        [sys.executable, "-m", "hmis_mcp.server", "--export", str(export_dir),
         "--transport", "http", "--port", str(_free_port()),
         "--state-file", str(tmp_path / "s.json"), *extra],
        env={**clean, **env}, capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and "secret" in proc.stderr


def test_no_auth_is_allowed_on_loopback(export_dir, tmp_path):
    proc, base = _start(export_dir, tmp_path, "--no-auth")
    try:
        async def run():
            async with Client(f"{base}/mcp") as c:
                return await c.list_tools()
        assert len(asyncio.run(run()).tools) == 4
    finally:
        proc.terminate()
        proc.wait(5)
