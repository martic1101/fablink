"""MES-lite: lot creation, dispatch selection and disposition."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import RECIPES
from ..models import Lot, LotStatus

PRODUCTS = {"ETCH": ["N4-A17", "N4-M2"], "CVD": ["N4-A17", "N3-X1"]}


class LotService:
    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._rng = rng or random.Random()
        self._seq = 0

    async def init_sequence(self, s: AsyncSession) -> None:
        self._seq = (await s.scalar(select(func.count()).select_from(Lot))) or 0

    async def create(
        self,
        s: AsyncSession,
        tool_type: str,
        wafers: int,
        recipe: Optional[str] = None,
        product: Optional[str] = None,
        priority: int = 5,
    ) -> Lot:
        if tool_type not in RECIPES:
            raise ValueError(f"unknown tool type {tool_type}")
        recipe = recipe or self._rng.choice(RECIPES[tool_type])
        if recipe not in RECIPES[tool_type]:
            raise ValueError(f"recipe {recipe} is not qualified on {tool_type}")
        if not 1 <= wafers <= 25:
            raise ValueError("a FOUP holds 1-25 wafers")
        self._seq += 1
        lot = Lot(
            id=f"TA{datetime.now(timezone.utc):%y%m%d}{self._seq:04d}.1",
            product=product or self._rng.choice(PRODUCTS[tool_type]),
            tool_type=tool_type,
            recipe=recipe,
            wafers=wafers,
            priority=priority,
        )
        s.add(lot)
        return lot

    async def create_random(self, s: AsyncSession, tool_type: str) -> Lot:
        return await self.create(s, tool_type, self._rng.randint(6, 12))

    @staticmethod
    async def queued_count(s: AsyncSession, tool_type: str) -> int:
        # Counted per tool type: a single fab-wide WIP target let one area's
        # queue fill up with lots for the other, starving its tools.
        q = (
            select(func.count())
            .select_from(Lot)
            .where(Lot.status == LotStatus.QUEUED, Lot.tool_type == tool_type)
        )
        return (await s.scalar(q)) or 0

    @staticmethod
    async def next_for(s: AsyncSession, tool_type: str) -> Optional[Lot]:
        """Dispatch rule: priority first (hot lots), then FIFO."""
        q = (
            select(Lot)
            .where(Lot.status == LotStatus.QUEUED, Lot.tool_type == tool_type)
            .order_by(Lot.priority, Lot.created_at)
            .limit(1)
        )
        return (await s.execute(q)).scalar_one_or_none()

    @staticmethod
    async def release(s: AsyncSession, lot: Lot) -> Lot:
        """Engineering disposition of a held lot: requeue the unprocessed wafers."""
        if lot.status != LotStatus.HOLD:
            raise ValueError("only HOLD lots can be released")
        remaining = lot.wafers - lot.wafers_done
        if remaining <= 0:
            lot.status = LotStatus.COMPLETED
        else:
            lot.status = LotStatus.QUEUED
            lot.equipment_id = None
            lot.hold_reason = None
            lot.priority = min(lot.priority, 2)  # aborted lots get expedited
        return lot
