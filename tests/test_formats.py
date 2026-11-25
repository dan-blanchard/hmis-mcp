"""One population written in each supported HUD CSV revision must give the same answers."""

import csv
import shutil

import pytest

from hmis_mcp import sample
from hmis_mcp.engine import Engine
from hmis_mcp.model import Model
from hmis_mcp.source import CsvExportSource, SourceError, detect_format

VERSIONS = (2022, 2024, 2026)
# Dimensions whose meaning is the same in every revision.
SHARED = ["project", "project_type", "coc", "age_group", "race_ethnicity", "veteran_status",
          "household_type", "disabling_condition", "dv_survivor", "prior_living_situation",
          "prior_living_situation_detail", "destination", "destination_detail",
          "length_of_stay", "income_at_entry", "entry_quarter", "exit_quarter"]


@pytest.fixture(scope="module")
def exports(tmp_path_factory):
    root = tmp_path_factory.mktemp("formats")
    return {v: sample.generate(root / str(v), clients=900, version=v) for v in VERSIONS}


@pytest.fixture(scope="module")
def models(exports):
    return {v: Model(CsvExportSource(path)) for v, path in exports.items()}


def header(path, name):
    with (path / f"{name}.csv").open() as f:
        return next(csv.reader(f))


def test_the_fixtures_really_differ(exports):
    """Guard against comparing three copies of the same layout."""
    c22, c24, c26 = (header(exports[v], "Client") for v in VERSIONS)
    assert {"Ethnicity", "Female", "NoSingleGender"} <= set(c22) and "Woman" not in c22
    assert {"HispanicLatinaeo", "Woman", "NonBinary"} <= set(c24) and "Sex" not in c24
    assert {"HispanicLatinao", "Sex"} <= set(c26) and "Woman" not in c26
    assert "TrackingMethod" in header(exports[2022], "Project")
    assert "TrackingMethod" not in header(exports[2024], "Project")
    assert "EnrollmentCoC" not in header(exports[2022], "Enrollment")
    assert (exports[2022] / "EnrollmentCoC.csv").exists()
    assert "DomesticViolenceVictim" in header(exports[2022], "HealthAndDV")

    def codes(v, name, col):
        with (exports[v] / f"{name}.csv").open() as f:
            return {int(r[col]) for r in csv.DictReader(f)}
    assert max(codes(2022, "Exit", "Destination")) < 100
    assert max(codes(2024, "Exit", "Destination")) > 400
    assert codes(2022, "Project", "ProjectType") >= {1} and 0 not in codes(2022, "Project", "ProjectType")
    assert {0, 1} <= codes(2024, "Project", "ProjectType")


def test_detection(exports, models):
    for v in VERSIONS:
        assert detect_format(header(exports[v], "Client")) == v
        assert models[v].csv_format == v
        desc = Engine(models[v]).describe()
        assert desc["source"]["csv_format"] == f"FY{v}" and "warnings" not in desc["source"]


@pytest.mark.parametrize("dim", SHARED)
def test_same_answers_in_every_revision(models, dim):
    results = {v: Engine(models[v]).query(
        ["clients", "enrollments", "median_length_of_stay_days"], [dim])["rows"]
        for v in VERSIONS}
    assert results[2022] == results[2024] == results[2026]
    assert any(not r.get("suppressed") for r in results[2026])


def test_two_way_breakdown_and_bed_nights_agree(models):
    for run in (
        lambda e: e.query(["clients"], ["project_type", "destination"], cohort="exited"),
        lambda e: e.bed_utilization("2026-01-01", "2026-04-30"),
        lambda e: e.list_projects(),
    ):
        a, b, c = (run(Engine(models[v])) for v in VERSIONS)
        assert a == b == c


def test_fy2022_emergency_shelters_split_by_tracking_method(models):
    types = {p["project"]: p["project_type"] for p in Engine(models[2022]).list_projects()}
    assert types["Sample Emergency Shelter"] == "Emergency Shelter - Entry/Exit"
    assert types["Sample Overflow Shelter"] == "Emergency Shelter - Night-by-Night"


