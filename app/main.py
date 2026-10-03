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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
