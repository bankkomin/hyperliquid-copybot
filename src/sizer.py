"""Pure sizing functions — one scale ratio drives the whole mirror.

Because every rung uses the same ratio, the leader's ladder shape (0.05 -> 0.12
-> 0.20 BTC pyramid), each rung's % of equity, and his leverage profile all
carry over exactly.
"""

import math

from src.models import MirrorAction, Order

SZ_DECIMALS = 5
MIN_NOTIONAL_USD = 10.0  # Hyperliquid exchange minimum


def _floor_sz(x: float) -> float:
    return math.floor(x * 10**SZ_DECIMALS) / 10**SZ_DECIMALS


def compute_scale(our_equity: float, leader_equity: float) -> float:
    return our_equity / leader_equity


def mirror_size(
    leader_sz: float, scale: float, px: float, min_notional_usd: float = MIN_NOTIONAL_USD
) -> float:
    """Scaled rung size, rounded DOWN. Returns 0.0 when the rung is too small to
    place — the caller logs the skip (below ~$3k equity his small rungs start
    dropping and the mirror degrades)."""
    sz = _floor_sz(leader_sz * scale)
    return sz if sz * px >= min_notional_usd else 0.0


def diff_ladders(
    leader_orders: list[Order],
    mirror: dict[int, dict],
    scale: float,
    rebalance_pct: float,
) -> list[MirrorAction]:
    """Actions that make our resting ladder equal his x scale, at his prices."""
    actions: list[MirrorAction] = []
    his = {o.oid: o for o in leader_orders}

    for loid, m in mirror.items():
        stale_scale = abs(scale / m["scale_used"] - 1) * 100 > rebalance_pct
        if loid in his and not stale_scale:
            continue
        actions.append(
            MirrorAction(
                kind="cancel",
                side=his[loid].side if loid in his else "B",
                px=m["px"],
                sz=m["our_sz"],
                leader_oid=loid,
                our_oid=m["our_oid"],
            )
        )
        if stale_scale and loid in his:  # re-place at the new scale
            o = his[loid]
            sz = mirror_size(o.sz, scale, o.px)
            if sz:
                actions.append(
                    MirrorAction(kind="place", side=o.side, px=o.px, sz=sz, leader_oid=loid)
                )

    for oid, o in his.items():
        if oid not in mirror:
            sz = mirror_size(o.sz, scale, o.px)
            if sz:
                actions.append(
                    MirrorAction(kind="place", side=o.side, px=o.px, sz=sz, leader_oid=oid)
                )
    return actions


def position_delta(
    leader_pos: float, scale: float, our_pos: float, drift_pct: float
) -> float:
    """Reconciliation delta. 0.0 when inside the drift band (don't taker-chase
    every dollar of equity fluctuation)."""
    target = leader_pos * scale
    if target and abs(our_pos / target - 1) * 100 < drift_pct:
        return 0.0
    delta = target - our_pos
    return math.copysign(_floor_sz(abs(delta)), delta)
