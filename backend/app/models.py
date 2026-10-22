"""Relational schema (works on SQLite for the demo and PostgreSQL in production)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 with an explicit UTC offset. SQLite hands back naive datetimes
    even for timezone-aware columns; without this the browser would read them
    as local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class LotStatus:
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"  # S2F41 START accepted, waiting for LOT_STARTED
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    HOLD = "HOLD"  # aborted / needs engineering disposition


class Equipment(Base):
    __tablename__ = "equipment"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tool_type: Mapped[Optional[str]] = mapped_column(String(16))
    mdln: Mapped[Optional[str]] = mapped_column(String(32))
    softrev: Mapped[Optional[str]] = mapped_column(String(32))
    host: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="OFFLINE")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lot(Base):
    __tablename__ = "lots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    product: Mapped[str] = mapped_column(String(32))
    tool_type: Mapped[str] = mapped_column(String(16))
    recipe: Mapped[str] = mapped_column(String(32))
    wafers: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=5)  # 1 = hot lot
    status: Mapped[str] = mapped_column(String(16), default=LotStatus.QUEUED, index=True)
    equipment_id: Mapped[Optional[str]] = mapped_column(ForeignKey("equipment.id"))
    wafers_done: Mapped[int] = mapped_column(Integer, default=0)
    wafers_scrapped: Mapped[int] = mapped_column(Integer, default=0)
    hold_reason: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    @property
    def remaining(self) -> int:
        return self.wafers - self.wafers_done


class WaferResult(Base):
    """One row per processed wafer, with the FDC summary of the health sensor."""

    __tablename__ = "wafer_results"
    __table_args__ = (Index("ix_wafer_eq_time", "equipment_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("lots.id"))
    equipment_id: Mapped[str] = mapped_column(ForeignKey("equipment.id"))
    slot: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(8))
    health_value: Mapped[Optional[float]] = mapped_column(Float)
    predictive_mode: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alarm(Base):
    __tablename__ = "alarms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_id: Mapped[str] = mapped_column(ForeignKey("equipment.id"), index=True)
    alid: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String(128))
    severity: Mapped[int] = mapped_column(Integer)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class WorkOrderStatus:
    OPEN = "OPEN"  # raised; tool is held (no new lots)
    SENT = "SENT"  # handed to the technicians
    IN_PROGRESS = "IN_PROGRESS"  # tool reported MAINT_STARTED
    DONE = "DONE"
    CANCELLED = "CANCELLED"

    ACTIVE = (OPEN, SENT, IN_PROGRESS)


class Maintenance(Base):
    """A maintenance work order and, once the tool reports it, the maintenance itself."""

    __tablename__ = "maintenance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_id: Mapped[str] = mapped_column(ForeignKey("equipment.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # PM | REPAIR
    trigger: Mapped[str] = mapped_column(String(16))  # PREDICTIVE | REACTIVE | MANUAL
    status: Mapped[str] = mapped_column(String(16), default=WorkOrderStatus.OPEN, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(160))
    predictive_mode: Mapped[bool] = mapped_column(default=True)
    # For a repair this is the failure time: downtime counts from here.
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class StateHistory(Base):
    """E10-style state intervals, used to compute availability."""

    __tablename__ = "state_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_id: Mapped[str] = mapped_column(ForeignKey("equipment.id"), index=True)
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[Optional[str]] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_id: Mapped[str] = mapped_column(String(32), index=True)
    event: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
