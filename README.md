# Aiona Voice — Production AI Outbound Calling Platform

Production-grade AI voice calling SaaS built on **LiveKit Agents 1.x + Gemini Live**.

## Stack
- **LiveKit Agents 1.x** — voice orchestration
- **Google Gemini Live** (`gemini-2.0-flash-live-001`) — sub-100ms real-time voice AI
- **Vobiz SIP trunk** — outbound telephony dial-out
- **FastAPI + Uvicorn** — REST API + dashboard server
- **Supabase (PostgreSQL)** — all persistence, zero local SQLite
- **APScheduler** — campaign cron scheduling (once / daily / weekdays)
- **Chart.js CDN** — analytics charts
- **Vanilla HTML/CSS/JS** — single-file dashboard, no build step
- **Docker + Coolify** — deployment

## Quick Start

### 1. Setup
```bash
cp .env.example .env
# Fill in all values in .env
```

### 2. Database
Run `schema.sql` in your **Supabase SQL editor** to create all tables.

### 3. Run locally (dev)
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — API + Dashboard
python api.py

# Terminal 2 — LiveKit Agent Worker
python agent.py start
```

Open http://localhost:8000 for the dashboard.

### 4. Docker (production)
```bash
docker compose up -d
```

### 5. Coolify deployment
- Create a new **Docker Compose** service in Coolify
- Point to this repo, use `docker-compose.yml`
- Set all `.env` variables in Coolify's env editor

## Files
| File | Purpose |
|------|---------|
| `agent.py` | LiveKit agent worker — Gemini Live, Vobiz SIP dial, tools |
| `api.py` | FastAPI REST API + serves `dashboard.html` |
| `scheduler.py` | APScheduler campaign engine |
| `db.py` | Supabase helper functions |
| `dashboard.html` | Single-file full-stack SPA dashboard |
| `schema.sql` | Supabase database schema |
| `Dockerfile` | Container build |
| `docker-compose.yml` | Orchestrates api + agent services |

## Dashboard Sections
1. **Overview** — live stats + charts
2. **Single Call** — dial one number immediately
3. **Batch CSV** — upload CSV, call everyone
4. **Campaigns** — scheduled mass campaigns (once/daily/weekdays)
5. **Call Logs** — history with recording links
6. **CRM** — contacts with AI memory
7. **Appointments** — AI-booked meetings (+ Cal.com sync)
8. **AI Agents** — named agent profiles (voice/model/prompt)
9. **Settings** — BYOK API keys stored in Supabase
10. **Live Logs** — SSE real-time log stream
11. **Charts** — call analytics
