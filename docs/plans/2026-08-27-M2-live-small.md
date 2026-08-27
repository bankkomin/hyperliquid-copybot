# M2 Live-Small Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the paper broker with a live Hyperliquid executor behind the `mode` flag, fire-drill the kill-switch on testnet, and go live with test-size capital.

**Architecture:** `LiveBroker` implements the same interface as `PaperBroker` (`execute`, `state`, plus real `cancel_all`). `main.py` picks the broker from `cfg.mode`. Reconciliation taker top-up added to the cycle. Everything else (sizer, risk, store, dashboard) is unchanged M1 code.

**Tech Stack:** adds `hyperliquid-python-sdk` Exchange class (signing) + `eth-account`. API wallet (agent key) only — never the main private key.

## Global Constraints

- All M1 global constraints still apply
- `mode: live` requires `secrets.yaml` (git-ignored) with `api_wallet_key`; refuse to start live without it
- ALO (post-only) for mirror orders; IOC with 0.15% slippage cap for reconciliation
- B3 price-integrity gate becomes active: mirror order px must equal his px exactly; reconciliation px within 1% of mark
- Live account funding: test-size only (decided at kickoff — PRD §15 Q1, ≥ $10k so every rung clears $10 min notional)

## Prerequisite (do first)

- [ ] M1 exit criteria all checked in `docs/plans/2026-08-27-M1-shadow.md`

---

### Task 1: Exchange client wrapper

**Files:**
- Create: `src/live.py`, `secrets.example.yaml`
- Modify: `.gitignore` (add `secrets.yaml`)
- Test: `tests/test_live.py`

**Interfaces:**
- Produces:

```python
class LiveBroker:
    def __init__(self, exchange, info, address: str): ...   # injected SDK objects
    def state(self, mark_px: float, now_ms: int) -> AccountState  # from info API (our address)
    def execute(self, a: MirrorAction, now_ms: int) -> int  # ALO limit order / cancel; returns oid
    def taker(self, side: str, sz: float, mark_px: float,
              slippage_cap_pct: float) -> int               # IOC at capped price
    def cancel_all(self) -> int                             # returns count canceled
```

- [ ] **Step 1: Write the failing test** (SDK mocked — no network in tests)

```python
# tests/test_live.py
from unittest.mock import MagicMock
from src.live import LiveBroker
from src.models import MirrorAction

def broker():
    ex, info = MagicMock(), MagicMock()
    ex.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
        {"resting": {"oid": 555}}]}}}
    return LiveBroker(ex, info, "0xME"), ex

def test_place_uses_alo_at_his_exact_price():
    b, ex = broker()
    oid = b.execute(MirrorAction(kind="place", side="B", px=57_860.0,
                                 sz=0.0301, leader_oid=1), now_ms=1)
    assert oid == 555
    _, kwargs = ex.order.call_args
    assert kwargs["limit_px"] == 57_860.0
    assert kwargs["order_type"] == {"limit": {"tif": "Alo"}}
    assert kwargs["is_buy"] is True and kwargs["sz"] == 0.0301

def test_taker_caps_slippage():
    b, ex = broker()
    b.taker("B", 0.01, mark_px=80_000.0, slippage_cap_pct=0.15)
    _, kwargs = ex.order.call_args
    assert kwargs["limit_px"] == 80_120.0        # mark * 1.0015
    assert kwargs["order_type"] == {"limit": {"tif": "Ioc"}}

def test_cancel_all_cancels_every_open_order():
    b, ex = broker()
    b.info.open_orders.return_value = [{"coin": "BTC", "oid": 1}, {"coin": "BTC", "oid": 2}]
    assert b.cancel_all() == 2
    assert ex.cancel.call_count == 2

def test_cancel_all_survives_one_failing_cancel():
    b, ex = broker()
    b.info.open_orders.return_value = [{"coin": "BTC", "oid": n} for n in (1, 2, 3)]
    ex.cancel.side_effect = [None, ConnectionError("429"), None]
    assert b.cancel_all() == 2                   # confirmed cancels, not attempts
    assert ex.cancel.call_count == 3             # kept going past the failure

def test_rejected_alo_returns_none_not_phantom_oid():
    b, ex = broker()
    ex.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
        {"error": "Post only order would have immediately matched"}]}}}
    oid = b.execute(MirrorAction(kind="place", side="B", px=80_100.0,
                                 sz=0.01, leader_oid=1), now_ms=1)
    assert oid is None                           # cycle must NOT mirror_put on None

def test_top_level_err_returns_none():
    b, ex = broker()
    ex.order.return_value = {"status": "err", "response": "Insufficient margin"}
    oid = b.execute(MirrorAction(kind="place", side="B", px=70_000.0,
                                 sz=0.01, leader_oid=1), now_ms=1)
    assert oid is None
```

