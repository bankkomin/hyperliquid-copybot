"""Wiring: one asyncio process running the sync cycle, with the dashboard as a
daemon thread. Entry point for start_copybot.bat."""

import asyncio
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from src import logging_setup
from src.config import Config, load_config
from src.models import AccountState, RiskState
from src.paper import PaperBroker
from src.report import write_daily
from src.risk import check_order, run_monitors
from src.sizer import compute_scale, diff_ladders, position_delta
from src.store import Store
from src.watcher import Watcher

log = structlog.get_logger(__name__)

PID_FILE = Path("logs/copybot.pid")


@dataclass
class Deps:
    cfg: Config
    store: Store
    watcher: object
    broker: object
    last_leader: AccountState | None = None  # staleness baseline across cycles
    last_fill_ts: int = 0
    _last_report_day: str = ""


def _record_leader_orders(store: Store, orders, now_ms: int) -> None:
    """Live ladder rewritten every cycle (so an emptied ladder clears), history
    appended only when it changes (so we don't write 1,440 identical snapshots/day)."""
    if store.set_live_ladder(orders, now_ms):
        log.info("leader_ladder_changed", rungs=len(orders))


async def _fetch_leader(deps: Deps, now_ms: int):
    """Returns (leader_state, prev_position) or None when there is no baseline.

    On failure the LAST known state is reused so its fetched_at_ms ages
    naturally — that is what makes staleness real instead of always-zero.
    """
    try:
        leader = await deps.watcher.fetch(now_ms)
    except Exception:
        log.warning("leader_fetch_failed")
        if deps.last_leader is None:
            deps.store.record_event(now_ms, "warning", "ws_lost", "no baseline yet")
            return None
        if not deps.store.outage_open():
            deps.store.record_event(now_ms, "warning", "ws_lost", "leader fetch failing")
        return deps.last_leader, deps.last_leader.position

    if deps.last_leader is None:
        deps.last_fill_ts = now_ms  # don't replay his whole fill history on first run
    if deps.store.outage_open():
        deps.store.record_event(now_ms, "info", "ws_recovered", "leader fetch ok")
    prev_pos = deps.last_leader.position if deps.last_leader else leader.position
    deps.last_leader = leader
    return leader, prev_pos


async def _ingest_leader_fills(deps: Deps, leader, now_ms: int) -> None:
    """Leader fills -> leader_fills table -> our mirrored paper fills.

    Without this a filled leader order is indistinguishable from a canceled one
    (both vanish from his open orders) and the mirror would never trade.
    """
    try:
        fills = await deps.watcher.fetch_fills(deps.last_fill_ts)
    except Exception:
        log.warning("leader_fills_fetch_failed")
        fills = []

    mirror_before = deps.store.mirror_get()
    for f in fills:
        deps.store.conn.execute(
            "INSERT OR REPLACE INTO leader_fills VALUES (?,?,?,?,?,?,?)",
            (
                f["tid"],
                f["time"],
                f["side"],
                float(f["px"]),
                float(f["sz"]),
                int(bool(f.get("crossed"))),
                f.get("dir", ""),
            ),
        )
        deps.broker.on_leader_fill(f.get("oid", 0), float(f["sz"]), float(f["px"]), now_ms)
        deps.last_fill_ts = max(deps.last_fill_ts, f["time"] + 1)
    deps.store.conn.commit()

    live_oids = {o.oid for o in leader.open_orders}
    for loid, m in mirror_before.items():
        if loid not in live_oids and m["our_oid"] not in deps.broker.open:
            deps.store.mirror_close(loid, now_ms, "his_fill")


def _enforce_halt(deps: Deps, ours: AccountState, mark_px: float, now_ms: int) -> None:
    """HALT must ACT, not just record: cancel everything, flatten, stop copying."""
    deps.broker.cancel_all()
    for loid in deps.store.mirror_get():
        deps.store.mirror_close(loid, now_ms, "halt")
    if ours.position:
        deps.broker.market_fill(
            "A" if ours.position > 0 else "B", abs(ours.position), mark_px, now_ms
        )
    deps.store.record_decision(now_ms, "monitor_M1", "halt", RiskState.HALT.value)
    log.critical("halt_enforced", position=ours.position)


