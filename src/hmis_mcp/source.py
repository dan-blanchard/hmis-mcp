"""Data sources.

A source's whole job is to populate `raw_*` tables in a DuckDB connection using
HUD HMIS CSV column names. Everything downstream (the fact table, dimensions,
disclosure control) is source-agnostic, so a live-API backend only has to
implement `DataSource.load`.

Columns are allowlisted per table. Names, SSNs and free-text fields are not on
any list, so they never enter the database no matter what the source holds.

Three revisions of the HUD CSV format are accepted: FY2022, FY2024 and FY2026.
Columns that were only renamed between revisions are folded together here.
Changes of meaning (project type and living situation codes) are translated in
model.py, which is told the revision through the raw_Meta table.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Protocol

import duckdb

from .codes import GENDER_COLUMNS, RACE_COLUMNS

# table -> (required columns, optional columns)
ALLOWED_COLUMNS: dict[str, tuple[list[str], list[str]]] = {
    "Export": ([], ["ExportID", "SourceName", "SourceID", "ExportStartDate", "ExportEndDate",
                    "ExportDate", "CSVVersion", "SoftwareName", "HashStatus"]),
    "Organization": (["OrganizationID", "OrganizationName"], ["DateDeleted"]),
    "Project": (["ProjectID", "OrganizationID", "ProjectName", "ProjectType"],
                ["OperatingStartDate", "OperatingEndDate", "TrackingMethod", "DateDeleted"]),
    "Inventory": (["ProjectID", "BedInventory"],
                  ["InventoryID", "CoCCode", "HouseholdType", "UnitInventory",
                   "InventoryStartDate", "InventoryEndDate", "DateDeleted"]),
    "Client": (["PersonalID"],
               ["DOB", "DOBDataQuality", "VeteranStatus", "Sex", "Hispanic", "RaceNone",
                "GenderNone", "DateDeleted", *RACE_COLUMNS, *GENDER_COLUMNS]),
    "Enrollment": (["EnrollmentID", "PersonalID", "ProjectID", "EntryDate"],
                   ["HouseholdID", "RelationshipToHoH", "EnrollmentCoC", "LivingSituation",
                    "DisablingCondition", "MoveInDate", "DateDeleted"]),
    # FY2022 only: the CoC was a separate file before it moved onto Enrollment.
    "EnrollmentCoC": (["EnrollmentID", "CoCCode"],
                      ["InformationDate", "DataCollectionStage", "DateDeleted"]),
    "Exit": (["EnrollmentID", "ExitDate"], ["Destination", "DateDeleted"]),
    "IncomeBenefits": (["EnrollmentID"],
                       ["InformationDate", "IncomeFromAnySource", "TotalMonthlyIncome",
                        "DataCollectionStage", "DateDeleted"]),
    "HealthAndDV": (["EnrollmentID"],
                    ["InformationDate", "DomesticViolenceSurvivor", "DataCollectionStage",
                     "DateDeleted"]),
    "Services": (["EnrollmentID", "DateProvided", "RecordType"], ["DateDeleted"]),
}
REQUIRED_TABLES = ("Project", "Client", "Enrollment")

# Our column name -> names it has gone by, newest first. The gender renames
# follow HUD Data Lab's FY2022-to-FY2024 conversion (Female -> Woman,
# Male -> Man, NoSingleGender -> NonBinary). Ethnicity was a separate 0/1
# element until FY2024 made Hispanic/Latina/e/o one of the race flags.
ALIASES: dict[str, dict[str, list[str]]] = {
    "Client": {
        "Hispanic": ["HispanicLatinao", "HispanicLatinaeo", "Ethnicity"],
        "NativeHIPacific": ["NativeHIOtherPacific"],
        "Woman": ["Female"],
        "Man": ["Male"],
        "NonBinary": ["NoSingleGender", "GenderOther"],
    },
    "HealthAndDV": {"DomesticViolenceSurvivor": ["DomesticViolenceVictim"]},
}

SUPPORTED_FORMATS = (2022, 2024, 2026)


def detect_format(client_columns: list[str]) -> int | None:
    """Which HUD CSV revision a Client.csv header belongs to.

    Each revision changed how gender or sex is recorded, so that part of the
    header identifies it without trusting the CSVVersion a vendor wrote.
    """
    cols = {c.lower() for c in client_columns}
    if "sex" in cols:
        return 2026
    if "woman" in cols:
        return 2024
    if "female" in cols:
        return 2022
    if "gender" in cols:
        return 2020
    return None


class SourceError(Exception):
    pass


class DataSource(Protocol):
    def load(self, con: duckdb.DuckDBPyConnection) -> None:
        """Create raw_<table> for every key of ALLOWED_COLUMNS (all VARCHAR), plus
        raw_Meta(csv_format, detected_format) naming the HUD CSV revision whose
        code lists the data uses: 2022, 2024 or 2026."""
        ...

    def describe(self) -> str: ...


class CsvExportSource:
    """A HUD HMIS CSV export: a directory of CSV files, or the zip the HMIS produced."""

    def __init__(self, path: str | Path, csv_format: int | None = None):
        """csv_format overrides detection: 2022, 2024 or 2026."""
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise SourceError(f"HMIS export not found: {self.path}")
        if csv_format is not None and csv_format not in SUPPORTED_FORMATS:
            raise SourceError(f"csv_format must be one of {SUPPORTED_FORMATS}")
        self.csv_format = csv_format

    def describe(self) -> str:
        return f"HUD HMIS CSV export at {self.path}"

    def load(self, con: duckdb.DuckDBPyConnection) -> None:
        if self.path.is_dir():
            self._load_dir(con, self.path)
            return
        if not zipfile.is_zipfile(self.path):
            raise SourceError(f"{self.path} is neither a directory nor a zip file")
        wanted = {f"{t}.csv".lower() for t in ALLOWED_COLUMNS}
        with tempfile.TemporaryDirectory(prefix="hmis-mcp-") as tmp, zipfile.ZipFile(self.path) as zf:
            for info in zf.infolist():
                name = Path(info.filename).name
                if name.lower() in wanted and not info.is_dir():
                    # Write under our own filename; never trust a path from the archive.
                    (Path(tmp) / name).write_bytes(zf.read(info))
            self._load_dir(con, Path(tmp))

    def _load_dir(self, con: duckdb.DuckDBPyConnection, directory: Path) -> None:
        files = {p.name.lower(): p for p in directory.iterdir() if p.suffix.lower() == ".csv"}
        for table, (required, optional) in ALLOWED_COLUMNS.items():
            path = files.get(f"{table}.csv".lower())
            if path is None:
                if table in REQUIRED_TABLES:
                    raise SourceError(f"export is missing required file {table}.csv")
                _create_empty(con, table, required + optional)
                continue
            rel = con.read_csv(str(path), header=True, all_varchar=True)
            present = {c.lower(): c for c in rel.columns}
            missing = [c for c in required if c.lower() not in present]
            if missing:
                raise SourceError(f"{table}.csv is missing required columns: {missing}")
            if table == "Client":
                self._record_format(con, rel.columns)
            aliases = ALIASES.get(table, {})
            select = []
            for col in required + optional:
                names = [col, *aliases.get(col, [])]
                actual = next((present[n.lower()] for n in names if n.lower() in present), None)
                select.append(f'"{actual}" AS "{col}"' if actual else f'NULL::VARCHAR AS "{col}"')
            con.execute(
                f"CREATE OR REPLACE TABLE raw_{table} AS SELECT {', '.join(select)} "
                f"FROM read_csv(?, header=true, all_varchar=true)",
                [str(path)],
            )


    def _record_format(self, con: duckdb.DuckDBPyConnection, client_columns: list[str]) -> None:
        detected = detect_format(client_columns)
        chosen = self.csv_format or detected
        if chosen is None:
            raise SourceError(
                "can't tell which HUD CSV revision this export uses: Client.csv has none of "
                "the Sex, Woman or Female columns. Pass --csv-version to say which.")
        if chosen not in SUPPORTED_FORMATS:
            raise SourceError(
                f"this looks like an FY{chosen} export. Supported revisions are "
                + ", ".join(f"FY{v}" for v in SUPPORTED_FORMATS) + ".")
        con.execute("CREATE OR REPLACE TABLE raw_Meta AS SELECT ?::INTEGER AS csv_format, "
                    "?::INTEGER AS detected_format", [chosen, detected])


def _create_empty(con: duckdb.DuckDBPyConnection, table: str, columns: list[str]) -> None:
    cols = ", ".join(f'"{c}" VARCHAR' for c in columns)
    con.execute(f"CREATE OR REPLACE TABLE raw_{table} ({cols})")
