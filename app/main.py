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
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from z3 import (
    And,
    Bool,
    If,
    Int,
    Real,
    RealVal,
    Solver,
    sat,
    unknown,
    unsat,
)

getcontext().prec = 100

SOLVE_TIMEOUT_MS = 10_000
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
                snapshot_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                diagnosis_json TEXT NOT NULL,
                FOREIGN KEY (snapshot_id) REFERENCES snapshots (id)
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


def get_snapshot_record(snapshot_id: int) -> Optional[Tuple[str, str]]:
    """Return (status, request_json) for a snapshot, or None when missing."""
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT status, request_json FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def diagnosed_snapshot_ids() -> Set[int]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute("SELECT snapshot_id FROM diagnoses").fetchall()
    return {int(row[0]) for row in rows}


def save_diagnosis(snapshot_id: int, elapsed_ms: int, diagnosis: Dict[str, Any]) -> None:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    diagnosis_json = json.dumps(diagnosis, ensure_ascii=False)
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            INSERT INTO diagnoses (snapshot_id, created_at, elapsed_ms, diagnosis_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                created_at = excluded.created_at,
                elapsed_ms = excluded.elapsed_ms,
                diagnosis_json = excluded.diagnosis_json
            """,
            (snapshot_id, created_at, elapsed_ms, diagnosis_json),
        )
        conn.commit()


def get_diagnosis(snapshot_id: int) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT diagnosis_json FROM diagnoses WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def list_snapshots() -> List[Dict[str, Any]]:
    diagnosed = diagnosed_snapshot_ids()
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
                "has_diagnosis": snapshot_id in diagnosed,
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


# A model is split into permanent constraints (always enabled: setpoint
# integrality, the exact temperature recurrence, non-negativity and the
# positive-part definition) and ordered switchable groups (ranges, ramp
# limits, per-minute locks, then per-zone upper bounds / final temperature /
# minimum exposure).  Both the original solver and the unsat diagnosis build
# from this exact partition, so the diagnosis never diverges from the model
# used to solve.
ConstraintGroup = Dict[str, Any]
BuiltModel = Tuple[List[Any], List[ConstraintGroup], List[Any]]


def build_constraint_model(request: SolveRequest) -> BuiltModel:
    """Build the precise Z3 model partitioned into constraint groups."""
    setpoint_vars = [Int(f"u_{minute}") for minute in range(1, request.duration + 1)]
    permanent: List[Any] = []
    groups: List[ConstraintGroup] = []

    # Group 1: 0..300 range of every setpoint.
    groups.append(
        {
            "key": "setpoint_range",
            "name": "设定值 0～300 范围",
            "kind": "setpoint_range",
            "related": {"minutes": list(range(1, request.duration + 1))},
            "constraints": [
                c
                for value in setpoint_vars
                for c in (value >= 0, value <= 300)
            ],
        }
    )

    # Group 2: upper bound on every adjacent jump.
    groups.append(
        {
            "key": "ramp_limits",
            "name": "相邻设定温度跃变上限",
            "kind": "ramp_limits",
            "related": {
                "max_change": request.max_setpoint_change,
                "pairs": list(range(1, request.duration)),
            },
            "constraints": [
                c
                for previous, current in zip(setpoint_vars, setpoint_vars[1:])
                for c in (
                    current - previous <= request.max_setpoint_change,
                    previous - current <= request.max_setpoint_change,
                )
            ],
        }
    )

    # Groups 3..: one lock per minute, in increasing minute order.
    for minute in sorted(request.locked_minutes):
        groups.append(
            {
                "key": f"lock_{minute}",
                "name": f"第 {minute} 分钟锁定设定温度 = {request.locked_minutes[minute]}℃",
                "kind": "lock",
                "related": {
                    "minute": minute,
                    "value": request.locked_minutes[minute],
                },
                "constraints": [setpoint_vars[minute - 1] == request.locked_minutes[minute]],
            }
        )

    # Permanent constraints: exact recurrence, non-negativity of every
    # minute-end temperature and the positive-part definition of the heat
    # exposure increment. Trajectory temperature/exposure sequences are kept
    # so the ordered zone groups below can reference them.
    zone_trajectories: List[List[Tuple[List[Any], Any]]] = []
    for zone_index, zone in enumerate(request.zones):
        initial = decimal_to_fraction(Decimal(zone.initial_temperature))
        threshold = decimal_to_fraction(zone.exposure_threshold)
        per_zone: List[Tuple[List[Any], Any]] = []

        for trajectory_index, coefficient in enumerate(zone.coefficients):
            alpha = fraction_to_z3(decimal_to_fraction(coefficient))
            temperature = fraction_to_z3(initial)
            exposure = RealVal(0)
            temperatures: List[Any] = [temperature]

            for minute, setpoint in enumerate(setpoint_vars, start=1):
                temperature = temperature + alpha * (setpoint - temperature)
                temperatures.append(temperature)
                permanent.append(temperature >= 0)

                excess = Real(f"excess_z{zone_index}_a{trajectory_index}_t{minute}")
                positive_branch = Bool(
                    f"positive_z{zone_index}_a{trajectory_index}_t{minute}"
                )
                above_threshold = temperature - fraction_to_z3(threshold)
                # The two branches encode excess == max(0, T-threshold).
                permanent.append(
                    If(
                        positive_branch,
                        And(excess == above_threshold, excess >= 0, above_threshold >= 0),
                        And(excess == 0, above_threshold <= 0),
                    )
                )
                exposure = exposure + excess

            per_zone.append((temperatures, exposure))
        zone_trajectories.append(per_zone)

    # Zone groups, in input zone order: full-horizon upper bounds, final
    # temperature interval, minimum heat exposure (all trajectories of the
    # zone in one group each).
    for zone_index, zone in enumerate(request.zones):
        upper = decimal_to_fraction(zone.max_temperature)
        low = decimal_to_fraction(zone.final_low)
        high = decimal_to_fraction(zone.final_high)
        required = decimal_to_fraction(zone.min_exposure)
        per_zone = zone_trajectories[zone_index]
        trajectory_labels = [
            {"coefficient_index": coefficient_index, "coefficient": str(coefficient)}
            for coefficient_index, coefficient in enumerate(zone.coefficients, start=1)
        ]

        groups.append(
            {
                "key": f"zone_{zone_index}_upper",
                "name": f"{zone.name}：全部轨迹全程温度上限 ≤ {zone.max_temperature}℃",
                "kind": "upper_bound",
                "related": {
                    "zone_index": zone_index + 1,
                    "zone_name": zone.name,
                    "limit": str(zone.max_temperature),
                    "trajectories": trajectory_labels,
                },
                "constraints": [
                    temperature <= fraction_to_z3(upper)
                    for temperatures, _ in per_zone
                    for temperature in temperatures[1:]
                ],
            }
        )
        groups.append(
            {
                "key": f"zone_{zone_index}_final",
                "name": (
                    f"{zone.name}：终温闭区间 [{zone.final_low}, {zone.final_high}]℃"
                ),
                "kind": "final_range",
                "related": {
                    "zone_index": zone_index + 1,
                    "zone_name": zone.name,
                    "low": str(zone.final_low),
                    "high": str(zone.final_high),
                    "trajectories": trajectory_labels,
                },
                "constraints": [
                    c
                    for temperatures, _ in per_zone
                    for c in (
                        temperatures[-1] >= fraction_to_z3(low),
                        temperatures[-1] <= fraction_to_z3(high),
                    )
                ],
            }
        )
        groups.append(
            {
                "key": f"zone_{zone_index}_exposure",
                "name": f"{zone.name}：最低累计热暴露 ≥ {zone.min_exposure}",
                "kind": "min_exposure",
                "related": {
                    "zone_index": zone_index + 1,
                    "zone_name": zone.name,
                    "required": str(zone.min_exposure),
                    "threshold": str(zone.exposure_threshold),
                    "trajectories": trajectory_labels,
                },
                "constraints": [
                    exposure >= fraction_to_z3(required) for _, exposure in per_zone
                ],
            }
        )

    return permanent, groups, setpoint_vars


def make_solver(
    request: SolveRequest,
) -> Tuple[Solver, List[Any]]:
    """Build the same linear MILP model as the original solve path."""
    permanent, groups, setpoint_vars = build_constraint_model(request)
    solver = Solver()
    solver.add(*permanent)
    for group in groups:
        solver.add(*group["constraints"])
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


class DiagnosticFailure(Exception):
    """Raised when a diagnosis cannot be completed and must not be saved."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# The diagnosis shares one 10 s budget across every Z3 re-run, mirroring the
# solve timeout; keep a small reserve for extraction and verification.
DIAGNOSE_TIMEOUT_MS = SOLVE_TIMEOUT_MS
DIAGNOSE_RESERVE_MS = 200


def _eval_int(model: Any, expr: Any) -> int:
    numeral = model.eval(expr, model_completion=True)
    try:
        return int(numeral.as_long())
    except Exception:
        # With the range group switched off Z3 may return values outside the
        # machine-int conversion path; parse the decimal numeral directly.
        return int(numeral.as_string())


def _build_tactic_solver(assertions: Sequence[Any]) -> Any:
    from z3 import Then

    solver = Then("simplify", "propagate-values", "qflia").solver()
    solver.add(*assertions)
    return solver


# Fresh solvers alternate engines on each restart. Caps are sized so multiple
# attempts fit the shared 10 s budget; easy checks finish in milliseconds and
# hand the rest of the budget to the remaining re-runs.
DIAGNOSE_FIRST_CAP_MS = 1_200
DIAGNOSE_RESTART_CAP_MS = 1_200

# Auxiliary, very wide setpoint bound used only to speed up the *candidate*
# deletion pass. A sat under these bounds is exact (and independently verified
# via Fractions); only an unbounded proof can establish unsat, so this bound is
# never present when proving unsatisfiability or recording the MUS.
DIAGNOSE_PROBE_BOUND = 10 ** 9

# Per-check wall caps keep one hard re-run from consuming the whole shared
# 10 s budget: the probe pass fails fast and conservatively retains the group,
# the strict pass gets a little more, and final witnesses get the remainder.
DIAGNOSE_PROBE_WALL_MS = 800
DIAGNOSE_STRICT_WALL_MS = 1_500
DIAGNOSE_WITNESS_WALL_MS = 3_000


def _solver_for(assertions: Sequence[Any], use_tactic: bool) -> Any:
    if use_tactic:
        try:
            return _build_tactic_solver(assertions)
        except Exception:
            pass
    solver = Solver()
    solver.add(*assertions)
    return solver


def _solve_with_assertions(
    assertions: Sequence[Any],
    deadline: float,
    attempt_log: Optional[List[Dict[str, Any]]] = None,
    engine_seed: int = 0,
    max_wall_ms: Optional[int] = None,
) -> Tuple[Any, Any, int]:
    """Decide assertions under the shared deadline, restarting across engines.

    Each attempt is a fresh solver (generic SMT and the QF_LIA tactic pipeline
    alternate) solving the identical constraints. Easy checks finish in
    milliseconds and hand the rest of the budget on; a hard, unlucky run times
    out and gets restarted until one decides, the shared budget is spent, or
    ``max_wall_ms`` of wall time for this check is used (so a single hard check
    cannot starve the remaining re-runs). Every sat/unsat answer is exact; an
    exhausted budget is reported as unknown and the caller aborts without
    saving anything.
    """
    wall_started = time.perf_counter()
    total_ms = 0
    attempt = engine_seed
    while True:
        global_remaining_ms = int((deadline - time.perf_counter()) * 1000)
        if global_remaining_ms <= 0:
            solver = Solver()
            solver.add(*assertions)
            if attempt_log is not None:
                attempt_log.append(
                    {
                        "engine": "budget",
                        "outcome": "unknown",
                        "elapsed_ms": 0,
                        "timeout_ms": 0,
                        "reason": "diagnose budget exhausted before re-run",
                    }
                )
            return unknown, solver, total_ms
        if max_wall_ms is not None:
            wall_remaining_ms = max_wall_ms - int(
                (time.perf_counter() - wall_started) * 1000
            )
            if wall_remaining_ms <= 0:
                solver = Solver()
                solver.add(*assertions)
                return unknown, solver, total_ms
        else:
            wall_remaining_ms = global_remaining_ms
        remaining_ms = min(global_remaining_ms, wall_remaining_ms)
        cap_ms = (
            DIAGNOSE_FIRST_CAP_MS if attempt == engine_seed else DIAGNOSE_RESTART_CAP_MS
        )
        cap_ms = min(cap_ms, remaining_ms)
        use_tactic = (attempt - engine_seed) % 2 == 1
        engine = "qflia" if use_tactic else "smt"
        started = time.perf_counter()
        solver = _solver_for(assertions, use_tactic)
        solver.set("timeout", cap_ms)
        outcome = solver.check()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        total_ms += elapsed_ms
        if attempt_log is not None:
            attempt_log.append(
                {
                    "engine": engine,
                    "outcome": str(outcome),
                    "elapsed_ms": elapsed_ms,
                    "timeout_ms": cap_ms,
                    "reason": None
                    if outcome in (sat, unsat)
                    else str(solver.reason_unknown()),
                }
            )

        if outcome in (sat, unsat):
            return outcome, solver, total_ms
        if str(outcome) not in ("sat", "unsat", "unknown"):
            # Tactic not applicable: continue once on the generic engine.
            if attempt - engine_seed >= 2:
                return unknown, solver, total_ms
        elif elapsed_ms < cap_ms:
            # Unknown for a reason other than timeout (incomplete theory): a
            # retry of identical constraints cannot help beyond one engine swap.
            if attempt - engine_seed >= 1:
                return unknown, solver, total_ms
        attempt += 1


def check_active_groups(
    permanent: Sequence[Any],
    groups: Sequence[ConstraintGroup],
    active_keys: Set[str],
    setpoint_vars: Sequence[Any],
    deadline: float,
    attempt_log: Optional[List[Dict[str, Any]]] = None,
    *,
    bounded: bool = False,
    bound: int = DIAGNOSE_PROBE_BOUND,
    engine_seed: int = 0,
    max_wall_ms: Optional[int] = None,
) -> Tuple[str, Optional[List[int]], str, int]:
    """Run one Z3 check with exactly the given groups switched on.

    When ``bounded`` is true, auxiliary wide bounds ``-bound..bound`` are
    temporarily added to every setpoint: a sat result is then exact (the
    witness is independently re-derived with Fractions), while an unsat result
    is reported as ``"bounded_unsat"`` (not an unbounded proof).
    """
    if deadline <= time.perf_counter():
        return "unknown", None, "diagnose budget exhausted before re-run", 0

    assertions: List[Any] = list(permanent)
    if bounded:
        for value in setpoint_vars:
            assertions.extend((value >= -bound, value <= bound))
    for group in groups:
        if group["key"] in active_keys:
            assertions.extend(group["constraints"])

    outcome, solver, elapsed_ms = _solve_with_assertions(
        assertions, deadline, attempt_log, engine_seed, max_wall_ms
    )
    if outcome == sat:
        values = [_eval_int(solver.model(), value) for value in setpoint_vars]
        return "sat", values, "", elapsed_ms
    if outcome == unsat:
        if bounded:
            return (
                "bounded_unsat",
                None,
                "no solution within the probe bounds (not an unbounded proof)",
                elapsed_ms,
            )
        return "unsat", None, "", elapsed_ms
    return "unknown", None, str(solver.reason_unknown()), elapsed_ms


def build_feasibility_witness(
    request: SolveRequest,
    setpoints: List[int],
    groups: Sequence[ConstraintGroup],
    active_keys: Set[str],
    removed_key: str,
) -> Optional[Dict[str, Any]]:
    """Rebuild trajectories with Fractions and independently verify a witness.

    Every active group (permanent constraints plus the surviving switchable
    groups) must hold exactly; violations of switched-off groups are collected
    so the UI can show why relaxing the removed group makes the subset feasible.
    """
    if len(setpoints) != request.duration or any(not isinstance(v, int) for v in setpoints):
        return None

    by_key = {group["key"]: group for group in groups}

    def group_violation(group_key: str, payload: Dict[str, Any]) -> None:
        violations.append({"group_key": group_key, "group_name": by_key[group_key]["name"], **payload})

    violations: List[Dict[str, Any]] = []

    if "setpoint_range" not in active_keys:
        for minute, value in enumerate(setpoints, start=1):
            if not 0 <= value <= 300:
                group_violation(
                    "setpoint_range",
                    {
                        "kind": "setpoint_range",
                        "minute": minute,
                        "value": value,
                        "low": 0,
                        "high": 300,
                    },
                )

    if "ramp_limits" not in active_keys:
        for minute, (previous, current) in enumerate(
            zip(setpoints, setpoints[1:]), start=1
        ):
            change = abs(current - previous)
            if change > request.max_setpoint_change:
                group_violation(
                    "ramp_limits",
                    {
                        "kind": "ramp_limits",
                        "from_minute": minute,
                        "to_minute": minute + 1,
                        "change": change,
                        "limit": request.max_setpoint_change,
                    },
                )

    for group in groups:
        if group["kind"] != "lock":
            continue
        minute = group["related"]["minute"]
        expected = group["related"]["value"]
        actual = setpoints[minute - 1]
        if group["key"] not in active_keys:
            if actual != expected:
                group_violation(
                    group["key"],
                    {
                        "kind": "lock",
                        "minute": minute,
                        "expected": expected,
                        "actual": actual,
                    },
                )
        elif actual != expected:
            return None

    # Active setpoint-side groups must hold exactly.
    if "setpoint_range" in active_keys and any(not 0 <= v <= 300 for v in setpoints):
        return None
    if "ramp_limits" in active_keys and any(
        abs(a - b) > request.max_setpoint_change
        for a, b in zip(setpoints, setpoints[1:])
    ):
        return None

    trajectory_payloads: List[Dict[str, Any]] = []
    trajectory_index = 0
    for zone_index, zone in enumerate(request.zones):
        upper = decimal_to_fraction(zone.max_temperature)
        low = decimal_to_fraction(zone.final_low)
        high = decimal_to_fraction(zone.final_high)
        threshold = decimal_to_fraction(zone.exposure_threshold)
        required = decimal_to_fraction(zone.min_exposure)
        upper_key = f"zone_{zone_index}_upper"
        final_key = f"zone_{zone_index}_final"
        exposure_key = f"zone_{zone_index}_exposure"

        for coefficient_index, coefficient in enumerate(zone.coefficients, start=1):
            alpha = decimal_to_fraction(coefficient)
            temperatures = [Fraction(zone.initial_temperature)]
            for setpoint in setpoints:
                previous = temperatures[-1]
                temperatures.append(previous + alpha * (Fraction(setpoint) - previous))

            # Non-negativity is a permanent constraint of the exact model.
            if any(value < 0 for value in temperatures[1:]):
                return None

            final_temperature = temperatures[-1]
            max_temperature = max(temperatures[1:])
            exposure_total = sum(
                max(Fraction(0), value - threshold) for value in temperatures[1:]
            )

            if upper_key not in active_keys:
                for minute, value in enumerate(temperatures[1:], start=1):
                    if value > upper:
                        group_violation(
                            upper_key,
                            {
                                "kind": "upper_bound",
                                "zone_index": zone_index + 1,
                                "zone_name": zone.name,
                                "coefficient_index": coefficient_index,
                                "minute": minute,
                                "value": rational_payload(value),
                                "limit": str(zone.max_temperature),
                            },
                        )
            elif any(value > upper for value in temperatures[1:]):
                return None

            if final_key not in active_keys:
                if not low <= final_temperature <= high:
                    group_violation(
                        final_key,
                        {
                            "kind": "final_range",
                            "zone_index": zone_index + 1,
                            "zone_name": zone.name,
                            "coefficient_index": coefficient_index,
                            "low": str(zone.final_low),
                            "high": str(zone.final_high),
                            "actual": rational_payload(final_temperature),
                        },
                    )
            elif not low <= final_temperature <= high:
                return None

            if exposure_key not in active_keys:
                if exposure_total < required:
                    group_violation(
                        exposure_key,
                        {
                            "kind": "min_exposure",
                            "zone_index": zone_index + 1,
                            "zone_name": zone.name,
                            "coefficient_index": coefficient_index,
                            "required": str(zone.min_exposure),
                            "actual": rational_payload(exposure_total),
                        },
                    )
            elif exposure_total < required:
                return None

            trajectory_payloads.append(
                {
                    "zone_index": zone_index + 1,
                    "zone_name": zone.name,
                    "coefficient_index": coefficient_index,
                    "coefficient": str(coefficient),
                    "final_temperature": rational_payload(final_temperature),
                    "max_temperature": rational_payload(max_temperature),
                    "exposure_total": rational_payload(exposure_total),
                    "final_ok": low <= final_temperature <= high,
                    "upper_ok": max_temperature <= upper,
                    "exposure_ok": exposure_total >= required,
                }
            )
            trajectory_index += 1

    return {
        "removed_group": removed_key,
        "setpoints": setpoints,
        "setpoint_sum": sum(setpoints),
        "trajectories": trajectory_payloads,
        "violations": violations,
    }


def group_summary(group: ConstraintGroup) -> Dict[str, Any]:
    return {
        "key": group["key"],
        "name": group["name"],
        "kind": group["kind"],
        "related": group["related"],
    }


def diagnose_request(request: SolveRequest) -> Dict[str, Any]:
    """Find a minimal conflicting group set via ordered trial deletion.

    The ordered groups are processed in the fixed order (setpoint range, ramp
    limits, per-minute locks, then per-zone upper/final/exposure groups). A
    group is deleted only after the remaining groups are *proved* unsatisfiable
    by the exact unbounded model; groups whose removal cannot be proved (or
    that make the remainder satisfiable) stay in.

    To keep the single shared 10 s budget even when a relaxed set turns into a
    hard unbounded integer search, the deletion scan runs in two phases:

    1. a fast probe pass adds temporary wide setpoint bounds — its ``sat``
       answers are exact (witnesses are independently re-derived with
       Fractions) so the group is safely retained, while its ``unsat`` answers
       are only candidate deletions;
    2. every candidate deletion is re-checked on the exact unbounded model and
       deleted only when that model is proved unsatisfiable.

    Finally, for each surviving group a feasibility witness is produced by
    deleting that one group from the final subset (never by reusing a superset
    result), solving, and independently verifying the returned integer curve.
    """
    started = time.perf_counter()
    deadline = started + (DIAGNOSE_TIMEOUT_MS - DIAGNOSE_RESERVE_MS) / 1000

    permanent, groups, setpoint_vars = build_constraint_model(request)
    ordered_keys = [group["key"] for group in groups]
    traces: List[Dict[str, Any]] = []
    checks = 0

    def run(
        active: Set[str],
        stage: str,
        removed: Optional[str],
        *,
        bounded: bool = False,
        engine_seed: int = 0,
        max_wall_ms: Optional[int] = None,
    ) -> Tuple[str, Optional[List[int]]]:
        nonlocal checks
        attempts: List[Dict[str, Any]] = []
        outcome, values, reason, elapsed_ms = check_active_groups(
            permanent,
            groups,
            active,
            setpoint_vars,
            deadline,
            attempts,
            bounded=bounded,
            engine_seed=engine_seed,
            max_wall_ms=max_wall_ms,
        )
        checks += 1
        traces.append(
            {
                "stage": stage,
                "removed_group": removed,
                "bounded": bounded,
                "outcome": str(outcome),
                "elapsed_ms": elapsed_ms,
                "reason": reason or None,
                "attempts": attempts,
            }
        )
        return outcome, values

    def require_decided(outcome: str, message: str) -> None:
        if outcome == "unknown":
            raise DiagnosticFailure(409, "inconclusive", message)

    # First reprove unsat on the exact full group set using the stored input.
    active: Set[str] = set(ordered_keys)
    outcome, _ = run(active, "verify_full", None)
    require_decided(
        outcome,
        "10 秒预算内无法用原模型重新证明全集无解（超时或未决），诊断未保存。",
    )
    if outcome == "sat":
        raise DiagnosticFailure(
            409,
            "not_unsat",
            "按快照保存的输入重跑，原模型存在可行解；该快照的“约束无解”结论无法复现，无法诊断。",
        )

    # Phase 1: fast bounded probe. sat (exact) -> retain; unknown (timeout) ->
    # retain conservatively; bounded_unsat -> mark as a candidate whose
    # deletion must still be proved on the exact unbounded model.
    candidates: List[str] = []
    for key in ordered_keys:
        trial = active - {key}
        outcome, _ = run(
            trial,
            "probe_delete",
            key,
            bounded=True,
            max_wall_ms=DIAGNOSE_PROBE_WALL_MS,
        )
        if outcome == "bounded_unsat":
            candidates.append(key)
            active = trial
        # sat -> group retained; unknown -> group conservatively retained.

    # Phase 2: strictly prove each candidate deletion on the exact unbounded
    # model, in ordered order. Only a proved unsat keeps the deletion.
    for key in ordered_keys:
        if key in active or key not in candidates:
            continue
        trial = active - {key}
        outcome, _ = run(
            trial,
            "strict_delete",
            key,
            bounded=False,
            max_wall_ms=DIAGNOSE_STRICT_WALL_MS,
        )
        require_decided(
            outcome,
            "10 秒预算内无法严格证明某组删除后仍无解（超时或未决），诊断未保存。",
        )
        traces[-1]["action"] = "deleted" if outcome == "unsat" else "reinstated"
        if outcome == "unsat":
            active = trial
        else:
            active.add(key)

    minimal_keys = [key for key in ordered_keys if key in active]

    # Feasibility witnesses must come from re-running the FINAL minimal subset
    # with one group removed at a time; earlier superset sat results are never
    # reused as witnesses. Prefer the exact unbounded model; if that stays
    # undecided within the shared budget, accept a sat found under the wide
    # probe bounds (still exact as a witness, then Fraction-verified).
    witnesses: List[Dict[str, Any]] = []
    for key in minimal_keys:
        trial = active - {key}
        # Each witness gets up to the witness cap, never more than the shared
        # budget still remaining; easy witnesses finish in milliseconds and
        # leave the rest for the others.
        remaining = max(0, int((deadline - time.perf_counter()) * 1000))
        share = min(DIAGNOSE_WITNESS_WALL_MS, remaining)
        if share <= 0:
            raise DiagnosticFailure(
                409,
                "inconclusive",
                "10 秒共享预算已耗尽，无法取得全部删除见证，诊断未保存。",
            )
        values: Optional[List[int]] = None
        outcome, probed = run(
            trial,
            "witness",
            key,
            bounded=False,
            max_wall_ms=share,
        )
        if outcome == "sat" and probed is not None:
            values = probed
            traces[-1]["model"] = "exact"
        elif outcome == "unknown":
            outcome2, bounded_values = run(
                trial,
                "witness_bounded",
                key,
                bounded=True,
                engine_seed=1,
                max_wall_ms=max(
                    0, min(DIAGNOSE_WITNESS_WALL_MS, int((deadline - time.perf_counter()) * 1000))
                ),
            )
            if outcome2 == "sat" and bounded_values is not None:
                values = bounded_values
                traces[-1]["model"] = "bounded_witness"
            else:
                require_decided(
                    outcome2,
                    "最小冲突组删除见证未能在预算内取得可行解，诊断未保存。",
                )
        else:
            raise DiagnosticFailure(
                409,
                "inconclusive",
                "最小冲突组删除后仍被判定无解，与最小性矛盾，诊断未保存。",
            )

        witness = build_feasibility_witness(request, values, groups, trial, key)
        if witness is None:
            raise DiagnosticFailure(
                409,
                "inconclusive",
                "Z3 返回的可行性见证未通过精确有理数独立校验，诊断未保存。",
            )
        witnesses.append(witness)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "timeout_ms": DIAGNOSE_TIMEOUT_MS,
        "elapsed_ms": elapsed_ms,
        "checks": checks,
        "total_groups": len(ordered_keys),
        "groups": [group_summary(group) for group in groups],
        "minimal_groups": minimal_keys,
        "witnesses": witnesses,
        "traces": traces,
    }


