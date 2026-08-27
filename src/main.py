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
from src.risk import check_order, exposure, run_monitors
from src.sizer import MIN_NOTIONAL_USD, compute_scale, diff_ladders, position_delta
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

    # Snapshot our book ONCE: LiveBroker.open is an API call, so touching it per
    # rung would be an N+1 against the exchange every cycle. If it cannot be read
    # we skip the his_fill sweep rather than guess — closing a row for an order
    # that is actually still resting would orphan it.
    try:
        our_open = set(deps.broker.open)
    except Exception:
        log.exception("our_open_orders_failed")
        return
    live_oids = {o.oid for o in leader.open_orders}
    for loid, m in mirror_before.items():
        if loid not in live_oids and m["our_oid"] not in our_open:
            deps.store.mirror_close(loid, now_ms, "his_fill")


def _funding_monitor(deps: Deps, equity: float, now_ms: int) -> None:
    """PRD 6.2 M4 — alert only. Long perps bleed carry; the leader paid ~0.6%/mo
    at peak, so a sustained 2%/30d means the copy economics have changed."""
    if deps.cfg.mode != "live" or not equity:
        return  # paper fills pay no funding
    paid = deps.broker.funding_since(now_ms - 30 * 86_400_000)
    deps.store.conn.execute(
        "UPDATE equity_curve SET funding_cum=? WHERE ts=?", (paid, now_ms)
    )
    deps.store.conn.commit()
    if -paid > equity * deps.cfg.risk.funding_alert_pct_30d / 100:
        deps.store.record_event(
            now_ms, "warning", "funding_bleed", f"{paid:.2f} USD over 30d"
        )


def _enforce_halt(deps: Deps, ours: AccountState, mark_px: float, now_ms: int) -> bool:
    """HALT must ACT, not just record: cancel everything, flatten, stop copying.

    Returns True only when the account is verifiably clean. The caller re-runs
    this every cycle while HALT persists, because the one thing we cannot do is
    give up after a single attempt: the IOC fires during the very crash that
    triggered HALT, which is exactly when it misses the book.
    """
    try:
        canceled = deps.broker.cancel_all()
    except Exception:
        log.exception("halt_cancel_all_failed")
        canceled = 0
    for loid in deps.store.mirror_get():
        deps.store.mirror_close(loid, now_ms, "halt")
    filled = True
    if ours.position and mark_px:
        filled = deps.broker.market_fill(
            "A" if ours.position > 0 else "B", abs(ours.position), mark_px, now_ms,
            reduce_only=True,  # can never flip us into fresh naked exposure
        )
    elif ours.position:
        filled = False  # no usable mark price yet; retry next cycle
    # book_is_clean() is False when the exchange could not be asked — an
    # unanswered query must never be reported as a clean book.
    still_open = 0 if deps.broker.book_is_clean() else 1
    clean = filled and not still_open
    log.critical(
        "halt_enforced", position=ours.position, canceled=canceled,
        flatten_accepted=filled, orders_left=still_open,
    )
    if not clean:
        deps.store.record_event(
            now_ms, "critical", "halt_incomplete",
            f"orders_left={still_open} flatten_accepted={filled} — retrying next cycle",
        )
    return clean


async def _halt_cycle(deps: Deps, now_ms: int) -> None:
    """One cycle while HALTED: keep the operator's view alive and keep pushing
    the account toward flat.

    The mark price is sourced independently of the leader: after a restart
    `last_leader` is None, and an earlier version took the price from there —
    which meant a HALT that survived a reboot could never flatten.
    """
    mark = 0.0
    try:
        mark = await deps.watcher.fetch_mark()
    except Exception:
        log.warning("halt_mark_fetch_failed")
    if not mark:
        mark = (deps.last_leader.mark_px if deps.last_leader else 0.0) or deps.store.last_mark()

    ours = deps.broker.state(mark, now_ms)
    # Keep writing snapshots/equity: otherwise every operator-facing surface
    # freezes at its pre-HALT values, and the person deciding whether the HALT
    # worked is reading numbers from before it.
    deps.store.record_snapshot("copy", ours)
    deps.store.update_equity(now_ms, ours.equity)
    if ours.position or not deps.broker.book_is_clean():
        _enforce_halt(deps, ours, mark, now_ms)


