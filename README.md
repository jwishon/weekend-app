# weekend-app

Self-hosted weekly weekend planner for the Wishon household. Served at **weekend.riptide9.com** (LAN-only via Zoraxy → Coolify).

A Wednesday cron inside the container does fresh web research via Claude + NWS weather + pinned event sources, then renders a mobile-first card grid of what's worth doing that weekend. Thumbs voting trains household preferences over time.

**Atlas anchor:** `C:\Users\John\Documents\Atlas\Claude\Projects\Weekend\` — planning, prompts, seed data live there.

## Phases

- **Phase 1 (this scaffold):** static page + hand-authored `data/sample-week.json`, deploys to Coolify.
- **Phase 2:** thumbs voting + SQLite + preference summary endpoint.
- **Phase 3:** Wednesday cron, Claude API research, real weekly data.
- **Phase 4:** polish — calendar export, archive view, notifications.

Full build plan: `Atlas\Claude\Projects\Weekend\resources\build-plan.md`.

## Run it locally

```bash
docker compose up --build
# then visit http://localhost:8000
```

Or without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Deploy to Coolify

1. Push this repo to GitHub (see "Going live" below).
2. In Coolify, create a new Application → Source: this repo, Branch: `main`.
3. Build Pack: Dockerfile.
4. Domain: `weekend.riptide9.com`.
5. Confirm Zoraxy is routing `*.riptide9.com` to Coolify (already configured).

Phase 3 will need an Anthropic API key as a Coolify env var — not required yet.

## Going live (one-time setup)

```bash
# from this folder
cd C:\Users\John\Documents\GitHub\weekend-app    # after moving the scaffold here
git init
git add .
git commit -m "Phase 1: static scaffold with sample week"
# create the empty repo on github.com first
git remote add origin git@github.com:<you>/weekend-app.git
git branch -M main
git push -u origin main
```

## Stack

- FastAPI + Jinja2 templates
- Tailwind via CDN (no build step)
- Vanilla JS for thumbs (Phase 2 wires the POST)
- SQLite (Phase 2)
- Inter font from Google Fonts
- Lucide icons inline as SVG

## Layout

```
weekend-app/
├── app/
│   ├── main.py              # FastAPI app + routes
│   └── templates/
│       └── index.html       # mobile-first card grid
├── static/
│   └── styles.css
├── data/
│   └── sample-week.json     # Phase 1 demo content
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
