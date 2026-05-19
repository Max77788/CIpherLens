"""
cipher_lens/app/app.py
======================
Cipher Lens — Flask app entry point.

Routes:
  GET  /                        — Landing page (intro + CTA)
  GET  /acknowledgment          — Disclaimer + sign-up form
  POST /acknowledgment          — Submit name/email + acknowledgment
  GET  /profile                 — Optional 4-question profile
  POST /profile                 — Save profile answers (or skip)
  GET  /tool                    — Main scoring tool (Mode A)
  POST /api/score               — Score a list of tickers
  GET  /how-it-works            — Public framework explanation
  GET  /my-profile              — View/edit own profile

Data storage (lightweight, JSON files in data/):
  acknowledgments.json — list of {timestamp, name, email, ip}
  profiles.json        — {email: {experience, position_size, frequency, goal}}
  audit.json           — all scoring requests for analytics

Session: cookie-based, just stores the user's email. No password.
Refresh-resilient — if cookie missing, redirected back to acknowledgment.

Deployment note for developer:
  - For Vercel: this Flask app works with vercel-python serverless functions
  - JSON file storage needs to be replaced with a real DB (Vercel KV, Postgres, etc.)
    before going to production. For Mastermind demo (low traffic) JSON is fine.
  - Add real auth (e.g., Auth0, magic links) when monetizing.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    session, abort, make_response
)

# Make engine importable
_LENS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_LENS_ROOT))
from engine.scoring import score_tickers, score_ticker  # noqa: E402

# ============================================================
# App setup
# ============================================================

app = Flask(
    __name__,
    template_folder=str(_LENS_ROOT / "templates"),
    static_folder=str(_LENS_ROOT / "static"),
)
app.config["SECRET_KEY"] = os.environ.get("CIPHER_LENS_SECRET", "dev-key-change-me-in-prod")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

DATA_DIR = _LENS_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ACK_FILE = DATA_DIR / "acknowledgments.json"
PROFILE_FILE = DATA_DIR / "profiles.json"
AUDIT_FILE = DATA_DIR / "audit.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cipher_lens")


# ============================================================
# Data helpers
# ============================================================

def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _now_iso():
    return datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S UTC")


def _save_acknowledgment(name: str, email: str, ip: str):
    acks = _read_json(ACK_FILE, [])
    record = {
        "id": str(uuid.uuid4()),
        "timestamp_utc": _now_iso(),
        "name": name,
        "email": email.lower(),
        "ip": ip,
    }
    acks.append(record)
    _write_json(ACK_FILE, acks)
    return record


def _save_profile(email: str, data: dict):
    profiles = _read_json(PROFILE_FILE, {})
    profiles[email.lower()] = {
        **data,
        "updated_at_utc": _now_iso(),
    }
    _write_json(PROFILE_FILE, profiles)


def _get_profile(email: str):
    profiles = _read_json(PROFILE_FILE, {})
    return profiles.get(email.lower())


def _save_audit(email: str, action: str, payload: dict):
    audit = _read_json(AUDIT_FILE, [])
    audit.append({
        "timestamp_utc": _now_iso(),
        "email": email,
        "action": action,
        "payload": payload,
    })
    _write_json(AUDIT_FILE, audit)


def _is_acknowledged() -> bool:
    return bool(session.get("user_email"))


# ============================================================
# Routes — Public pages
# ============================================================

@app.route("/")
def index():
    return render_template("index.html",
                           acknowledged=_is_acknowledged())


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html",
                           acknowledged=_is_acknowledged())


@app.route("/roadmap")
def roadmap():
    return render_template("roadmap.html",
                           acknowledged=_is_acknowledged())


# ============================================================
# Routes — Acknowledgment flow
# ============================================================

@app.route("/acknowledgment", methods=["GET", "POST"])
def acknowledgment():
    if request.method == "GET":
        return render_template("acknowledgment.html")

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    accepted = request.form.get("accepted")

    errors = []
    if not name or len(name) < 2:
        errors.append("Please enter your name.")
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        errors.append("Please enter a valid email address.")
    if not accepted:
        errors.append("You must accept the terms to continue.")

    if errors:
        return render_template("acknowledgment.html",
                               errors=errors,
                               name=name, email=email)

    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    _save_acknowledgment(name, email, ip)
    session.permanent = True
    session["user_email"] = email
    session["user_name"] = name
    return redirect(url_for("profile"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if not _is_acknowledged():
        return redirect(url_for("acknowledgment"))

    email = session["user_email"]

    if request.method == "GET":
        existing = _get_profile(email) or {}
        return render_template("profile.html",
                               profile=existing,
                               user_name=session.get("user_name"))

    # POST
    if request.form.get("skip"):
        return redirect(url_for("tool"))

    profile_data = {
        "experience": request.form.get("experience") or None,
        "position_size": request.form.get("position_size") or None,
        "frequency": request.form.get("frequency") or None,
        "goal": request.form.get("goal") or None,
    }
    _save_profile(email, profile_data)
    return redirect(url_for("tool"))


@app.route("/my-profile", methods=["GET", "POST"])
def my_profile():
    if not _is_acknowledged():
        return redirect(url_for("acknowledgment"))
    # Same as profile() but accessed from a "settings" link in the header
    return profile()


# ============================================================
# Routes — Main tool
# ============================================================

@app.route("/tool")
def tool():
    if not _is_acknowledged():
        return redirect(url_for("acknowledgment"))
    return render_template("tool.html",
                           user_name=session.get("user_name"),
                           user_email=session.get("user_email"))


@app.route("/api/score", methods=["POST"])
def api_score():
    if not _is_acknowledged():
        return jsonify({"error": "Please acknowledge the terms first."}), 401

    body = request.get_json(silent=True) or {}
    raw_tickers = body.get("tickers", "")
    if isinstance(raw_tickers, str):
        # Allow comma, space, or newline separation
        parts = []
        for token in raw_tickers.replace(",", " ").replace("\n", " ").split():
            tok = token.strip().upper()
            if tok and tok.replace(".", "").replace("-", "").isalnum() and len(tok) <= 8:
                parts.append(tok)
        tickers = list(dict.fromkeys(parts))  # dedupe, preserve order
    elif isinstance(raw_tickers, list):
        tickers = [t.strip().upper() for t in raw_tickers if t.strip()]
    else:
        return jsonify({"error": "Invalid tickers format"}), 400

    if not tickers:
        return jsonify({"error": "No valid tickers provided"}), 400

    if len(tickers) > 20:
        return jsonify({"error": "Please limit to 20 tickers per scan"}), 400

    log.info(f"Scoring {len(tickers)} tickers for {session['user_email']}: {tickers}")

    results = score_tickers(tickers)

    # Convert to dicts for JSON serialization
    from dataclasses import asdict
    results_out = []
    for r in results:
        d = asdict(r)
        # Categories also dataclasses
        d["categories"] = [asdict(c) if hasattr(c, '__dataclass_fields__') else c
                           for c in (r.categories or [])]
        results_out.append(d)

    # Sort: tier (green → yellow → red → insufficient_data), then signals passed desc, then ticker
    _RATING_ORDER = {"green": 0, "yellow": 1, "red": 2, "insufficient_data": 3}

    def _sort_key(d):
        tier = _RATING_ORDER.get(d.get("rating"), 4)
        signals_passed = sum(c.get("passed", 0) for c in d.get("categories", []))
        return (tier, -signals_passed, d.get("ticker", ""))

    results_out.sort(key=_sort_key)

    # Audit log
    _save_audit(session["user_email"], "score", {
        "tickers": tickers,
        "result_count": len(results_out),
    })

    return jsonify({
        "ok": True,
        "results": results_out,
        "scored_at_utc": _now_iso(),
    }), 200


# ============================================================
# Misc
# ============================================================

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "cipher-lens", "time_utc": _now_iso()}), 200


if __name__ == "__main__":
    print("=" * 60)
    print("Cipher Lens v0.1")
    print(f"  Data dir: {DATA_DIR}")
    print(f"  Open: http://localhost:5000")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5000, debug=False)
