# hmis-mcp

An MCP server that lets an AI agent answer questions about an agency's HMIS data
(how many people a shelter served, where they exited to, how full the beds were)
while only ever seeing aggregate numbers. The agent cannot retrieve a name, an
SSN, a birth date, a client ID, or a row about one person, because the server
has no tool that returns them and small counts are suppressed.

It reads the HUD HMIS CSV export, which every HMIS vendor is required to
produce, so it is not tied to one vendor or one state: WellSky Community
Services, Bitfocus Clarity, ClientTrack and the rest all export it.

## Install

The only thing you install yourself is [uv](https://docs.astral.sh/uv/), a
small tool that fetches Python and this server when they are needed. There is
nothing to clone and no Python to set up. This works the same on Windows, macOS
and Linux.

On Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

On macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal afterwards so the `uvx` command is found. Then make a
synthetic export and check that the server can read it:

```bash
uvx hmis-mcp sample hmis-sample
```

```bash
uvx hmis-mcp check --export hmis-sample
```

`check` prints the export's layout, its date range and a few totals, then `OK`.
It never prints client data. For real data, run your HMIS's HUD CSV export for
your agency's projects and give `check` the zip or the folder instead.

`uvx` downloads the server from PyPI the first time and keeps a copy. To move
to a newer release later, run it once as `uvx hmis-mcp@latest`.

### If you are an AI agent installing this for someone

1. Run `uvx --version`. If it is missing, ask before installing uv with the
   command above for their operating system.
2. Ask where their HMIS CSV export is (a zip or a folder). Do not open, list,
   print or read the export yourself. It holds client names, birth dates and
   Social Security numbers, and keeping those out of your context is the point
   of this server. Only pass its path along.
3. Run the `check` command above with that path. It prints counts only. If it
   fails, show the person the message and stop.
4. Add the server to your own MCP configuration using the block under
   "Agents that run a local command". Use absolute paths. In JSON on Windows,
   write paths with forward slashes (`C:/Users/name/export.zip`).
5. If the app reports that `uvx` can't be found, which happens with desktop
   apps that don't inherit the terminal's PATH, replace `"uvx"` with its full
   path: `where uvx` on Windows, `which uvx` elsewhere.
6. Reload your MCP servers and call `describe_dataset` to confirm it answers.
7. Leave the HTTP transport and tunnels alone unless the person asks for them.

## Supported export formats

HUD has revised the CSV format three times since October 2021, and all three
revisions load:

| Revision | In effect from | What differs |
|---|---|---|
| FY2026 | October 2025 | `Sex` replaces the gender columns |
| FY2024 | October 2023 | Gender as `Woman`, `Man`, `NonBinary` and others; Hispanic/Latina/e/o is one of the race flags; three-digit living situation codes; two emergency shelter project types |
| FY2022 | October 2021 | Gender as `Female`, `Male`, `NoSingleGender`; a separate `Ethnicity` column; one- and two-digit living situation codes; one emergency shelter type plus `TrackingMethod`; CoC in `EnrollmentCoC.csv` |

The revision is read from the Client.csv header, not from the `CSVVersion` a
vendor wrote in Export.csv, and `describe_dataset` reports it. If the two
disagree the server says so. FY2022 data is translated into the current code
lists using the same crosswalk as HUD Data Lab's FY2022-to-FY2024 conversion,
so a question gets the same answer whichever revision the export is in. Two
things can't be recovered from an FY2022 export: which kind of rental subsidy
someone had (seven old codes became one), and Middle Eastern or North African
as a race, which FY2022 didn't collect.

FY2020 and earlier exports are refused with a message saying so. If detection
fails on an export you know is supported, pass `--csv-version 2022`, `2024` or
`2026`.

## Connecting agents

There are two ways in, and which one you need depends on whether the agent can
start a program on your computer.

### Agents that run a local command (stdio)

Claude Code, Claude Desktop, Codex CLI, Gemini CLI, Cursor and VS Code all
launch the server themselves. Nothing listens on the network. They all want the
same two facts, a command and its arguments, and differ only in which file
holds them:

```json
{
  "mcpServers": {
    "hmis": {
      "command": "uvx",
      "args": ["hmis-mcp", "--export", "/absolute/path/to/export.zip"]
    }
  }
}
```

| Agent | Where it goes |
|---|---|
| Claude Desktop | `claude_desktop_config.json` |
| Claude Code | `claude mcp add hmis -- uvx hmis-mcp --export /absolute/path/to/export.zip` |
| Cursor | `.cursor/mcp.json` |
| Gemini CLI | `~/.gemini/settings.json` |
| VS Code | `.vscode/mcp.json`, with the top-level key `servers` instead of `mcpServers` |
| Codex CLI | `~/.codex/config.toml`, as a `[mcp_servers.hmis]` table with `command` and `args` |

These file locations change between releases. If one doesn't work, check that
agent's MCP documentation; the command and args stay the same.

### Agents that connect to a URL (HTTP)

ChatGPT cannot start a local program. Its servers call yours, so the server
needs a public `https://` address. The same goes for the OpenAI Responses API
and most hosted agent platforms.

Make a secret. This writes it to a file in your home folder, readable only by
you on macOS and Linux, and by whoever can read your user profile on Windows:

```bash
uvx hmis-mcp secret --out ~/.hmis-mcp-secret
```

Give the server a public address. The server itself stays bound to
`127.0.0.1`; a tunnel carries HTTPS traffic to it. With Cloudflare's:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

That prints an address like `https://something.trycloudflare.com`. Start the
server with it:

```bash
uvx hmis-mcp --export /absolute/path/to/export.zip --transport http --public-url https://something.trycloudflare.com --secret-file ~/.hmis-mcp-secret
```

The MCP endpoint is the public address plus `/mcp`.

**ChatGPT.** Add a custom MCP server in ChatGPT's settings (OpenAI has moved
this menu more than once; look under connectors, apps or plugins), give it the
`/mcp` URL, and choose OAuth. ChatGPT registers itself and sends your browser
to this server's approval page. Paste the secret there. Tokens refresh on their
own after that, and approvals survive a restart.

