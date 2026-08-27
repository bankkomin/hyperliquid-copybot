"""Simulated broker for paper mode.

State is PERSISTED (paper_state + orders tables) so a restart rehydrates instead
of resetting equity — a reset would false-trip the kill-switch against the
stored high-water mark.

Same-price rule: our mirrored order fills when the leader's does, proportionally,
at his price. ponytail: no partial-queue modeling — his fill ratio IS our fill
ratio; upgrade to queue-position modeling only if M2 shows real fills diverging.
"""

from src.models import AccountState, MirrorAction, Order
from src.store import Store

MAKER_FEE = 0.00015  # 0.015%
TAKER_FEE = 0.00045  # 0.045% — reconciliation fills cross the spread


class PaperBroker:
    def __init__(self, store: Store, start_equity: float):
        self.store = store
        row = store.conn.execute(
            "SELECT cash, position, entry_notional FROM paper_state WHERE id=1"
        ).fetchone()
        self.cash, self.position, self.entry_notional = row or (start_equity, 0.0, 0.0)
        self.open: dict[int, Order] = {
            oid: Order(oid=oid, side=side, px=px, sz=sz, ts_ms=ts)
            for oid, side, px, sz, ts in store.conn.execute(
                "SELECT oid, side, px, sz, ts FROM orders WHERE status='open'"
            )
        }
        self._next_oid = min(self.open, default=0) - 1  # synthetic negative oids
        self._save()

    def _save(self) -> None:
        self.store.conn.execute(
            "INSERT OR REPLACE INTO paper_state VALUES (1,?,?,?)",
            (self.cash, self.position, self.entry_notional),
        )
        self.store.conn.commit()

    def _set_order(self, o: Order, status: str, exec_style: str = "maker") -> None:
        self.store.conn.execute(
            "INSERT OR REPLACE INTO orders(oid,ts,side,px,sz,exec_style,status)"
            " VALUES (?,?,?,?,?,?,?)",
            (o.oid, o.ts_ms, o.side, o.px, o.sz, exec_style, status),
        )
        self.store.conn.commit()

    def execute(self, a: MirrorAction, now_ms: int) -> int:
        if a.kind == "cancel":
            o = self.open.pop(a.our_oid, None)
            if o:
                self._set_order(o, "canceled")
            return a.our_oid or 0
        oid, self._next_oid = self._next_oid, self._next_oid - 1
        o = Order(oid=oid, side=a.side, px=a.px, sz=a.sz, ts_ms=now_ms)
        self.open[oid] = o
        self._set_order(o, "open")
        return oid

    def _book_fill(
        self,
        side: str,
        sz: float,
        px: float,
        fee_rate: float,
        oid: int,
        now_ms: int,
        crossed: int,
    ) -> None:
        signed = sz if side == "B" else -sz
        self.position = round(self.position + signed, 5)
        self.entry_notional += signed * px
        fee = sz * px * fee_rate
        self.cash -= fee
        # tid omitted -> sqlite autoincrement; two fills in one millisecond
        # must not overwrite each other.
        self.store.conn.execute(
            "INSERT INTO fills(oid,ts,side,px,sz,crossed,closed_pnl,fee)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (oid, now_ms, side, px, sz, crossed, 0.0, fee),
        )
        self._save()

    def on_leader_fill(self, leader_oid: int, fill_sz: float, px: float, now_ms: int) -> None:
        m = self.store.mirror_get().get(leader_oid)
        if not m or m["our_oid"] not in self.open:
            return
        o = self.open[m["our_oid"]]
        ratio = min(1.0, fill_sz / m["leader_sz"]) if m["leader_sz"] else 1.0
        sz = round(o.sz * ratio, 5)
        if sz <= 0:
            return
        self._book_fill(o.side, sz, px, MAKER_FEE, o.oid, now_ms, crossed=0)
        remaining = round(o.sz - sz, 5)
        if remaining > 0:
            self.open[o.oid] = o.model_copy(update={"sz": remaining})
            self._set_order(self.open[o.oid], "open")
        else:
            del self.open[o.oid]
            self._set_order(o, "filled")

    def market_fill(self, side: str, sz: float, px: float, now_ms: int) -> None:
        """Reconciliation / flatten fill — crosses the spread, taker fee."""
        self._book_fill(side, sz, px, TAKER_FEE, 0, now_ms, crossed=1)

    def cancel_all(self) -> int:
        n = len(self.open)
        for o in list(self.open.values()):
            self._set_order(o, "canceled")
        self.open.clear()
        return n

    def state(self, mark_px: float, now_ms: int) -> AccountState:
        upnl = self.position * mark_px - self.entry_notional
        entry = (self.entry_notional / self.position) if self.position else None
        return AccountState(
            equity=self.cash + upnl,
            position=self.position,
            entry_px=entry,
            mark_px=mark_px,
            fetched_at_ms=now_ms,
            open_orders=list(self.open.values()),
        )