def restore_request(request_json: Optional[str]) -> SolveRequest:
    if not request_json:
        raise DiagnosticFailure(
            404, "missing_input", "快照缺少保存的输入参数，无法按原输入诊断。"
        )
    try:
        payload = json.loads(request_json)
        return SolveRequest.model_validate(payload)
    except (ValueError, ValidationError, TypeError) as exc:
        raise DiagnosticFailure(
            404, "missing_input", f"快照保存的输入参数缺失或无法解析：{exc}"
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
    result["has_diagnosis"] = snapshot_id in diagnosed_snapshot_ids()
    return result


@app.post("/api/snapshots/{snapshot_id}/diagnose")
def create_diagnosis(snapshot_id: int) -> Dict[str, Any]:
    record = get_snapshot_record(snapshot_id)
    if record is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    status, request_json = record
    if status != "no_solution":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_proved_unsat",
                "message": "只有状态为“约束无解”（已证明无解）的快照才能诊断；未决或超时快照不属于已证无解。",
            },
        )
    try:
        request = restore_request(request_json)
    except DiagnosticFailure as failure:
        raise HTTPException(
            status_code=failure.status_code,
            detail={"code": failure.code, "message": failure.message},
        )
    try:
        diagnosis = diagnose_request(request)
    except DiagnosticFailure as failure:
        # Undecided / timeout / verification failure: do not persist anything.
        raise HTTPException(
            status_code=failure.status_code,
            detail={"code": failure.code, "message": failure.message},
        )
    save_diagnosis(snapshot_id, diagnosis["elapsed_ms"], diagnosis)
    diagnosis["snapshot_id"] = snapshot_id
    diagnosis["saved"] = True
    return diagnosis


@app.get("/api/snapshots/{snapshot_id}/diagnose")
def read_diagnosis(snapshot_id: int) -> Dict[str, Any]:
    if get_snapshot_record(snapshot_id) is None:
        raise HTTPException(status_code=404, detail="快照不存在")
    diagnosis = get_diagnosis(snapshot_id)
    if diagnosis is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_diagnosis", "message": "该快照尚无已保存的诊断。"},
        )
    diagnosis["snapshot_id"] = snapshot_id
    diagnosis["saved"] = True
    return diagnosis


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