**Agents that send headers.** Use the secret as a bearer token:
`Authorization: Bearer <secret>`. For the OpenAI Responses API that is an `mcp`
tool with `server_url` set to the `/mcp` URL and `authorization` set to the
secret.

The server will not start on HTTP without a secret unless you pass `--no-auth`,
and it accepts `--no-auth` only on a loopback address with no public URL.

Before you expose this, understand what changes. Over stdio, aggregates go to
whichever model your local agent uses. Over HTTP with ChatGPT, they go to
OpenAI, and anyone who obtains the secret can query your agency's statistics
from anywhere. No client-level data is reachable either way, but aggregate
numbers about a small shelter are still your agency's data. Stop the tunnel
when you are not using it.

## Options

| Option | Env var | Default | |
|---|---|---|---|
| `--export` | `HMIS_EXPORT_PATH` | required | Export directory or zip |
| `--min-cell-size` | `HMIS_MIN_CELL_SIZE` | 11 | Smallest client count released. Floor is 5. |
| `--csv-version` | `HMIS_CSV_VERSION` | detected | Force the HUD CSV revision: 2022, 2024 or 2026 |
| `--audit-log` | `HMIS_AUDIT_LOG` | off | One JSON line per query: the request and how many cells were released or suppressed. No results. |
| `--transport` | `HMIS_MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `--host`, `--port` | | `127.0.0.1`, `8000` | Where the HTTP transport listens |
| `--public-url` | `HMIS_MCP_PUBLIC_URL` | | The `https://` address clients use |
| `--secret-file` | `HMIS_MCP_SECRET` (the secret itself) | | Access secret, at least 24 characters |
| `--state-file` | `HMIS_MCP_STATE` | `~/.hmis-mcp/oauth-state.json` | Approved clients and token hashes |

## Tools

- `describe_dataset`: export period, privacy rules, and every valid measure,
  cohort, dimension and dimension value.
