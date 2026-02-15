# Colmi Ring HTTP API

Lightweight HTTP server that wraps `colmi_r02_client` CLI calls for the Colmi R02/R06 smart rings. Provides endpoints for bot's biometric polling service.

## Endpoints

| Endpoint | Description | Response Time |
|---|---|---|
| `GET /heartrate` | Heart rate only | ~15s |
| `GET /metrics` | Full sensor suite (HR, SpO2, stress, HRV, steps, battery) | ~1-2min |
| `GET /health` | Ring connectivity check | ~5s |

## Setup

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

## Systemd Service

```bash
sudo cp colmi-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable colmi-api
sudo systemctl start colmi-api

# Check status
sudo systemctl status colmi-api
sudo journalctl -u colmi-api -f
```

## BOT Configuration

In BOT's `.env`:
```
COLMI_API_URL=http://<pi-ip>:8080
BIOMETRICS_ENABLED=true
```

## Caching

Responses are cached in-memory for 10 seconds to prevent concurrent BLE collisions. Multiple requests within the cache window return the same reading instantly.
