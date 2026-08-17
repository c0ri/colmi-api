# Roadmap

## 2026-08-17 — Collapse per-cycle BLE connections 6 → 1 (done)

**Problem:** system `dbus-daemon` on peaktribe had leaked to ~4.65GB RSS +
1.8GB swap after 19 days uptime, pushing the whole Pi to 358MB free. Traced
(via `strace` on live `dbus-daemon` + journal counts, see
`~/debian-dbus-leak.md`) to `run_cycle()` calling `read_metric()`/
`read_battery()` once per sensor (heart-rate/spo2/pressure/hrv/steps/
battery), each going through `run_colmi_command()` — a `bluetoothctl
disconnect` + a brand-new `colmi_r02_client` CLI subprocess per metric,
i.e. a fresh BLE connect/disconnect and a fresh `bluetoothd` "Adv Monitor
app" registration on the system D-Bus bus, 6 times every cycle. Confirmed
via git history that this pattern was introduced by the 2026-07-29
poll-loop redesign below and had been running unnoticed for the entire
19-day leak window (two prior boots of 50+ days each, before this
redesign, had no equivalent issue).

Built a 5-variant synthetic dbus-daemon reproducer to check whether this
was actually a dbus-daemon bug before touching production code — all five
came back negative even at 1000x+ the real churn volume (see
`~/dbus-leak-repro/`), so this was never filed as a Debian bug. The git
timeline correlation was strong enough on its own to justify fixing the
churn regardless of whose bug it technically is.

**Fix:** `poller.py` now uses `colmi_r02_client`'s async `Client` class
directly (added as a real dependency — same pinned commit already running
via `pipx` — instead of shelling out to its CLI) and opens **one** BLE
connection per cycle, reading all six metrics through it before
disconnecting. Reconnects on-demand only if the connection actually drops
mid-cycle (this ring is occasionally flaky mid-session — an unconditional
single connection wasn't reliable enough on its own, see live-test notes
below), rather than reconnecting unconditionally per metric like before.
Verified live against the real ring: a full cycle now produces exactly 1
`Adv Monitor app` disconnect event in the bluetoothd journal instead of 6.

Also fixes the "**`steps` comes back `null` most cycles**" open follow-up
below as a side effect: the old regex-based CLI-output parser expected a
bracketed number list, but `get-steps`' pretty-printed output is a pipe
table with no brackets, so it silently never matched anything. Reading
`client.get_steps()` directly now returns typed `SportDetail` objects;
`steps` is stored as their sum for the day.

Old subprocess/CLI-based implementation preserved at `poller.py.bak` for
rollback.

**Still open:** `hrv` still fails most cycles (times out against this
ring specifically — genuine flakiness, not a parsing bug, degrades
gracefully). Whether the churn fix actually slowed dbus-daemon's growth
is unconfirmed as of this writing — a cron-based email alert
(`~/bin/dbus-leak-alert.sh`, fires once per boot if dbus-daemon RSS
crosses 1GB) is watching it going forward.

## 2026-07-31 — Idle poll backoff to save ring battery (done)

**Problem:** Evelyn only checks the ring closely in dominant mode, but the
poller was reading it on a fixed ~5min-ish cadence around the clock
regardless of whether anyone was looking — unnecessary BLE wake-ups draining
the ring's battery for data nobody's consuming yet.

**Fix:** `app.py` now records `last_queried_at` (via `db.record_query()`) on
every `/latest`, `/heartrate`, `/metrics` request from a non-localhost caller
— localhost is excluded specifically so `colmi-watchdog`'s own checks don't
look like real activity and pin the poller to its active interval forever.
`poller.py` checks this each sleep tick (`current_poll_interval()`): if no
real caller has queried in `IDLE_BACKOFF_SEC` (default 90min), it backs off
to `POLL_INTERVAL_BACKOFF_SEC` (default 30min) instead of `POLL_INTERVAL_SEC`.
A query arriving mid-sleep clears it within one 5s tick, no restart needed.
`STALE_AFTER_SEC` bumped 900s → 2100s (35min) to comfortably cover a full
backoff gap without `/latest` reporting `stale: true` on the first query back.

**Important interaction with `colmi-watchdog`:** the watchdog's bluetoothd
health check was deliberately decoupled from data staleness (see the
2026-07-31 entry below) specifically so this backoff feature wouldn't slow
down real-fault recovery — if it still gated on staleness, a legitimate
30min-idle backoff window would look identical to a wedged bluetoothd, and
recovery could take up to 35min instead of the watchdog's normal 5min
cadence.

## 2026-07-31 — bluetoothd wedge incident + colmi-watchdog rework (done)

**Problem:** ring was off-wrist charging; `colmi-poller`'s connect/disconnect retry
loop (2 attempts × 45s × 6 metrics per cycle, repeated every cycle) against an
unreachable device wedged `bluetoothd`'s D-Bus interface — the process stayed
"running" but every `bluetoothctl`/D-Bus call started timing out or returning
"No default controller available". Went unnoticed for 7+ hours because `/health`
only tracks "is the poll loop still ticking" (`db.record_cycle()` fires on
failed cycles too), so it kept reporting `poller_alive: true` throughout. The
old `service-watchdog.timer` also didn't help — it checked the same
loop-liveness signal and, even on a real failure, only ever restarted
`colmi-api.service`, which was never the broken piece.

