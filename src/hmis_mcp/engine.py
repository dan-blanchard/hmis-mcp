"""Structured aggregate queries.

Callers name measures, dimensions and filters from a fixed vocabulary. This
module builds the SQL, and hands every result to DisclosureControl before
anything is returned. There is no code path from the database to a caller that
skips `_release`.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import codes
from .model import Model
from .privacy import Cell, DisclosureControl, Policy, bitset

MAX_GROUP_BY = 3
MAX_CELLS = 2000

MEASURES: dict[str, tuple[str, str]] = {
    "clients": ("Distinct people", "count(DISTINCT cidx)"),
    "enrollments": ("Project enrollments (a person can have several)", "count(*)"),
    "households": ("Distinct households", "count(DISTINCT household_id)"),
    "avg_length_of_stay_days": (
        "Mean days from entry to exit, or to period end if still enrolled",
        "round(avg(los_days), 1)",
    ),
    "median_length_of_stay_days": ("Median of the same", "median(los_days)"),
}

COHORTS: dict[str, tuple[str, str]] = {
    "active": (
        "Enrolled at any point in the period",
        "entry_date <= $period_end::DATE AND (exit_date IS NULL OR exit_date >= $period_start::DATE)",
    ),
    "entered": (
        "Entered a project during the period",
        "entry_date BETWEEN $period_start::DATE AND $period_end::DATE",
    ),
    "exited": (
        "Exited a project during the period",
        "exit_date BETWEEN $period_start::DATE AND $period_end::DATE",
    ),
}

SUPPRESSION_NOTES = {
    "small_cell": "fewer than {k} clients",
    "differencing": "combined with an earlier answer it would describe fewer than {k} clients",
    "complementary": "withheld so a suppressed cell can't be derived from totals",
}


class QueryError(ValueError):
    """Bad request. The message is safe to show to the caller."""


class Engine:
    def __init__(self, model: Model, policy: Policy | None = None,
                 audit_log: str | Path | None = None):
        self.model = model
        self.policy = policy or Policy()
        self._control = DisclosureControl(self.policy)
        self._audit_path = Path(audit_log).expanduser() if audit_log else None
        self._lock = threading.Lock()

    # -- public operations -------------------------------------------------

    def describe(self) -> dict[str, Any]:
        info = self.model.export
        totals = self.query(["clients", "enrollments", "households"], _audit=False)
        return {
            "source": {
                "name": info.source_name,
                "software": info.software,
                "csv_version": info.csv_version,
                "csv_format": f"FY{info.csv_format}",
                **({"warnings": list(info.warnings)} if info.warnings else {}),
                "export_period": {"start": info.start.isoformat(), "end": info.end.isoformat()},
            },
            "totals_for_export_period": totals["rows"][0] if totals["rows"] else None,
            "privacy": {
                "min_cell_size": self.policy.min_cell_size,
                "rules": "Cells covering fewer clients than min_cell_size are suppressed, as are "
                         "cells that would let a suppressed value be derived by subtraction. "
                         "No client-level rows, names, birth dates or identifiers are available.",
            },
            "measures": {name: desc for name, (desc, _) in MEASURES.items()},
            "cohorts": {name: desc for name, (desc, _) in COHORTS.items()},
            "dimensions": {
                d.name: {"description": d.description,
                         **({"values": list(d.values)} if d.values else {})}
                for d in self.model.dimensions.values()
            },
            "limits": {"max_group_by": MAX_GROUP_BY, "max_cells": MAX_CELLS},
        }

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.model.con.execute("""
                SELECT p.project_name, p.project_type, p.organization, p.operating_start,
                       p.operating_end,
                       (SELECT sum(i.beds) FROM inventory i WHERE i.project_id = p.project_id
                          AND coalesce(i.start_date, DATE '1900-01-01') <= $asof::DATE
                          AND coalesce(i.end_date, DATE '9999-12-31') >= $asof::DATE)
                FROM project p ORDER BY p.organization, p.project_name
            """, {"asof": self.model.export.end}).fetchall()
        return [
            {"project": r[0], "project_type": r[1], "organization": r[2],
             "operating_start": _iso(r[3]), "operating_end": _iso(r[4]), "bed_inventory": r[5]}
            for r in rows
        ]

    def query(
        self,
        measures: list[str],
        group_by: list[str] | None = None,
        filters: dict[str, list[str]] | None = None,
        cohort: str = "active",
        period_start: str | date | None = None,
        period_end: str | date | None = None,
        _audit: bool = True,
    ) -> dict[str, Any]:
        group_by = list(group_by or [])
        filters = dict(filters or {})
        if not measures:
            raise QueryError(f"Pick at least one measure: {sorted(MEASURES)}")
        _check_names("measure", measures, MEASURES)
        _check_names("dimension", [*group_by, *filters], self.model.dimensions)
        if len(set(group_by)) != len(group_by):
            raise QueryError("group_by repeats a dimension")
        if len(group_by) > MAX_GROUP_BY:
            raise QueryError(f"group_by takes at most {MAX_GROUP_BY} dimensions")
        if cohort not in COHORTS:
            raise QueryError(f"Unknown cohort {cohort!r}. Choose from {sorted(COHORTS)}")
        start, end = self._period(period_start, period_end)

        params: dict[str, Any] = {"period_start": start, "period_end": end}
        where = []
        for i, (name, values) in enumerate(filters.items()):
            if isinstance(values, str):
                values = [values]
            if not values:
                raise QueryError(f"Filter on {name!r} has no values")
            dim = self.model.dimensions[name]
            if dim.values:
                unknown = sorted(set(values) - set(dim.values))
                if unknown:
                    raise QueryError(
                        f"Unknown value(s) {unknown} for {name!r}. Valid: {list(dim.values)}")
            params[f"f{i}"] = [str(v) for v in values]
            where.append(f"list_contains($f{i}::VARCHAR[], d_{name})")

        used = list(dict.fromkeys([*group_by, *filters]))
        dim_cols = "".join(f", {self.model.dimensions[n].sql} AS d_{n}" for n in used)
        group_cols = ", ".join(f"d_{n}" for n in group_by)
        aggs = ", ".join(f"{MEASURES[m][1]} AS {m}" for m in measures)
        sql = f"""
            WITH e AS (
                SELECT *, greatest(date_diff('day', entry_date,
                           CASE WHEN exit_date <= $period_end::DATE THEN exit_date
                                ELSE $period_end::DATE + 1 END), 0) AS los_days
                FROM enr WHERE {COHORTS[cohort][1]}
            ), d AS (
                SELECT e.cidx, e.household_id, e.los_days{dim_cols} FROM e
            )
            SELECT {group_cols + ', ' if group_by else ''}{aggs}, list(DISTINCT cidx) AS members
            FROM d {('WHERE ' + ' AND '.join(where)) if where else ''}
            {('GROUP BY ' + group_cols) if group_by else ''}
        """
        with self._lock:
            rows = self.model.con.execute(sql, params).fetchall()
            if len(rows) > MAX_CELLS:
                raise QueryError(
                    f"That breakdown has {len(rows)} cells; the limit is {MAX_CELLS}. "
                    "Use fewer or coarser dimensions, or add filters.")
            n = len(group_by)
            cells = [
                Cell(key=tuple(r[:n]), members=bitset(r[-1] or ()),
                     values=dict(zip(measures, r[n:-1])))
                for r in rows
            ]
            result = self._release(cells, group_by)
        result = {
            "cohort": cohort,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            **result,
        }
        if _audit:
            self._audit("query_aggregate", {
                "measures": measures, "group_by": group_by, "filters": filters, "cohort": cohort,
                "period_start": start.isoformat(), "period_end": end.isoformat()}, result)
        return result

    def bed_utilization(
        self,
        period_start: str | date | None = None,
        period_end: str | date | None = None,
        projects: list[str] | None = None,
    ) -> dict[str, Any]:
        """Monthly bed nights against bed inventory, per project."""
        start, end = self._period(period_start, period_end)
        params: dict[str, Any] = {"period_start": start, "period_end": end}
        project_filter = ""
        if projects:
            known = set(self.model.dimensions["project"].values or ())
            unknown = sorted(set(projects) - known)
            if unknown:
                raise QueryError(f"Unknown project(s) {unknown}. See list_projects.")
            params["projects"] = list(projects)
            project_filter = "AND list_contains($projects::VARCHAR[], p.project_name)"
        ph = ", ".join(map(str, codes.PH_PROJECT_TYPES))
        res = ", ".join(map(str, codes.RESIDENTIAL_PROJECT_TYPES))
        sql = f"""
            WITH months AS (
                SELECT greatest(m, $period_start::DATE) AS ms,
                       least(m + INTERVAL 1 MONTH, $period_end::DATE + 1)::DATE AS me,
                       strftime(m, '%Y-%m') AS label
                FROM (SELECT unnest(generate_series(date_trunc('month', $period_start::DATE),
                                                    $period_end::DATE, INTERVAL 1 MONTH))::DATE AS m)
            ), stays AS (
                SELECT cidx, project_id,
                       CASE WHEN project_type_code IN ({ph}) THEN move_in_date ELSE entry_date END
                           AS s,
                       coalesce(exit_date, DATE '9999-12-31') AS x
                FROM enr WHERE project_type_code IN ({res})
                  AND project_type_code <> {codes.NBN_PROJECT_TYPE}
            ), nights AS (
                SELECT st.project_id, mo.label, st.cidx,
                       date_diff('day', greatest(st.s, mo.ms), least(st.x, mo.me)) AS n
                FROM stays st JOIN months mo ON st.s < mo.me AND st.x > mo.ms
                UNION ALL
                SELECT b.project_id, mo.label, b.cidx, 1
                FROM bed_night b JOIN months mo ON b.night >= mo.ms AND b.night < mo.me
                JOIN project bp ON bp.project_id = b.project_id
                 AND bp.project_type_code = {codes.NBN_PROJECT_TYPE}
            ), used AS (
                SELECT project_id, label, sum(n) AS bed_nights, list(DISTINCT cidx) AS members
                FROM nights GROUP BY ALL
            ), capacity AS (
                SELECT i.project_id, mo.label,
                       sum(i.beds * date_diff('day',
                               greatest(coalesce(i.start_date, DATE '1900-01-01'), mo.ms),
                               least(coalesce(i.end_date + 1, DATE '9999-12-31'), mo.me)))
                           AS available
                FROM inventory i JOIN months mo
                  ON coalesce(i.start_date, DATE '1900-01-01') < mo.me
                 AND coalesce(i.end_date + 1, DATE '9999-12-31') > mo.ms
                GROUP BY ALL
            )
            SELECT p.project_name, u.label, u.bed_nights, c.available,
                   date_diff('day', mo.ms, mo.me) AS days, u.members
            FROM used u
            JOIN project p USING (project_id)
            JOIN months mo ON mo.label = u.label
            LEFT JOIN capacity c ON c.project_id = u.project_id AND c.label = u.label
            WHERE true {project_filter}
        """
        with self._lock:
            rows = self.model.con.execute(sql, params).fetchall()
            cells = []
            for name, label, nights, available, days, members in rows:
                cell = Cell(key=(name, label), members=bitset(members or ()), values={})
                cell.values = {
                    "clients": cell.size,
                    "bed_nights": int(nights),
                    "avg_nightly_occupancy": round(nights / days, 1),
                    "bed_nights_available": int(available) if available else None,
                    "utilization_pct": round(100 * nights / available, 1) if available else None,
                }
                cells.append(cell)
            result = self._release(cells, ["project", "month"])
        result = {"period": {"start": start.isoformat(), "end": end.isoformat()}, **result}
        self._audit("bed_utilization", {"projects": projects, "period_start": start.isoformat(),
                                        "period_end": end.isoformat()}, result)
        return result

    # -- internals ---------------------------------------------------------

    def _period(self, start: Any, end: Any) -> tuple[date, date]:
        s = _parse_date(start, "period_start") or self.model.export.start
        e = _parse_date(end, "period_end") or self.model.export.end
        if s > e:
            raise QueryError(f"period_start {s} is after period_end {e}")
        return s, e

    def _release(self, cells: list[Cell], dims: list[str]) -> dict[str, Any]:
        """The only way results leave the engine."""
        k = self.policy.min_cell_size
        released = self._control.release(cells)
        released.sort(key=lambda c: tuple(str(v) for v in c.key))
        rows, reasons = [], set()
        for c in released:
            row = dict(zip(dims, c.key))
            if c.suppressed:
                row["suppressed"] = True
                reasons.add(c.suppressed)
            else:
                row.update(c.values)
            rows.append(row)
        notes = [f"Suppressed: {SUPPRESSION_NOTES[r].format(k=k)}." for r in sorted(reasons)]
        if reasons:
            notes.append("Suppressed values are not zero. Report them as "
                         f"'fewer than {k}' or 'suppressed', and do not estimate them.")
        if not rows:
            notes.append("No clients matched.")
        return {
            "min_cell_size": k,
            "rows": rows,
            "suppressed_cells": sum(1 for c in released if c.suppressed),
            "notes": notes,
        }

    def _audit(self, tool: str, request: dict[str, Any], result: dict[str, Any]) -> None:
        if not self._audit_path:
            return
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "request": request,
            "cells_released": len(result["rows"]) - result["suppressed_cells"],
            "cells_suppressed": result["suppressed_cells"],
        }
        with self._audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _check_names(kind: str, names: list[str], valid: dict[str, Any]) -> None:
    unknown = [n for n in names if n not in valid]
    if unknown:
        raise QueryError(f"Unknown {kind}(s) {unknown}. Valid: {sorted(valid)}")


def _parse_date(value: Any, field: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise QueryError(f"{field} must be an ISO date (YYYY-MM-DD); got {value!r}") from None


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None
