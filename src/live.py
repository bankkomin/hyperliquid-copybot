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
        self._last_fill_ts = 0

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

    def execute(self, a: MirrorAction, now_ms: int) -> int | None:
        if a.kind == "cancel":
            try:
                self.ex.cancel("BTC", a.our_oid)
            except Exception:
                log.exception("cancel_failed", oid=a.our_oid)
            return a.our_oid
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

    def market_fill(self, side: str, sz: float, px: float, now_ms: int) -> int | None:
        """Reconciliation / flatten: IOC with a hard slippage cap off the mark."""
        cap_pct = self._slippage_cap_pct
        limit = px * (1 + cap_pct / 100 * (1 if side == "B" else -1))
        try:
            resp = self.ex.order(
                name="BTC", is_buy=side == "B", sz=sz, limit_px=round(limit, PX_DECIMALS),
                order_type={"limit": {"tif": "Ioc"}}, reduce_only=False,
            )
        except Exception:
            log.exception("taker_failed", sz=sz, px=px)
            return None
        return self._oid(resp)

    def on_leader_fill(self, leader_oid: int, fill_sz: float, px: float, now_ms: int) -> None:
        """No-op live: the exchange fills our resting order on its own. Fill
        bookkeeping comes from ingest_our_fills."""
        return None

    def cancel_all(self) -> int:
        """Safety-critical (kill-switch, startup recovery): isolate per-order
        errors and return CONFIRMED cancels, so a single 429 cannot leave the
        rest of the ladder resting while the caller believes cleanup ran."""
        try:
            oo = [o for o in self.info.open_orders(self.address) if o.get("coin") == "BTC"]
        except Exception:
            log.exception("open_orders_failed")
            return 0
        ok = 0
        for o in oo:
            try:
                self.ex.cancel("BTC", o["oid"])
                ok += 1
            except Exception:
                log.exception("cancel_failed", oid=o["oid"])
        if ok < len(oo):
            log.warning("cancel_all_incomplete", canceled=ok, total=len(oo))
        return ok

    # ---- state ----------------------------------------------------------
    @property
    def open(self) -> dict:
        """Our live resting orders, keyed by oid (mirrors PaperBroker.open)."""
        try:
            return {
                o["oid"]: o
                for o in self.info.frontend_open_orders(self.address)
                if o.get("coin") == "BTC"
            }
        except Exception:
            log.exception("open_orders_failed")
            return {}

    def state(self, mark_px: float, now_ms: int) -> AccountState:
        ch = self.info.user_state(self.address)
        oo = [o for o in self.info.frontend_open_orders(self.address) if o.get("coin") == "BTC"]
        return parse_clearinghouse(ch, oo, mark_px, now_ms)

    def ingest_our_fills(self, now_ms: int) -> int:
        """Persist OUR exchange fills — the paper broker wrote these itself, so
        without this the maker-%, fee and cost lines go dark at go-live."""
        try:
            fills = self.info.user_fills_by_time(self.address, self._last_fill_ts or 0)
        except Exception:
            log.exception("our_fills_fetch_failed")
            return 0
        n = 0
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
            self._last_fill_ts = max(self._last_fill_ts, f["time"] + 1)
            n += 1
        self.store.conn.commit()
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
