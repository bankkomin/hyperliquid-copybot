"""Live Hyperliquid executor. Same interface as PaperBroker so main.cycle is
unchanged between modes.

Uses an API wallet (agent key) — never the main wallet key. Mirror orders are
ALO (post-only) at the leader's exact price, so we rest alongside him and earn
the same maker fills; only reconciliation crosses the spread.
"""

from pathlib import Path

import structlog
import yaml

from src.config import Config
from src.models import AccountState, MirrorAction
from src.store import Store
from src.watcher import parse_clearinghouse

log = structlog.get_logger(__name__)

SECRETS = Path("secrets.yaml")
PX_DECIMALS = 0  # BTC perp trades in whole dollars on Hyperliquid


class LiveBroker:
    def __init__(self, exchange, info, address: str, store: Store):
        self.ex, self.info, self.address, self.store = exchange, info, address, store

    # ---- order plumbing -------------------------------------------------
    def _oid(self, resp) -> int | None:
        """None = rejected. Callers must not record a mirror row for None."""
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            log.warning("order_rejected", detail=str(resp)[:200])
            return None
        try:
            s = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            log.warning("order_response_unparsed", detail=str(resp)[:200])
            return None
        if not isinstance(s, dict) or "error" in s:
            log.warning("order_rejected", detail=str(s)[:200])
            return None
        return (s.get("resting") or s.get("filled") or {}).get("oid")

    def _cancel_ok(self, oid: int) -> bool:
        """The SDK only raises on HTTP >= 400; a REJECTED cancel comes back 200
        with an error payload. Counting that as success would leave live orders
        resting while the caller believes the book is clean."""
        try:
            resp = self.ex.cancel("BTC", oid)
        except Exception:
            log.exception("cancel_failed", oid=oid)
            return False
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            log.warning("cancel_rejected", oid=oid, detail=str(resp)[:200])
            return False
        try:
            s = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return True  # accepted, shape just differs
        if isinstance(s, dict) and "error" in s:
            log.warning("cancel_rejected", oid=oid, detail=str(s)[:200])
            return False
        # Mark it locally too: the dashboard reads `orders` for our ladder, and a
        # row left 'open' after a HALT shows the operator a book that is gone.
        self.store.conn.execute("UPDATE orders SET status='canceled' WHERE oid=?", (oid,))
        self.store.conn.commit()
        return True

    def execute(self, a: MirrorAction, now_ms: int) -> int | None:
        if a.kind == "cancel":
            if not self._cancel_ok(a.our_oid):
                return None  # caller must NOT close the mirror row
            return a.our_oid

        # A transport timeout on an accepted order would otherwise be retried
        # next cycle and double our size at that rung. Adopt the resting twin.
        for oid, o in self.open.items():
            if (
                o.get("side") == a.side
                and abs(float(o.get("limitPx", 0)) - a.px) < 1e-9
                and abs(float(o.get("sz", 0)) - a.sz) < 1e-9
            ):
                log.info("adopted_existing_order", oid=oid, px=a.px)
                return oid
        try:
            resp = self.ex.order(
                name="BTC", is_buy=a.side == "B", sz=a.sz, limit_px=a.px,
                order_type={"limit": {"tif": "Alo"}}, reduce_only=False,
            )
        except Exception:
            log.exception("order_failed", px=a.px, sz=a.sz)
            return None
        oid = self._oid(resp)
        if oid is not None:
            self.store.conn.execute(
                "INSERT OR REPLACE INTO orders(oid,ts,side,px,sz,exec_style,status)"
                " VALUES (?,?,?,?,?,?,?)",
                (oid, now_ms, a.side, a.px, a.sz, "maker", "open"),
            )
            self.store.conn.commit()
        return oid

    def market_fill(
        self, side: str, sz: float, px: float, now_ms: int, reduce_only: bool = False
    ) -> bool:
        """Reconciliation / flatten: IOC with a hard slippage cap off the mark.

        Returns whether the exchange accepted it. A flatten passes
        reduce_only=True so a position that moved between our snapshot and this
        order can never be flipped into fresh naked exposure.
        """
        limit = px * (1 + self._slippage_cap_pct / 100 * (1 if side == "B" else -1))
        try:
            resp = self.ex.order(
                name="BTC", is_buy=side == "B", sz=sz, limit_px=round(limit, PX_DECIMALS),
                order_type={"limit": {"tif": "Ioc"}}, reduce_only=reduce_only,
            )
        except Exception:
            log.exception("taker_failed", sz=sz, px=px)
            return False
        return self._oid(resp) is not None

    def on_leader_fill(self, leader_oid: int, fill_sz: float, px: float, now_ms: int) -> None:
        """No-op live: the exchange fills our resting order on its own. Fill
        bookkeeping comes from ingest_our_fills."""
        return None

    def cancel_all(self) -> int:
        """Safety-critical (kill-switch, startup recovery): isolate per-order
        errors and return CONFIRMED cancels, so a single 429 cannot leave the
        rest of the ladder resting while the caller believes cleanup ran.

        Raises if the book cannot be read at all — callers must not mistake an
        unanswered query for an empty one.
        """
        oids = list(self.open)
        ok = sum(1 for oid in oids if self._cancel_ok(oid))
        if ok < len(oids):
            log.warning("cancel_all_incomplete", canceled=ok, total=len(oids))
        return ok

    def book_is_clean(self) -> bool:
        """Verified-empty book. False on API failure — unknown is never clean."""
        try:
            return not self.open
        except Exception:
            log.exception("book_check_failed")
            return False

    # ---- state ----------------------------------------------------------
    @property
    def open(self) -> dict:
        """Our live resting orders, keyed by oid (mirrors PaperBroker.open).

        RAISES on API failure — it must never return {} for "I could not ask".
        An empty book and an unanswered question look identical to every safety
        check that counts orders, and treating a timeout as "clean" is how a
        HALT reports success while the ladder is still resting.
        """
        return {
            o["oid"]: o
            for o in self.info.frontend_open_orders(self.address)
            if o.get("coin") == "BTC"
        }

    def state(self, mark_px: float, now_ms: int) -> AccountState:
        ch = self.info.user_state(self.address)
        return parse_clearinghouse(ch, list(self.open.values()), mark_px, now_ms)

    def ingest_our_fills(self, now_ms: int) -> int:
        """Persist OUR exchange fills — the paper broker wrote these itself, so
        without this the maker-%, fee and cost lines go dark at go-live.

        The cursor lives in the DB: an in-memory one resets to 0 on restart and
        re-walks the account's whole fill history, blanking the recent fills the
        report needs exactly when a crash makes them most interesting.
        """
        since = self.store.fill_cursor()
        try:
            fills = self.info.user_fills_by_time(self.address, since)
        except Exception:
            log.exception("our_fills_fetch_failed")
            return 0
        n, newest = 0, since
        for f in fills:
            if f.get("coin") != "BTC":
                continue
            self.store.conn.execute(
                "INSERT OR REPLACE INTO fills(tid,oid,ts,side,px,sz,crossed,closed_pnl,fee)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (f["tid"], f.get("oid", 0), f["time"], f["side"], float(f["px"]),
                 float(f["sz"]), int(bool(f.get("crossed"))), float(f.get("closedPnl", 0)),
                 float(f.get("fee", 0))),
            )
            # A resting order that filled is no longer part of our ladder.
            self.store.conn.execute(
                "UPDATE orders SET status='filled', filled_sz=COALESCE(filled_sz,0)+?, "
                "avg_px=? WHERE oid=?",
                (float(f["sz"]), float(f["px"]), f.get("oid", 0)),
            )
            newest = max(newest, f["time"] + 1)
            n += 1
        self.store.conn.commit()
        if newest > since:
            self.store.set_fill_cursor(newest)
        return n

    def funding_since(self, since_ms: int) -> float:
        try:
            rows = self.info.user_funding_history(self.address, since_ms)
        except Exception:
            log.exception("funding_fetch_failed")
            return 0.0
        return sum(float(r["delta"]["usdc"]) for r in rows)


def build_live_broker(cfg: Config, store: Store) -> LiveBroker:
    """Refuses to run live without secrets — a missing key must never silently
    fall back to a mode the operator did not choose."""
    if not SECRETS.exists():
        raise SystemExit(
            f"mode: live requires {SECRETS} (copy secrets.example.yaml and add your "
            "Hyperliquid API wallet key). Refusing to start."
        )
    secrets = yaml.safe_load(SECRETS.read_text()) or {}
    key = secrets.get("api_wallet_key", "")
    if not key or not key.startswith("0x"):
        raise SystemExit(f"{SECRETS}: api_wallet_key missing or malformed. Refusing to start.")

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    wallet = Account.from_key(key)
    address = cfg.our_address or wallet.address
    info = Info(cfg.api_url, skip_ws=True)
    exchange = Exchange(wallet, cfg.api_url, account_address=address)
    broker = LiveBroker(exchange, info, address, store)
    broker._slippage_cap_pct = cfg.execution.taker_slippage_cap_pct
    log.info("live_broker_ready", address=address, api=cfg.api_url)
    return broker


# Default for brokers built directly in tests.
LiveBroker._slippage_cap_pct = 0.15
