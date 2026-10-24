# Engineering notes

Bugs found while building and running FabLink, how they were found, and the
regression test that keeps each one fixed. They are kept here because most
of them are typical of equipment integration work: concurrency between a host
and a tool that both act on their own, and state that has to survive restarts.

## 1. A tool could run several lots at once

**Symptom.** Running the full fab in run-to-failure mode, the event log for
CVD01 showed `LOT_STARTED` for a new lot while the previous one was still
reporting wafers. Five lots ended up interleaved on one chamber.

**Investigation.** I reproduced it in-process with the simulator's
`remote_command` and `_set_state` wrapped to print a timeline, plus an
assertion on "command accepted while a job is still running". The first
violation was not a START but a **second REPAIR**:

```
46.047 SIM cmd MAINT state=DOWN -> ACCEPTED
46.047 SIM state DOWN->DOWN (REPAIR in progress)
47.053 SIM cmd MAINT state=DOWN -> ACCEPTED      <- again
48.069 SIM cmd MAINT state=DOWN -> ACCEPTED      <- and again
```

**Root cause.** A tool under repair stays in the `DOWN` state. The host
cleared its "command in flight" flag on every `STATE_CHANGED` event,
including `DOWN → DOWN`, so the next dispatcher cycle saw a DOWN tool with no
repair pending and sent another REPAIR. Each repair task later set the tool
back to IDLE, and one of those landed in the middle of a lot. From then on
every IDLE let another START in.

**Fix (on both sides).**
- Tool: reject START/MAINT with `HCACK=2` while any job is running — this is
  what real equipment does and the host must not rely on it being absent.
- Host: track `maint_active` from `MAINT_STARTED`/`MAINT_COMPLETED`, because
  the state alone can't distinguish "broken" from "being fixed".

**Tests.** `test_simulator.py::test_rejects_second_command_while_job_running`,
`test_integration.py::test_reactive_failure_holds_lot_and_repairs_exactly_once`.

*Later:* remote maintenance commands were removed altogether (see 9). The
lesson, "track the action in progress, not just the state", carried over:
the host tracks work-order status instead of a busy flag.

## 2. A peer's message could be mistaken for our reply

**Symptom.** None in practice. I caught it while reviewing the HSMS layer.

**Root cause.** Host and tool allocate system bytes independently. The first
version treated any non-W-bit message whose system bytes matched a pending
request as the reply. A tool sending `S6F1` trace data with the same system
bytes as our outstanding `S2F41` would have "answered" the command.

**Fix.** A message is a reply only if its system bytes match **and** it is
`S{n}F{m+1}` (or the `S{n}F0` abort) for the pending request.

**Test.** `test_hsms.py::test_peer_primary_with_same_system_bytes_is_not_taken_as_reply`
sends exactly that collision.

## 3. Etch tools starved while CVD lots piled up

**Symptom.** After a PM, ETCH01 sat IDLE indefinitely in an integration test
while the queue was full.

**Root cause.** The demo lot starter kept the *fab-wide* queue at
`WIP_TARGET`. If the random lots happened to all be CVD, the queue was "full"
and no etch lot was ever created.

**Fix.** WIP target per tool type. **Test:** caught by
`test_predictive_mode_schedules_pm_before_failure`, which needs the tool to
keep receiving lots.

## 4. Releasing a held lot reprocessed finished wafers

**Symptom.** Found while clicking "Release remaining wafers" in the UI: the
released lot showed 1/12 wafers done and was about to be dispatched as a
12-wafer job.

**Fix.** `Lot.remaining`; `START` sends `WAFERS=remaining`, and the
dispatcher's health check uses the remaining count too.

**Test.** `test_integration.py::test_released_hold_lot_only_runs_remaining_wafers`.

## 5. A host restart made the predictive model forget tool wear

**Symptom.** After restarting the API, a tool at "0 wafers left" was given a
12-wafer lot.

**Root cause.** Health models lived only in memory. After a restart they
started empty, the trend needs 4 wafers to say anything, and the dispatcher
treated "no data" as "healthy".

**Fix.** When a tool comes online, the host rebuilds its model from
`wafer_results` since that tool's last completed maintenance.

