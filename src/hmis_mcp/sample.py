"""Synthetic HUD HMIS CSV export, for demos and tests. No real people.

Every organization and project name is invented, as is the CoC code XX-500: no state
is abbreviated XX, so the data can't be mistaken for a real community's.

Names and SSNs are filled in on purpose: the tests check that none of them can
be made to come back out.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

from .codes import LEGACY_LIVING_SITUATION

FIRST = ["Zephyrine", "Quillon", "Marisol", "Thaddeus", "Ottoline", "Caspian", "Isolde", "Leopold"]
LAST = ["Vanterpool", "Quackenbush", "Okonkwo", "Featherstone", "Abernathy", "Villanueva"]

PROJECTS = [
    # id, org, name, type, beds
    ("P1", "O1", "Sample Emergency Shelter", 0, 150),
    ("P2", "O1", "Sample Overflow Shelter", 1, 45),
    ("P3", "O1", "Sample Transitional Housing", 2, 25),
    ("P4", "O2", "Sample Rapid Re-Housing", 13, 45),
    ("P5", "O2", "Sample Supportive Housing", 3, 70),
    ("P6", "O2", "Sample Street Outreach", 4, 0),
]
ORGS = {"O1": "Example Shelter Agency", "O2": "Example Housing Agency"}

DESTINATIONS = [410, 435, 422, 423, 312, 313, 101, 116, 207, 206, 30, 99, 8, 24]
DEST_WEIGHTS = [18, 14, 8, 4, 8, 6, 8, 10, 3, 2, 10, 6, 2, 1]
PRIOR = [116, 101, 312, 313, 314, 207, 206, 410, 99]
PRIOR_WEIGHTS = [30, 20, 12, 10, 6, 5, 4, 8, 5]


# FY2024 code -> the FY2022 codes that became it (435 absorbed seven of them).
_TO_LEGACY: dict[int, list[int]] = {}
for _old, _new in LEGACY_LIVING_SITUATION.items():
    _TO_LEGACY.setdefault(_new, []).append(_old)


def _situation(code: int, version: int, n: int) -> int:
    if version >= 2024 or code not in _TO_LEGACY:
        return code
    options = _TO_LEGACY[code]
    return options[n % len(options)]


def _write(directory: Path, name: str, header: list[str], rows: list[dict]) -> None:
    with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, quoting=csv.QUOTE_NONNUMERIC)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in header} for r in rows)


def generate(directory: str | Path, clients: int = 1500, seed: int = 7,
             start: date = date(2025, 10, 1), end: date = date(2026, 9, 30),
             version: int = 2026) -> Path:
    """Write a synthetic export laid out as HUD CSV FY2022, FY2024 or FY2026.

    The same seed describes the same people and stays in every version, so the
    three layouts can be compared with each other.
    """
    if version not in (2022, 2024, 2026):
        raise ValueError("version must be 2022, 2024 or 2026")
    legacy = version == 2022
    rng = random.Random(seed)
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    span = (end - start).days

    _write(out, "Export",
           ["ExportID", "SourceType", "SourceID", "SourceName", "ExportDate", "ExportStartDate",
            "ExportEndDate", "SoftwareName", "CSVVersion", "HashStatus"],
           [{"ExportID": "E1", "SourceType": 1, "SourceID": "XX-500", "SourceName": "Synthetic Continuum of Care",
             "ExportDate": f"{end} 00:00:00", "ExportStartDate": start, "ExportEndDate": end,
             "SoftwareName": "hmis-mcp sample generator",
             "CSVVersion": f"{version} v1.0", "HashStatus": 1}])
    _write(out, "Organization", ["OrganizationID", "OrganizationName", "VictimServiceProvider",
                                 "DateDeleted"],
           [{"OrganizationID": k, "OrganizationName": v, "VictimServiceProvider": 0}
            for k, v in ORGS.items()])
    # FY2022 had a single Emergency Shelter type (1) and a separate tracking method.
    project_rows = []
    for p, o, n, t, _ in PROJECTS:
        row = {"ProjectID": p, "OrganizationID": o, "ProjectName": n, "ProjectType": t,
               "OperatingStartDate": date(2015, 1, 1)}
        if legacy and t in (0, 1):
            row.update(ProjectType=1, TrackingMethod=3 if t == 1 else 0)
        project_rows.append(row)
    _write(out, "Project", ["ProjectID", "OrganizationID", "ProjectName", "OperatingStartDate",
                            "OperatingEndDate", "ProjectType"]
           + (["TrackingMethod"] if legacy else []) + ["DateDeleted"], project_rows)
    _write(out, "Inventory", ["InventoryID", "ProjectID", "CoCCode", "HouseholdType",
                              "BedInventory", "InventoryStartDate", "InventoryEndDate",
                              "DateDeleted"],
           [{"InventoryID": f"I{p}", "ProjectID": p, "CoCCode": "XX-500", "HouseholdType": 1,
             "BedInventory": beds, "InventoryStartDate": date(2015, 1, 1)}
            for p, _, _, _, beds in PROJECTS if beds])

    hisp_col = {2022: "Ethnicity", 2024: "HispanicLatinaeo", 2026: "HispanicLatinao"}[version]
    # The synthetic people use only race and gender values every version can record.
    races = ["AmIndAKNative", "Asian", "BlackAfAmerican", "NativeHIPacific", "White"]
    race_weights = [2, 1, 38, 1, 50]
    genders = ["Woman", "Man", "NonBinary", "Transgender", "Questioning"]
    gender_cols = dict(zip(genders, ["Female", "Male", "NoSingleGender", "Transgender",
                                     "Questioning"] if legacy else genders))
    id_cols = ["PersonalID", "FirstName", "MiddleName", "LastName", "NameSuffix",
               "NameDataQuality", "SSN", "SSNDataQuality", "DOB", "DOBDataQuality"]
    if version == 2022:
        client_header = (id_cols + races + ["RaceNone", "Ethnicity"] + list(gender_cols.values())
                         + ["GenderNone"])
    elif version == 2024:
        client_header = (id_cols + races[:3] + [hisp_col, "MidEastNAfrican"] + races[3:]
                         + ["RaceNone", "AdditionalRaceEthnicity"] + genders[:3]
                         + ["CulturallySpecific"] + genders[3:]
                         + ["DifferentIdentity", "GenderNone", "DifferentIdentityText"])
    else:
        client_header = (id_cols + ["Sex"] + races[:3] + [hisp_col, "MidEastNAfrican"]
                         + races[3:] + ["RaceNone", "AdditionalRaceEthnicity"])
    client_header += ["VeteranStatus", "DateDeleted"]
    coc_rows = []
    dv_col = "DomesticViolenceVictim" if legacy else "DomesticViolenceSurvivor"
    client_rows, enroll_rows, exit_rows, income_rows, dv_rows, service_rows = [], [], [], [], [], []
    eid = 0
    for i in range(clients):
        pid = f"C{i:06d}"
        age = rng.choices([3, 9, 15, 21, 30, 40, 50, 60, 70], [5, 6, 4, 10, 20, 20, 18, 12, 5])[0]
        dob = start - timedelta(days=age * 365 + rng.randrange(365))
        row = {
            "PersonalID": pid, "FirstName": rng.choice(FIRST), "LastName": rng.choice(LAST),
            "MiddleName": "Xavier", "NameDataQuality": 1,
            "SSN": f"{rng.randrange(900000000, 999999999)}", "SSNDataQuality": 1,
            "DOB": dob, "DOBDataQuality": 1 if rng.random() > 0.03 else 99,
            "VeteranStatus": (1 if rng.random() < 0.08 else 0) if age >= 18 else 99,
            "AdditionalRaceEthnicity": "free text that must never surface",
            hisp_col: int(rng.random() < 0.09),
        }
        picked = set(rng.choices(races, race_weights, k=1 if rng.random() > 0.05 else 2))
        if rng.random() < 0.03:
            picked = set()
        for r in races:
            row[r] = int(r in picked)
        row["RaceNone"] = 99 if not picked and not row[hisp_col] else ""
        # Draw both every time so each version consumes the same random numbers.
        sex = rng.choices([0, 1, 99], [42, 55, 3])[0]
        g = rng.choices(genders, [41, 53, 3, 2, 1])[0]
        if version == 2026:
            row["Sex"] = sex
        else:
            for name, col in gender_cols.items():
                row[col] = int(name == g)
        if version >= 2024:
            row["MidEastNAfrican"] = 0
        if version == 2024:
            row["CulturallySpecific"] = row["DifferentIdentity"] = 0
        client_rows.append(row)

        for _ in range(rng.choices([1, 2, 3], [70, 22, 8])[0]):
            eid += 1
            project, _, _, ptype, _ = rng.choices(PROJECTS, [40, 15, 8, 14, 8, 15])[0]
            entry = start + timedelta(days=rng.randrange(-120, span))
            stay = int(rng.expovariate(1 / (400 if ptype == 3 else 55))) + 1
            exit_date = entry + timedelta(days=stay)
            enroll_rows.append({
                "EnrollmentID": f"N{eid}", "PersonalID": pid, "ProjectID": project,
                "EntryDate": entry, "HouseholdID": f"H{eid}", "RelationshipToHoH": 1,
                "EnrollmentCoC": "" if legacy else "XX-500",
                "LivingSituation": _situation(rng.choices(PRIOR, PRIOR_WEIGHTS)[0], version, eid),
                "DisablingCondition": rng.choices([0, 1, 99], [50, 44, 6])[0],
                "MoveInDate": entry + timedelta(days=rng.randrange(5, 40)) if ptype in (3, 13) else "",
            })
            if legacy:
                coc_rows.append({"EnrollmentCoCID": f"L{eid}", "EnrollmentID": f"N{eid}",
                                 "PersonalID": pid, "InformationDate": entry,
                                 "CoCCode": "XX-500", "DataCollectionStage": 1})
            if exit_date <= end:
                exit_rows.append({"ExitID": f"X{eid}", "EnrollmentID": f"N{eid}",
                                  "PersonalID": pid, "ExitDate": exit_date,
                                  "Destination": _situation(
                                      rng.choices(DESTINATIONS, DEST_WEIGHTS)[0], version, eid),
                                  "OtherDestination": "free text destination"})
            has_income = rng.random() < 0.4
            income_rows.append({
                "IncomeBenefitsID": f"B{eid}", "EnrollmentID": f"N{eid}", "PersonalID": pid,
                "InformationDate": entry, "IncomeFromAnySource": int(has_income),
                "TotalMonthlyIncome": f"{rng.randrange(200, 2600)}.00" if has_income else "0.00",
                "DataCollectionStage": 1})
            dv_rows.append({"HealthAndDVID": f"D{eid}", "EnrollmentID": f"N{eid}",
                            "PersonalID": pid, "InformationDate": entry,
                            dv_col: rng.choices([0, 1, 99], [70, 22, 8])[0],
                            "DataCollectionStage": 1})
            if ptype == 1:
                for n in range(min(stay, 30)):
                    night = entry + timedelta(days=n)
                    if start <= night <= end and rng.random() < 0.8:
                        service_rows.append({
                            "ServicesID": f"S{eid}-{n}", "EnrollmentID": f"N{eid}",
                            "PersonalID": pid, "DateProvided": night, "RecordType": 200,
                            "TypeProvided": 200})

    _write(out, "Client", client_header, client_rows)
    _write(out, "Enrollment", ["EnrollmentID", "PersonalID", "ProjectID", "EntryDate",
                               "HouseholdID", "RelationshipToHoH"]
           + ([] if legacy else ["EnrollmentCoC"])
           + ["LivingSituation", "DisablingCondition", "MoveInDate", "DateDeleted"], enroll_rows)
    if legacy:
        _write(out, "EnrollmentCoC", ["EnrollmentCoCID", "EnrollmentID", "PersonalID",
                                      "InformationDate", "CoCCode", "DataCollectionStage",
                                      "DateDeleted"], coc_rows)
    _write(out, "Exit", ["ExitID", "EnrollmentID", "PersonalID", "ExitDate", "Destination",
                         "OtherDestination", "DateDeleted"], exit_rows)
    _write(out, "IncomeBenefits", ["IncomeBenefitsID", "EnrollmentID", "PersonalID",
                                   "InformationDate", "IncomeFromAnySource", "TotalMonthlyIncome",
                                   "DataCollectionStage", "DateDeleted"], income_rows)
    _write(out, "HealthAndDV", ["HealthAndDVID", "EnrollmentID", "PersonalID", "InformationDate",
                                dv_col, "DataCollectionStage", "DateDeleted"],
           dv_rows)
    _write(out, "Services", ["ServicesID", "EnrollmentID", "PersonalID", "DateProvided",
                             "RecordType", "TypeProvided", "DateDeleted"], service_rows)
    return out
