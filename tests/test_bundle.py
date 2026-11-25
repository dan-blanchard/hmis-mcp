"""The Claude Desktop bundle, launched the way a bundle host launches it."""

import asyncio
import importlib.util
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH")


def _builder():
    spec = importlib.util.spec_from_file_location("build_bundle", ROOT / "scripts" / "build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    return _builder().build(tmp_path_factory.mktemp("bundle") / "hmis-mcp")


@pytest.fixture(scope="module")
def export_zip(tmp_path_factory, export_dir):
    target = tmp_path_factory.mktemp("zip") / "export.zip"
    with zipfile.ZipFile(target, "w") as zf:
        for csv_file in export_dir.glob("*.csv"):
            zf.write(csv_file, csv_file.name)
    return target


def _launch(installed, settings):
    """Substitute manifest variables as a host does. A blank optional setting is
    left as its literal placeholder, which is the worst case to handle."""
    manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["server"]["mcp_config"]
    values = {"__dirname": str(installed),
              **{f"user_config.{k}": str(v) for k, v in settings.items()}}

    def sub(text):
        for key, value in values.items():
            text = text.replace("${" + key + "}", value)
        return text

    return manifest, StdioServerParameters(
        command=config["command"], args=[sub(a) for a in config["args"]],
        env={k: sub(v) for k, v in config["env"].items()})


def _session(params, fn):
    async def run():
        async with Client(params) as client:
            return await fn(client)
    return asyncio.run(run())


def test_manifest_matches_the_project(installed):
    manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{manifest["version"]}"' in pyproject
    assert manifest["server"]["type"] == "uv"
    assert (installed / manifest["server"]["entry_point"]).is_file()
    assert manifest["user_config"]["export_path"]["required"] is True
    # Nothing but source, lock file and docs ships in the bundle.
    shipped = {p.relative_to(installed).as_posix() for p in installed.rglob("*") if p.is_file()}
    assert not [f for f in shipped if f.endswith((".csv", ".zip", ".pyc", ".jsonl"))]
    assert "uv.lock" in shipped and "src/hmis_mcp/server.py" in shipped


def test_bundle_serves_with_all_settings(installed, export_zip, tmp_path):
    manifest, params = _launch(installed, {
        "export_path": export_zip, "min_cell_size": 20, "audit_dir": tmp_path})

    async def use(client):
        tools = sorted(t.name for t in (await client.list_tools()).tools)
        described = (await client.call_tool("describe_dataset", {})).structured_content
        rows = (await client.call_tool(
            "query_aggregate", {"measures": ["clients"], "group_by": ["project_type"]}
        )).structured_content["rows"]
        return tools, described, rows

    tools, described, rows = _session(params, use)
    assert tools == sorted(t["name"] for t in manifest["tools"])
    assert described["privacy"]["min_cell_size"] == 20
    assert all(r.get("suppressed") or r["clients"] >= 20 for r in rows)
    log = (tmp_path / "hmis-mcp-audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[0])["tool"] == "query_aggregate"


def test_bundle_serves_with_only_the_required_setting(installed, export_zip):
    _, params = _launch(installed, {"export_path": export_zip})
    assert "${user_config.audit_dir}" in params.env.values()  # host left it unfilled
    described = _session(
        params, lambda c: c.call_tool("describe_dataset", {})).structured_content
    assert described["privacy"]["min_cell_size"] == 11
