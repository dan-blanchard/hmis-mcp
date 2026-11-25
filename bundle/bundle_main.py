"""Entry point for the Claude Desktop bundle.

The host fills in settings as environment variables. A setting the person left
blank can arrive empty or as the literal placeholder text, so both are cleared
before the normal command line takes over.
"""

import os
import sys
from pathlib import Path


def _setting(name: str) -> str | None:
    value = os.environ.pop(name, "").strip()
    if not value or "${" in value:
        return None
    return value


def main() -> None:
    export = _setting("HMIS_EXPORT_PATH")
    if not export:
        sys.exit("hmis-mcp: no HMIS export chosen. Open this extension's settings and pick "
                 "the export zip file.")
    args = ["serve", "--export", export]
    cell = _setting("HMIS_MIN_CELL_SIZE")
    if cell:
        args += ["--min-cell-size", str(int(float(cell)))]
    audit_dir = _setting("HMIS_AUDIT_DIR")
    if audit_dir:
        args += ["--audit-log", str(Path(audit_dir) / "hmis-mcp-audit.jsonl")]

    from hmis_mcp.server import main as serve
    serve(args)


if __name__ == "__main__":
    main()
