"""Risk backstops — account-level only.

We copy the leader's risk management (pyramid ladder, maker entries, no stops),
so there are no per-trade risk opinions here. What remains protects against the
mirror itself breaking, or the account being destroyed: parity, staleness, and
the drawdown kill-switch.
"""

from src.config import Config
from src.models import AccountState, MirrorAction, RiskState, Verdict


def exposure(a: AccountState) -> float:
    """Position notional + resting order notional."""
    return abs(a.position) * a.mark_px + sum(o.sz * o.px for o in a.open_orders)


def check_order(
    a: MirrorAction,
    ours: AccountState,
    leader: AccountState,
    now_ms: int,
    state: RiskState,
    cfg: Config,
) -> Verdict:
    """Hard gates. A veto can only shrink or block — never enlarge an order.

    B1 (BTC-only) and B3 (price integrity) are structural in paper mode: the
    watcher parses BTC only and mirror actions always carry the leader's price.
    B3 becomes an explicit check in M2 when the live executor exists.
    """
    if state == RiskState.HALT:
        return Verdict(approved=False, reason="B5_state")
    if a.kind != "place":
        # Risk REDUCTION is never blocked. Staleness and WARNING are exactly the
        # states where pulling orders matters most, so gating cancels here would
        # strand the ladder in the move that caused the alarm.
        return Verdict(approved=True)
    if state == RiskState.WARNING:
        return Verdict(approved=False, reason="B5_state")  # never ADD in WARNING
    if (now_ms - leader.fetched_at_ms) / 1000 > cfg.risk.leader_staleness_max_s:
        return Verdict(approved=False, reason="B4_stale")
    # B3 price integrity: a mirror order must sit at HIS exact price. If it
    # doesn't, our ladder is not his ladder and the whole premise is broken.
    his = next((o for o in leader.open_orders if o.oid == a.leader_oid), None)
    if his is None or a.px != his.px:
        return Verdict(approved=False, reason="B3_price")
    if not leader.equity:
        return Verdict(approved=False, reason="B2_parity")
    scale = ours.equity / leader.equity
    cap = exposure(leader) * scale * cfg.risk.mirror_parity_tolerance
    if exposure(ours) + a.sz * a.px > cap:
        return Verdict(approved=False, reason="B2_parity")
    return Verdict(approved=True)


def run_monitors(
    drawdown_pct: float,
    leader_age_s: float,
    state: RiskState,
    cfg: Config,
    upnl_pct: float = 0.0,
) -> tuple[RiskState, list[str]]:
    """Standing monitors. HALT is sticky — it never auto-exits (an operator
    inserts a manual_reset event; see store.latest_risk_state)."""
    alerts: list[str] = []
    if state == RiskState.HALT:
        return RiskState.HALT, alerts
    if drawdown_pct <= cfg.risk.max_drawdown_pct:
        alerts.append(f"kill_switch drawdown {drawdown_pct:.1f}%")
        return RiskState.HALT, alerts
    # M6, OFF by default: the leader trades without stops and that is what we
    # copy. Set risk.stop_loss_overlay to opt into training wheels.
    if cfg.risk.stop_loss_overlay is not None and upnl_pct <= cfg.risk.stop_loss_overlay:
        alerts.append(f"stop_loss_overlay upnl {upnl_pct:.1f}%")
        return RiskState.HALT, alerts
    if leader_age_s > cfg.risk.leader_staleness_max_s:
        if state != RiskState.WARNING:
            alerts.append(f"leader_stale {leader_age_s:.0f}s")
        return RiskState.WARNING, alerts
    return RiskState.NORMAL, alerts
