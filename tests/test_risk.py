from src.models import AccountState, MirrorAction, Order, RiskState
from src.risk import check_order, run_monitors

BUY = MirrorAction(kind="place", side="B", px=72_000.0, sz=0.01, leader_oid=1)


def acct(equity, pos, orders_notional, mark=80_000.0):
    orders = (
        [Order(oid=1, side="B", px=mark * 0.9, sz=orders_notional / (mark * 0.9), ts_ms=0)]
        if orders_notional
        else []
    )
    return AccountState(
        equity=equity, position=pos, entry_px=None, mark_px=mark,
        fetched_at_ms=1_000_000, open_orders=orders,
    )


def test_b2_parity_vetoes_over_exposure(cfg_paper):
    leader = acct(66_435, 1.33557, 84_000)
    ours_over = acct(10_000, 0.30, 20_000)
    v = check_order(BUY, ours_over, leader, 1_000_000, RiskState.NORMAL, cfg_paper)
    assert not v.approved and v.reason == "B2_parity"


def test_b4_staleness_vetoes(cfg_paper):
    v = check_order(
        BUY, acct(10_000, 0.18, 0), acct(66_435, 1.33557, 0),
        1_000_000 + 301_000, RiskState.NORMAL, cfg_paper,
    )
    assert not v.approved and v.reason == "B4_stale"


def test_b5_halt_blocks_everything(cfg_paper):
    v = check_order(
        BUY, acct(10_000, 0.18, 0), acct(66_435, 1.33557, 0),
        1_000_000, RiskState.HALT, cfg_paper,
    )
    assert not v.approved and v.reason == "B5_state"


def test_warning_blocks_places_but_allows_cancels(cfg_paper):
    leader, ours = acct(66_435, 1.33557, 84_000), acct(10_000, 0.18, 10_000)
    assert not check_order(BUY, ours, leader, 1_000_000, RiskState.WARNING, cfg_paper).approved
    cancel = MirrorAction(kind="cancel", side="B", px=72_000.0, sz=0.01, leader_oid=1, our_oid=5)
    assert check_order(cancel, ours, leader, 1_000_000, RiskState.WARNING, cfg_paper).approved


def test_normal_order_approved(cfg_paper):
    v = check_order(
        BUY, acct(10_000, 0.18, 10_000), acct(66_435, 1.33557, 84_000),
        1_000_000, RiskState.NORMAL, cfg_paper,
    )
    assert v.approved


def test_kill_switch_fires_at_minus_35(cfg_paper):
    state, alerts = run_monitors(-36.0, 10, RiskState.NORMAL, cfg_paper)
    assert state == RiskState.HALT and "kill_switch" in alerts[0]


def test_kill_switch_does_not_fire_above_threshold(cfg_paper):
    state, _ = run_monitors(-34.9, 10, RiskState.NORMAL, cfg_paper)
    assert state == RiskState.NORMAL


def test_stale_leader_goes_warning_and_recovers(cfg_paper):
    state, _ = run_monitors(-2.0, 400, RiskState.NORMAL, cfg_paper)
    assert state == RiskState.WARNING
    state, _ = run_monitors(-2.0, 10, RiskState.WARNING, cfg_paper)
    assert state == RiskState.NORMAL


def test_stale_data_still_allows_cancels(cfg_paper):
    """Regression: staleness is what CAUSES warning, so gating cancels on it
    stranded the ladder exactly when pulling orders mattered most."""
    cancel = MirrorAction(kind="cancel", side="B", px=72_000.0, sz=0.01, leader_oid=1, our_oid=5)
    v = check_order(
        cancel, acct(10_000, 0.18, 0), acct(66_435, 1.33557, 0),
        1_000_000 + 999_000, RiskState.WARNING, cfg_paper,
    )
    assert v.approved


def test_stop_loss_overlay_off_by_default(cfg_paper):
    state, _ = run_monitors(-2.0, 10, RiskState.NORMAL, cfg_paper, upnl_pct=-80.0)
    assert state == RiskState.NORMAL  # faithful mirror: leader has no stops


def test_stop_loss_overlay_fires_when_enabled(cfg_paper):
    cfg = cfg_paper.model_copy(
        update={"risk": cfg_paper.risk.model_copy(update={"stop_loss_overlay": -20.0})}
    )
    state, alerts = run_monitors(-2.0, 10, RiskState.NORMAL, cfg, upnl_pct=-25.0)
    assert state == RiskState.HALT and "stop_loss_overlay" in alerts[0]


def test_halt_is_sticky(cfg_paper):
    state, _ = run_monitors(-2.0, 10, RiskState.HALT, cfg_paper)
    assert state == RiskState.HALT  # never auto-exits