- [ ] **Step 2: Run** — `venv\Scripts\python.exe -m pytest tests/test_live.py -v` → FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# src/live.py
from src.models import AccountState, MirrorAction
from src.watcher import parse_clearinghouse

import structlog
log = structlog.get_logger(__name__)

class LiveBroker:
    def __init__(self, exchange, info, address: str):
        self.ex, self.info, self.address = exchange, info, address

    def _oid(self, resp) -> int | None:
        """None = rejected. Callers must skip mirror_put/decision-success on None."""
        if resp.get("status") != "ok":                 # top-level err: response is a string
            log.warning("order_rejected", detail=str(resp.get("response")))
            return None
        s = resp["response"]["data"]["statuses"][0]
        if "error" in s:                               # e.g. ALO would have crossed
            log.warning("order_rejected", detail=s["error"])
            return None
        return s.get("resting", s.get("filled", {})).get("oid")

    def execute(self, a: MirrorAction, now_ms: int) -> int | None:
        if a.kind == "cancel":
            self.ex.cancel("BTC", a.our_oid)
            return a.our_oid
        resp = self.ex.order(name="BTC", is_buy=a.side == "B", sz=a.sz,
                             limit_px=a.px, order_type={"limit": {"tif": "Alo"}})
        return self._oid(resp)

    def taker(self, side: str, sz: float, mark_px: float,
              slippage_cap_pct: float) -> int | None:
        cap = mark_px * (1 + slippage_cap_pct / 100 * (1 if side == "B" else -1))
        resp = self.ex.order(name="BTC", is_buy=side == "B", sz=sz,
                             limit_px=round(cap), order_type={"limit": {"tif": "Ioc"}})
        return self._oid(resp)

    def cancel_all(self) -> int:
        """Safety-critical (kill-switch, startup): isolate per-order errors,
        return CONFIRMED cancels only. Caller re-runs next cycle if short."""
        oo = [o for o in self.info.open_orders(self.address) if o["coin"] == "BTC"]
        ok = 0
        for o in oo:
            try:
                self.ex.cancel("BTC", o["oid"])
                ok += 1
            except Exception:
                log.exception("cancel_failed", oid=o["oid"])
        return ok

    def state(self, mark_px: float, now_ms: int) -> AccountState:
        ch = self.info.user_state(self.address)
        oo = self.info.frontend_open_orders(self.address)
        return parse_clearinghouse(ch, oo, mark_px, now_ms)
```

```yaml
# secrets.example.yaml  (copy to secrets.yaml, git-ignored)
api_wallet_key: "0x..."   # Hyperliquid API wallet (agent) key — NOT the main wallet key
```

- [ ] **Step 4: Run** — `venv\Scripts\python.exe -m pytest tests/test_live.py -v` → PASS (6 tests)
- [ ] **Step 5: Commit** — `git add src/live.py secrets.example.yaml .gitignore tests/test_live.py && git commit -m "feat: live broker — ALO mirror orders, capped IOC taker, cancel_all"`

---

### Task 2: Mode switch + B3 gate + reconciliation in the cycle

**Files:**
- Modify: `src/main.py` (broker factory + reconciliation step), `src/risk.py` (B3)
- Create: `tests/conftest.py` — fixtures `cfg_paper` (loads `config.example.yaml`) and `cfg_live` (same, with `mode` set to `"live"` via `model_copy(update=...)`)
- Test: `tests/test_mode_switch.py`, extend `tests/test_risk.py`

**Interfaces:**
- Produces: `make_broker(cfg, store) -> PaperBroker | LiveBroker` — `live` mode raises `SystemExit` if `secrets.yaml` missing. Cycle gains: after ladder diff, if `position_delta(...) != 0` → `check_order` → `broker.taker(...)`, decision-logged with trigger `reconcile`.
- B3 in `check_order`: place actions must satisfy `px == leader order px` (lookup by `leader_oid`); taker actions must satisfy `|px/mark − 1| ≤ 1%`.
- Funding monitor (PRD §6.2 M4, only meaningful live): cycle sums `userFunding` deltas into `equity_curve.funding_cum`; if 30-day funding > `funding_alert_pct_30d` of equity → `events` alert row (alert only, no state change).
- **Our-fills ingestion (live):** the M1 paper broker wrote our `fills` rows itself; live fills happen on-exchange, so the cycle must also poll `userFillsByTime` for OUR address (a second `Watcher(cfg.our_address)`-style fetch) and INSERT into `fills` with the real exchange `tid` — otherwise the dashboard maker-% panel and the §14 go/no-go cost lines go dark at go-live. Marks matching `orders` rows filled.
- **None-oid handling:** cycle treats `broker.execute(...) is None` as a rejection — no `mirror_put`, decision row `action='veto', veto_reason='exchange_reject'`; the rung retries on the next cycle's diff.
- Manual un-HALT: `store.latest_risk_state()` (M1 Task 3) already consumes an operator-inserted `events` row with `kind='manual_reset'` — the drill in Task 3 relies on it.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mode_switch.py
import pytest
from src.main import make_broker
from src.paper import PaperBroker

def test_paper_mode_returns_paper_broker(tmp_path, cfg_paper):
    assert isinstance(make_broker(cfg_paper, tmp_path), PaperBroker)

def test_live_mode_without_secrets_refuses(tmp_path, cfg_live):
    with pytest.raises(SystemExit):
        make_broker(cfg_live, tmp_path)         # no secrets.yaml present
```

