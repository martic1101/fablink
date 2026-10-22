"""Per-tool health model and remaining-useful-life (RUL) estimation.

Input: one feature per processed wafer (mean of the health sensor while the
wafer was in the chamber). Output:

* ``health``  0-100 — how much of the margin between nominal and the spec
  limit is still left (on an EWMA-smoothed value, so a single noisy wafer
  doesn't flip the status);
* ``rul_wafers`` — wafers until the spec limit is crossed, from a least-squares
  line over the most recent wafers. Degradation accelerates near end of life,
  so a short recent window keeps the slope honest;
* ``status`` — OK / WATCH / PM_REQUIRED, which the dispatcher acts on.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

from ..config import HealthIndicator
from .spc import ControlLimits, Violation, check_rules, compute_limits

EWMA_LAMBDA = 0.3
TREND_WINDOW = 8
BASELINE_WAFERS = 8
WATCH_RUL = 40


class HealthStatus:
    OK = "OK"
    WATCH = "WATCH"
    PM_REQUIRED = "PM_REQUIRED"


def linear_fit(ys: List[float]) -> Tuple[float, float]:
    """Ordinary least squares on (0..n-1, ys). Returns (slope, intercept)."""
    n = len(ys)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    mx = (n - 1) / 2
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in range(n))
    sxy = sum((x - mx) * (y - my) for x, y in enumerate(ys))
    slope = sxy / sxx
    return slope, my - slope * mx


@dataclass
class HealthSnapshot:
    health: float
    ewma: Optional[float]
    rul_wafers: Optional[float]
    slope: float
    status: str
    wafers_since_maint: int
    limits: Optional[ControlLimits]
    violations: List[Violation]


@dataclass
class HealthModel:
    indicator: HealthIndicator
    pm_margin_wafers: int = 3
    history: Deque[float] = field(default_factory=lambda: deque(maxlen=400))
    ewma: Optional[float] = None
    wafers_since_maint: int = 0
    _baseline: List[float] = field(default_factory=list)
    limits: Optional[ControlLimits] = None
    last_violations: List[Violation] = field(default_factory=list)

    def reset(self) -> None:
        """Called after a PM / repair: the part is new, start learning again."""
        self.history.clear()
        self.ewma = None
        self.wafers_since_maint = 0
        self._baseline = []
        self.limits = None
        self.last_violations = []

    def add_wafer(self, value: float) -> List[Violation]:
        self.history.append(value)
        self.wafers_since_maint += 1
        self.ewma = value if self.ewma is None else EWMA_LAMBDA * value + (1 - EWMA_LAMBDA) * self.ewma
        if self.limits is None:
            self._baseline.append(value)
            if len(self._baseline) >= BASELINE_WAFERS:
                # Floor sigma so a very quiet baseline doesn't flag pure noise.
                spread = abs(self.indicator.spec_limit - self.indicator.nominal)
                self.limits = compute_limits(self._baseline, min_sigma=spread * 0.01)
            self.last_violations = []
        else:
            self.last_violations = check_rules(list(self.history)[-10:], self.limits)
        return self.last_violations

    # ---- derived values ----------------------------------------------------------------------
    @property
    def health(self) -> float:
        if self.ewma is None:
            return 100.0
        ind = self.indicator
        used = (self.ewma - ind.nominal) / (ind.spec_limit - ind.nominal)
        return round(max(0.0, min(1.0, 1.0 - used)) * 100, 1)

    def trend(self) -> Tuple[float, Optional[float]]:
        """(slope per wafer, predicted wafers until spec limit or None)."""
        recent = list(self.history)[-TREND_WINDOW:]
        if len(recent) < 4:
            return 0.0, None
        slope, intercept = linear_fit(recent)
        current = intercept + slope * (len(recent) - 1)
        remaining = self.indicator.spec_limit - current
        if remaining <= 0:
            return slope, 0.0
        if slope <= 1e-12:
            return slope, None  # not degrading
        return slope, remaining / slope

    def needs_pm_before(self, wafers: int) -> bool:
        """Would running ``wafers`` more wafers likely cross the spec limit?"""
        _, rul = self.trend()
        if self.ewma is not None and self.ewma >= self.indicator.spec_limit:
            return True
        return rul is not None and rul < wafers + self.pm_margin_wafers

    def snapshot(self, next_lot_wafers: int = 12) -> HealthSnapshot:
        slope, rul = self.trend()
        if self.needs_pm_before(next_lot_wafers):
            status = HealthStatus.PM_REQUIRED
        elif (rul is not None and rul < WATCH_RUL) or self.health < 40:
            status = HealthStatus.WATCH
        else:
            status = HealthStatus.OK
        return HealthSnapshot(
            health=self.health,
            ewma=round(self.ewma, 4) if self.ewma is not None else None,
            rul_wafers=round(rul, 1) if rul is not None else None,
            slope=slope,
            status=status,
            wafers_since_maint=self.wafers_since_maint,
            limits=self.limits,
            violations=self.last_violations,
        )
