"""Builds the analytic model from raw_* tables and defines the query vocabulary.

One fact table, `enr`, with a row per enrollment. Dimensions are named SQL
expressions over it. Callers pick dimensions by name; they never supply SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import duckdb

from . import codes
from .source import DataSource

MISSING = "('8', '9', '99')"


@dataclass(frozen=True)
class Dimension:
    name: str
    description: str
    sql: str  # expression over alias e; may reference $period_start / $period_end
    values: tuple[str, ...] | None = None  # fixed vocabulary, when there is one


@dataclass(frozen=True)
class ExportInfo:
    source_name: str | None
    software: str | None
    csv_version: str | None
    start: date
    end: date
    hashed: bool
    csv_format: int = 2026
    warnings: tuple[str, ...] = ()


def _case(col: str, mapping: dict[int, str], default: str | None = None) -> str:
    whens = " ".join(f"WHEN {k} THEN '{v.replace(chr(39), chr(39) * 2)}'" for k, v in mapping.items())
    fallback = default if default is not None else f"'Code ' || {col}"
    return f"CASE TRY_CAST({col} AS INTEGER) {whens} ELSE {fallback} END"


def _buckets(expr: str, buckets: list[tuple[int, int, str]]) -> str:
    whens = " ".join(f"WHEN {expr} BETWEEN {lo} AND {hi} THEN '{label}'" for lo, hi, label in buckets)
    return f"CASE WHEN {expr} IS NULL THEN '{codes.UNKNOWN}' {whens} ELSE '{codes.UNKNOWN}' END"


def _situation_category(col: str) -> str:
    c = f"TRY_CAST({col} AS INTEGER)"
    return f"""CASE
        WHEN {c} BETWEEN 100 AND 199 THEN 'Homeless situation'
        WHEN {c} BETWEEN 200 AND 299 THEN 'Institutional situation'
        WHEN {c} BETWEEN 300 AND 399 THEN 'Temporary situation'
        WHEN {c} BETWEEN 400 AND 499 THEN 'Permanent situation'
        WHEN {c} = 24 THEN 'Deceased'
        WHEN {c} = 17 THEN 'Other'
        ELSE 'Unknown' END"""


def _modern_situation(col: str, csv_format: int) -> str:
    """A living situation or destination column as FY2024+ codes."""
    if csv_format >= 2024:
        return col
    whens = " ".join(f"WHEN {old} THEN '{new}'" for old, new in
                     codes.LEGACY_LIVING_SITUATION.items())
    return f"CASE TRY_CAST({col} AS INTEGER) {whens} ELSE {col} END"


def _modern_project_type(csv_format: int) -> str:
    if csv_format >= 2024:
        return 'p."ProjectType"'
    return f"""CASE WHEN TRY_CAST(p."ProjectType" AS INTEGER) = {codes.LEGACY_ES_PROJECT_TYPE}
                 THEN (CASE WHEN TRY_CAST(p."TrackingMethod" AS INTEGER) = {codes.LEGACY_TRACKING_NBN}
                            THEN '1' ELSE '0' END)
                 ELSE p."ProjectType" END"""


def _yes_no(col: str) -> str:
    return f"CASE {col} WHEN '1' THEN 'Yes' WHEN '0' THEN 'No' ELSE '{codes.UNKNOWN}' END"


def _not_deleted(alias: str = "") -> str:
    col = f'{alias}"DateDeleted"'
    return f"({col} IS NULL OR trim({col}) = '')"


def _race_sql() -> str:
    flags = [f"(c.\"{col}\" = '1')::INT" for col in codes.RACE_COLUMNS]
    n = " + ".join(f"coalesce({f}, 0)" for f in flags)
    single = " ".join(
        f"WHEN c.\"{col}\" = '1' THEN '{label}'" for col, label in codes.RACE_COLUMNS.items()
    )
    h = codes.HISPANIC_LABEL
    return f"""CASE
        WHEN ({n}) = 0 AND c."Hispanic" = '1' THEN '{h}'
        WHEN ({n}) = 0 THEN '{codes.UNKNOWN}'
        WHEN ({n}) = 1 AND c."Hispanic" = '1' THEN (CASE {single} END) || ' & {h}'
        WHEN ({n}) = 1 THEN (CASE {single} END)
        WHEN c."Hispanic" = '1' THEN 'Multi-racial & {h}'
        ELSE 'Multi-racial (not {h})' END"""


RACE_VALUES = tuple(
    [*codes.RACE_COLUMNS.values(), codes.HISPANIC_LABEL]
    + [f"{v} & {codes.HISPANIC_LABEL}" for v in codes.RACE_COLUMNS.values()]
    + [f"Multi-racial & {codes.HISPANIC_LABEL}", f"Multi-racial (not {codes.HISPANIC_LABEL})",
       codes.UNKNOWN]
)


def _gender_sql() -> str:
    n = " + ".join(f"coalesce((c.\"{col}\" = '1')::INT, 0)" for col in codes.GENDER_COLUMNS)
    single = " ".join(
        f"WHEN c.\"{col}\" = '1' THEN '{label}'" for col, label in codes.GENDER_COLUMNS.items()
    )
    return f"""CASE WHEN ({n}) = 0 THEN '{codes.UNKNOWN}'
        WHEN ({n}) = 1 THEN (CASE {single} END) ELSE 'Multiple gender identities' END"""


class Model:
    def __init__(self, source: DataSource):
        self.con = duckdb.connect(":memory:")
        source.load(self.con)
        self.source_description = source.describe()
        self.csv_format, self._detected_format = self.con.execute(
            "SELECT csv_format, detected_format FROM raw_Meta").fetchone()
        self._has_sex = self._any("raw_Client", "Sex")
        self._has_gender = any(self._any("raw_Client", c) for c in codes.GENDER_COLUMNS)
        self._build()
        self.export = self._export_info()
        self.client_count = self.con.execute("SELECT count(*) FROM client").fetchone()[0]
        self.dimensions = {d.name: d for d in self._dimensions()}
        # From here on the connection only needs to read.
        for table in self.con.execute("SHOW TABLES").fetchall():
            if table[0].startswith("raw_"):
                self.con.execute(f'DROP TABLE "{table[0]}"')

    def _any(self, table: str, col: str) -> bool:
        return bool(self.con.execute(
            f'SELECT count(*) FROM {table} WHERE "{col}" IS NOT NULL AND trim("{col}") <> \'\''
        ).fetchone()[0])

    def _build(self) -> None:
        con = self.con
        sex = _case('c."Sex"', {0: "Female", 1: "Male"}, f"'{codes.UNKNOWN}'")
        con.execute(f"""
            CREATE TABLE client AS
            SELECT c."PersonalID" AS personal_id,
                   (row_number() OVER (ORDER BY c."PersonalID") - 1)::INTEGER AS cidx,
                   CASE WHEN c."DOBDataQuality" IN {MISSING} THEN NULL
                        ELSE TRY_CAST(c."DOB" AS DATE) END AS dob,
                   {_race_sql()} AS race_ethnicity,
                   {_gender_sql()} AS gender,
                   {sex} AS sex,
                   {_yes_no('c."VeteranStatus"')} AS veteran_status
            FROM raw_Client c WHERE {_not_deleted()}
        """)
        con.execute(f"""
            CREATE TABLE project AS
            SELECT p.* EXCLUDE (ptype), TRY_CAST(ptype AS INTEGER) AS project_type_code,
                   {_case('ptype', codes.PROJECT_TYPE)} AS project_type
            FROM (
            SELECT p."ProjectID" AS project_id, p."ProjectName" AS project_name,
                   {_modern_project_type(self.csv_format)} AS ptype,
                   coalesce(o."OrganizationName", '{codes.UNKNOWN}') AS organization,
                   TRY_CAST(p."OperatingStartDate" AS DATE) AS operating_start,
                   TRY_CAST(p."OperatingEndDate" AS DATE) AS operating_end
            FROM raw_Project p
            LEFT JOIN (SELECT * FROM raw_Organization WHERE {_not_deleted()}) o
              ON o."OrganizationID" = p."OrganizationID"
            WHERE {_not_deleted('p.')}
            ) p
        """)
        con.execute(f"""
            CREATE TABLE inventory AS
            SELECT "ProjectID" AS project_id, TRY_CAST("BedInventory" AS INTEGER) AS beds,
                   TRY_CAST("InventoryStartDate" AS DATE) AS start_date,
                   TRY_CAST("InventoryEndDate" AS DATE) AS end_date
            FROM raw_Inventory WHERE {_not_deleted()}
        """)
        # One exit per enrollment; if an export carries duplicates keep the latest.
        con.execute(f"""
            CREATE TEMP TABLE ex AS
            SELECT "EnrollmentID" AS enrollment_id, TRY_CAST("ExitDate" AS DATE) AS exit_date,
                   {_modern_situation('"Destination"', self.csv_format)} AS destination
            FROM raw_Exit WHERE {_not_deleted()}
            QUALIFY row_number() OVER (PARTITION BY "EnrollmentID" ORDER BY "ExitDate" DESC) = 1
        """)
        con.execute(f"""
            CREATE TEMP TABLE inc AS
            SELECT "EnrollmentID" AS enrollment_id,
                   CASE WHEN "IncomeFromAnySource" = '0' THEN 0
                        WHEN "IncomeFromAnySource" = '1' THEN TRY_CAST("TotalMonthlyIncome" AS DOUBLE)
                   END AS income
            FROM raw_IncomeBenefits WHERE {_not_deleted()} AND "DataCollectionStage" = '1'
            QUALIFY row_number() OVER (PARTITION BY "EnrollmentID" ORDER BY "InformationDate") = 1
        """)
        con.execute(f"""
            CREATE TEMP TABLE dv AS
            SELECT "EnrollmentID" AS enrollment_id, "DomesticViolenceSurvivor" AS survivor
            FROM raw_HealthAndDV WHERE {_not_deleted()} AND "DataCollectionStage" = '1'
            QUALIFY row_number() OVER (PARTITION BY "EnrollmentID" ORDER BY "InformationDate") = 1
        """)
        # FY2022 kept the CoC in its own file; take the record made at project entry.
        con.execute(f"""
            CREATE TEMP TABLE entry_coc AS
            SELECT "EnrollmentID" AS enrollment_id, "CoCCode" AS coc
            FROM raw_EnrollmentCoC WHERE {_not_deleted()}
            QUALIFY row_number() OVER (PARTITION BY "EnrollmentID"
                ORDER BY ("DataCollectionStage" = '1') DESC NULLS LAST, "InformationDate") = 1
        """)
        con.execute(f"""
            CREATE TABLE enr AS
            WITH base AS (
                SELECT n."EnrollmentID" AS enrollment_id, c.cidx, c.dob,
                       coalesce(nullif(trim(n."HouseholdID"), ''), 'solo-' || n."EnrollmentID")
                           AS household_id,
                       n."ProjectID" AS project_id,
                       TRY_CAST(n."EntryDate" AS DATE) AS entry_date,
                       TRY_CAST(n."MoveInDate" AS DATE) AS move_in_date,
                       n."RelationshipToHoH" AS relationship,
                       {_modern_situation('n."LivingSituation"', self.csv_format)}
                           AS living_situation,
                       n."DisablingCondition" AS disabling,
                       coalesce(nullif(trim(n."EnrollmentCoC"), ''), ec.coc) AS coc,
                       c.race_ethnicity, c.gender, c.sex, c.veteran_status
                FROM raw_Enrollment n JOIN client c ON c.personal_id = n."PersonalID"
                LEFT JOIN entry_coc ec ON ec.enrollment_id = n."EnrollmentID"
                WHERE {_not_deleted('n.')}
            ), hh AS (
                SELECT household_id,
                       bool_or(date_sub('year', dob, entry_date) >= 18) AS has_adult,
                       bool_or(date_sub('year', dob, entry_date) < 18) AS has_child,
                       bool_or(dob IS NULL) AS has_unknown
                FROM base GROUP BY household_id
            )
            SELECT b.*, ex.exit_date, ex.destination, inc.income, dv.survivor,
                   p.project_name, p.project_type, p.project_type_code, p.organization,
                   CASE WHEN hh.has_adult AND hh.has_child THEN 'Adults and children'
                        WHEN hh.has_unknown THEN '{codes.UNKNOWN}'
                        WHEN hh.has_adult THEN 'Adults only'
                        WHEN hh.has_child THEN 'Children only'
                        ELSE '{codes.UNKNOWN}' END AS household_type
            FROM base b
            JOIN project p USING (project_id)
            JOIN hh USING (household_id)
            LEFT JOIN ex USING (enrollment_id)
            LEFT JOIN inc USING (enrollment_id)
            LEFT JOIN dv USING (enrollment_id)
            WHERE b.entry_date IS NOT NULL
        """)
        con.execute(f"""
            CREATE TABLE bed_night AS
            SELECT e.cidx, e.project_id, TRY_CAST(s."DateProvided" AS DATE) AS night
            FROM raw_Services s JOIN enr e ON e.enrollment_id = s."EnrollmentID"
            WHERE s."RecordType" = '200' AND {_not_deleted('s.')}
        """)
        # Nothing downstream needs the source's own client identifier.
        con.execute("ALTER TABLE client DROP COLUMN personal_id")

    def _export_info(self) -> ExportInfo:
        row = self.con.execute("""
            SELECT "SourceName", "SoftwareName", "CSVVersion",
                   TRY_CAST("ExportStartDate" AS DATE), TRY_CAST("ExportEndDate" AS DATE),
                   "HashStatus" FROM raw_Export LIMIT 1
        """).fetchone() or (None,) * 6
        lo, hi = self.con.execute(
            "SELECT min(entry_date), max(greatest(entry_date, coalesce(exit_date, entry_date))) FROM enr"
        ).fetchone()
        today = date.today()
        warnings = []
        if self._detected_format is None:
            warnings.append(f"The export's layout could not be identified; it was read as "
                            f"FY{self.csv_format} because that was requested.")
        elif self._detected_format != self.csv_format:
            warnings.append(f"The export's columns look like FY{self._detected_format}, but it "
                            f"was read as FY{self.csv_format} because that was requested.")
        stated = re.search(r"20\d\d", row[2] or "")
        if stated and int(stated.group()) != self.csv_format:
            warnings.append(f"Export.csv says CSVVersion {row[2]!r}, but the columns are laid "
                            f"out as FY{self.csv_format}. The columns were believed.")
        return ExportInfo(row[0], row[1], row[2], row[3] or lo or today, row[4] or hi or today,
                          hashed=row[5] not in (None, "", "1"),
                          csv_format=self.csv_format, warnings=tuple(warnings))

    def _dimensions(self) -> list[Dimension]:
        age = "date_sub('year', e.dob, greatest(e.entry_date, $period_start::DATE))"
        # An exit after the period end hasn't happened yet as far as the period is concerned.
        exit_ = "(CASE WHEN e.exit_date <= $period_end::DATE THEN e.exit_date END)"
        income = (
            f"CASE WHEN e.income = 0 THEN 'No income' ELSE "
            f"{_buckets('ceil(e.income)::BIGINT', codes.INCOME_BUCKETS)} END"
        )
        project_names = tuple(r[0] for r in self.con.execute(
            "SELECT DISTINCT project_name FROM project ORDER BY 1").fetchall())
        orgs = tuple(r[0] for r in self.con.execute(
            "SELECT DISTINCT organization FROM project ORDER BY 1").fetchall())
        dims = [
            Dimension("project", "Project name", "e.project_name", project_names),
            Dimension("organization", "Organization operating the project", "e.organization", orgs),
            Dimension("project_type", "HUD project type", "e.project_type",
                      tuple(codes.PROJECT_TYPE.values())),
            Dimension("coc", "CoC code on the enrollment: two-letter state, dash, three digits",
                      f"coalesce(nullif(trim(e.coc), ''), '{codes.UNKNOWN}')"),
            Dimension("age_group", "Age at the later of project entry or period start",
                      _buckets(age, codes.AGE_GROUPS),
                      tuple(b[2] for b in codes.AGE_GROUPS) + (codes.UNKNOWN,)),
            Dimension("race_ethnicity", "HUD race and ethnicity, rolled up", "e.race_ethnicity",
                      RACE_VALUES),
            Dimension("veteran_status", "Veteran status", "e.veteran_status", tuple(codes.YES_NO)),
            Dimension("household_type", "Household composition by age at entry", "e.household_type",
                      ("Adults only", "Adults and children", "Children only", codes.UNKNOWN)),
            Dimension("relationship_to_hoh", "Relationship to head of household",
                      _case("e.relationship", codes.RELATIONSHIP_TO_HOH, f"'{codes.UNKNOWN}'"),
                      tuple(codes.RELATIONSHIP_TO_HOH.values()) + (codes.UNKNOWN,)),
            Dimension("disabling_condition", "Disabling condition reported at entry",
                      _yes_no("e.disabling"), tuple(codes.YES_NO)),
            Dimension("dv_survivor", "Domestic violence survivor, as reported at entry",
                      _yes_no("e.survivor"), tuple(codes.YES_NO)),
            Dimension("prior_living_situation", "Category of living situation before entry",
                      _situation_category("e.living_situation"), tuple(codes.SITUATION_CATEGORIES)),
            Dimension("prior_living_situation_detail", "Specific living situation before entry",
                      f"CASE WHEN e.living_situation IS NULL THEN '{codes.UNKNOWN}' ELSE "
                      f"{_case('e.living_situation', codes.LIVING_SITUATION)} END"),
            Dimension("destination", "Category of exit destination; 'Still enrolled' if no exit "
                      "by period end",
                      f"CASE WHEN {exit_} IS NULL THEN 'Still enrolled' ELSE "
                      f"{_situation_category('e.destination')} END",
                      ("Still enrolled", *codes.SITUATION_CATEGORIES)),
            Dimension("destination_detail", "Specific exit destination",
                      f"CASE WHEN {exit_} IS NULL THEN 'Still enrolled' "
                      f"WHEN e.destination IS NULL THEN '{codes.UNKNOWN}' ELSE "
                      f"{_case('e.destination', codes.LIVING_SITUATION)} END"),
            Dimension("length_of_stay", "Days from entry to exit, or to period end if still enrolled",
                      _buckets("los_days", codes.LOS_BUCKETS),
                      tuple(b[2] for b in codes.LOS_BUCKETS) + (codes.UNKNOWN,)),
            Dimension("income_at_entry", "Total monthly income recorded at entry", income,
                      ("No income", *(b[2] for b in codes.INCOME_BUCKETS), codes.UNKNOWN)),
            Dimension("entry_year", "Year of project entry (YYYY)", "strftime(e.entry_date, '%Y')"),
            Dimension("entry_quarter", "Quarter of project entry (YYYY-Qn)",
                      "strftime(e.entry_date, '%Y') || '-Q' || quarter(e.entry_date)"),
            Dimension("entry_month", "Month of project entry (YYYY-MM)",
                      "strftime(e.entry_date, '%Y-%m')"),
            Dimension("exit_year", "Year of exit (YYYY); 'Still enrolled' if none",
                      f"coalesce(strftime({exit_}, '%Y'), 'Still enrolled')"),
            Dimension("exit_quarter", "Quarter of exit (YYYY-Qn); 'Still enrolled' if none",
                      f"coalesce(strftime({exit_}, '%Y') || '-Q' || quarter({exit_}), 'Still enrolled')"),
            Dimension("exit_month", "Month of exit (YYYY-MM); 'Still enrolled' if none",
                      f"coalesce(strftime({exit_}, '%Y-%m'), 'Still enrolled')"),
        ]
        # FY2026 exports carry Sex; earlier ones carry Gender. Offer what the data has.
        if self._has_sex:
            dims.append(Dimension("sex", "Sex (HUD element 4.21)", "e.sex",
                                  ("Female", "Male", codes.UNKNOWN)))
        if self._has_gender:
            dims.append(Dimension("gender", "Gender (HUD element 3.06; FY2022 and FY2024 exports)", "e.gender",
                                  (*codes.GENDER_COLUMNS.values(), "Multiple gender identities",
                                   codes.UNKNOWN)))
        return dims
