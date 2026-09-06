# Outstanding Balances Dashboard

Displays current outstanding balances across all leases, pulled live from
the Buildium API, with aging buckets (0-30 / 31-60 / 61-90 / 90+) and
notice/eviction flags. A small Flask server holds your Buildium API
credentials so they never touch the browser; the dashboard itself is a
single static page that calls that server.

## 1. Get Buildium API credentials

In Buildium: **Settings → Developer Tools → API** (this may be under a
different label depending on your plan — search for "API" in Buildium
settings if you don't see it). Generate a Client ID and Client Secret.
**Copy the secret immediately — Buildium only shows it once.**

## 2. Run it locally first

```bash
cd buildium-balances
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste in your real Client ID / Secret

export $(cat .env | xargs)        # or use a tool like python-dotenv/foreman
python app.py
