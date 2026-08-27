"""Simulated broker for paper mode.

State is PERSISTED (paper_state + orders tables) so a restart rehydrates instead
of resetting equity — a reset would false-trip the kill-switch against the
stored high-water mark.

Same-price rule: our mirrored order fills when the leader's does, proportionally,
at his price. Fills are tracked CUMULATIVELY against his original rung size, so a
rung that fills in several partials still ends fully mirrored.

ponytail: no order-queue position modeling — his fill ratio IS our fill ratio;
revisit only if M2 shows real fills diverging from this assumption.
"""

from src.models import AccountState, MirrorAction, Order
from src.store import Store

MAKER_FEE = 0.00015  # 0.015%
TAKER_FEE = 0.00045  # 0.045% — reconciliation fills cross the spread


class PaperBroker:
    def __init__(self, store: Store, start_equity: float):
        self.store = store
        row = store.conn.execute(
            "SELECT cash, position, avg_entry, realized FROM paper_state WHERE id=1"
        ).fetchone()
        self.cash, self.position, self.avg_entry, self.realized = row or (
            start_equity, 0.0, None, 0.0,
        )
        self.open: dict[int, Order] = {
            oid: Order(oid=oid, side=side, px=px, sz=sz, ts_ms=ts)
            for oid, side, px, sz, ts in store.conn.execute(
                "SELECT oid, side, px, sz, ts FROM orders WHERE status='open'"
            )
        }
        # Seed from ALL historical oids, not just open ones: reusing a retired
        # oid would clobber its `orders` row and orphan the fills pointing at it.
        low = store.conn.execute("SELECT MIN(oid) FROM orders").fetchone()[0]
        self._next_oid = min(low or 0, 0) - 1  # synthetic negative oids
        self._save()

    def _save(self) -> None:
        self.store.conn.execute(
            "INSERT OR REPLACE INTO paper_state VALUES (1,?,?,?,?)",
            (self.cash, self.position, self.avg_entry, self.realized),
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
        self, side: str, sz: float, px: float, fee_rate: float, oid: int,
        now_ms: int, crossed: int,
    ) -> None:
        """Average-cost accounting: closing size realizes PnL into cash, opening
        size moves the average entry. Keeps entry_px honest through flips."""
        signed = sz if side == "B" else -sz
        closed_pnl = 0.0
        if self.position and self.avg_entry is not None and (signed * self.position) < 0:
            closing = min(abs(signed), abs(self.position))
            direction = 1.0 if self.position > 0 else -1.0
            closed_pnl = closing * (px - self.avg_entry) * direction
            self.realized += closed_pnl
            self.cash += closed_pnl
            remainder = abs(signed) - closing
            self.position = round(self.position + signed, 5)
            self.avg_entry = px if remainder > 0 else (None if not self.position else self.avg_entry)
        else:
            base = abs(self.position)
            self.avg_entry = (
                px if not base or self.avg_entry is None
                else (self.avg_entry * base + px * sz) / (base + sz)
            )
            self.position = round(self.position + signed, 5)

        fee = sz * px * fee_rate
        self.cash -= fee
        # tid omitted -> sqlite autoincrement; two fills in the same millisecond
        # must not overwrite each other.
        self.store.conn.execute(
            "INSERT INTO fills(oid,ts,side,px,sz,crossed,closed_pnl,fee)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (oid, now_ms, side, px, sz, crossed, closed_pnl, fee),
        )
        self._save()

    def on_leader_fill(self, leader_oid: int, fill_sz: float, px: float, now_ms: int) -> None:
        m = self.store.mirror_get().get(leader_oid)
        if not m or m["our_oid"] not in self.open or not m["leader_sz"]:
            return
        # Cumulative, not per-fill: our total filled must track HIS total filled
        # as a fraction of his original rung, or multi-partial rungs under-fill.
        leader_filled = min(m["leader_sz"], m["leader_filled"] + fill_sz)
        target = m["our_sz"] * (leader_filled / m["leader_sz"])
        sz = round(target - m["our_filled"], 5)
        if sz <= 0:
            return
        o = self.open[m["our_oid"]]
        sz = min(sz, o.sz)
        self._book_fill(o.side, sz, px, MAKER_FEE, o.oid, now_ms, crossed=0)
        self.store.mirror_fill(leader_oid, leader_filled, m["our_filled"] + sz)

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
        upnl = (mark_px - self.avg_entry) * self.position if self.avg_entry else 0.0
        return AccountState(
            equity=self.cash + upnl,
            position=self.position,
            entry_px=self.avg_entry,
            mark_px=mark_px,
            fetched_at_ms=now_ms,
            open_orders=list(self.open.values()),
        )