async def cycle(deps: Deps, now_ms: int) -> None:
    # The dashboard HALT button is read FIRST, before anything that can fail:
    # the operator presses it precisely when things are going wrong, so it must
    # not depend on the leader API being reachable.
    entry_state = RiskState(deps.store.latest_risk_state())
    if entry_state != RiskState.HALT and deps.store.halt_requested():
        deps.store.record_event(now_ms, "critical", "state_change", RiskState.HALT.value)
        deps.store.record_event(now_ms, "critical", "monitor", "halt_requested from dashboard")
        entry_state = RiskState.HALT

    # HALT enforcement also runs before the leader fetch: flattening our own
    # account has nothing to do with his data, and his endpoint is most likely
    # degraded during the crash that caused the HALT.
    if entry_state == RiskState.HALT:
        await _halt_cycle(deps, now_ms)
        return

    fetched = await _fetch_leader(deps, now_ms)
    if fetched is None:
        return
    leader, prev_pos = fetched

    await _ingest_leader_fills(deps, leader, now_ms)

    # Live only: our own fills happen on-exchange, so pull them into `fills` or
    # the maker-%, fee and cost lines have nothing to read.
    if deps.cfg.mode == "live":
        deps.broker.ingest_our_fills(now_ms)

    ours = deps.broker.state(leader.mark_px, now_ms)
    deps.store.record_snapshot("leader", leader)
    deps.store.record_snapshot("copy", ours)
    _record_leader_orders(deps.store, leader.open_orders, now_ms)

    dd = deps.store.update_equity(now_ms, ours.equity)
    _funding_monitor(deps, ours.equity, now_ms)
    state = RiskState(deps.store.latest_risk_state())
    age_s = (now_ms - leader.fetched_at_ms) / 1000
    upnl_pct = (
        (ours.mark_px - ours.entry_px) * ours.position / ours.equity * 100
        if ours.entry_px and ours.equity
        else 0.0
    )
    new_state, alerts = run_monitors(dd, age_s, state, deps.cfg, upnl_pct)
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
        return  # enforcement already ran above; next cycle re-checks until clean

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
            if our_oid is None:
                # The cancel was REJECTED. Closing the mirror row here would
                # orphan an order that is still live: diff_ladders can only
                # cancel rungs it still has a row for, so it would rest forever.
                deps.store.record_event(
                    now_ms, "warning", "cancel_failed",
                    f"leader_oid={a.leader_oid} our_oid={a.our_oid} — retry next cycle",
                )
                continue
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
                a.leader_oid, our_oid, a.px, a.leader_sz, a.sz, scale, now_ms
            )

    # Reconciliation catches his taker trades and skipped rungs.
    # NORMAL only: WARNING must never ADD exposure (PRD 6.3).
    delta = position_delta(
        leader.position, scale, ours.position, deps.cfg.mirror.drift_threshold_pct
    )
    # Dust guard: below the exchange minimum the IOC is rejected every cycle
    # forever. This bites when the leader goes flat and only our dust remains.
    if delta and abs(delta) * leader.mark_px < MIN_NOTIONAL_USD:
        delta = 0.0
    if delta and state == RiskState.NORMAL:
        over_parity = (
            exposure(ours) + abs(delta) * leader.mark_px
            > exposure(leader) * scale * deps.cfg.risk.mirror_parity_tolerance
        )
        if over_parity and abs(ours.position) < abs(target):
            deps.store.record_decision(
                now_ms, "reconcile", "veto", state.value, veto_reason="B2_parity",
                leader_pos=leader.position, scale=scale, target=target, delta=delta,
            )
        else:
            ok = deps.broker.market_fill(
                "B" if delta > 0 else "A", abs(delta), leader.mark_px, now_ms
            )
            deps.store.record_decision(
                now_ms, "reconcile", "order" if ok else "veto", state.value,
                veto_reason="" if ok else "exchange_reject",
                leader_pos=leader.position, scale=scale, target=target, delta=delta,
            )
    elif not actions:
        deps.store.record_decision(
            now_ms, "poll", "skip_dust", state.value, leader_pos=leader.position, scale=scale
        )

    # Re-record our snapshot AFTER this cycle's fills. The earlier one is the
    # pre-trade state the risk gates are judged against; leaving it as the last
    # word made the dashboard and daily report show a position one cycle stale
    # (e.g. "0.00000 BTC, drift -100%" right after a reconciliation bought in).
    deps.store.record_snapshot("copy", deps.broker.state(leader.mark_px, now_ms))


