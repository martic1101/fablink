"""FabHost — wires equipment events to the MES, analytics and the UI.

Event flow for one wafer::

    tool --S6F11 WAFER_STARTED--> host  (start collecting trace samples)
    tool --S6F1  trace ......---> host  (health sensor samples)
    tool --S6F11 WAFER_COMPLETED> host  -> wafer feature = mean(samples)
                                        -> HealthModel.add_wafer() -> SPC / RUL
                                        -> WaferResult row, lot counters
                                        -> WebSocket push

Maintenance follows the fab's division of labour: the host never performs it.
When the health model predicts the tool will cross its spec limit (or the tool
fails), the host opens a work order, stops dispatching lots to the tool and
hands the work order to the technicians. They work at the tool in ONLINE LOCAL
control, and the tool reports MAINT_STARTED / MAINT_COMPLETED.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Deque, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .analytics.health import HealthModel, HealthStatus
from .bus import EventBus
from .config import HEALTH_INDICATORS, RECIPES, Settings
from .equipment.gateway import EquipmentLink
from .maintenance import WorkOrderSink
from .mes.service import LotService
from .models import (
    Alarm,
    Equipment,
    EventLog,
    Lot,
    LotStatus,
    Maintenance,
    StateHistory,
    WaferResult,
    WorkOrderStatus,
    iso,
    utcnow,
)
from .secs.hsms import SecsMessage
from .secs.messages import (
    CEID,
    HCACK,
    RCMD,
    ControlState,
    EquipmentState,
    EventReport,
    ack_body,
    parse_s5f1,
    parse_s6f1,
    parse_s6f11,
)
from .simulation import SimulatorControl

log = logging.getLogger(__name__)

OK_ACKS = (HCACK.OK, HCACK.ACCEPTED_WILL_FINISH_LATER)
SEND_RETRY_SECONDS = 5.0


class FabHost:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        bus: EventBus,
        work_order_sink: Optional[WorkOrderSink] = None,
        simulator: Optional[SimulatorControl] = None,
    ) -> None:
        self.settings = settings
        self._sessionmaker = sessionmaker
        self.bus = bus
        self.work_order_sink = work_order_sink
        self.simulator = simulator  # demo only: lets the console pause simulated time
        self.lots = LotService()
        self.links: Dict[str, EquipmentLink] = {}
        self.models: Dict[str, HealthModel] = {}
        self.secs_log: Deque[Dict[str, Any]] = deque(maxlen=400)
        # Plain-language log of what each part of the system just did. Drives
        # the System map page; also handy when debugging a tool integration.
        self.activity_log: Deque[Dict[str, Any]] = deque(maxlen=600)
        self._activity_seq = 0
        # Operator-controllable modes
        self.running = True  # False = simulation paused: nothing is dispatched or sent
        self.predictive = True
        self.auto_dispatch = True
        self.auto_work_orders = True  # hand work orders to technicians without an engineer clicking
        self.auto_lots = True
        self._tasks: List[asyncio.Task] = []
        # work order id -> loop time of the next send attempt after a failure
        self._send_retry_at: Dict[int, float] = {}

        for tool_id, host, port in settings.equipment:
            self.links[tool_id] = EquipmentLink(
                tool_id,
                host,
                port,
                self._on_message,
                self._on_status,
                self._tap,
                lambda link, kind, text, data: self.activity("gateway", link.tool_id, kind, text, data),
                t3=settings.t3_seconds,
                linktest=settings.linktest_seconds,
            )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as s:
            yield s
            await s.commit()

    # ---- lifecycle ----------------------------------------------------------------------------
    async def start(self) -> None:
        async with self.session() as s:
            await self.lots.init_sequence(s)
            for link in self.links.values():
                eq = await s.get(Equipment, link.tool_id)
                if eq is None:
                    s.add(Equipment(id=link.tool_id, host=link.host, port=link.port))
                else:
                    eq.host, eq.port, eq.state = link.host, link.port, EquipmentState.OFFLINE
            # Crash recovery: lots that were on a tool when the host died need disposition.
            await s.execute(
                update(Lot)
                .where(Lot.status.in_([LotStatus.DISPATCHED, LotStatus.RUNNING]))
                .values(status=LotStatus.HOLD, hold_reason="host restarted while lot in process")
            )
            await s.execute(
                update(StateHistory).where(StateHistory.ended_at.is_(None)).values(ended_at=utcnow())
            )
            # Open work orders survive a restart: the tools stay held.
            active = await s.execute(
                select(Maintenance).where(Maintenance.status.in_(WorkOrderStatus.ACTIVE))
            )
            for wo in active.scalars():
                link = self.links.get(wo.equipment_id)
                if link:
                    link.work_order = self.work_order_view(wo)
                    link.maint_active = wo.status == WorkOrderStatus.IN_PROGRESS
        if self.simulator:
            # Adopt the simulator's clock state, e.g. after a host restart while paused.
            try:
                self.running = not await self.simulator.is_paused()
            except Exception:
                pass
        for link in self.links.values():
            link.start()
        if self.settings.start_background:
            self._tasks.append(asyncio.ensure_future(self._dispatch_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for link in self.links.values():
            await link.stop()

    # ---- activity stream ----------------------------------------------------------------------
    def activity(
        self, block: str, tool: Optional[str], kind: str, text: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record what a block of the system did. ``block`` is one of
        simulator, gateway, host, analytics, dispatcher, mes, database, api."""
        self._activity_seq += 1
        entry = {
            "id": self._activity_seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "block": block,
            "tool": tool,
            "kind": kind,
            "text": text,
            "data": data or {},
        }
        self.activity_log.append(entry)
        self.bus.publish("activity", entry)

    # ---- SECS message log ---------------------------------------------------------------------
    def _tap(self, link: EquipmentLink, direction: str, msg: SecsMessage) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": link.tool_id,
            "dir": direction,
            "sf": msg.name,
            "sml": msg.to_sml(),
        }
        self.secs_log.append(entry)
        if msg.stream != 6 or msg.function != 1:  # trace data is too chatty for the live log
            self.bus.publish("secs", entry)

    # ---- incoming messages --------------------------------------------------------------------
    async def _on_message(self, link: EquipmentLink, msg: SecsMessage) -> Optional[SecsMessage]:
        sf = (msg.stream, msg.function)
        if sf == (6, 1):
            self._on_trace(link, msg)
            return SecsMessage(6, 2, ack_body())
        if sf == (6, 11):
            ev = parse_s6f11(msg.body)
            # The tool resends a report whose reply timed out, even if we had
            # already processed it. Acknowledge again but don't apply it twice.
            if link.seen_before((ev.dataid, int(ev.ceid), repr(ev.values))):
                self.activity(
                    "host",
                    link.tool_id,
                    "event.duplicate",
                    f"Ignored a resent S6F11 {ev.ceid.name} (DATAID {ev.dataid}): already processed. "
                    "The tool resends a report when our reply times out.",
                    {"dataid": ev.dataid},
                )
            else:
                await self._on_event(link, ev)
            return SecsMessage(6, 12, ack_body())
        if sf == (5, 1):
            await self._on_alarm(link, msg)
            return SecsMessage(5, 2, ack_body())
        return SecsMessage(msg.stream, 0)

    def _on_trace(self, link: EquipmentLink, msg: SecsMessage) -> None:
        tr = parse_s6f1(msg.body)
        link.add_trace(tr.stime, tr.values)
        link.last_frame = (tr.stime, msg)
        ind = HEALTH_INDICATORS.get(link.tool_type or "")
        if ind and link.slot:
            idx = link.sensor_index(ind.sv_name)
            if idx is not None:
                link.wafer_samples.append(tr.values[idx])
                if link.first_trace_pending:
                    link.first_trace_pending = False
                    svid, name, units = link.sensors[idx]
                    self.activity(
                        "host",
                        link.tool_id,
                        "trace.first",
                        f"S6F1 trace: value #{idx + 1} is SVID {svid} = {name} (learned from S1F12) "
                        f"= {tr.values[idx]:.4g} {units}. Sampling every trace until the wafer ends.",
                        {"svid": svid, "name": name, "value": tr.values[idx]},
                    )
        self.bus.publish("trace", {"tool": link.tool_id, "ts": tr.stime, "values": tr.values})

    async def _on_status(self, link: EquipmentLink) -> None:
        """Called by the gateway on connect / disconnect."""
        if link.connected:
            # Everything the tool sends before its "communication established"
            # event is a replay of what it spooled while we were away.
            link.counting_replay, link.replay_count = True, 0
        async with self.session() as s:
            if link.connected and link.tool_type in HEALTH_INDICATORS and link.tool_id not in self.models:
                self.models[link.tool_id] = await self._restore_model(s, link)
            eq = await s.get(Equipment, link.tool_id)
            if eq:
                eq.tool_type, eq.mdln, eq.softrev = link.tool_type, link.mdln, link.softrev
            if not link.connected:
                await self._record_state(s, link, EquipmentState.OFFLINE, link.last_error or "link down")
                link.recorded_state = EquipmentState.OFFLINE
        self.publish_equipment(link)

    async def _restore_model(self, s: AsyncSession, link: EquipmentLink) -> HealthModel:
        """Rebuild the health model from wafers processed since the tool's last
        completed maintenance. Without this a host restart would forget how worn
        every tool is, and the dispatcher would happily load a lot onto a tool
        that is a few wafers from failing."""
        model = HealthModel(HEALTH_INDICATORS[link.tool_type or ""])
        last_maint = await s.scalar(
            select(Maintenance.finished_at)
            .where(Maintenance.equipment_id == link.tool_id, Maintenance.status == WorkOrderStatus.DONE)
            .order_by(Maintenance.finished_at.desc())
            .limit(1)
        )
        q = select(WaferResult.health_value).where(
            WaferResult.equipment_id == link.tool_id, WaferResult.health_value.is_not(None)
        )
        if last_maint is not None:
            q = q.where(WaferResult.created_at > last_maint)
        values = (await s.execute(q.order_by(WaferResult.id.desc()).limit(200))).scalars().all()
        for v in reversed(values):
            model.add_wafer(v)
        if values:
            log.info("%s: health model restored from %d wafers", link.tool_id, len(values))
        return model

    async def _record_state(self, s: AsyncSession, link: EquipmentLink, state: str, reason: str) -> None:
        now = utcnow()
        await s.execute(
            update(StateHistory)
            .where(StateHistory.equipment_id == link.tool_id, StateHistory.ended_at.is_(None))
            .values(ended_at=now)
        )
        s.add(StateHistory(equipment_id=link.tool_id, state=state, reason=reason, started_at=now))
        await s.execute(
            update(Equipment).where(Equipment.id == link.tool_id).values(state=state, updated_at=now)
        )

    async def _on_event(self, link: EquipmentLink, ev: EventReport) -> None:
        v = ev.values
        if link.counting_replay:
            if ev.ceid is CEID.STATE_CHANGED and v[1] == "communication established":
                link.counting_replay = False
                self.activity(
                    "host",
                    link.tool_id,
                    "spool.replayed",
                    f"Tool replayed {link.replay_count} spooled report(s) after S6F23; "
                    "host state has caught up."
                    if link.replay_count
                    else "Tool had nothing spooled; host state is current.",
                    {"count": link.replay_count},
                )
            else:
                link.replay_count += 1
        async with self.session() as s:
            s.add(EventLog(equipment_id=link.tool_id, event=ev.ceid.name, data={"values": v}))

            if ev.ceid is CEID.STATE_CHANGED:
                state, reason = str(v[0]), str(v[1])
                failed = state == EquipmentState.DOWN and link.control_state != ControlState.LOCAL
                note = " Recorded as unscheduled downtime." if failed else ""
                self.activity(
                    "host",
                    link.tool_id,
                    "state",
                    f"S6F11 STATE_CHANGED: {state} ({reason}).{note}",
                    {"state": state, "reason": reason},
                )
                # While disconnected the OFFLINE interval stands; a handler from
                # the dropped session must not overwrite it.
                if link.connected and state != link.recorded_state:
                    await self._record_state(s, link, state, reason)
                    link.recorded_state = state
                link.state = state
                link.busy = False
                if failed:
                    await self.open_work_order(s, link, "REPAIR", "REACTIVE", reason)

            elif ev.ceid is CEID.CONTROL_STATE_CHANGED:
                link.control_state = str(v[0])
                local = link.control_state == ControlState.LOCAL
                self.activity(
                    "host",
                    link.tool_id,
                    "control",
                    f"S6F11 CONTROL_STATE_CHANGED: {link.control_state}. "
                    + (
                        "Someone is working at the tool; it refuses remote commands."
                        if local
                        else "The host has control again."
                    ),
                    {"state": link.control_state},
                )

            elif ev.ceid is CEID.LOT_STARTED:
                link.lot_id, link.lot_wafers, link.slot = str(v[0]), int(v[2]), 0
                lot = await s.get(Lot, link.lot_id)
                if lot:
                    lot.status, lot.started_at, lot.equipment_id = LotStatus.RUNNING, utcnow(), link.tool_id
                    self.publish_lot(lot)
                self.activity(
                    "mes",
                    link.tool_id,
                    "lot.started",
                    f"Lot {v[0]} RUNNING on {link.tool_id}: {v[2]} wafers, recipe {v[1]}.",
                )

            elif ev.ceid is CEID.WAFER_STARTED:
                link.slot = int(v[1])
                link.wafer_samples = []
                link.first_trace_pending = True
                ind = HEALTH_INDICATORS.get(link.tool_type or "")
                self.activity(
                    "host",
                    link.tool_id,
                    "wafer.started",
                    f"S6F11 WAFER_STARTED: lot {v[0]} slot {v[1]}. "
                    f"Start collecting {ind.sv_name if ind else 'sensor'} from trace data.",
                    {"lot": v[0], "slot": v[1]},
                )

            elif ev.ceid is CEID.WAFER_COMPLETED:
                await self._on_wafer_completed(s, link, str(v[0]), int(v[1]), str(v[2]))

            elif ev.ceid is CEID.LOT_COMPLETED:
                lot = await s.get(Lot, str(v[0]))
                if lot:
                    lot.status, lot.finished_at = LotStatus.COMPLETED, utcnow()
                    self.publish_lot(lot)
                self.activity(
                    "mes",
                    link.tool_id,
                    "lot.completed",
                    f"Lot {v[0]} COMPLETED: {v[1]} good, {v[2]} scrapped.",
                )
                link.lot_id, link.slot = None, 0

            elif ev.ceid is CEID.LOT_ABORTED:
                lot = await s.get(Lot, str(v[0]))
                if lot:
                    lot.status, lot.hold_reason, lot.finished_at = LotStatus.HOLD, str(v[1]), utcnow()
                    self.publish_lot(lot)
                done = f" {lot.wafers_done} of {lot.wafers} wafers were processed." if lot else ""
                self.activity(
                    "mes",
                    link.tool_id,
                    "lot.hold",
                    f"S6F11 LOT_ABORTED: lot {v[0]} put on HOLD ({v[1]}).{done} "
                    "An engineer decides what happens to the rest.",
                    {"lot": v[0]},
                )
                link.lot_id, link.slot = None, 0

            elif ev.ceid is CEID.MAINT_STARTED:
                await self._on_maint_started(s, link, str(v[0]), int(v[1]) if len(v) > 1 else 0)

            elif ev.ceid is CEID.MAINT_COMPLETED:
                await self._on_maint_completed(s, link, str(v[0]), int(v[1]) if len(v) > 1 else 0)
        self.publish_equipment(link)

    async def _on_wafer_completed(
        self, s: AsyncSession, link: EquipmentLink, lot_id: str, slot: int, result: str
    ) -> None:
        feature: Optional[float] = None
        ind = HEALTH_INDICATORS.get(link.tool_type or "")
        if link.wafer_samples:
            feature = statistics.fmean(link.wafer_samples)
        elif ind and link.latest:
            idx = link.sensor_index(ind.sv_name)
            feature = link.latest[idx] if idx is not None else None
        n_samples = len(link.wafer_samples)
        link.wafer_samples = []
        self.activity(
            "host",
            link.tool_id,
            "wafer.completed",
            f"S6F11 WAFER_COMPLETED: lot {lot_id} slot {slot} result {result}.",
            {"lot": lot_id, "slot": slot, "result": result},
        )

        model = self.models.get(link.tool_id)
        previous = {v.rule for v in model.last_violations} if model else set()
        violations = model.add_wafer(feature) if (model and feature is not None) else []
        # Persist only rules that just started firing; a drifting tool would
        # otherwise log the same violation on every wafer.
        new_violations = [v for v in violations if v.rule not in previous]
        s.add(
            WaferResult(
                lot_id=lot_id,
                equipment_id=link.tool_id,
                slot=slot,
                result=result,
                health_value=feature,
                predictive_mode=self.predictive,
            )
        )
        lot = await s.get(Lot, lot_id)
        if lot:
            lot.wafers_done += 1
            if result == "SCRAP":
                lot.wafers_scrapped += 1
            self.publish_lot(lot)
        for vio in new_violations:
            s.add(
                EventLog(
                    equipment_id=link.tool_id,
                    event="SPC_VIOLATION",
                    data={"rule": vio.rule, "text": vio.description, "value": feature},
                )
            )
        if feature is not None and ind:
            self.activity(
                "analytics",
                link.tool_id,
                "analytics.feature",
                f"Wafer feature: mean {ind.sv_name} over {n_samples} trace samples "
                f"= {feature:.4g} {ind.units}.",
                {"value": feature, "samples": n_samples},
            )
        snap = model.snapshot() if (model and ind) else None
        if snap and ind:
            if snap.rul_wafers is None:
                rul = "no wear trend yet"
            else:
                rul = f"{snap.rul_wafers:.0f} wafers to spec limit"
            spc = f" SPC: {', '.join(v.description for v in violations)}." if violations else ""
            self.activity(
                "analytics",
                link.tool_id,
                "analytics.health",
                f"EWMA {snap.ewma:.4g} {ind.units}, health {snap.health:.0f}/100, {rul}, "
                f"status {snap.status}.{spc}"
                if snap.ewma is not None
                else f"Status {snap.status}.",
                {"status": snap.status, "health": snap.health, "rul": snap.rul_wafers},
            )
        value_sql = "NULL" if feature is None else f"{feature:.4g}"
        self.activity(
            "database",
            link.tool_id,
            "db.wafer",
            f"INSERT INTO wafer_results (lot_id, slot, result, health_value) "
            f"VALUES ('{lot_id}', {slot}, '{result}', {value_sql}).",
        )
        self.bus.publish(
            "wafer",
            {
                "tool": link.tool_id,
                "lot": lot_id,
                "slot": slot,
                "result": result,
                "value": feature,
                "violations": [v.description for v in violations],
            },
        )
        self.activity(
            "api",
            link.tool_id,
            "ws.pushed",
            f"Pushed the wafer update to {self.bus.subscriber_count} browser(s) over WebSocket.",
        )
        # Predictive maintenance: raise the work order as soon as the trend says
        # so, while the current lot finishes, so technicians can already set off.
        if self.predictive and snap and snap.status == HealthStatus.PM_REQUIRED and link.work_order is None:
            left = "now" if not snap.rul_wafers else f"in about {snap.rul_wafers:.0f} wafers"
            await self.open_work_order(
                s, link, "PM", "PREDICTIVE", f"health model predicts the spec limit {left}"
            )

    async def _on_alarm(self, link: EquipmentLink, msg: SecsMessage) -> None:
        al = parse_s5f1(msg.body)
        if link.counting_replay:
            link.replay_count += 1
        async with self.session() as s:
            open_q = select(Alarm).where(
                Alarm.equipment_id == link.tool_id, Alarm.alid == al.alid, Alarm.cleared_at.is_(None)
            )
            already_open = (await s.execute(open_q)).first() is not None
            if al.is_set and already_open:
                return  # a resend: alarms are idempotent by state
            self.activity(
                "host",
                link.tool_id,
                "alarm.set" if al.is_set else "alarm.cleared",
                f"S5F1 alarm {al.alid} {'set' if al.is_set else 'cleared'}: {al.text}.",
                {"alid": al.alid, "severity": al.severity},
            )
            if al.is_set:
                s.add(Alarm(equipment_id=link.tool_id, alid=al.alid, text=al.text, severity=al.severity))
            else:
                await s.execute(
                    update(Alarm)
                    .where(
                        Alarm.equipment_id == link.tool_id,
                        Alarm.alid == al.alid,
                        Alarm.cleared_at.is_(None),
                    )
                    .values(cleared_at=utcnow())
                )
        self.bus.publish(
            "alarm",
            {
                "tool": link.tool_id,
                "alid": al.alid,
                "text": al.text,
                "set": al.is_set,
                "severity": al.severity,
            },
        )

    # ---- maintenance work orders -------------------------------------------------------------
    @staticmethod
    def work_order_view(wo: Maintenance) -> Dict[str, Any]:
        return {
            "id": wo.id,
            "tool": wo.equipment_id,
            "kind": wo.kind,
            "trigger": wo.trigger,
            "status": wo.status,
            "reason": wo.reason,
            "requested_at": iso(wo.requested_at),
            "started_at": iso(wo.started_at),
            "finished_at": iso(wo.finished_at),
        }

    def _publish_work_order(self, wo: Maintenance) -> None:
        self.bus.publish("work_order", self.work_order_view(wo))

    async def open_work_order(
        self, s: AsyncSession, link: EquipmentLink, kind: str, trigger: str, reason: str
    ) -> Dict[str, Any]:
        """Raise a work order and hold the tool. One active work order per tool;
        a failure turns a pending PM into a repair."""
        current = link.work_order
        if current:
            if current["kind"] == kind or kind == "PM":
                return current
            await self.cancel_work_order(s, link, "superseded: the tool failed before the PM")
        wo = Maintenance(
            equipment_id=link.tool_id,
            kind=kind,
            trigger=trigger,
            reason=reason[:160],
            predictive_mode=self.predictive,
            status=WorkOrderStatus.OPEN,
        )
        s.add(wo)
        await s.flush()
        link.work_order = self.work_order_view(wo)
        busy = " The current lot finishes first." if link.state == EquipmentState.PROCESSING else ""
        self.activity(
            "dispatcher",
            link.tool_id,
            "wo.opened",
            f"Opened {kind} work order #{wo.id} ({trigger.lower()}): {reason}. "
            f"{link.tool_id} is held: no new lots.{busy}",
            {"id": wo.id, "kind": kind, "trigger": trigger},
        )
        if kind == "REPAIR":
            self.activity(
                "database",
                link.tool_id,
                "repair.opened",
                f"INSERT INTO maintenance (kind='REPAIR', trigger='{trigger}'): unscheduled downtime "
                "counts from the failure, not from when the technician arrives.",
            )
        self._publish_work_order(wo)
        return link.work_order

    async def cancel_work_order(self, s: AsyncSession, link: EquipmentLink, reason: str) -> None:
        current = link.work_order
        if not current:
            return
        wo = await s.get(Maintenance, current["id"])
        if wo is None or wo.status not in WorkOrderStatus.ACTIVE:
            link.work_order = None
            return
        was_sent = wo.status == WorkOrderStatus.SENT
        wo.status, wo.finished_at = WorkOrderStatus.CANCELLED, utcnow()
        wo.reason = f"{wo.reason or ''} | cancelled: {reason}"[:160]
        link.work_order = None
        if was_sent and self.work_order_sink:
            try:
                await self.work_order_sink.cancel(wo.id)
            except Exception as exc:  # best effort; the crew job just waits otherwise
                log.warning("could not cancel work order %d with technicians: %s", wo.id, exc)
        self.activity("dispatcher", link.tool_id, "wo.cancelled", f"Work order #{wo.id} cancelled: {reason}.")
        self._publish_work_order(wo)

    async def send_work_order(self, link: EquipmentLink) -> bool:
        """Hand the tool's open work order to the technicians."""
        current = link.work_order
        if not current or current["status"] != WorkOrderStatus.OPEN:
            return False
        wo_id = current["id"]
        if self.work_order_sink is None:
            return False
        try:
            await self.work_order_sink.send(current)
        except Exception as exc:
            first_failure = wo_id not in self._send_retry_at
            self._send_retry_at[wo_id] = asyncio.get_event_loop().time() + SEND_RETRY_SECONDS
            if first_failure:
                self.activity(
                    "dispatcher",
                    link.tool_id,
                    "wo.send_failed",
                    f"Could not reach the technicians for work order #{wo_id} ({exc}); retrying.",
                )
            return False
        self._send_retry_at.pop(wo_id, None)
        async with self.session() as s:
            wo = await s.get(Maintenance, wo_id)
            if wo is None or wo.status != WorkOrderStatus.OPEN:
                return False
            wo.status = WorkOrderStatus.SENT
            link.work_order = self.work_order_view(wo)
            self._publish_work_order(wo)
        self.activity(
            "dispatcher",
            link.tool_id,
            "wo.sent",
            f"Work order #{wo_id} ({current['kind']}) sent to the technicians. "
            "They will work at the tool; the host only waits for its reports.",
            {"id": wo_id},
        )
        self.publish_equipment(link)
        return True

    async def request_work_order(self, link: EquipmentLink, kind: str) -> Dict[str, Any]:
        """An engineer asks for maintenance from the console."""
        if kind == "REPAIR" and link.state != EquipmentState.DOWN:
            raise ValueError("a repair can only be requested for a tool that is DOWN")
        async with self.session() as s:
            view = await self.open_work_order(s, link, kind, "MANUAL", "requested by an engineer")
        self.publish_equipment(link)
        return view

    async def _on_maint_started(self, s: AsyncSession, link: EquipmentLink, kind: str, wo_id: int) -> None:
        link.maint_active = True
        wo = await s.get(Maintenance, wo_id) if wo_id else None
        if wo is None or wo.equipment_id != link.tool_id:
            # Someone started maintenance at the tool without a work order.
            wo = Maintenance(
                equipment_id=link.tool_id,
                kind=kind,
                trigger="MANUAL",
                reason="started at the tool without a work order",
                predictive_mode=self.predictive,
            )
            s.add(wo)
        wo.status, wo.started_at = WorkOrderStatus.IN_PROGRESS, utcnow()
        await s.flush()
        link.work_order = self.work_order_view(wo)
        self._publish_work_order(wo)
        self.activity(
            "host",
            link.tool_id,
            "maint.started",
            f"S6F11 MAINT_STARTED: {kind}, work order #{wo.id}. A technician is working at the tool.",
            {"kind": kind, "id": wo.id},
        )

    async def _on_maint_completed(self, s: AsyncSession, link: EquipmentLink, kind: str, wo_id: int) -> None:
        link.maint_active = False
        wo = await s.get(Maintenance, wo_id) if wo_id else None
        if wo is None:
            q = select(Maintenance).where(
                Maintenance.equipment_id == link.tool_id, Maintenance.status == WorkOrderStatus.IN_PROGRESS
            )
            wo = (await s.execute(q)).scalars().first()
        if wo:
            wo.status, wo.finished_at = WorkOrderStatus.DONE, utcnow()
            self._publish_work_order(wo)
        link.work_order = None
        model = self.models.get(link.tool_id)
        if model:
            model.reset()
        self.activity(
            "host",
            link.tool_id,
            "maint.completed",
            f"S6F11 MAINT_COMPLETED: {kind}. Work order #{wo.id if wo else '?'} closed and the tool is "
            "released for dispatch. Health model reset; it learns a new SPC baseline from the next "
            "8 wafers.",
            {"kind": kind},
        )
        self.bus.publish("maintenance", {"tool": link.tool_id, "kind": kind})

    # ---- simulation control (demo) --------------------------------------------------------------
    async def set_running(self, running: bool) -> None:
        """Pause or start the simulation: the simulator's clock freezes and the
        dispatcher stops sending lots and work orders. HSMS links stay up."""
        self.running = running
        note = ""
        if self.simulator:
            try:
                await (self.simulator.resume() if running else self.simulator.pause())
            except Exception as exc:
                note = (
                    f" The simulator clock could not be reached ({type(exc).__name__}); "
                    "only the dispatcher changed."
                )
        self.activity(
            "simulator",
            None,
            "sim.clock",
            (
                "Simulation started: simulated time runs and the dispatcher sends lots again."
                if running
                else "Simulation paused: simulated time is frozen and the dispatcher sends nothing. "
                "HSMS links stay up."
            )
            + note,
            {"running": running},
        )
        self.bus.publish("modes", self.modes())

    # ---- commands -----------------------------------------------------------------------------
    async def start_lot(self, link: EquipmentLink, lot: Lot) -> HCACK:
        # A released HOLD lot only sends the wafers that weren't processed yet.
        ack = await link.command(
            RCMD.START, {"LOTID": lot.id, "RECIPE": lot.recipe, "WAFERS": str(lot.remaining)}
        )
        if ack in OK_ACKS:
            link.busy = True
        return ack

    async def abort(self, link: EquipmentLink) -> HCACK:
        return await link.command(RCMD.ABORT)

    # ---- dispatcher -------------------------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        while True:
            try:
                await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatch cycle failed")
            await asyncio.sleep(self.settings.dispatch_interval)

    async def dispatch_once(self) -> None:
        if not self.running:
            return  # simulation paused
        async with self.session() as s:
            if self.auto_lots:
                tool_types = {l.tool_type for l in self.links.values() if l.tool_type in RECIPES}
                for tool_type in sorted(tool_types):
                    while await self.lots.queued_count(s, tool_type) < self.settings.wip_target:
                        lot = await self.lots.create_random(s, tool_type)
                        await s.flush()
                        self.publish_lot(lot)

        now = asyncio.get_event_loop().time()
        for link in self.links.values():
            if not link.connected:
                continue
            try:
                if link.work_order:
                    # Held for maintenance: no lots. Make sure the technicians know.
                    wo_id = link.work_order["id"]
                    if (
                        self.auto_work_orders
                        and link.work_order["status"] == WorkOrderStatus.OPEN
                        and now >= self._send_retry_at.get(wo_id, 0)
                    ):
                        await self.send_work_order(link)
                    continue
                if link.busy or link.maint_active or link.control_state == ControlState.LOCAL:
                    continue
                if link.state == EquipmentState.IDLE:
                    await self._dispatch_idle(link)
            except (ConnectionError, TimeoutError) as exc:
                log.warning("dispatch to %s failed: %s", link.tool_id, exc)

    async def _dispatch_idle(self, link: EquipmentLink) -> None:
        model = self.models.get(link.tool_id)
        async with self.session() as s:
            lot = await self.lots.next_for(s, link.tool_type or "") if self.auto_dispatch else None
            next_size = lot.remaining if lot else 12
            if self.predictive and model and model.needs_pm_before(next_size):
                # The key decision: stopping a healthy-looking tool for PM now is
                # cheaper than scrapping wafers + an unscheduled repair later.
                _, rul = model.trend()
                left = f"about {rul:.0f}" if rul else "no"
                held = f"lot {lot.id}" if lot else "the next lot"
                await self.open_work_order(
                    s,
                    link,
                    "PM",
                    "PREDICTIVE",
                    f"{held} needs {next_size} wafers + {model.pm_margin_wafers} margin, "
                    f"{left} wafers left before the spec limit",
                )
                self.publish_equipment(link)
                return
            if lot is None:
                return
            lot.status, lot.equipment_id = LotStatus.DISPATCHED, link.tool_id
            await s.flush()
            ack = await self.start_lot(link, lot)
            if ack not in OK_ACKS:
                lot.status, lot.equipment_id = LotStatus.QUEUED, None
            self.publish_lot(lot)
            self.activity(
                "dispatcher",
                link.tool_id,
                "dispatch.lot" if ack in OK_ACKS else "dispatch.rejected",
                f"S2F41 START lot {lot.id} ({lot.remaining} wafers, {lot.recipe}) -> HCACK {ack.name}.",
            )

    # ---- serialisation for API / WebSocket --------------------------------------------------
    def equipment_view(self, link: EquipmentLink) -> Dict[str, Any]:
        model = self.models.get(link.tool_id)
        snap = model.snapshot() if model else None
        ind = HEALTH_INDICATORS.get(link.tool_type or "")
        return {
            "id": link.tool_id,
            "tool_type": link.tool_type,
            "mdln": link.mdln,
            "softrev": link.softrev,
            "address": f"{link.host}:{link.port}",
            "connected": link.connected,
            "state": link.state if link.connected else EquipmentState.OFFLINE,
            "control_state": link.control_state,
            "busy": link.busy,
            "maint_active": link.maint_active,
            "work_order": link.work_order,
            "lot_id": link.lot_id,
            "slot": link.slot,
            "lot_wafers": link.lot_wafers,
            "last_error": link.last_error,
            "sensors": [
                {"svid": svid, "name": n, "units": u, "value": val}
                for (svid, n, u), val in zip(link.sensors, link.latest or [None] * len(link.sensors))
            ],
            "health": None
            if snap is None
            else {
                "indicator": ind.sv_name if ind else None,
                "units": ind.units if ind else None,
                "nominal": ind.nominal if ind else None,
                "spec_limit": ind.spec_limit if ind else None,
                "score": snap.health,
                "ewma": snap.ewma,
                "rul_wafers": snap.rul_wafers,
                "status": snap.status if link.connected else HealthStatus.OK,
                "wafers_since_maint": snap.wafers_since_maint,
                "ucl": snap.limits.ucl if snap.limits else None,
                "lcl": snap.limits.lcl if snap.limits else None,
                "center": snap.limits.mean if snap.limits else None,
                "violations": [v.description for v in snap.violations],
                "history": list(model.history)[-60:] if model else [],
            },
        }

    def publish_equipment(self, link: EquipmentLink) -> None:
        self.bus.publish("equipment", self.equipment_view(link))

    @staticmethod
    def lot_view(lot: Lot) -> Dict[str, Any]:
        return {
            "id": lot.id,
            "product": lot.product,
            "tool_type": lot.tool_type,
            "recipe": lot.recipe,
            "wafers": lot.wafers,
            "priority": lot.priority,
            "status": lot.status,
            "equipment_id": lot.equipment_id,
            "wafers_done": lot.wafers_done,
            "wafers_scrapped": lot.wafers_scrapped,
            "hold_reason": lot.hold_reason,
            "created_at": iso(lot.created_at),
            "started_at": iso(lot.started_at),
            "finished_at": iso(lot.finished_at),
        }

    def publish_lot(self, lot: Lot) -> None:
        self.bus.publish("lot", self.lot_view(lot))

    def modes(self) -> Dict[str, bool]:
        return {
            "running": self.running,
            "predictive": self.predictive,
            "auto_dispatch": self.auto_dispatch,
            "auto_work_orders": self.auto_work_orders,
            "auto_lots": self.auto_lots,
        }
