"""MCP server: aggregate, PII-free access to an HMIS export.

Two transports. stdio is for agents that launch a local process (Claude Code,
Claude Desktop, Codex CLI, Gemini CLI, Cursor, VS Code). Streamable HTTP is for
agents that connect to a URL (ChatGPT, the OpenAI Responses API, hosted agents).

Tool input schemas are kept deliberately plain: no nullable unions, no
free-form maps, every object closed. Several model providers reject anything
fancier.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .auth import LOGIN_PATH, SCOPE, AccessProvider, new_secret
from .engine import Engine, QueryError
from .model import Model
from .privacy import DEFAULT_MIN_CELL, Policy
from .source import CsvExportSource, SourceError

INSTRUCTIONS = """\
Aggregate statistics from a Homeless Management Information System (HMIS) export.
No client-level data is available through this server: no names, identifiers, birth
dates, or rows about individuals. Counts covering fewer clients than the configured
minimum cell size come back as suppressed.

Start with describe_dataset to learn the export period and the valid measures,
dimensions and dimension values. Use query_aggregate for counts and breakdowns,
and bed_utilization for occupancy.

When writing reports: a suppressed cell is not zero. Say "fewer than N" or
"suppressed". Do not back-calculate suppressed values from totals, and do not ask
the user to paste client-level records into the conversation to fill gaps.
"""

# Every tool only reads, touches nothing outside this dataset, and is safe to repeat.
READ_ONLY = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                 openWorldHint=False)

Measure = Literal["clients", "enrollments", "households", "avg_length_of_stay_days",
                  "median_length_of_stay_days"]
IsoDate = Annotated[str, Field(description="ISO date YYYY-MM-DD, inclusive. Leave empty to "
                                           "use the export period.")]


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: str = Field(description="Dimension name, as listed by describe_dataset")
    values: list[str] = Field(description="Keep rows whose value is any of these")


def build_server(engine: Engine, provider: AccessProvider | None = None) -> MCPServer:
    auth = None
    if provider:
        auth = AuthSettings(
            issuer_url=provider.public_url,
            resource_server_url=f"{provider.public_url}/mcp",
            # The static secret has no audience to check, so the provider decides.
            validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
        )
    server = MCPServer("hmis", title="HMIS statistics", instructions=INSTRUCTIONS,
                       auth_server_provider=provider, auth=auth)
    if provider:
        server.custom_route(LOGIN_PATH, methods=["GET", "POST"])(provider.login)

    def guarded(fn):
        # Only QueryError text reaches the model. Anything else is reported by the
        # SDK as a bare "Error executing tool", so a database error can't carry
        # data out with it.
        try:
            return fn()
        except QueryError as e:
            raise ToolError(str(e)) from None

    @server.tool(title="Describe the dataset", annotations=ToolAnnotations(**READ_ONLY))
    def describe_dataset() -> dict[str, Any]:
        """Export period, privacy rules, and the full query vocabulary: measures,
        cohorts, and dimensions with their allowed values. Call this first."""
        return engine.describe()

    @server.tool(title="List projects", annotations=ToolAnnotations(**READ_ONLY))
    def list_projects() -> dict[str, Any]:
        """Projects in the export: name, HUD project type, organization, operating
        dates and current bed inventory. Reference data only, no client counts."""
        return {"projects": engine.list_projects()}

    @server.tool(title="Query aggregate statistics", annotations=ToolAnnotations(**READ_ONLY))
    def query_aggregate(
        measures: Annotated[list[Measure], Field(description="What to compute for each cell")],
        group_by: Annotated[list[str], Field(
            description="Up to three dimension names to break the result down by. "
                        "Empty for a single total.")] = [],
        filters: Annotated[list[Filter], Field(
            description="Restrict the population. A row must satisfy every filter.")] = [],
        cohort: Annotated[Literal["active", "entered", "exited"], Field(
            description="active: enrolled at any point in the period. entered: entered a "
                        "project during it. exited: exited during it.")] = "active",
        period_start: IsoDate = "",
        period_end: IsoDate = "",
    ) -> dict[str, Any]:
        """Count clients, enrollments or households, or summarize length of stay,
        optionally broken down by up to three dimensions.

        Rows with "suppressed": true cover too few clients to release. Percentages
        are yours to compute from released counts. Example, exits to permanent
        housing by project type for a federal fiscal year:
            measures=["clients"], group_by=["project_type", "destination"],
            cohort="exited", period_start="2025-10-01", period_end="2026-09-30"
        """
        merged: dict[str, list[str]] = {}
        for f in filters:
            if f.dimension in merged:
                raise ToolError(f"Two filters name {f.dimension!r}; combine their values in one")
            merged[f.dimension] = f.values
        return guarded(lambda: engine.query(
            list(measures), group_by, merged, cohort, period_start, period_end))

    @server.tool(title="Bed utilization", annotations=ToolAnnotations(**READ_ONLY))
    def bed_utilization(
        period_start: IsoDate = "",
        period_end: IsoDate = "",
        projects: Annotated[list[str], Field(
            description="Project names from list_projects. Empty for all.")] = [],
    ) -> dict[str, Any]:
        """Monthly bed nights, average nightly occupancy and utilization against bed
        inventory, per project. Entry/exit dates are used for most project types,
        move-in dates for permanent housing, and bed-night records for
        night-by-night shelters. Months in which a project served fewer clients
        than the minimum cell size are suppressed."""
        return guarded(lambda: engine.bed_utilization(period_start, period_end, projects))

    return server


# -- command line ----------------------------------------------------------


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _decode_secret(raw: bytes) -> str:
    """Windows PowerShell's `>` writes UTF-16 with a byte-order mark; accept that too."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16").strip()
    return raw.decode("utf-8-sig").strip()


