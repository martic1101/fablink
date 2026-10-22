"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class HealthIndicator:
    """Which sensor tells us a tool type is wearing out, and its limits.

    ``spec_limit`` is the process quality limit: past it wafers start to be
    scrapped, so this is what the remaining-useful-life (RUL) predicts against.
    """

    sv_name: str
    nominal: float
    spec_limit: float
    units: str


HEALTH_INDICATORS: Dict[str, HealthIndicator] = {
    "ETCH": HealthIndicator("RF_REFL_POWER", 12.0, 40.0, "W"),
    "CVD": HealthIndicator("FORELINE_PRESSURE", 0.15, 0.47, "Torr"),
}

RECIPES: Dict[str, List[str]] = {
    "ETCH": ["ETCH_POLY_01", "ETCH_OX_02"],
    "CVD": ["CVD_TEOS_01", "CVD_SIN_02"],
}


def _parse_equipment(raw: str) -> List[Tuple[str, str, int]]:
    """Parse "ETCH01@127.0.0.1:5001,CVD01@10.0.0.5:5000" into [(id, host, port), ...]."""
    out = []
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        tool_id, addr = entry.split("@")
        host, port = addr.rsplit(":", 1)
        out.append((tool_id, host, int(port)))
    return out


DEFAULT_EQUIPMENT = "ETCH01@127.0.0.1:5001,ETCH02@127.0.0.1:5002,CVD01@127.0.0.1:5003,CVD02@127.0.0.1:5004"


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./fab.db")
    )
    equipment: List[Tuple[str, str, int]] = field(
        default_factory=lambda: _parse_equipment(os.getenv("FAB_EQUIPMENT", DEFAULT_EQUIPMENT))
    )
    t3_seconds: float = float(os.getenv("HSMS_T3", "5"))
    linktest_seconds: float = float(os.getenv("HSMS_LINKTEST", "10"))
    dispatch_interval: float = float(os.getenv("DISPATCH_INTERVAL", "1.0"))
    # Demo behaviour: keep this many lots queued so the fab never runs dry.
    wip_target: int = int(os.getenv("WIP_TARGET", "6"))
    start_background: bool = os.getenv("START_BACKGROUND", "1") == "1"
    # Allow the console's "Shut down" button (stops the simulator and this process).
    allow_shutdown: bool = os.getenv("ALLOW_SHUTDOWN", "1") == "1"
    # Simulator control API (demo only): scenario controls proxied under
    # /api/sim, and the simulated technicians who receive work orders.
    sim_control_url: str = field(
        default_factory=lambda: os.getenv("SIM_CONTROL_URL", "http://127.0.0.1:8100")
    )
