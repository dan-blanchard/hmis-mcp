"""Things that differ between operating systems, and the install-time commands."""

import os
import shutil
import subprocess
import sys

import pytest

from hmis_mcp.engine import Engine
from hmis_mcp.model import Model
from hmis_mcp.server import _decode_secret, main
from hmis_mcp.source import CsvExportSource


def run(*args, env=None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("HMIS_")}
    return subprocess.run([sys.executable, "-m", "hmis_mcp.server", *args],
                          env={**clean, **(env or {})}, capture_output=True, text=True,
                          timeout=120)


def test_check_reports_without_client_data(export_dir):
    out = run("check", "--export", str(export_dir))
    assert out.returncode == 0, out.stderr
    assert "FY2026 layout" in out.stdout and out.stdout.rstrip().endswith("OK")
    assert "Projects: 6" in out.stdout


def test_check_fails_clearly_on_a_bad_path(tmp_path):
    out = run("check", "--export", str(tmp_path / "nope.zip"))
    assert out.returncode != 0 and "not found" in out.stderr


def test_zip_export_leaves_nothing_behind(tmp_path, export_dir, monkeypatch):
    """The zip is unpacked to a temp folder that must be gone afterwards, on every OS."""
    archive = shutil.make_archive(str(tmp_path / "export"), "zip", export_dir)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    assert Engine(Model(CsvExportSource(archive))).query(["clients"])["rows"]
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("encoding, newline", [
    ("utf-8-sig", "\r\n"),   # byte-order mark and Windows line endings
    ("utf-8", "\n"),
])
def test_exports_with_bom_and_crlf(tmp_path, export_dir, engine, encoding, newline):
    target = tmp_path / "x"
    target.mkdir()
    for src in export_dir.glob("*.csv"):
        data = src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline.encode())
        bom = b"\xef\xbb\xbf" if encoding == "utf-8-sig" else b""
        (target / src.name).write_bytes(bom + data)
    assert (target / "Client.csv").read_bytes().startswith(b"\xef\xbb\xbf") == (
        encoding == "utf-8-sig")
    got = Engine(Model(CsvExportSource(target)))
    for dim in ("project_type", "sex", "race_ethnicity"):
        assert got.query(["clients"], [dim]) == engine.query(["clients"], [dim])


def test_secret_file_written_and_read_back(tmp_path):
    target = tmp_path / "nested" / "secret.txt"
    out = run("secret", "--out", str(target))
    assert out.returncode == 0, out.stderr
    secret = target.read_text(encoding="utf-8").strip()
    assert len(secret) >= 32 and secret not in out.stdout
    if os.name != "nt":
        assert oct(target.stat().st_mode & 0o777) == "0o600"
    again = run("secret", "--out", str(target))
    assert again.returncode != 0 and "already exists" in again.stderr
    assert target.read_text(encoding="utf-8").strip() == secret


def test_secret_printed_when_no_file_given(capsys):
    main(["secret"])
    assert len(capsys.readouterr().out.strip()) >= 32


@pytest.mark.parametrize("raw", [
    b"abc123\n",
    b"\xef\xbb\xbfabc123\r\n",                       # UTF-8 with BOM
    "abc123\r\n".encode("utf-16"),                    # what PowerShell 5's > writes
    b"\xfe\xff" + "abc123\n".encode("utf-16-be"),
])
def test_secret_files_from_any_shell_decode(raw):
    assert _decode_secret(raw) == "abc123"