**Test.** `test_integration.py::test_health_model_survives_host_restart`.

## 6. A scenario stalled because the effect was logged before its cause

**Symptom.** On the System map, the "Network drops" scenario ticked its first
step and then never moved on, although the tool visibly went offline and came
back.

**Root cause.** The host's proxy forwarded "cut the link" to the simulator and
logged the action *after* the HTTP reply came back. Cutting the link closes the
HSMS session immediately, so the gateway's `link.down` entry was written
first. Scenario steps match in order, so "gateway sees the session close" had
already gone by when the run was still waiting for "simulator cut the link".

**Fix.** The proxy logs the action before forwarding it, and logs a
`sim.failed` entry if the simulator rejects it.

**Test.** `test_sim_control.py::test_proxy_logs_action_before_its_effects`.

## 7. One reply timeout silenced a tool until the next reconnect

**Symptom.** Found by reading the simulator's reporting code during a review,
before it showed up in a run.

**Root cause.** A report whose reply timed out (T3) went into the spool, and
every later report queued behind it, because the spool was only flushed after
a *new* session's S1F13. On a link that was still up, the host simply stopped
hearing from the tool. The report that timed out could also have been
processed by the host already, so flushing it later would have double-counted
it.

**Fix.**
- Tool: one ordered outbox, sent one report at a time. After a T3 timeout the
  same report is resent on the same session after a short pause. After a
  disconnect it stays at the head of the outbox, which is the spool, and is
  only sent once the host asks with **S6F23**, as GEM spooling works.
- Host: drop duplicates by `(DATAID, CEID, values)` within a recent window,
  acknowledging them without applying them again. Alarms are idempotent by
  state.

