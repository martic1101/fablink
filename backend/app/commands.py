"""Everything the console can ask the fab to do, in one place.

In a single process (``make demo``) the API calls ``execute`` directly. In the
cloud the API is stateless: it forwards the same commands over Redis to the
fab daemon, which calls ``execute`` there. Either way the logic exists once.
"""

from __future__ import annotations

from typing import Any, Dict

from .equipment.gateway import EquipmentLink
from .host import OK_ACKS, FabHost
from .models import Lot
from .secs.hsms import T3Timeout
from .simulation import ACTIONS, SimulatorError

MODE_KEYS = ("predictive", "auto_dispatch", "auto_work_orders", "auto_lots")


class CommandError(Exception):
    """A command failed in a way the console should show (HTTP status + text)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _link(host: FabHost, tool: str) -> EquipmentLink:
    link = host.links.get(tool)
    if link is None:
        raise CommandError(404, f"unknown equipment {tool}")
    return link


def describe_sim_action(action: str, body: Dict[str, Any]) -> str:
    if action == "wear":
        return (
            f"Simulator: consumable wear set to {body['wear']:.1%}. "
            "The host is not told; it can only see the sensors."
        )
    if action == "degrade":
        return (
            f"Simulator: part now wears {body['multiplier']:g}x faster until the next maintenance "
            "(e.g. a bad batch of parts). The host is not told."
        )
    if action == "lose_reply":
        return (
            "Simulator: the host's reply to the tool's next report will be lost on the way back, "
            "so the tool will time out (T3) and send that report again."
        )
    return (
        f"Simulator: network to the host dropped; new connections refused for {body['seconds']:g} s. "
        "The tool keeps processing and spools its reports."
    )


async def execute(host: FabHost, name: str, args: Dict[str, Any]) -> Any:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise CommandError(400, f"unknown command {name}")
    return await handler(host, **args)


# ---- handlers ---------------------------------------------------------------------------------------
async def _abort(host: FabHost, tool: str) -> Dict[str, Any]:
    link = _link(host, tool)
    try:
        ack = await host.abort(link)
    except T3Timeout as exc:
        raise CommandError(504, str(exc)) from exc
    except ConnectionError as exc:
        raise CommandError(409, str(exc)) from exc
    if ack not in OK_ACKS:
        raise CommandError(409, f"equipment rejected ABORT: HCACK={ack.name}")
    return {"hcack": ack.name}


async def _request_work_order(host: FabHost, tool: str, kind: str) -> Dict[str, Any]:
    try:
        return await host.request_work_order(_link(host, tool), kind)
    except ValueError as exc:
        raise CommandError(409, str(exc)) from exc


async def _send_work_order(host: FabHost, wo_id: int) -> Dict[str, Any]:
    link = next((l for l in host.links.values() if l.work_order and l.work_order["id"] == wo_id), None)
    if link is None:
        raise CommandError(404, "no active work order with that id")
    if not await host.send_work_order(link):
        raise CommandError(409, "work order is not open, or the technicians could not be reached")
    return link.work_order  # type: ignore[return-value]


async def _create_lot(host: FabHost, tool_type: str, wafers: int, recipe: Any = None, priority: int = 5):
    async with host.session() as s:
        try:
            lot = await host.lots.create(s, tool_type, wafers, recipe, priority=priority)
        except ValueError as exc:
            raise CommandError(422, str(exc)) from exc
        await s.flush()
        host.publish_lot(lot)
        return host.lot_view(lot)


async def _release_lot(host: FabHost, lot_id: str) -> Dict[str, Any]:
    async with host.session() as s:
        lot = await s.get(Lot, lot_id)
        if lot is None:
            raise CommandError(404, "lot not found")
        try:
            await host.lots.release(s, lot)
        except ValueError as exc:
            raise CommandError(409, str(exc)) from exc
        host.publish_lot(lot)
        return host.lot_view(lot)


async def _set_modes(host: FabHost, **changes: Any) -> Dict[str, Any]:
    running = changes.pop("running", None)
    for key, value in changes.items():
        if key not in MODE_KEYS:
            raise CommandError(422, f"unknown mode {key}")
        setattr(host, key, bool(value))
    if running is not None and running != host.running:
        await host.set_running(bool(running), by_operator=True)
    host.bus.publish("modes", host.modes())
    return host.modes()


async def _sim(host: FabHost, tool: str, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if host.simulator is None:
        raise CommandError(503, "No simulator is attached to this host.")
    if action not in ACTIONS:
        raise CommandError(404, f"unknown simulator action {action}")
    # Log first: a cut link makes the gateway react before the simulator
    # answers, and the activity stream must keep cause before effect.
    try:
        host.activity("simulator", tool, f"sim.{action}", describe_sim_action(action, body), body)
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandError(422, f"invalid body for {action}") from exc
    try:
        return await host.simulator.action(tool, action, body)
    except SimulatorError as exc:
        host.activity("simulator", tool, "sim.failed", f"Simulator rejected {action}: {exc.detail}")
        raise CommandError(exc.status, exc.detail) from exc


async def _sim_tools(host: FabHost) -> Any:
    if host.simulator is None:
        raise CommandError(503, "No simulator is attached to this host.")
    try:
        return await host.simulator.tools()
    except SimulatorError as exc:
        raise CommandError(exc.status, exc.detail) from exc


_HANDLERS = {
    "abort": _abort,
    "request_work_order": _request_work_order,
    "send_work_order": _send_work_order,
    "create_lot": _create_lot,
    "release_lot": _release_lot,
    "set_modes": _set_modes,
    "sim": _sim,
    "sim_tools": _sim_tools,
}
