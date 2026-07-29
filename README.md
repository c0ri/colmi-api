# Colmi Ring HTTP API

Two systemd services for the Colmi R02/R06 smart ring:

- **`colmi-poller.service`** — background loop, the *sole* owner of the BLE connection.
  Polls the ring every `POLL_INTERVAL_SEC` (default 60s) and writes each reading to a
  local SQLite database (`data/colmi.db`). Feeds systemd's watchdog on loop liveness,
  independent of whether the ring itself answered that cycle.
- **`colmi-api.service`** — read-only Flask API. Never touches BLE; every request is a
  single-digit-millisecond SQLite read of the latest row the poller wrote.

This split exists because the previous request-driven design (each HTTP request
triggering a live BLE read) had multiple callers — the systemd watchdog and Evelyn's
Celery poller — contending for the ring's single BLE connection slot, which caused
`BleakDBusError: br-connection-canceled` / `Operation already in progress` failures.
With the poller as sole BLE owner and the API purely reading a cache-of-record, there's
nothing left to contend over. The pre-redesign implementation is preserved at git tag
`pre-poll-loop-redesign`.

## Endpoints

All endpoints read from SQLite — no BLE call happens on request, so these respond in
single-digit milliseconds. Every response includes `age_seconds` and `stale` so callers
can tell "ring's fine, data's just a bit old" from "ring's been unreachable for a while"
(`stale: true` once a reading is older than `STALE_AFTER_SEC`, default 3x the poll interval).

| Endpoint | Description |
|---|---|
| `GET /latest` | Full latest reading (HR, SpO2, stress, HRV, steps, battery) |
| `GET /heartrate` | Heart rate only, same freshness fields |
| `GET /metrics` | Alias for `/latest` (kept for backwards compatibility) |
| `GET /health` | **Poller** liveness (last cycle time/success) — not ring connectivity |

## Setup

You need the colmi_r02_client.
To install and use colmi_r02_client on Linux, use {Link: pipx https://tahnok.github.io/colmi_r02_client/} to install the Python package directly from GitHub, allowing you to scan for and interact with the Colmi R02 smart ring via Bluetooth. 

Then go on with the install below:

```bash
# On Raspberry Pi:
cd /home/pi
git clone <repo> colmi-api
cd colmi-api

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — set COLMI_ADDRESS (find with: colmi_r02_client scan)

# Test
python app.py
curl http://localhost:8080/health
```

## Systemd Services

```bash
sudo cp colmi-poller.service colmi-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now colmi-poller
sudo systemctl enable --now colmi-api

# Check status
sudo systemctl status colmi-poller colmi-api
sudo journalctl -u colmi-poller -f
sudo journalctl -u colmi-api -f
```

## BOT Configuration

In BOT's `.env`:
```
COLMI_API_URL=http://<pi-ip>:8080
BIOMETRICS_ENABLED=true
```

Evelyn (or anything else) can poll `/latest` or `/heartrate` on whatever cadence it
wants — there's no BLE contention risk anymore since the API never touches the ring.
The old fast/slow adaptive poll-interval logic on Evelyn's side is no longer needed for
this reason; it can just read on a fixed interval and check `stale`.