**Tests.** `test_spooling.py::test_reply_timeout_resends_and_does_not_stall`
(a fake host delays one reply past the tool's T3),
`test_spooling.py::test_spool_is_held_until_host_sends_s6f23`,
`test_integration.py::test_host_ignores_resent_events`,
`test_integration.py::test_lost_reply_resend_is_dropped_by_host`.

## 8. A handler from a dropped session overwrote OFFLINE

**Symptom.** The network-loss integration test failed about one run in ten:
the gateway logged "session closed" twice and never "retrying".

**Root cause.** When the link dropped, an event handler from the old session
was still running. It finished after the gateway had marked the tool OFFLINE
and set the tool's state back to PROCESSING. The gateway used "state is not
OFFLINE" to mean "a session was up", so the next failed connect attempt was
reported as another session ending. The same late write could also add a
bogus interval to `state_history` while the tool was unreachable.

**Fix.** Keep two things apart: the tool's last reported state, and whether
the host has a session to it. The gateway decides "was up" from `connected`
only; the API shows OFFLINE whenever there is no session; state history is
only written while connected, against a separately tracked `recorded_state`.

**Test.** `test_integration.py::test_network_loss_spools_and_replays`, run
in a loop.

## 9. Design change: the host no longer starts maintenance

**Why.** The first version sent `S2F41 MAINT KIND=PM|REPAIR` to the tool.
That is not how fabs work. Maintenance is done by technicians at the tool;
the host's job is to decide that maintenance is needed, stop sending the tool
lots and record the downtime.

**Now.** The host raises a work order and holds the tool. The work order goes
to a `WorkOrderSink` (in the demo, a simulated crew in the simulator process).
A technician switches the tool to ONLINE LOCAL (it then refuses remote
commands), works on it, and the tool reports `MAINT_STARTED` /
`MAINT_COMPLETED` with the work order number. The predictive work order is
raised as soon as the trend calls for it, so technicians can walk over while
the current lot finishes.

**Tests.** `test_integration.py::test_predictive_mode_schedules_pm_before_failure`
(also checks no lot is dispatched while the work order is open),
`test_integration.py::test_manual_work_order_waits_for_engineer`,
`test_simulator.py::test_technician_does_pm_locally_and_tool_reports_it`,
`test_simulator.py::test_tool_in_local_control_refuses_remote_commands`.

## 10. CI hung for five hours on Python 3.12

**Symptom.** The first CI run on GitHub: Python 3.9 passed in 40 s, the 3.12
job was still running five hours later. The log stopped after 72 tests.

**Root cause.** Since Python 3.12.1, `asyncio.Server.wait_closed()` waits
until every accepted client connection has closed; on 3.9 it returned at
once. The simulated tool's `stop()` called it during test teardown while a
connection could still be open, so teardown waited forever on 3.12 only.

**Fix.** Each simulated tool tracks the connections it accepted and closes
them before waiting, with a time limit as a backstop. CI also got
`pytest-timeout` (no test may run past 120 s) and a `conftest.py` hook that
prints every pending asyncio task after 45 s, so a hang now fails fast and
says where it is stuck.

## 11. Shutdown cancelled database work mid-query

**Symptom.** With the timeout in place, a later run on Python 3.9 stopped
after 62 tests. The new diagnostic printed a single pending task:
SQLAlchemy's `_terminate_graceful_close` for an aiosqlite connection,
waiting while pytest closed the event loop.

**Root cause.** `FabHost.stop()` and the gateway *cancelled* the dispatcher
loop and the tasks handling tool messages, which could be in the middle of a
database transaction, and the engine was disposed right after. A connection
interrupted mid-query could not finish closing. It was rare and timing
dependent, and it affects the server's real shutdown path, not just tests.

**Fix.** Orderly shutdown: stop taking new work (a stop flag that the loops
check, and an event that wakes the reconnect backoff), close the HSMS
session, *wait* for the run loop and every in-flight message handler to
finish (`HsmsConnection.drain()`), and only cancel what is still running
after a grace period.

**Test.** `test_integration.py::test_stop_lets_in_flight_work_finish` stops
the host mid-production and checks that no dispatcher, link or
connection-close task is left behind.

## 12. Design change: splitting the fab for Vercel

**Problem.** The host keeps four HSMS sessions, a dispatcher and an in-memory
health model running for hours. A Vercel function is a request handler that
lives at most 300 s, may run as many copies as there is traffic, and may be
frozen between requests. Moving `app/main.py` there as-is would have started a
new fab per instance and lost it after five minutes.

**Change.** The fab moved into a Vercel *daemon* (one long-running container)
and the API became stateless. The first step was to find everything the routes
took from the host directly and put it behind `Fab`; the command logic moved
out of the routes into `commands.py`, so the daemon runs exactly the code the
local app runs. Live state then had to reach API instances the daemon can't
call (daemons take no inbound traffic): it goes through Redis as batched
events and periodic snapshots, and commands go the other way over pub/sub.

**Cost.** A free Upstash plan allows 500K commands a month. One PUBLISH per
event would be about 100 a second; batching every 0.5 s and snapshotting every
3 s brings a running fab to about 3 commands a second. That is still about
50 hours of running a month, so the fab pauses when no console is open, which
brings an idle one to zero. The daemon's CPU quota on the
Hobby plan is the same argument. The operator's pause and the idle pause are
kept apart (`paused_by`), or a visitor opening the page would undo an
operator's decision.

**Tests.** `test_split.py` runs the real daemon and the real cloud API in one
event loop over an in-memory Redis (`fakeredis`); CI's `e2e-split` job runs
the same topology in containers on PostgreSQL and real Redis.

## 13. Smaller ones

- **Timestamps.** SQLite returns naive datetimes even for timezone-aware
  columns, so the browser showed UTC times as local time. All API timestamps
  now go through `models.iso()`, which always emits an offset.
  (`test_api.py::test_create_and_list_lots` checks for `+00:00`.)
- **Database portability.** The first KPI query used SQLite's `julianday()`.
  It would have broken on PostgreSQL; durations are now aggregated in Python
  (the table is small). CI runs the stack on PostgreSQL to catch this class
  of bug.
- **SPC log flooding.** A drifting tool violates the same rule on every wafer;
  only rules that *start* firing are written to the event log.
- **Flaky test.** One integration test asserted "no active alarms", but the
  simulator raises random 50 ms nuisance alarms; under CPU load one could be
  active at assertion time. The assertion now checks only the fault alarm.