def _read_secret(args: argparse.Namespace) -> str | None:
    if args.secret_file:
        return _decode_secret(Path(args.secret_file).expanduser().read_bytes())
    return os.environ.get("HMIS_MCP_SECRET") or None


def _write_secret(path: str) -> Path:
    """Create a secret file readable only by its owner, without shell redirection."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_secret() + "\n")
    return target


def _check(args: argparse.Namespace) -> None:
    """Load an export and report what the server would see. Prints no client data."""
    try:
        engine = Engine(Model(CsvExportSource(args.export, args.csv_version)),
                        Policy(args.min_cell_size))
    except (SourceError, ValueError) as e:
        sys.exit(f"hmis-mcp: {e}")
    desc = engine.describe()
    src, totals = desc["source"], desc["totals_for_export_period"]
    print(f"Export loaded: {src['csv_format']} layout, "
          f"{src['export_period']['start']} to {src['export_period']['end']}")
    print(f"Projects: {len(engine.list_projects())}")
    if totals and not totals.get("suppressed"):
        print(f"Clients: {totals['clients']}  Enrollments: {totals['enrollments']}")
    else:
        print(f"Clients: fewer than {desc['privacy']['min_cell_size']} (suppressed)")
    for warning in src.get("warnings", []):
        print(f"Warning: {warning}")
    print("OK")


def _serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not args.export:
        parser.error("no export given: pass --export or set HMIS_EXPORT_PATH")
    provider = None
    if args.transport == "http":
        secret = _read_secret(args)
        public_url = (args.public_url or f"http://{args.host}:{args.port}").rstrip("/")
        local_only = _is_loopback(args.host) and not args.public_url
        if secret:
            try:
                provider = AccessProvider(secret, public_url, args.state_file)
            except ValueError as e:
                sys.exit(f"hmis-mcp: {e}")
        elif not (args.no_auth and local_only):
            sys.exit(
                "hmis-mcp: the HTTP transport needs an access secret. Set HMIS_MCP_SECRET or "
                "pass --secret-file (generate one with `hmis-mcp secret`). --no-auth is "
                "accepted only on a loopback address with no --public-url.")
    try:
        engine = Engine(Model(CsvExportSource(args.export, args.csv_version)),
                        Policy(args.min_cell_size), audit_log=args.audit_log)
    except (SourceError, ValueError) as e:
        sys.exit(f"hmis-mcp: {e}")
    server = build_server(engine, provider)
    if args.transport == "stdio":
        server.run("stdio")
        return
    public = urlparse(public_url)
    hosts = {public.netloc, f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    origins = {f"{public.scheme}://{public.netloc}", f"http://127.0.0.1:{args.port}",
               f"http://localhost:{args.port}"}
    print(f"hmis-mcp: serving {public_url}/mcp "
          f"({'secret required' if provider else 'NO AUTH, loopback only'})", file=sys.stderr)
    server.run(
        "streamable-http", host=args.host, port=args.port,
        # One engine serves every caller, so there is no per-session state to keep.
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=sorted(hosts), allowed_origins=sorted(origins)),
    )


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ("serve", "sample", "secret", "check")
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")

    parser = argparse.ArgumentParser(
        prog="hmis-mcp", description="Aggregate-only MCP server over a HUD HMIS CSV export.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the MCP server (the default command)")
    serve.add_argument("--export", default=os.environ.get("HMIS_EXPORT_PATH"),
                       help="HUD HMIS CSV export: a directory or .zip (env HMIS_EXPORT_PATH)")
    serve.add_argument("--min-cell-size", type=int,
                       default=int(os.environ.get("HMIS_MIN_CELL_SIZE", DEFAULT_MIN_CELL)),
                       help=f"Smallest client count released (default {DEFAULT_MIN_CELL})")
    serve.add_argument("--audit-log", default=os.environ.get("HMIS_AUDIT_LOG"),
                       help="Append one JSON line per query to this file")
    serve.add_argument("--csv-version", type=int, choices=(2022, 2024, 2026),
                       default=int(os.environ["HMIS_CSV_VERSION"])
                       if os.environ.get("HMIS_CSV_VERSION") else None,
                       help="HUD CSV revision of the export. Detected from Client.csv; set "
                            "this only if detection fails.")
    serve.add_argument("--transport", choices=("stdio", "http"),
                       default=os.environ.get("HMIS_MCP_TRANSPORT", "stdio"))
    http = serve.add_argument_group("HTTP transport")
    http.add_argument("--host", default="127.0.0.1")
    http.add_argument("--port", type=int, default=8000)
    http.add_argument("--public-url", default=os.environ.get("HMIS_MCP_PUBLIC_URL"),
                      help="The https:// address clients reach this server at, e.g. a tunnel URL")
    http.add_argument("--secret-file",
                      help="File holding the access secret (or set env HMIS_MCP_SECRET)")
    http.add_argument("--state-file",
                      default=os.environ.get("HMIS_MCP_STATE", "~/.hmis-mcp/oauth-state.json"),
                      help="Where approved clients and token hashes are kept")
    http.add_argument("--no-auth", action="store_true",
                      help="Serve without a secret. Loopback only.")

    sample = sub.add_parser("sample", help="Write a synthetic export for trying things out")
    sample.add_argument("directory")
    sample.add_argument("--clients", type=int, default=1500)
    sample.add_argument("--csv-version", type=int, choices=(2022, 2024, 2026), default=2026)
    secret = sub.add_parser("secret", help="Make a new random access secret")
    secret.add_argument("--out", help="Write it to this file (owner-only) instead of printing")
    check = sub.add_parser("check", help="Load an export and confirm it is readable")
    check.add_argument("--export", default=os.environ.get("HMIS_EXPORT_PATH"), required=not
                       os.environ.get("HMIS_EXPORT_PATH"))
    check.add_argument("--csv-version", type=int, choices=(2022, 2024, 2026))
    check.add_argument("--min-cell-size", type=int, default=DEFAULT_MIN_CELL)

    args = parser.parse_args(argv)
    if args.command == "sample":
        from .sample import generate
        print(f"Wrote synthetic export to {generate(args.directory, args.clients, version=args.csv_version)}")
    elif args.command == "secret":
        if args.out:
            try:
                print(f"Wrote a new secret to {_write_secret(args.out)}")
            except FileExistsError:
                sys.exit(f"hmis-mcp: {args.out} already exists; not overwriting it")
        else:
            print(new_secret())
    elif args.command == "check":
        _check(args)
    else:
        _serve(args, serve)


if __name__ == "__main__":
    main()
