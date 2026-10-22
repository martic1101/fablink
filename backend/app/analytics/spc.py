"""Statistical Process Control — Western Electric rules on per-wafer features.

Control limits come from a baseline window (the first wafers after a
maintenance) rather than from the spec, which is how FDC systems catch a tool
that is drifting long before it is out of spec.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class ControlLimits:
    mean: float
    sigma: float

    def zone(self, x: float) -> float:
        return (x - self.mean) / self.sigma if self.sigma > 0 else 0.0

    @property
    def ucl(self) -> float:
        return self.mean + 3 * self.sigma

    @property
    def lcl(self) -> float:
        return self.mean - 3 * self.sigma


def compute_limits(baseline: Sequence[float], min_sigma: float = 1e-9) -> Optional[ControlLimits]:
    if len(baseline) < 2:
        return None
    return ControlLimits(statistics.fmean(baseline), max(statistics.stdev(baseline), min_sigma))


@dataclass(frozen=True)
class Violation:
    rule: int
    description: str


def check_rules(values: Sequence[float], limits: ControlLimits) -> List[Violation]:
    """Evaluate the Western Electric rules on the *latest* point of ``values``.

    1. One point beyond 3 sigma
    2. Two of three consecutive points beyond 2 sigma on the same side
    3. Four of five consecutive points beyond 1 sigma on the same side
    4. Eight consecutive points on the same side of the centre line
    Plus the commonly added trend rule:
    5. Six consecutive points steadily increasing or decreasing
    """
    if not values:
        return []
    z = [limits.zone(v) for v in values]
    out: List[Violation] = []

    if abs(z[-1]) > 3:
        out.append(Violation(1, "point beyond 3σ"))

    def same_side_count(window: List[float], k: float) -> int:
        side = 1 if window[-1] > 0 else -1
        return sum(1 for v in window if v * side > k)

    if len(z) >= 3 and abs(z[-1]) > 2 and same_side_count(z[-3:], 2) >= 2:
        out.append(Violation(2, "2 of 3 points beyond 2σ"))
    if len(z) >= 5 and abs(z[-1]) > 1 and same_side_count(z[-5:], 1) >= 4:
        out.append(Violation(3, "4 of 5 points beyond 1σ"))
    if len(z) >= 8 and (all(v > 0 for v in z[-8:]) or all(v < 0 for v in z[-8:])):
        out.append(Violation(4, "8 points on one side of centre"))
    if len(values) >= 6:
        last = values[-6:]
        diffs = [b - a for a, b in zip(last, last[1:])]
        if all(d > 0 for d in diffs) or all(d < 0 for d in diffs):
            out.append(Violation(5, "6 points trending"))
    return out