- `list_projects`: project names, types, organizations, bed inventory.
- `query_aggregate`: counts of clients, enrollments or households, and mean or
  median length of stay, broken down by up to three dimensions, with filters, a
  cohort (active, entered, exited) and a reporting period.
- `bed_utilization`: monthly bed nights and utilization per project.

Dimensions cover project, organization, project type, CoC, age group,
race/ethnicity, sex (FY2026) or gender (FY2024), veteran status, household
type, relationship to head of household, disabling condition, DV survivor
status, prior living situation, exit destination, length of stay, income at
entry, and entry/exit month, quarter and year.

The agent picks from that vocabulary. It never writes SQL.

## How the privacy protection works

Threat model first, because it determines what the rest is worth. The person
running this server already holds the export and is authorized to see it. The
thing being protected against is client data reaching a model provider, a chat
transcript, or a generated report. The server is not built to stop a determined
insider with unlimited queries from learning something about a client; that
person can open the CSV.

Within that model:

1. Names, SSNs and free-text fields are never read into the database. Each CSV
   has a column allowlist and those columns are not on it.
2. Birth dates and a per-client index stay in the in-memory database, because
   ages and distinct counts need them, but no tool returns them. Ages come out
   as bands and dates as months.
3. Any cell covering fewer than 11 clients is suppressed. Eleven is the CMS
   convention; you can change it, down to 5.
4. If one suppressed cell on a row or column could be recovered by subtracting
   the others from a total, a second cell is suppressed as well.
5. The server remembers which client groups it has already answered about. A
   later query whose group differs from an earlier one by fewer than 11 clients,
   with one containing the other, is refused. That blocks the "everyone, then
   everyone except the people who entered on March 3rd" trick.
6. Unexpected errors return a generic message, so a database error can't carry
   data out.
7. Over HTTP, every request needs the access secret or a token issued after
   someone typed it into the approval page. Only hashes of issued tokens are
   stored. Ten wrong guesses lock the approval page for ten minutes.

Known limits, stated plainly:

- The differencing check is pairwise and lives in memory. Restarting the server
  clears it, and an attack that combines three or more overlapping queries can
  get past it.
- Attribute disclosure is not addressed. If all 14 clients in some cell share a
  sensitive attribute, a breakdown will show that. Anyone who knows a person is
  in the cell learns the attribute.
- A suppressed cell still shows that between 1 and 10 clients are in it.
- Small projects will see a lot of suppression on detailed breakdowns. That is
  the rule working. Use coarser dimensions or longer periods.

Have your HMIS lead review this before using it on real data. HUD has said
publicly that general-purpose AI tools are not appropriate for client data, and
your CoC's privacy policy may have its own rules about AI and about minimum
cell sizes.

## What the numbers mean

These are counts from your export, not certified HUD reports. Where they can
differ from an APR or CAPER:

- Age is taken at the later of entry or period start, as HUD reports do.
- Household type is computed from members' ages at entry.
- Length of stay runs from entry to exit, or to the period end for people still
  enrolled. It does not use move-in dates.
- Bed utilization uses entry and exit dates, move-in dates for permanent
  housing, and bed-night records for night-by-night shelters.
- Chronic homelessness is not implemented.

## Adding a live backend

`hmis_mcp.source.DataSource` is the seam. A source fills `raw_*` tables using
HUD CSV column names, and everything after that is shared. WellSky does not
publish API documentation, so a live backend needs an arrangement with WellSky
or your HMIS lead before it can be written.

## Development

```bash
git clone https://github.com/dan-blanchard/hmis-mcp
```

```bash
cd hmis-mcp && uv run pytest
```

The tests build a synthetic export with planted names and SSNs, run every tool
across every dimension, and fail if any planted value appears in any output.
They also write the same population in each supported HUD CSV revision and
require identical answers, check that tool schemas avoid the JSON Schema
features some model providers reject, and drive the HTTP transport through a
full OAuth sign-in. CI runs them on Windows, macOS and Linux.

To release: raise `version` in `pyproject.toml`, merge, then publish a GitHub
release tagged `v` plus that version. The release workflow uploads it to PyPI.

## License

0BSD. Do what you like with it; no attribution needed.