def make_broker(cfg: Config, store: Store):
    """Paper unless explicitly configured live AND secrets are present."""
    if cfg.mode != "live":
        return PaperBroker(store, cfg.paper.start_equity)
    from src.live import build_live_broker  # imported lazily: needs eth-account

    return build_live_broker(cfg, store)


def startup(deps: Deps) -> None:
    """Recover from however the last process died.

    Live: cancel everything first — orders left by a dead process are unmanaged
    exposure, and re-mirroring from a clean book is the only way to guarantee no
    duplicates. Paper: the broker rehydrates its own book from SQLite, so the
    mirror rows still describe real orders and must NOT be cleared.
    """
    now_ms = int(time.time() * 1000)

    # Consume a stop request that was never completed (e.g. the operator ran
    # stop_copybot.bat against an already-dead process). Left latched, it would
    # make every future start run one cycle and immediately shut down again.
    if deps.store.stop_requested():
        deps.store.record_event(now_ms, "info", "stopped", "stale stop request cleared at startup")
        log.warning("stale_stop_request_cleared")

    if deps.cfg.mode == "live":
        try:
            n = deps.broker.cancel_all()
        except Exception:
            log.exception("startup_cancel_all_failed")
            n = 0
        # Only forget the old ladder once the exchange confirms it is gone.
        # Wiping rows we did not actually cancel orphans live orders that
        # diff_ladders can never reach again, while the first cycle re-places
        # the same rungs — double size, half of it unmanaged.
        if deps.broker.book_is_clean():
            for loid in deps.store.mirror_get():
                deps.store.mirror_close(loid, now_ms, "restart")
            log.info("startup_recovery", canceled=n, book="clean")
        else:
            deps.store.record_event(
                now_ms, "critical", "startup_dirty_book",
                f"cancel_all confirmed {n}; orders may still rest — mirror rows kept",
            )
            log.critical("startup_recovery_incomplete", canceled=n)

    state = deps.store.latest_risk_state()
    if state == RiskState.HALT.value:
        # HALT survives restarts. Say so loudly: the operator must clear it with
        # a manual_reset event, and until then we only observe.
        deps.store.record_event(
            now_ms, "critical", "startup_halted",
            "restarted while HALTED — insert a manual_reset event to resume",
        )
        log.critical("startup_while_halted")
    log.info("startup", mode=deps.cfg.mode, state=state)


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
        if await _sleep_unless_stopped(deps, cfg.storage.snapshot_interval_s):
            shutdown(deps)
            return


async def _sleep_unless_stopped(deps: Deps, seconds: int, slice_s: int = 2) -> bool:
    """Sleep between cycles, waking early for a stop request so the operator does
    not wait a whole cycle for the bot to come down."""
    for _ in range(max(1, seconds // slice_s)):
        if deps.store.stop_requested():
            return True
        await asyncio.sleep(slice_s)
    return deps.store.stop_requested()


def shutdown(deps: Deps) -> None:
    """Graceful stop: pull our resting orders so nothing is left unattended, but
    KEEP the position — stopping the bot must not liquidate a healthy book."""
    now_ms = int(time.time() * 1000)
    canceled = 0
    try:
        if deps.cfg.mode == "live":
            canceled = deps.broker.cancel_all()
            for loid in deps.store.mirror_get():
                deps.store.mirror_close(loid, now_ms, "shutdown")
    except Exception:
        log.exception("shutdown_cancel_failed")
    # Record `stopped` FIRST and unconditionally: if anything below raises, the
    # stop flag stays latched and every future start would shut itself down.
    deps.store.record_event(now_ms, "info", "stopped", f"canceled={canceled}")
    try:
        mark = deps.last_leader.mark_px if deps.last_leader else 0.0
        pos = deps.broker.state(mark, now_ms).position if mark else None
    except Exception:
        pos = None
    log.info("shutdown_complete", canceled=canceled, position_kept=pos)


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

        def _serve() -> None:
            # A dead dashboard means no HALT button and no equity view while the
            # bot keeps trading. Under pythonw the traceback would go to a
            # non-existent stderr, so record it where the operator can find it.
            try:
                app.run(port=cfg.dashboard.port, debug=False)
            except Exception:
                log.exception("dashboard_failed", port=cfg.dashboard.port)
                Store(cfg.storage.db_path).record_event(
                    int(time.time() * 1000), "critical", "dashboard_down",
                    f"port {cfg.dashboard.port} unavailable — no HALT button",
                )

        threading.Thread(target=_serve, daemon=True).start()
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log.info("shutdown_requested")
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
