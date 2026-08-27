# M3 Drill Log

All drills run in `paper` mode against the live Hyperliquid API on 2026-08-27,
leader `0xdae4...7637`. Live-money drills (testnet fire-drill, go-live) are
listed at the bottom as operator tasks — they require funding an account and
placing real orders.

## Drill 1 — hard kill (`taskkill /f` mid-cycle)

Started under `pythonw.exe`, let it mirror, then force-killed and restarted.

| | rungs | our open orders | position | mirror rows total |
|---|---|---|---|---|
| before kill | 14 | 14 | 0.20486 | 14 |
| after restart | 14 | 14 | 0.20486 | **14** |

- No duplicate orders (total mirror rows unchanged — a duplicate would show 28).
- High-water mark preserved at 10,000.0 → **a reboot cannot clear the kill-switch**.
- Paper book rehydrated from SQLite, so the mirror rows still described real orders.

**PASS.**

## Drill 2 — graceful stop (`stop_copybot.bat` path)

`python -m src.stop` writes a `stop_requested` event; the bot polls every 2 s.

- Process exited within 10 s (no force kill needed).
- Events: `stop_requested` → `stopped (canceled=0 position_kept=0.20486)`.
- **Position kept** — stopping the bot must never liquidate a healthy book.
- pid file removed on exit, so the next start is not blocked.

**PASS.** Note: the request goes through the DB, not a signal — `pythonw.exe`
has no window to receive `WM_CLOSE`, so `taskkill` can only force-kill and a
finally-block shutdown would never run.

## Drill 3 — network cut

Baseline cycle healthy, then the API host was pointed at a dead endpoint
(staleness threshold compressed to 10 s for the drill).

- During outage: state **WARNING**, exactly **1** `ws_lost` event (not one per cycle).
- **Resting orders KEPT** (14 rungs) — they match the leader's standing intent
  (PRD §6.2 M2); cancelling on a blip would be the wrong reflex.
- After reconnect: state back to **NORMAL**, 1 `ws_recovered` event.

**PASS.**

## Drill 4 — HALT button + un-HALT

Pressed the dashboard's HALT control (`request_halt` row), waited one cycle.

- State → **HALT**, position flattened **0.20486 → 0.0**, all 14 orders canceled,
  all mirror rungs closed.
- Restarting while halted logs `startup_halted` and does **not** resume trading.
- Inserting the documented `manual_reset` event → state NORMAL, 14 rungs
  re-mirrored, position re-acquired (0.20456).

**PASS.**

## Operator tasks (cannot be automated here)

These involve real or testnet funds and placing real orders:

- [ ] **Testnet fire-drill** — fund a testnet account, set `api_url` to
      `https://api.hyperliquid-testnet.xyz`, `mode: live`, temporarily set
      `max_drawdown_pct: -0.1`, and confirm the kill-switch cancels + flattens
      on-venue. Restore `-35` and clear with a `manual_reset` afterwards.
- [ ] **Reboot drill** — reboot the VPS, confirm `start_copybot.bat` (or the
      Task Scheduler "At log on" entry) comes back and re-mirrors cleanly. The
      stale-pid guard now checks `tasklist` before refusing, so a pid file left
      by the reboot no longer blocks the restart.
- [ ] **48 h unattended soak** after the above.