```python
# tests/test_risk.py (additions)
def test_b3_mirror_price_must_match_leader():
    leader = acct(66_435, 1.33557, 0)
    leader.open_orders.append(Order(oid=1, side="B", px=57_860.0, sz=0.2, ts_ms=0))
    ours = acct(10_000, 0.18, 0)
    bad = MirrorAction(kind="place", side="B", px=57_900.0, sz=0.01, leader_oid=1)
    v = check_order(bad, ours, leader, 1_000_000, RiskState.NORMAL, CFG)
    assert not v.approved and v.reason == "B3_price"
```

- [ ] **Step 2: Run** — both new tests FAIL
- [ ] **Step 3: Implement** — `make_broker` reads `cfg.mode`; live path loads `secrets.yaml`, builds SDK `Exchange`/`Info`, wraps in `LiveBroker`. B3 branch in `check_order` before B2. Reconciliation block appended to `cycle`.
- [ ] **Step 4: Run** — `venv\Scripts\python.exe -m pytest tests/ -v` → all green
- [ ] **Step 5: Commit** — `git commit -am "feat: mode switch, B3 price gate, taker reconciliation"`

---

### Task 3: Testnet fire-drill (manual, scripted checklist)

**Files:**
- Create: `docs/plans/M2-drill-log.md` (fill in as you go)

- [ ] **Step 1:** Point `config.yaml` at testnet (`api_url: https://api.hyperliquid-testnet.xyz`), fund the testnet account from the faucet, set `mode: live`.
- [ ] **Step 2:** Run one full day. Verify in dashboard: ladder mirrored (parity ≥ 99%), orders visible on testnet UI.
- [ ] **Step 3:** Kill-switch drill: temporarily set `max_drawdown_pct: -0.1`, restart. Expected within one cycle: HALT event, `cancel_all` executed (0 open orders on testnet UI), position flattened, pid still alive, dashboard banner red HALT, restart does NOT resume (sticky HALT until `events` row `manual_reset` is inserted by hand).
- [ ] **Step 4:** Restore `max_drawdown_pct: -35`, insert manual reset, verify NORMAL resumes and the ladder re-mirrors.
- [ ] **Step 5:** Record every observation in `M2-drill-log.md`; commit.

---

### Task 4: Go-live

- [ ] **Step 1:** Fund the live account with the kickoff-decided test-size capital (≥ $10k).
- [ ] **Step 2:** `config.yaml`: mainnet URL, `mode: live`. Start via `start_copybot.bat`.
- [ ] **Step 3:** Verify within the first hour: full ladder resting (compare vs catseye Active Orders), B2 parity ≥ 99%, telegram alive.
- [ ] **Step 4:** Run until exit criteria met.

## M2 Exit Criteria (from PRD §13)

- [ ] Full ladder mirrored, parity ≥ 99% (query: our resting notional / (leader × scale))
- [ ] ≥ 2 rungs filled **as maker alongside his fills** (join `fills` × `leader_fills` on px)
- [ ] Position drift < 1% sustained
- [ ] Kill-switch fire-drilled on testnet (drill log committed)
- [ ] Copy-quality daily report reviewed: maker %, fill lag, tracking error — economics still positive vs holding BTC (PRD §14 go/no-go)