async def cycle(deps: Deps, now_ms: int) -> None:
    fetched = await _fetch_leader(deps, now_ms)
    if fetched is None:
        return
    leader, prev_pos = fetched

    await _ingest_leader_fills(deps, leader, now_ms)

    ours = deps.broker.state(leader.mark_px, now_ms)
    deps.store.record_snapshot("leader", leader)
    deps.store.record_snapshot("copy", ours)
    _record_leader_orders(deps.store, leader.open_orders, now_ms)

    dd = deps.store.update_equity(now_ms, ours.equity)
    state = RiskState(deps.store.latest_risk_state())
    age_s = (now_ms - leader.fetched_at_ms) / 1000
    upnl_pct = (
        (ours.mark_px - ours.entry_px) * ours.position / ours.equity * 100
        if ours.entry_px and ours.equity
        else 0.0
    )
    new_state, alerts = run_monitors(dd, age_s, state, deps.cfg, upnl_pct)
    if state != RiskState.HALT and deps.store.halt_requested():
        new_state, alerts = RiskState.HALT, ["halt_requested from dashboard"]
    if new_state != state:
        deps.store.record_event(
            now_ms,
            "critical" if new_state == RiskState.HALT else "warning",
            "state_change",
            new_state.value,
        )
        for a in alerts:
            deps.store.record_event(now_ms, "critical", "monitor", a)
        if new_state == RiskState.HALT:
            _enforce_halt(deps, ours, leader.mark_px, now_ms)
    state = new_state
    if state == RiskState.HALT:
        return

    if leader.equity <= 0:  # liquidated / withdrawn: no ratio to mirror by
        deps.store.record_event(now_ms, "critical", "leader_zero_equity", "cannot compute scale")
        deps.store.record_decision(now_ms, "poll", "veto", state.value,
                                   veto_reason="leader_zero_equity")
        return
    scale = compute_scale(ours.equity, leader.equity)

    # Alert-only monitors (PRD 6.2 M3 anomaly, M5 divergence) — never a brake.
    if prev_pos and abs(leader.position - prev_pos) > 0.5 * abs(prev_pos):
        deps.store.record_event(
            now_ms, "warning", "leader_anomaly", f"position {prev_pos} -> {leader.position}"
        )
    target = leader.position * scale
    if target and abs(ours.position / target - 1) > 0.05:
        deps.store.record_event(
            now_ms, "warning", "divergence", f"ours {ours.position} vs target {target:.5f}"
        )

    actions = diff_ladders(
        leader.open_orders, deps.store.mirror_get(), scale, deps.cfg.mirror.scale_rebalance_pct
    )
    # Cancels first, then re-read our state: otherwise a scale re-balance measures
    # B2 parity against exposure the cancels already removed and vetoes every
    # replacement, leaving the ladder empty exactly during a fast move.
    actions.sort(key=lambda a: a.kind != "cancel")
    for i, a in enumerate(actions):
        if a.kind == "place" and i and actions[i - 1].kind == "cancel":
            ours = deps.broker.state(leader.mark_px, now_ms)
        v = check_order(a, ours, leader, now_ms, state, deps.cfg)
        deps.store.record_decision(
            now_ms,
            "poll",
            "order" if v.approved else "veto",
            state.value,
            veto_reason=v.reason,
            leader_pos=leader.position,
            scale=scale,
            target=target,
        )
        if not v.approved:
            continue
        our_oid = deps.broker.execute(a, now_ms)
        if a.kind != "place":
            # He still has the order -> we are re-mirroring it (scale/amendment),
            # not following a cancel of his. PRD 10.1 reserves 'rebalance' for this.
            still_his = any(o.oid == a.leader_oid for o in leader.open_orders)
            deps.store.mirror_close(
                a.leader_oid, now_ms, "rebalance" if still_his else "his_cancel"
            )
        elif our_oid is None:  # exchange rejected (live mode) — retry next cycle
            deps.store.record_decision(
                now_ms, "poll", "veto", state.value, veto_reason="exchange_reject"
            )
        else:
            deps.store.mirror_put(
                a.leader_oid, our_oid, a.px, a.sz / scale if scale else 0.0, a.sz, scale, now_ms
            )

    # Reconciliation catches his taker trades and skipped rungs.
    # NORMAL only: WARNING must never ADD exposure (PRD 6.3).
    delta = position_delta(
        leader.position, scale, ours.position, deps.cfg.mirror.drift_threshold_pct
    )
    if delta and state == RiskState.NORMAL:
        deps.broker.market_fill(
            "B" if delta > 0 else "A", abs(delta), leader.mark_px, now_ms
        )
        deps.store.record_decision(
            now_ms, "reconcile", "order", state.value,
            leader_pos=leader.position, scale=scale, target=target, delta=delta,
        )
    elif not actions:
        deps.store.record_decision(
            now_ms, "poll", "skip_dust", state.value, leader_pos=leader.position, scale=scale
        )


def make_broker(cfg: Config, store: Store):
    """Paper unless explicitly configured live AND secrets are present."""
    if cfg.mode != "live":
        return PaperBroker(store, cfg.paper.start_equity)
    from src.live import build_live_broker  # imported lazily: needs eth-account

    return build_live_broker(cfg, store)


def startup(deps: Deps) -> None:
    """Never trust orders left by a dead process."""
    if deps.cfg.mode == "live":
        n = deps.broker.cancel_all()
        for loid in deps.store.mirror_get():
            deps.store.mirror_close(loid, int(time.time() * 1000), "restart")
        log.info("startup_recovery", canceled=n)
    log.info("startup", mode=deps.cfg.mode, state=deps.store.latest_risk_state())


async def run(cfg: Config) -> None:
    store = Store(cfg.storage.db_path)
    deps = Deps(
        cfg=cfg,
        store=store,
        watcher=Watcher(cfg.leader, cfg.api_url),
        broker=make_broker(cfg, store),
    )
    startup(deps)
    while True:
        now_ms = int(time.time() * 1000)
        try:
            await cycle(deps, now_ms)
            _maybe_daily_report(deps, now_ms)
        except Exception:
            log.exception("cycle_failed")
        await asyncio.sleep(cfg.storage.snapshot_interval_s)


def _maybe_daily_report(deps: Deps, now_ms: int) -> None:
    """Once per UTC day, write yesterday's report. The last written day lives in
    the DB, so a restart cannot silently skip a day."""
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if deps.store.last_report_day() == today:
        return
    deps.store.record_event(now_ms, "info", "daily_report", today)
    day_start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    if deps.store.conn.execute(
        "SELECT 1 FROM equity_curve WHERE ts < ? LIMIT 1", (day_start_ms,)
    ).fetchone():
        write_daily(deps.store, (now - timedelta(days=1)).strftime("%Y-%m-%d"), deps.cfg)


def main() -> None:
    logging_setup.configure()  # MUST come first: pythonw has no stdout to log to
    cfg = load_config("config.yaml")
    PID_FILE.parent.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    try:
        from src.dashboard import make_app

        app = make_app(cfg)
        threading.Thread(
            target=lambda: app.run(port=cfg.dashboard.port, debug=False),
            daemon=True,
        ).start()
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log.info("shutdown_requested")
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