**Fix:**
1. Manual recovery: `stop colmi-poller` → `restart bluetooth.service` →
   `start colmi-poller`. Documented as a repeatable procedure in
   `~/.claude/skills/colmi-recover/SKILL.md`.
2. Replaced `service-watchdog.timer` (which also covered `qiui-ble`, an
   unrelated service with a different failure/recovery model) with a
   dedicated `colmi-watchdog.timer`. It checks `/latest`'s `age_seconds`
   instead of `/health`, and only acts when data is stale *and*
   `bluetoothctl show` confirms bluetoothd is actually wedged — avoids
   false-triggering on the ring simply being out of range, which the poller
   already retries on its own. See `colmi-watchdog.sh` / `README.md`.

## 2026-07-29 — Poll-loop redesign (done)

**Problem:** the old design triggered a live BLE read against the ring on
every HTTP request. Two independent pollers — the Pi's own `service-watchdog`
health check and Evelyn's Celery-driven biometrics polling — both hit the API
on their own schedules, and both ended up contending for the ring's single
BLE connection slot. That contention surfaced as `BleakDBusError:
br-connection-canceled` / `Operation already in progress` failures. A
separate bug on Evelyn's side (throttle logic keyed off last *successful*
reading instead of last *attempt*) meant a single failed poll would disable
her backoff entirely, turning what should've been a 10-minute polling
interval into a 30-second retry storm — which made the contention much worse
in practice, though it wasn't the root cause.

**Fix:** split into two services with a hard ownership boundary.

- `colmi-poller.service` — the *only* process that ever opens a BLE
  connection to the ring. Runs a fixed-interval loop (`POLL_INTERVAL_SEC`,
  default 60s), reads whatever sensors respond, and writes the result to
  SQLite (`data/colmi.db`).
- `colmi-api.service` — read-only Flask API. Every endpoint is a single
  `SELECT ... ORDER BY recorded_at DESC LIMIT 1` — no BLE call on the request
  path, so responses are single-digit milliseconds regardless of how many
  clients poll or how often.

Callers (Evelyn or anything else) can now poll on whatever cadence they
want — there's nothing left to contend over, since the API never touches the
ring.

**Two bugs found and fixed during the build, not part of the original plan:**

1. `NOTIFY_SOCKET` leaked into the `colmi_r02_client` subprocess (inherited
   via `subprocess.Popen`'s default env passthrough). Its own dependency
   stack (anyio/asyncclick) sent a `STOPPING=1` systemd notification on
   normal exit, which — since `NotifyAccess=all` — systemd accepted as *our*
   service stopping, and killed the poll loop after every single metric
   read.
2. Even after fixing (1), `Type=notify` + `WatchdogSec` remained unreliable
   with this subprocess-forking pattern (repeated `deactivating
   (stop-sigterm)` cycling for reasons never fully root-caused past that
   point). Isolated via an A/B test against `Type=simple`, which came back
   immediately stable (0 restarts vs. constant crash-looping). Dropped the
   systemd watchdog integration entirely — liveness is tracked via
   `db.record_cycle()` and surfaced through `GET /health` instead, which is
   externally inspectable and doesn't depend on systemd's notify-socket
   semantics at all.

Pre-redesign (request-driven) implementation is preserved at git tag
`pre-poll-loop-redesign`.

## Open follow-ups

- **Evelyn-side response parsing.** `/heartrate` and `/metrics` dropped the
  old `"timestamp"` field in favor of `"recorded_at"` + `"age_seconds"` +
  `"stale"`. `/health`'s meaning changed entirely — it now reports poller
  liveness, not ring BLE connectivity. Evelyn's parsing code needs a small
  update to match (or the API needs a backward-compat `"timestamp"` alias —
  not yet decided which).
- **`hrv` comes back `null` most cycles.** Still open as of 2026-08-17 —
  genuine timeout against this ring, not a parsing bug (see that entry
  above; `steps` was the same bucket but is now fixed). Worth a proper
  look if HRV data actually matters downstream.
- **No reading history/trends yet.** `readings` is append-only and already
  supports it, but nothing queries anything but the latest row. Fine for
  Evelyn's current use case; revisit if trend data becomes useful.
- **Actual poll cycle time regularly exceeds `POLL_INTERVAL_SEC` by 3-5x.**
  Confirmed 2026-07-29, still true after the 2026-08-17 connection-count
  fix above (that fix targeted D-Bus churn, not per-metric timeout
  duration): individual real-time reads can each take up to ~40s
  internally when the ring doesn't produce a valid sample quickly, and
  with up to 4 real-time metrics per cycle that can stack to several
  minutes even on a single connection. `STALE_AFTER_SEC` already bumped to
  900s to absorb this. Revisit if it starts mattering again, e.g. by
  reading real-time metrics concurrently instead of sequentially.
