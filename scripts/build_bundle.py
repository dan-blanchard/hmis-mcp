"""Assemble the Claude Desktop bundle directory.

    python scripts/build_bundle.py [output_dir]

The bundle carries this project's source and lock file, so the host installs
exactly the tested dependency versions. Pack the result with:

    npx @anthropic-ai/mcpb pack <output_dir> hmis-mcp.mcpb
"""

import json
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["pyproject.toml", "uv.lock", ".python-version", "README.md", "LICENSE"]


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest = json.loads((ROOT / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shutil.copy(ROOT / "bundle" / "bundle_main.py", out / "bundle_main.py")
    for name in FILES:
        shutil.copy(ROOT / name, out / name)
    shutil.copytree(ROOT / "src" / "hmis_mcp", out / "src" / "hmis_mcp",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "bundle"
    print(build(target))
