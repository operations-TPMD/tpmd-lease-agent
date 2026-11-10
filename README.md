# The Property Management Doctor — Lease Agent

AI-powered lease lead management system for TPMD.

## Features

- Automated lead scanning every hour (9am-7pm ET)
- AI-powered decision making (send SMS, move stage, or skip)
- Web dashboard for reviewing and approving actions
- Instant webhook response to inbound messages
- DRY RUN and LIVE modes

## Status: retired (no hosting)

This Python bot is no longer deployed. The Railway hosting was shut down — the live
leasing system (inbound/outbound calls, lead bot, reports, pipelines) runs entirely on
Base44 in `operations-TPMD/the-property-management-doctor`, which is where all new work goes.
The Railway deploy files (`Procfile`, `runtime.txt`) were removed so nothing can redeploy it.
Code is kept here for reference only.

## Local Development

```bash
python -m pip install -r requirements.txt
python dashboard.py
```

Open `http://localhost:8000`