def test_fy2022_destinations_land_in_the_right_categories(models):
    rows = Engine(models[2022]).query(["clients"], ["destination_detail"], cohort="exited")["rows"]
    labels = {r["destination_detail"] for r in rows}
    assert "Rental by client, with ongoing subsidy" in labels
    assert not [l for l in labels if l.startswith("Code ")], labels
    cats = Engine(models[2022]).query(["clients"], ["destination"], cohort="exited")["rows"]
    permanent = next(r for r in cats if r["destination"] == "Permanent situation")
    assert permanent["clients"] > 100


def test_gender_and_sex_follow_the_revision(models):
    assert "sex" in models[2026].dimensions and "gender" not in models[2026].dimensions
    for v in (2022, 2024):
        assert "gender" in models[v].dimensions and "sex" not in models[v].dimensions
    old, new = (Engine(models[v]).query(["clients"], ["gender"])["rows"] for v in (2022, 2024))
    assert old == new  # FY2022 Female/Male/NoSingleGender read as Woman/Man/Non-binary
    assert {"Woman", "Man"} <= {r["gender"] for r in new if not r.get("suppressed")}


def test_hispanic_ethnicity_is_read_from_all_three_column_names(models):
    for v in VERSIONS:
        rows = Engine(models[v]).query(["clients"], ["race_ethnicity"])["rows"]
        assert any("& Hispanic" in r["race_ethnicity"] and not r.get("suppressed") for r in rows)


def _rewrite_header(path, name, rename):
    file = path / f"{name}.csv"
    lines = file.read_text().splitlines(keepends=True)
    for old, new in rename.items():
        lines[0] = lines[0].replace(f'"{old}"', f'"{new}"')
    file.write_text("".join(lines))


def test_early_fy2022_column_names(tmp_path, exports, models):
    """FY2022 v1.0 said GenderOther and NativeHIOtherPacific before they were renamed."""
    shutil.copytree(exports[2022], tmp_path / "x")
    _rewrite_header(tmp_path / "x", "Client", {"NoSingleGender": "GenderOther",
                                               "NativeHIPacific": "NativeHIOtherPacific"})
    early = Engine(Model(CsvExportSource(tmp_path / "x")))
    for dim in ("gender", "race_ethnicity"):
        assert early.query(["clients"], [dim]) == Engine(models[2022]).query(["clients"], [dim])


def test_older_and_unrecognized_layouts_are_refused(tmp_path, exports):
    shutil.copytree(exports[2022], tmp_path / "fy20")
    for col in ("Male", "NoSingleGender", "Transgender", "Questioning"):
        _rewrite_header(tmp_path / "fy20", "Client", {col: f"Unused{col}"})
    _rewrite_header(tmp_path / "fy20", "Client", {"Female": "Gender"})
    with pytest.raises(SourceError, match="FY2020"):
        Model(CsvExportSource(tmp_path / "fy20"))

    shutil.copytree(exports[2026], tmp_path / "odd")
    _rewrite_header(tmp_path / "odd", "Client", {"Sex": "Mystery"})
    with pytest.raises(SourceError, match="csv-version"):
        Model(CsvExportSource(tmp_path / "odd"))
    # Saying which revision it is gets past that, and is reported.
    forced = Engine(Model(CsvExportSource(tmp_path / "odd", csv_format=2026))).describe()
    assert forced["source"]["csv_format"] == "FY2026" and forced["source"]["warnings"]


def test_a_wrong_csvversion_label_is_flagged_not_believed(tmp_path, exports):
    shutil.copytree(exports[2024], tmp_path / "x")
    export = tmp_path / "x" / "Export.csv"
    export.write_text(export.read_text().replace("2024 v1.0", "2022 v1.3"))
    desc = Engine(Model(CsvExportSource(tmp_path / "x"))).describe()
    assert desc["source"]["csv_format"] == "FY2024"
    assert "2022 v1.3" in desc["source"]["warnings"][0]
