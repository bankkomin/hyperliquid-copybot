from src.models import Order
from src.sizer import compute_scale, diff_ladders, mirror_size, position_delta

SCALE = compute_scale(10_000, 66_435)  # PRD 5.1 worked example


def test_scale_matches_prd():
    assert round(SCALE, 4) == 0.1505


def test_mirror_size_rounds_down_5dp():
    assert mirror_size(0.2, SCALE, 57_860) == 0.0301


def test_mirror_size_skips_below_min_notional():
    assert mirror_size(0.05, 0.002, 60_000) == 0.0  # $6 notional < $10 minimum


def test_rung_percentages_match_the_leader():
    """The whole point of one scale ratio: each rung is the same % of equity."""
    leader_equity, our_equity = 66_435, 10_000
    scale = compute_scale(our_equity, leader_equity)
    for leader_sz, px in [(0.05, 73_521), (0.12, 62_944), (0.20, 57_860)]:
        ours = mirror_size(leader_sz, scale, px)
        his_pct = leader_sz * px / leader_equity
        our_pct = ours * px / our_equity
        assert abs(our_pct - his_pct) < 0.001


def test_diff_places_new_and_cancels_gone():
    his = [Order(oid=1, side="B", px=57860, sz=0.2, ts_ms=0)]
    actions = diff_ladders(his, {}, SCALE, rebalance_pct=5)
    assert [a.kind for a in actions] == ["place"]
    assert actions[0].sz == 0.0301 and actions[0].px == 57860

    mirror = {
        1: {
            "leader_oid": 1, "our_oid": 9, "px": 57860.0,
            "leader_sz": 0.2, "our_sz": 0.0301, "scale_used": SCALE,
        }
    }
    actions = diff_ladders([], mirror, SCALE, rebalance_pct=5)
    assert [a.kind for a in actions] == ["cancel"]
    assert actions[0].our_oid == 9


def test_diff_ignores_small_scale_wiggle_but_rebalances_big_move():
    mirror = {
        1: {
            "leader_oid": 1, "our_oid": 9, "px": 57860.0,
            "leader_sz": 0.2, "our_sz": 0.0301, "scale_used": 0.1505,
        }
    }
    his = [Order(oid=1, side="B", px=57860, sz=0.2, ts_ms=0)]
    assert diff_ladders(his, mirror, 0.1520, rebalance_pct=5) == []  # +1% wiggle
    actions = diff_ladders(his, mirror, 0.1700, rebalance_pct=5)  # +13% move
    assert [a.kind for a in actions] == ["cancel", "place"]


def _mirror_row(px=57860.0, leader_sz=0.2, our_sz=0.0301, scale=SCALE):
    return {1: {"leader_oid": 1, "our_oid": 9, "px": px, "leader_sz": leader_sz,
                "our_sz": our_sz, "scale_used": scale, "leader_filled": 0.0,
                "our_filled": 0.0}}


def test_amended_price_is_re_mirrored():
    """Regression: an amended order keeps its oid — without a price check our
    order stays at the old price forever."""
    his = [Order(oid=1, side="B", px=61_000, sz=0.2, ts_ms=0)]  # he moved it up
    actions = diff_ladders(his, _mirror_row(), SCALE, rebalance_pct=5)
    assert [a.kind for a in actions] == ["cancel", "place"]
    assert actions[1].px == 61_000


def test_grown_size_is_re_mirrored_but_partial_fill_is_not():
    grew = [Order(oid=1, side="B", px=57_860, sz=0.30, ts_ms=0)]
    assert [a.kind for a in diff_ladders(grew, _mirror_row(), SCALE, 5)] == ["cancel", "place"]
    # A SHRUNK size means he was partially filled — the fill path owns that,
    # re-mirroring here would cancel a live order mid-fill.
    shrunk = [Order(oid=1, side="B", px=57_860, sz=0.10, ts_ms=0)]
    assert diff_ladders(shrunk, _mirror_row(), SCALE, 5) == []


def test_diff_mirrors_sell_side_too():
    his = [Order(oid=2, side="A", px=90_000, sz=0.1, ts_ms=0)]
    actions = diff_ladders(his, {}, SCALE, rebalance_pct=5)
    assert actions[0].side == "A"


def test_position_delta_drift_band():
    assert position_delta(1.33557, SCALE, 0.2000, drift_pct=1.0) == 0.0  # 0.5% drift
    assert round(position_delta(1.33557, SCALE, 0.1000, drift_pct=1.0), 5) == 0.10103


def test_position_delta_handles_shorts():
    d = position_delta(-1.0, SCALE, 0.0, drift_pct=1.0)
    assert d < 0  # sell to reach a short target
