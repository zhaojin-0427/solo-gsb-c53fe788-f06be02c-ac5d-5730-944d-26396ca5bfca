"""FastAPI application and Z3 solver for the dual-zone oven curve trial tool."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from z3 import (
    And,
    Bool,
    If,
    Int,
    Real,
    RealVal,
    Solver,
    sat,
    unsat,
)

getcontext().prec = 100

SOLVE_TIMEOUT_MS = 10_000
DIAGNOSE_TIMEOUT_MS = 10_000
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATABASE_PATH = Path(
    os.environ.get("DATABASE_PATH", str(BASE_DIR.parent / "data" / "oven.db"))
)


class ZoneInput(BaseModel):
    name: str = Field(default="", max_length=30)
    initial_temperature: int = Field(ge=0, le=300)
    coefficients: List[Decimal] = Field(min_length=2, max_length=2)
    final_low: Decimal = Field(ge=0, le=300)
    final_high: Decimal = Field(ge=0, le=300)
    max_temperature: Decimal = Field(ge=0, le=300)
    exposure_threshold: Decimal = Field(ge=0, le=300)
    min_exposure: Decimal = Field(ge=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        return value[:30]

    @field_validator("coefficients")
    @classmethod
    def validate_coefficients(cls, values: List[Decimal]) -> List[Decimal]:
        for value in values:
            if not (Decimal("0.01") <= value <= Decimal("1.00")):
                raise ValueError("响应系数必须位于 0.01 到 1.00 之间")
            exponent = value.as_tuple().exponent
            if isinstance(exponent, int) and exponent < -2:
                raise ValueError("响应系数最多保留两位小数")
        return values

    @model_validator(mode="after")
    def validate_zone(self) -> "ZoneInput":
        if self.final_low > self.final_high:
            raise ValueError("终温区间下限不能大于上限")
        if self.initial_temperature > self.max_temperature:
            raise ValueError(f"{self.name or '温区'}的初温不能超过全程上限")
        return self


class SolveRequest(BaseModel):
    duration: int = Field(ge=1, le=24)
    max_setpoint_change: int = Field(ge=0, le=300)
    locked_minutes: Dict[int, int] = Field(default_factory=dict)
    zones: List[ZoneInput] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_request(self) -> "SolveRequest":
        for minute, value in self.locked_minutes.items():
            if not 1 <= minute <= self.duration:
                raise ValueError(f"锁定分钟 {minute} 不在 1 到 {self.duration} 范围内")
            if not 0 <= value <= 300:
                raise ValueError(f"锁定分钟 {minute} 的设定温度必须在 0 到 300℃ 之间")

        for index, zone in enumerate(self.zones, start=1):
            if not zone.name:
                zone.name = f"温区 {index}"
        return self


def decimal_to_fraction(value: Decimal) -> Fraction:
    return Fraction(value)


def fraction_to_z3(value: Fraction):
    return RealVal(value.numerator) / value.denominator



def finite_decimal(value: Fraction) -> str:
    """Return the exact finite decimal representation of a trajectory rational."""
    result = Decimal(value.numerator) / Decimal(value.denominator)
    text = format(result, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def display_decimal(value: Fraction, places: int = 4) -> str:
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    quantum = Decimal("1").scaleb(-places)
    return format(exact.quantize(quantum, rounding=ROUND_HALF_UP), "f")


def rational_payload(value: Fraction) -> Dict[str, str]:
    return {
        "fraction": str(value),
        "decimal": finite_decimal(value),
        "display": display_decimal(value),
    }


def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                solver_ms INTEGER NOT NULL,
                objective_total INTEGER,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS diagnoses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_snapshot(request: SolveRequest, result: Dict[str, Any]) -> int:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    request_json = json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
    result_json = json.dumps(result, ensure_ascii=False)
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO snapshots
                (created_at, status, solver_ms, objective_total, request_json, result_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                result["status"],
                result["elapsed_ms"],
                result.get("objective_total"),
                request_json,
                result_json,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def get_snapshot(snapshot_id: int) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT result_json FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def get_snapshot_row(snapshot_id: int) -> Optional[Tuple[str, str]]:
    """Return (status, request_json) for a snapshot, or None if it does not exist."""
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT status, request_json FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def save_diagnosis(snapshot_id: int, result: Dict[str, Any]) -> Tuple[int, str]:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result_json = json.dumps(result, ensure_ascii=False)
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO diagnoses (snapshot_id, created_at, elapsed_ms, result_json)
            VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, created_at, result["elapsed_ms"], result_json),
        )
        conn.commit()
        return int(cursor.lastrowid), created_at


def get_latest_diagnosis(snapshot_id: int) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, created_at, result_json FROM diagnoses
            WHERE snapshot_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (snapshot_id,),
        ).fetchone()
    if not row:
        return None
    diagnosis_id, created_at, result_json = row
    result = json.loads(result_json)
    result["diagnosis_id"] = diagnosis_id
    result["snapshot_id"] = snapshot_id
    result["created_at"] = created_at
    return result


def list_snapshots() -> List[Dict[str, Any]]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, status, solver_ms, objective_total, result_json
            FROM snapshots
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()
    items: List[Dict[str, Any]] = []
    for snapshot_id, created_at, status, solver_ms, objective_total, result_json in rows:
        result = json.loads(result_json)
        items.append(
            {
                "id": snapshot_id,
                "created_at": created_at,
                "status": status,
                "status_text": result.get("status_text", status),
                "solver_ms": solver_ms,
                "objective_total": objective_total,
                "duration": result.get("input", {}).get("duration"),
            }
        )
    return items


def independently_feasible(
    request: SolveRequest,
    setpoints: List[int],
    trajectories: List[List[Fraction]],
) -> bool:
    """Check a model independently using exact Python rational arithmetic."""
    if len(setpoints) != request.duration:
        return False
    if any(not 0 <= value <= 300 for value in setpoints):
        return False
    if any(abs(a - b) > request.max_setpoint_change for a, b in zip(setpoints, setpoints[1:])):
        return False
    for minute, locked_value in request.locked_minutes.items():
        if setpoints[minute - 1] != locked_value:
            return False

    trajectory_index = 0
    for zone in request.zones:
        upper = decimal_to_fraction(zone.max_temperature)
        low = decimal_to_fraction(zone.final_low)
        high = decimal_to_fraction(zone.final_high)
        threshold = decimal_to_fraction(zone.exposure_threshold)
        required = decimal_to_fraction(zone.min_exposure)

        if Fraction(zone.initial_temperature) > upper:
            return False

        for alpha_decimal in zone.coefficients:
            temperatures = trajectories[trajectory_index]
            trajectory_index += 1
            alpha = decimal_to_fraction(alpha_decimal)
            reconstructed = [Fraction(zone.initial_temperature)]
            for setpoint in setpoints:
                previous = reconstructed[-1]
                reconstructed.append(previous + alpha * (Fraction(setpoint) - previous))

            if temperatures != reconstructed:
                return False
            if any(value > upper for value in temperatures):
                return False
            if not low <= temperatures[-1] <= high:
                return False
            exposure = sum(max(Fraction(0), value - threshold) for value in temperatures[1:])
            if exposure < required:
                return False
    return True


def build_result(request: SolveRequest, setpoints: List[int]) -> Dict[str, Any]:
    all_trajectories: List[List[Fraction]] = []
    trajectory_payloads: List[Dict[str, Any]] = []

    for zone_index, zone in enumerate(request.zones, start=1):
        upper = decimal_to_fraction(zone.max_temperature)
        low = decimal_to_fraction(zone.final_low)
        high = decimal_to_fraction(zone.final_high)
        threshold = decimal_to_fraction(zone.exposure_threshold)
        required = decimal_to_fraction(zone.min_exposure)

        for coefficient_index, coefficient in enumerate(zone.coefficients, start=1):
            alpha = decimal_to_fraction(coefficient)
            temperatures = [Fraction(zone.initial_temperature)]
            for setpoint in setpoints:
                previous = temperatures[-1]
                temperatures.append(previous + alpha * (Fraction(setpoint) - previous))
            all_trajectories.append(temperatures)

            exposure = Fraction(0)
            rows: List[Dict[str, Any]] = []
            for minute, temperature in enumerate(temperatures):
                setpoint = None if minute == 0 else setpoints[minute - 1]
                ramp_prev_margin = None
                ramp_next_margin = None
                if minute > 0:
                    if minute > 1:
                        ramp_prev_margin = request.max_setpoint_change - abs(
                            setpoints[minute - 1] - setpoints[minute - 2]
                        )
                    if minute < len(setpoints):
                        ramp_next_margin = request.max_setpoint_change - abs(
                            setpoints[minute] - setpoints[minute - 1]
                        )
                delta = Fraction(0) if minute == 0 else max(Fraction(0), temperature - threshold)
                exposure += delta
                rows.append(
                    {
                        "minute": minute,
                        "setpoint": setpoint,
                        "locked": minute in request.locked_minutes,
                        "ramp_prev_margin": ramp_prev_margin,
                        "ramp_next_margin": ramp_next_margin,
                        "temperature": rational_payload(temperature),
                        "upper_margin": rational_payload(upper - temperature),
                        "exposure_delta": rational_payload(delta),
                        "exposure_sum": rational_payload(exposure),
                    }
                )

            final_temperature = temperatures[-1]
            trajectory_payloads.append(
                {
                    "zone_index": zone_index,
                    "zone_name": zone.name,
                    "coefficient_index": coefficient_index,
                    "coefficient": str(coefficient),
                    "rows": rows,
                    "summary": {
                        "final_temperature": rational_payload(final_temperature),
                        "final_low_margin": rational_payload(final_temperature - low),
                        "final_high_margin": rational_payload(high - final_temperature),
                        "exposure_total": rational_payload(exposure),
                        "exposure_margin": rational_payload(exposure - required),
                        "minimum_upper_margin": rational_payload(
                            min(upper - value for value in temperatures)
                        ),
                    },
                }
            )

    return {
        "setpoints": setpoints,
        "objective_total": sum(setpoints),
        "trajectories": trajectory_payloads,
        "_exact_trajectories": all_trajectories,
    }


def unsolved_result(
    status: Literal["no_solution", "no_candidate"],
    message: str,
    elapsed_ms: int = 0,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": status,
        "status_text": "约束无解" if status == "no_solution" else "尚未找到曲线",
        "message": message,
        "elapsed_ms": elapsed_ms,
        "timeout_ms": SOLVE_TIMEOUT_MS,
        "setpoints": [],
        "objective_total": None,
        "trajectories": [],
    }
    if reason:
        result["solver_reason"] = reason
    return result


def make_solver(
    request: SolveRequest,
) -> Tuple[Solver, List[Any]]:
    """Build a linear MILP model.

    The recurrence itself is linear.  Each positive part max(0, T-threshold)
    is represented by a continuous e variable and one Boolean branch, giving
    Z3's arithmetic engine a piecewise-linear rather than unconstrained formula.
    """
    solver = Solver()
    setpoint_vars = [Int(f"u_{minute}") for minute in range(1, request.duration + 1)]

    for value in setpoint_vars:
        solver.add(value >= 0, value <= 300)
    for previous, current in zip(setpoint_vars, setpoint_vars[1:]):
        solver.add(current - previous <= request.max_setpoint_change)
        solver.add(previous - current <= request.max_setpoint_change)
    for minute, locked_value in request.locked_minutes.items():
        solver.add(setpoint_vars[minute - 1] == locked_value)

    for zone_index, zone in enumerate(request.zones):
        initial = decimal_to_fraction(Decimal(zone.initial_temperature))
        upper = decimal_to_fraction(zone.max_temperature)
        low = decimal_to_fraction(zone.final_low)
        high = decimal_to_fraction(zone.final_high)
        threshold = decimal_to_fraction(zone.exposure_threshold)
        required = decimal_to_fraction(zone.min_exposure)

        for trajectory_index, coefficient in enumerate(zone.coefficients):
            alpha = fraction_to_z3(decimal_to_fraction(coefficient))
            temperature = fraction_to_z3(initial)
            exposure = RealVal(0)

            for minute, setpoint in enumerate(setpoint_vars, start=1):
                temperature = temperature + alpha * (setpoint - temperature)
                solver.add(temperature >= 0, temperature <= upper)

                excess = Real(f"excess_z{zone_index}_a{trajectory_index}_t{minute}")
                positive_branch = Bool(f"positive_z{zone_index}_a{trajectory_index}_t{minute}")
                above_threshold = temperature - fraction_to_z3(threshold)
                # With 0 <= T,U <= 300, excess is bounded by 300. These two
                # implications encode excess == max(0, T-threshold).
                solver.add(
                    If(
                        positive_branch,
                        And(excess == above_threshold, excess >= 0, above_threshold >= 0),
                        And(excess == 0, above_threshold <= 0),
                    )
                )
                exposure = exposure + excess

            solver.add(temperature >= low, temperature <= high, exposure >= fraction_to_z3(required))

    return solver, setpoint_vars


CheckOutcome = Tuple[str, Optional[List[int]], str]


def check_candidate(
    request: SolveRequest,
    deadline: float,
    total_bound: Optional[int] = None,
    fixed: Optional[Dict[int, int]] = None,
    coordinate: Optional[int] = None,
    coordinate_bound: Optional[int] = None,
) -> CheckOutcome:
    remaining_ms = max(1, int((deadline - time.perf_counter()) * 1000))
    solver, setpoint_vars = make_solver(request)

    if total_bound is not None:
        solver.add(sum(setpoint_vars) <= total_bound)
    for index, value in (fixed or {}).items():
        solver.add(setpoint_vars[index] == value)
    if coordinate is not None and coordinate_bound is not None:
        solver.add(setpoint_vars[coordinate] <= coordinate_bound)

    solver.set("timeout", remaining_ms)
    result = solver.check()
    if result == sat:
        model = solver.model()
        values = [
            model.eval(value, model_completion=True).as_long() for value in setpoint_vars
        ]
        return "sat", values, ""
    if result == unsat:
        return "unsat", None, str(solver.reason_unknown())

    reason = str(solver.reason_unknown())
    try:
        model = solver.model()
        candidate = [
            model.eval(value, model_completion=True).as_long() for value in setpoint_vars
        ]
    except Exception:
        candidate = None
    return "unknown", candidate, reason


def build_diagnosis_model(
    request: SolveRequest,
) -> Tuple[List[Any], Dict[str, List[Any]], List[Dict[str, Any]]]:
    """Rebuild the exact Z3 model, split into always-on assertions and toggleable groups.

    Always-on (never removed): setpoint integrality (Int variables), the temperature
    recurrence, temperature non-negativity, and the positive-part definition of the
    heat exposure increments.  Toggleable groups, in deletion-pass order:

    1. the 0~300 range of all setpoints (one group);
    2. all adjacent-minute ramp limits (one group);
    3. each locked minute, in increasing minute order (one group per lock);
    4. per zone in input order: the full-trajectory upper bound of all its
       trajectories, the final-temperature interval, and the minimum heat exposure
       (one group each).

    The model is returned in two logically equivalent encodings of the positive
    part max(0, T-threshold): ``boolean`` is the piecewise-linear If/Boolean
    encoding used by make_solver, ``functional`` folds the same definition into
    the exposure sum as If-expressions.  Both define exactly the same constraint
    system; the diagnosis solver portfolio may use either, and every witness is
    additionally re-verified with exact Python rational arithmetic.
    """
    setpoint_vars = [Int(f"u_{minute}") for minute in range(1, request.duration + 1)]
    base_boolean: List[Any] = []
    base_functional: List[Any] = []
    groups: List[Dict[str, Any]] = []

    range_assertions: List[Any] = []
    for value in setpoint_vars:
        range_assertions.extend([value >= 0, value <= 300])
    groups.append(
        {
            "id": "setpoint_range",
            "kind": "setpoint_range",
            "name": "设定值 0～300℃ 范围",
            "assertions": range_assertions,
            "assertions_alt": range_assertions,
            "detail": {"minutes": list(range(1, request.duration + 1))},
        }
    )

    ramp_assertions: List[Any] = []
    for previous, current in zip(setpoint_vars, setpoint_vars[1:]):
        ramp_assertions.extend(
            [
                current - previous <= request.max_setpoint_change,
                previous - current <= request.max_setpoint_change,
            ]
        )
    groups.append(
        {
            "id": "ramp_limit",
            "kind": "ramp_limit",
            "name": f"相邻分钟跃变 ≤ {request.max_setpoint_change}℃",
            "assertions": ramp_assertions,
            "assertions_alt": ramp_assertions,
            "detail": {
                "max_change": request.max_setpoint_change,
                "pairs": [[minute, minute + 1] for minute in range(1, request.duration)],
            },
        }
    )

    for minute in sorted(request.locked_minutes):
        locked_value = request.locked_minutes[minute]
        lock_assertions = [setpoint_vars[minute - 1] == locked_value]
        groups.append(
            {
                "id": f"lock_{minute}",
                "kind": "lock",
                "name": f"锁定第 {minute} 分钟 = {locked_value}℃",
                "assertions": lock_assertions,
                "assertions_alt": lock_assertions,
                "detail": {"minute": minute, "value": locked_value},
            }
        )

    for zone_index, zone in enumerate(request.zones, start=1):
        initial = decimal_to_fraction(Decimal(zone.initial_temperature))
        upper = decimal_to_fraction(zone.max_temperature)
        low = decimal_to_fraction(zone.final_low)
        high = decimal_to_fraction(zone.final_high)
        threshold = decimal_to_fraction(zone.exposure_threshold)
        required = decimal_to_fraction(zone.min_exposure)
        upper_z3 = fraction_to_z3(upper)
        threshold_z3 = fraction_to_z3(threshold)

        upper_assertions: List[Any] = []
        final_assertions: List[Any] = []
        exposure_assertions: List[Any] = []
        exposure_assertions_alt: List[Any] = []

        for trajectory_index, coefficient in enumerate(zone.coefficients):
            alpha = fraction_to_z3(decimal_to_fraction(coefficient))
            temperature = fraction_to_z3(initial)
            upper_assertions.append(temperature <= upper_z3)
            exposure = RealVal(0)
            exposure_alt = RealVal(0)

            for minute, setpoint in enumerate(setpoint_vars, start=1):
                temperature = temperature + alpha * (setpoint - temperature)
                base_boolean.append(temperature >= 0)
                base_functional.append(temperature >= 0)
                upper_assertions.append(temperature <= upper_z3)

                above_threshold = temperature - threshold_z3
                # Same piecewise-linear encoding of max(0, T-threshold) as make_solver.
                excess = Real(f"excess_z{zone_index}_a{trajectory_index}_t{minute}")
                positive_branch = Bool(f"positive_z{zone_index}_a{trajectory_index}_t{minute}")
                base_boolean.append(
                    If(
                        positive_branch,
                        And(excess == above_threshold, excess >= 0, above_threshold >= 0),
                        And(excess == 0, above_threshold <= 0),
                    )
                )
                exposure = exposure + excess
                # Equivalent functional encoding of the same positive part.
                exposure_alt = exposure_alt + If(above_threshold >= 0, above_threshold, 0)

            final_assertions.extend(
                [temperature >= fraction_to_z3(low), temperature <= fraction_to_z3(high)]
            )
            exposure_assertions.append(exposure >= fraction_to_z3(required))
            exposure_assertions_alt.append(exposure_alt >= fraction_to_z3(required))

        coefficients = [str(coefficient) for coefficient in zone.coefficients]
        groups.append(
            {
                "id": f"zone_{zone_index}_upper",
                "kind": "zone_upper",
                "name": f"{zone.name}：全程温度 ≤ {zone.max_temperature}℃",
                "assertions": upper_assertions,
                "assertions_alt": upper_assertions,
                "detail": {
                    "zone_index": zone_index,
                    "zone_name": zone.name,
                    "coefficients": coefficients,
                    "minutes": list(range(0, request.duration + 1)),
                    "bound": str(zone.max_temperature),
                },
            }
        )
        groups.append(
            {
                "id": f"zone_{zone_index}_final",
                "kind": "zone_final",
                "name": f"{zone.name}：终温 ∈ [{zone.final_low}, {zone.final_high}]℃",
                "assertions": final_assertions,
                "assertions_alt": final_assertions,
                "detail": {
                    "zone_index": zone_index,
                    "zone_name": zone.name,
                    "coefficients": coefficients,
                    "minute": request.duration,
                    "low": str(zone.final_low),
                    "high": str(zone.final_high),
                },
            }
        )
        groups.append(
            {
                "id": f"zone_{zone_index}_exposure",
                "kind": "zone_exposure",
                "name": f"{zone.name}：累计热暴露 ≥ {zone.min_exposure}",
                "assertions": exposure_assertions,
                "assertions_alt": exposure_assertions_alt,
                "detail": {
                    "zone_index": zone_index,
                    "zone_name": zone.name,
                    "coefficients": coefficients,
                    "minutes": list(range(1, request.duration + 1)),
                    "threshold": str(zone.exposure_threshold),
                    "required": str(zone.min_exposure),
                },
            }
        )

    return setpoint_vars, {"boolean": base_boolean, "functional": base_functional}, groups


# Solver portfolio tried in order for every group-subset check.  All entries solve
# the same exact constraint system (the two encodings are logically equivalent), so
# any decisive verdict is valid; the per-attempt budget caps keep one pathological
# query from exhausting the shared 10 second diagnosis budget.
CHECK_PORTFOLIO = (
    ("boolean", {}, 0.3, 2.0),
    ("functional", {}, 0.4, 3.0),
    ("boolean", {"auto_config": False, "smt.case_split": 3}, 1.0, None),
)


def check_group_subset(
    base: Dict[str, List[Any]],
    groups: List[Dict[str, Any]],
    setpoint_vars: List[Any],
    active_ids: List[str],
    deadline: float,
) -> CheckOutcome:
    """Check always-on assertions plus the selected groups under the shared deadline."""
    active = set(active_ids)
    last_reason = "timeout"
    for encoding, config, fraction, cap_seconds in CHECK_PORTFOLIO:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        budget = remaining * fraction
        if cap_seconds is not None:
            budget = min(budget, cap_seconds)
        solver = Solver()
        for key, value in config.items():
            solver.set(key, value)
        solver.add(*base[encoding])
        for group in groups:
            if group["id"] in active:
                if encoding == "boolean":
                    solver.add(*group["assertions"])
                else:
                    solver.add(*group["assertions_alt"])
        solver.set("timeout", max(1, int(budget * 1000)))
        result = solver.check()
        if result == sat:
            model = solver.model()
            values = [
                model.eval(value, model_completion=True).as_long() for value in setpoint_vars
            ]
            return "sat", values, ""
        if result == unsat:
            return "unsat", None, str(solver.reason_unknown())
        last_reason = str(solver.reason_unknown())
    return "unknown", None, last_reason


def verify_subset(
    request: SolveRequest,
    setpoints: List[int],
    active_ids: set,
) -> bool:
    """Independently verify a witness with exact rational arithmetic.

    Checks the always-on constraints (recurrence and non-negativity hold by
    construction of the replayed trajectories) plus every active toggleable group.
    """
    if len(setpoints) != request.duration:
        return False
    if "setpoint_range" in active_ids:
        if any(not 0 <= value <= 300 for value in setpoints):
            return False
    if "ramp_limit" in active_ids:
        if any(
            abs(a - b) > request.max_setpoint_change
            for a, b in zip(setpoints, setpoints[1:])
        ):
            return False
    for minute, locked_value in request.locked_minutes.items():
        if f"lock_{minute}" in active_ids and setpoints[minute - 1] != locked_value:
            return False

    for zone_index, zone in enumerate(request.zones, start=1):
        upper = decimal_to_fraction(zone.max_temperature)
        low = decimal_to_fraction(zone.final_low)
        high = decimal_to_fraction(zone.final_high)
        threshold = decimal_to_fraction(zone.exposure_threshold)
        required = decimal_to_fraction(zone.min_exposure)

        for coefficient in zone.coefficients:
            alpha = decimal_to_fraction(coefficient)
            temperatures = [Fraction(zone.initial_temperature)]
            for setpoint in setpoints:
                previous = temperatures[-1]
                temperatures.append(previous + alpha * (Fraction(setpoint) - previous))

            if any(value < 0 for value in temperatures[1:]):
                return False
            if f"zone_{zone_index}_upper" in active_ids:
                if any(value > upper for value in temperatures):
                    return False
            if f"zone_{zone_index}_final" in active_ids:
                if not low <= temperatures[-1] <= high:
                    return False
            if f"zone_{zone_index}_exposure" in active_ids:
                exposure = sum(
                    max(Fraction(0), value - threshold) for value in temperatures[1:]
                )
                if exposure < required:
                    return False
    return True


def diagnose(request: SolveRequest) -> Dict[str, Any]:
    """Find a deletion-minimal infeasible group subset for a proven-unsat input.

    All Z3 re-runs share one 10 second budget.  The result is only returned when
    the final subset is proven infeasible and every single-group deletion from
    that final subset is proven feasible with an independently verified witness;
    otherwise an undecided outcome is returned and nothing is saved.
    """
    started = time.perf_counter()
    deadline = started + (DIAGNOSE_TIMEOUT_MS - 200) / 1000
    setpoint_vars, base, groups = build_diagnosis_model(request)
    checks = 0

    def run(active_ids: List[str]) -> CheckOutcome:
        nonlocal checks
        checks += 1
        return check_group_subset(base, groups, setpoint_vars, active_ids, deadline)

    def undecided(reason: Optional[str] = None) -> Dict[str, Any]:
        outcome: Dict[str, Any] = {
            "status": "undecided",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "checks": checks,
        }
        if reason:
            outcome["solver_reason"] = reason
        return outcome

    all_ids = [group["id"] for group in groups]
    status, _, reason = run(all_ids)
    if status == "sat":
        return {
            "status": "rerun_sat",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "checks": checks,
        }
    if status != "unsat":
        return undecided(reason)

    # Deletion pass over the full set, in group order: a group is dropped only
    # when the remaining groups are still proven infeasible.
    core = list(all_ids)
    for group in groups:
        group_id = group["id"]
        if group_id not in core:
            continue
        trial = [item for item in core if item != group_id]
        trial_status, _, _ = run(trial)
        if trial_status == "unsat":
            core = trial

    # Feasibility witnesses: delete each remaining group from the FINAL subset
    # (never reuse results obtained by deleting groups from the full set).
    group_by_id = {group["id"]: group for group in groups}
    witnesses: List[Dict[str, Any]] = []
    for group_id in core:
        trial = [item for item in core if item != group_id]
        witness_status, values, witness_reason = run(trial)
        if witness_status != "sat" or values is None:
            return undecided(witness_reason)
        if not verify_subset(request, values, set(trial)):
            return {"status": "witness_failed", "elapsed_ms": int((time.perf_counter() - started) * 1000)}
        witnesses.append(
            {
                "group_id": group_id,
                "group_name": group_by_id[group_id]["name"],
                "setpoints": values,
                "objective_total": sum(values),
            }
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    group_payloads = [
        {
            "id": group["id"],
            "kind": group["kind"],
            "name": group["name"],
            "detail": group["detail"],
            "constraint_count": len(group["assertions"]),
            "in_core": group["id"] in core,
        }
        for group in groups
    ]
    return {
        "status": "diagnosed",
        "status_text": "诊断完成",
        "message": (
            f"在 {len(groups)} 个可开关约束组中定位到 {len(core)} 组极小冲突："
            "该子集仍无解，且删除其中任意一组即可恢复可行。"
        ),
        "elapsed_ms": elapsed_ms,
        "timeout_ms": DIAGNOSE_TIMEOUT_MS,
        "checks": checks,
        "groups": group_payloads,
        "core": core,
        "witnesses": witnesses,
    }


def feasible_payload(
    request: SolveRequest,
    setpoints: List[int],
    *,
    elapsed_ms: int,
    status: Literal["optimal", "feasible_not_optimal"],
    message: str,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    result = build_result(request, setpoints)
    exact_trajectories = result.pop("_exact_trajectories")
    if not independently_feasible(request, setpoints, exact_trajectories):
        return None

    payload: Dict[str, Any] = {
        "status": status,
        "status_text": "已证明最优" if status == "optimal" else "找到可行曲线，尚未证明最优",
        "message": message,
        "elapsed_ms": elapsed_ms,
        "timeout_ms": SOLVE_TIMEOUT_MS,
    }
    if reason:
        payload["solver_reason"] = reason
    payload.update(result)
    return payload


def solve(request: SolveRequest) -> Dict[str, Any]:
    # Reserve a small margin for model extraction, independent verification, and JSON work.
    deadline = time.perf_counter() + (SOLVE_TIMEOUT_MS - 200) / 1000
    started = time.perf_counter()

    initial_status, initial_values, initial_reason = check_candidate(request, deadline)
    if initial_status == "unsat":
        return unsolved_result(
            "no_solution",
            "约束无解：不存在可同时满足四种轨迹的设定温度曲线。",
            int((time.perf_counter() - started) * 1000),
            initial_reason,
        )
    if initial_status != "sat" or initial_values is None:
        if initial_status == "unknown" and initial_values:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            payload = feasible_payload(
                request,
                initial_values,
                elapsed_ms=elapsed_ms,
                status="feasible_not_optimal",
                message="已找到同时满足四种轨迹的曲线，但 10 秒限时内未能证明最优。",
                reason=initial_reason,
            )
            if payload:
                return payload
        return unsolved_result(
            "no_candidate",
            "10 秒限时内尚未找到可行曲线。这不代表约束一定无解。",
            int((time.perf_counter() - started) * 1000),
            initial_reason,
        )

    best = initial_values
    low_total = 0
    high_total = sum(best)
    proved_total = False

    # Binary search the primary objective: total setpoint sum.
    while low_total < high_total:
        middle = (low_total + high_total) // 2
        status, values, reason = check_candidate(request, deadline, total_bound=middle)
        if status == "sat" and values is not None:
            best = values
            high_total = sum(best)
        elif status == "unsat":
            low_total = middle + 1
        else:
            if values:
                best = values
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            payload = feasible_payload(
                request,
                best,
                elapsed_ms=elapsed_ms,
                status="feasible_not_optimal",
                message=(
                    "已找到同时满足四种轨迹的曲线，但在 10 秒限时内未能完成最优性证明。"
                ),
                reason=reason or "timeout",
            )
            return payload or unsolved_result(
                "no_candidate",
                "10 秒限时内尚未得到通过精确校验的候选曲线。",
                elapsed_ms,
                reason,
            )
    proved_total = True

    fixed: Dict[int, int] = {}
    for coordinate in range(request.duration):
        low_value = 0
        high_value = best[coordinate]

        while low_value < high_value:
            middle = (low_value + high_value) // 2
            status, values, reason = check_candidate(
                request,
                deadline,
                total_bound=low_total,
                fixed=fixed,
                coordinate=coordinate,
                coordinate_bound=middle,
            )
            if status == "sat" and values is not None:
                best = values
                high_value = best[coordinate]
            elif status == "unsat":
                low_value = middle + 1
            else:
                if values:
                    best = values
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                payload = feasible_payload(
                    request,
                    best,
                    elapsed_ms=elapsed_ms,
                    status="feasible_not_optimal",
                    message=(
                        "已找到同时满足四种轨迹的曲线，并确定了设定温度总和；"
                        "但 10 秒限时内未能完成逐分钟字典序最优性证明。"
                    ),
                    reason=reason or "timeout",
                )
                return payload or unsolved_result(
                    "no_candidate",
                    "10 秒限时内尚未得到通过精确校验的候选曲线。",
                    elapsed_ms,
                    reason,
                )

        fixed[coordinate] = low_value
        best[coordinate] = low_value

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if not proved_total or sum(best) != low_total:
        payload = feasible_payload(
            request,
            best,
            elapsed_ms=elapsed_ms,
            status="feasible_not_optimal",
            message="已找到可行曲线，但未能完整证明其按目标顺序最优。",
        )
        return payload or unsolved_result(
            "no_candidate",
            "候选曲线未通过独立精确校验。",
            elapsed_ms,
        )

    payload = feasible_payload(
        request,
        best,
        elapsed_ms=elapsed_ms,
        status="optimal",
        message="找到可行曲线，并已在 10 秒时限内证明：先最小化设定温度总和，再按分钟序最小化各设定值。",
    )
    return payload or unsolved_result(
        "no_candidate",
        "求解模型未通过独立精确校验。",
        elapsed_ms,
    )


app = FastAPI(title="双区烘箱曲线试算", version="1.0.0")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/solve")
def create_solution(request: SolveRequest) -> Dict[str, Any]:
    result = solve(request)
    result["input"] = request.model_dump(mode="json")
    result["snapshot_id"] = save_snapshot(request, result)
    return result


@app.get("/api/snapshots")
def snapshots() -> Dict[str, List[Dict[str, Any]]]:
    return {"items": list_snapshots()}


@app.get("/api/snapshots/{snapshot_id}")
def snapshot_detail(snapshot_id: int) -> Dict[str, Any]:
    result = get_snapshot(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    result["snapshot_id"] = snapshot_id
    return result


@app.post("/api/snapshots/{snapshot_id}/diagnose")
def diagnose_snapshot(snapshot_id: int) -> Dict[str, Any]:
    row = get_snapshot_row(snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    status, request_json = row
    if status != "no_solution":
        raise HTTPException(
            status_code=409,
            detail="仅“约束无解”快照支持诊断：该快照未证明无解",
        )
    if not request_json:
        raise HTTPException(status_code=422, detail="快照缺少输入数据，无法还原诊断")
    try:
        request = SolveRequest(**json.loads(request_json))
    except Exception:
        raise HTTPException(status_code=422, detail="快照输入数据无法还原，诊断中止")

    outcome = diagnose(request)
    if outcome["status"] == "rerun_sat":
        raise HTTPException(
            status_code=409,
            detail="按原模型重跑后约束有解，与快照的无解结论不一致，未保存诊断",
        )
    if outcome["status"] == "witness_failed":
        raise HTTPException(
            status_code=500,
            detail="可行性见证未通过独立精确校验，未保存诊断",
        )
    if outcome["status"] != "diagnosed":
        raise HTTPException(
            status_code=504,
            detail="诊断在 10 秒共享预算内未决或超时，未保存部分诊断",
        )

    diagnosis_id, created_at = save_diagnosis(snapshot_id, outcome)
    outcome["diagnosis_id"] = diagnosis_id
    outcome["snapshot_id"] = snapshot_id
    outcome["created_at"] = created_at
    return outcome


@app.get("/api/snapshots/{snapshot_id}/diagnosis")
def snapshot_diagnosis(snapshot_id: int) -> Dict[str, Any]:
    if get_snapshot_row(snapshot_id) is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    diagnosis = get_latest_diagnosis(snapshot_id)
    if diagnosis is None:
        raise HTTPException(status_code=404, detail="该快照尚未保存诊断")
    return diagnosis


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
