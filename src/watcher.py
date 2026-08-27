"""Hyperliquid info API -> models. Read-only, no signing."""

import aiohttp

from src.models import AccountState, Order


def parse_clearinghouse(
    raw: dict, orders_raw: list[dict], mark_px: float, now_ms: int
) -> AccountState:
    pos, entry = 0.0, None
    for ap in raw.get("assetPositions", []):
        p = ap["position"]
        if p["coin"] == "BTC":  # B1: BTC only, by construction
            pos = float(p["szi"])
            entry = float(p["entryPx"]) if p.get("entryPx") else None
    orders = [
        Order(
            oid=o["oid"],
            side=o["side"],
            px=float(o["limitPx"]),
            sz=float(o["sz"]),
            ts_ms=o["timestamp"],
        )
        for o in orders_raw
        if o.get("coin") == "BTC"
    ]
    return AccountState(
        equity=float(raw["marginSummary"]["accountValue"]),
        position=pos,
        entry_px=entry,
        mark_px=mark_px,
        fetched_at_ms=now_ms,
        open_orders=orders,
    )


class Watcher:
    def __init__(self, address: str, api_url: str = "https://api.hyperliquid.xyz"):
        self.address, self.api_url = address, api_url

    async def _info(self, session: aiohttp.ClientSession, body: dict):
        async with session.post(f"{self.api_url}/info", json=body) as r:
            r.raise_for_status()
            return await r.json()

    async def fetch(self, now_ms: int) -> AccountState:
        async with aiohttp.ClientSession() as s:
            ch = await self._info(s, {"type": "clearinghouseState", "user": self.address})
            oo = await self._info(s, {"type": "frontendOpenOrders", "user": self.address})
            mids = await self._info(s, {"type": "allMids"})
        return parse_clearinghouse(ch, oo, float(mids["BTC"]), now_ms)

    async def fetch_mark(self) -> float:
        """BTC mark price alone. HALT needs a price to flatten at, and it must
        not depend on the leader's account data being reachable."""
        async with aiohttp.ClientSession() as s:
            mids = await self._info(s, {"type": "allMids"})
        return float(mids["BTC"])

    async def fetch_fills(self, since_ms: int) -> list[dict]:
        """Raw fills since `since_ms`. This is how the cycle distinguishes a
        leader FILL from a leader CANCEL — both make his order disappear."""
        async with aiohttp.ClientSession() as s:
            fills = await self._info(
                s,
                {"type": "userFillsByTime", "user": self.address, "startTime": since_ms},
            )
        return [f for f in fills if f.get("coin") == "BTC"]
