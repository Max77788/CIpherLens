# Cipher Lens v0.1

A stock-screening tool built for the Mastermind group. Single-page web app that
takes a list of tickers and returns a green/yellow/red rating with expandable
detail per ticker.

## What this is

- **Scoring framework**: multi-signal analysis (trend, momentum, volume, sector, fundamentals)
- **Earnings blackout**: tickers within 21 days of earnings get downgraded automatically
- **Acknowledgment gate**: every user must accept disclaimers before scoring
- **Profile capture**: optional 4-question profile after acknowledgment
- **Audit logging**: scoring requests are logged per user
- **Public pages**: Landing, Acknowledgment, Profile, Tool, How It Works, Roadmap

## What this is NOT

- Investment advice
- A licensed financial product
- Connected to brokerage accounts
- Production-ready (this is v0.1 — see "Hardening for Production" below)

## Directory layout

```
cipher_lens/
├── app/app.py              # Flask application (routes, sessions, scoring API)
├── engine/scoring.py       # Scoring engine — INTERNAL, do not expose source
├── templates/              # Jinja2 templates (Flask renders these)
├── static/styles.css       # Single stylesheet
├── data/                   # JSON storage (acknowledgments.json, profiles.json, audit.json)
└── requirements.txt
```

## Local development

```bash
cd cipher_lens
pip install -r requirements.txt
python -m app.app
# Or from app/ dir: python app.py
```

Open http://localhost:5060

## Deploying to Vercel

Vercel supports Python via serverless functions. Two options:

### Option A — Standard Flask app on a Python runtime

Use Vercel's `@vercel/python` runtime. Add `vercel.json`:

```json
{
  "version": 2,
  "builds": [
    { "src": "app/app.py", "use": "@vercel/python" }
  ],
  "routes": [
    { "src": "/(.*)", "dest": "app/app.py" }
  ],
  "env": {
    "CIPHER_LENS_SECRET": "@cipher-lens-secret"
  }
}
```

Vercel functions are stateless. **Two changes needed before Vercel works:**

1. **Replace JSON file storage** with a real database. Options:
   - Vercel KV (Redis-backed, simple key-value, free tier)
   - Vercel Postgres (proper DB, free tier)
   - Supabase / Neon / PlanetScale (external Postgres/MySQL)

   See `app/app.py` — replace the `_read_json` / `_write_json` calls in
   `_save_acknowledgment`, `_save_profile`, `_save_audit`, `_get_profile`.

2. **Session storage**: Flask's default session uses signed cookies — works on
   Vercel. No change needed unless you want server-side sessions.

### Option B — Deploy to a different host first

Vercel's Python is serverless and cold-start unfriendly for the scoring use case
(yfinance fetches take 5-30 seconds). For Mastermind launch, consider:

- **Fly.io** — runs the Flask app continuously, ~$5/month, no cold starts
- **Railway** — similar, easier deployment
- **Render** — free tier available

These hosts can use the JSON file storage as-is (filesystem persists).

## Hardening for production (when monetizing)

This is v0.1. Before charging money:

1. **Real authentication** — magic-link email signin or OAuth. Currently anyone with
   the same browser cookie can use the tool.
2. **Real database** — replace `data/*.json` with Postgres or similar
3. **Rate limiting** — currently anyone can spam the `/api/score` endpoint
4. **Caching layer** — cache yfinance responses (Redis, 15-30 min TTL) so multiple
   users querying the same ticker don't re-fetch
5. **API key support** — let users bring their own data API key (Finnhub free tier)
6. **Payment processing** — Stripe Checkout for tiered access
7. **Privacy policy + terms of service** — proper legal documents, not just the
   disclaimer in the acknowledgment flow
8. **Email confirmation** — verify the email address before granting access
9. **Logging + monitoring** — Sentry for errors, structured logs for analytics

## IP protection — what to NOT expose

The `engine/scoring.py` file contains the proprietary scoring rules.
The constants prefixed with `_CAT_A_` and `_CAT_B_` define the thresholds.
**Do not deploy the source publicly**. The compiled .pyc is fine; the source
should remain in private repos.

The output of the tool (ratings and category breakdowns) is designed to be
public-friendly without exposing thresholds. The "How It Works" page is
deliberately high-level.

## Environment variables

- `CIPHER_LENS_SECRET` — Flask session secret. Set to a long random string in production.

## Data files in `data/`

These accumulate over time:
- `acknowledgments.json` — list of every acknowledgment record (name, email, IP, timestamp)
- `profiles.json` — keyed by email, profile answers
- `audit.json` — list of every scoring request (timestamp, email, tickers, count)

For the Mastermind demo, these are the source of truth for "who's using it and how."

## Limitations and known issues

- **yfinance rate limits**: Yahoo Finance has unofficial rate limits. If 20 users hit
  the tool simultaneously, some will get errors. Caching (see "Hardening" above) fixes this.
- **No cold-start handling**: First request after a long idle period takes longer due to
  yfinance warm-up.
- **No mobile-optimized layout**: Works on mobile, but cards are designed desktop-first.
- **No tests**: No automated test suite. Manual smoke testing before each deploy.

## Contact

Built by [your name] for the Mastermind group. v0.1 launched [date].

Questions about the framework: see the "How It Works" page.
Bug reports: [your email].
