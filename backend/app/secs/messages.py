"""The message contract between host (EAP) and equipment.

On a real tool this is the vendor's "GEM manual": the list of status variables
(SVIDs), collection events (CEIDs), alarms (ALIDs) and remote commands (RCMDs).
Keeping it in one module means the simulator and the host can't drift apart,
and tests can assert on names instead of magic numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

from .codec import Fmt, Item

# ---- collection events (S6F11) -------------------------------------------------------


class CEID(IntEnum):
    STATE_CHANGED = 100  # [state, reason]
    CONTROL_STATE_CHANGED = 101  # [ONLINE_REMOTE | ONLINE_LOCAL]
    LOT_STARTED = 200  # [lot_id, recipe, wafer_count]
    WAFER_STARTED = 201  # [lot_id, slot]
    WAFER_COMPLETED = 202  # [lot_id, slot, result]
    LOT_COMPLETED = 203  # [lot_id, wafers_ok, wafers_scrapped]
    LOT_ABORTED = 204  # [lot_id, reason]
    # Reported by the tool when a technician starts / finishes maintenance at it.
    MAINT_STARTED = 300  # [kind, work_order_id]
    MAINT_COMPLETED = 301  # [kind, work_order_id]


# ---- host command acknowledge (S2F42 HCACK) ---------------------------------------------


class HCACK(IntEnum):
    OK = 0
    INVALID_COMMAND = 1
    CANNOT_PERFORM_NOW = 2
    PARAMETER_INVALID = 3
    ACCEPTED_WILL_FINISH_LATER = 4


class RCMD:
    """Remote commands the host may send. Maintenance is deliberately not one of
    them: technicians perform it at the tool, which reports it with events."""

    START = "START"  # params: LOTID, RECIPE, WAFERS
    ABORT = "ABORT"


class ControlState:
    """GEM control state (subset). In LOCAL the operator at the tool is in
    charge and the tool refuses remote commands."""

    REMOTE = "ONLINE_REMOTE"
    LOCAL = "ONLINE_LOCAL"


class EquipmentState:
    IDLE = "IDLE"
    PROCESSING = "PROCESSING"
    PM = "PM"  # scheduled down
    DOWN = "DOWN"  # unscheduled down
    OFFLINE = "OFFLINE"  # host-side only: no HSMS session


ALARM_SET = 0x80  # ALCD bit 8: alarm set (1) / cleared (0)


@dataclass
class EventReport:
    ceid: CEID
    values: List[object]
    dataid: int = 0


@dataclass
class AlarmReport:
    alid: int
    text: str
    is_set: bool
    severity: int  # lower 7 bits of ALCD (category)


@dataclass
class TraceReport:
    trid: int
    sample: int
    stime: str
    values: List[float]


# ---- S1: equipment status --------------------------------------------------------------


def s1f14_body(mdln: str, softrev: str, accepted: bool = True) -> Item:
    return Item.L(Item.B(0 if accepted else 1), Item.L(Item.A(mdln), Item.A(softrev)))


def parse_s1f14(body: Item) -> Tuple[bool, str, str]:
    return body[0].value[0] == 0, body[1][0].value, body[1][1].value


def s1f12_body(svs: List[Tuple[int, str, str]]) -> Item:
    return Item.L(*[Item.L(Item.U4(i), Item.A(n), Item.A(u)) for i, n, u in svs])


def parse_s1f12(body: Item) -> List[Tuple[int, str, str]]:
    return [(e[0].scalar, e[1].value, e[2].value) for e in body.value]


# ---- S2F41: remote commands --------------------------------------------------------------


def s2f41_body(rcmd: str, params: Optional[Dict[str, str]] = None) -> Item:
    cps = [Item.L(Item.A(k), Item.A(str(v))) for k, v in (params or {}).items()]
    return Item.L(Item.A(rcmd), Item.L(*cps))


def parse_s2f41(body: Item) -> Tuple[str, Dict[str, str]]:
    return body[0].value, {cp[0].value: cp[1].value for cp in body[1].value}


def s2f42_body(ack: HCACK) -> Item:
    return Item.L(Item.B(int(ack)), Item.L())


def parse_s2f42(body: Item) -> HCACK:
    return HCACK(body[0].value[0])


# ---- S5F1: alarms ---------------------------------------------------------------------------


def s5f1_body(alid: int, text: str, is_set: bool, severity: int = 4) -> Item:
    alcd = (ALARM_SET if is_set else 0) | (severity & 0x7F)
    return Item.L(Item.B(alcd), Item.U4(alid), Item.A(text))


def parse_s5f1(body: Item) -> AlarmReport:
    alcd = body[0].value[0]
    return AlarmReport(body[1].scalar, body[2].value, bool(alcd & ALARM_SET), alcd & 0x7F)


# ---- S6: data collection --------------------------------------------------------------------


def _value_item(v: object) -> Item:
    if isinstance(v, bool):
        return Item.BOOL(v)
    if isinstance(v, int):
        return Item.U4(v)
    if isinstance(v, float):
        return Item.F4(v)
    return Item.A(str(v))


def s6f11_body(dataid: int, ceid: CEID, values: List[object], rptid: int = 1) -> Item:
    report = Item.L(Item.U4(rptid), Item.L(*[_value_item(v) for v in values]))
    return Item.L(Item.U4(dataid), Item.U4(int(ceid)), Item.L(report))


def parse_s6f11(body: Item) -> EventReport:
    values: List[object] = []
    for rpt in body[2].value:
        values.extend(v.to_python() for v in rpt[1].value)
    return EventReport(CEID(body[1].scalar), values, body[0].scalar)


def s6f1_body(trid: int, sample: int, stime: str, values: List[float]) -> Item:
    return Item.L(Item.U4(trid), Item.U4(sample), Item.A(stime), Item.L(*[Item.F4(v) for v in values]))


def parse_s6f1(body: Item) -> TraceReport:
    return TraceReport(body[0].scalar, body[1].scalar, body[2].value, [v.scalar for v in body[3].value])


# ---- S6F23: request spooled data -------------------------------------------------------------

RSDC_TRANSMIT = 0
RSDC_PURGE = 1


def s6f23_body(rsdc: int = RSDC_TRANSMIT) -> Item:
    return Item.U1(rsdc)


def ack_body(code: int = 0) -> Item:
    """Generic single-byte acknowledge (ACKC5 / ACKC6)."""
    return Item(Fmt.B, bytes([code]))
