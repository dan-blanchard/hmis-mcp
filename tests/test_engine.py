import csv
import itertools
import json
import shutil

import pytest

from hmis_mcp import sample
from hmis_mcp.engine import Engine, QueryError
from hmis_mcp.model import Model
from hmis_mcp.privacy import Policy
from hmis_mcp.source import CsvExportSource, SourceError


def test_totals_match_the_csv(engine, export_dir):
    with (export_dir / "Enrollment.csv").open() as f:
        rows = list(csv.DictReader(f))
    out = engine.query(["clients", "enrollments"], period_start="1900-01-01",
                       period_end="2100-01-01")
    assert out["rows"] == [{"clients": len({r["PersonalID"] for r in rows}),
                            "enrollments": len(rows)}]


def test_released_cells_meet_the_threshold(engine):
    dims = list(engine.model.dimensions)
    for group in itertools.combinations(dims, 2):
        try:
            out = engine.query(["clients", "enrollments"], list(group))
        except QueryError:
            continue  # too many cells
        for row in out["rows"]:
            assert row.get("suppressed") or row["clients"] >= 11, (group, row)
            if row.get("suppressed"):
                assert set(row) == {*group, "suppressed"}


def _everything(engine):
    """Every kind of output the engine can produce, as one string."""
    out = [engine.describe(), engine.list_projects(), engine.bed_utilization()]
    for dim in engine.model.dimensions:
        for cohort in ("active", "entered", "exited"):
            out.append(engine.query(
                ["clients", "enrollments", "households", "avg_length_of_stay_days",
                 "median_length_of_stay_days"], [dim], cohort=cohort))
    return json.dumps(out, default=str)


def test_no_identifiers_in_any_output(engine, export_dir):
    blob = _everything(engine)
    with (export_dir / "Client.csv").open() as f:
        clients = list(csv.DictReader(f))
    forbidden = set(sample.FIRST) | set(sample.LAST) | {"Xavier", "free text"}
    forbidden |= {c["SSN"] for c in clients} | {c["PersonalID"] for c in clients}
    forbidden |= {c["DOB"] for c in clients}
    leaked = [s for s in forbidden if s in blob]
    assert not leaked, leaked[:5]


def test_pii_never_reaches_the_database(model):
    tables = [r[0] for r in model.con.execute("SHOW TABLES").fetchall()]
    assert not [t for t in tables if t.startswith("raw_")]
    for table in tables:
        cols = [r[0].lower() for r in model.con.execute(f'DESCRIBE "{table}"').fetchall()]
        assert not {"firstname", "lastname", "ssn", "personal_id", "personalid"} & set(cols), table
        text = json.dumps(model.con.execute(f'SELECT * FROM "{table}" LIMIT 5000').fetchall(),
                          default=str)
        for name in (*sample.FIRST, *sample.LAST, "free text"):
            assert name not in text, (table, name)


def test_filters(engine):
    all_ = engine.query(["clients"], ["project_type"])
    es = engine.query(["clients"], filters={"project_type": ["Emergency Shelter - Entry/Exit"]})
    want = next(r for r in all_["rows"] if r["project_type"].endswith("Entry/Exit"))["clients"]
    assert es["rows"][0]["clients"] == want


@pytest.mark.parametrize("kwargs", [
    {"measures": ["ssn"]},
    {"measures": []},
    {"measures": ["clients"], "group_by": ["first_name"]},
    {"measures": ["clients"], "group_by": ["e.dob"]},
    {"measures": ["clients"], "group_by": ["project_type) , e.dob AS x --"]},
    {"measures": ["clients"], "filters": {"dob": ["1990-01-01"]}},
    {"measures": ["clients"], "filters": {"project_type": ["nope"]}},
    {"measures": ["clients"], "cohort": "everyone"},
    {"measures": ["clients"], "period_start": "yesterday"},
    {"measures": ["clients"], "period_start": "2026-02-01", "period_end": "2026-01-01"},
    {"measures": ["clients"], "group_by": ["project", "age_group", "sex", "coc"]},
    {"measures": ["clients"], "group_by": ["sex", "sex"]},
])
def test_bad_requests_are_rejected(engine, kwargs):
    with pytest.raises(QueryError):
        engine.query(**kwargs)


def test_filter_values_are_bound_not_interpolated(engine):
    out = engine.query(["clients"], filters={"coc": ["x') OR 1=1 --"]})
    assert out["rows"] == []


def test_narrowing_a_date_window_cannot_isolate_a_few_people(engine, model):
    # Find two period ends whose active populations differ by a handful of clients.
    start = "2025-10-01"
    days = [r[0] for r in model.con.execute("""
        SELECT d::DATE FROM generate_series(DATE '2026-03-01', DATE '2026-03-20', INTERVAL 1 DAY) t(d)
    """).fetchall()]
    first = engine.query(["clients"], period_start=start, period_end=str(days[0]))
    assert "clients" in first["rows"][0]
    blocked = 0
    for day in days[1:]:
        row = engine.query(["clients"], period_start=start, period_end=str(day))["rows"][0]
        blocked += bool(row.get("suppressed"))
    assert blocked, "expected at least one day-by-day step to be refused"


def test_zip_export(tmp_path, export_dir, engine):
    archive = shutil.make_archive(str(tmp_path / "export"), "zip", export_dir)
    zipped = Engine(Model(CsvExportSource(archive)))
    assert zipped.query(["clients"])["rows"] == engine.query(["clients"])["rows"]


def test_missing_required_file(tmp_path, export_dir):
    shutil.copytree(export_dir, tmp_path / "x")
    (tmp_path / "x" / "Client.csv").unlink()
    with pytest.raises(SourceError):
        Model(CsvExportSource(tmp_path / "x"))


def test_bed_utilization(engine):
    out = engine.bed_utilization("2026-01-01", "2026-03-31")
    projects = {r["project"] for r in out["rows"]}
    assert "Sample Street Outreach" not in projects
    assert "Sample Overflow Shelter" in projects  # night-by-night, from bed-night records
    for row in out["rows"]:
        assert row.get("suppressed") or row["clients"] >= 11
    with pytest.raises(QueryError):
        engine.bed_utilization(projects=["No Such Project"])


def test_audit_log(tmp_path, model):
    log = tmp_path / "audit.jsonl"
    e = Engine(model, Policy(11), audit_log=log)
    e.query(["clients"], ["sex"])
    entry = json.loads(log.read_text().splitlines()[0])
    assert entry["tool"] == "query_aggregate" and entry["request"]["group_by"] == ["sex"]
    assert "rows" not in entry
