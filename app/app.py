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
  GET  /roadmap                 — Roadmap + Community Ideas (with voting/comments)
  POST /api/feedback/vote       — Vote on a roadmap/idea item
  POST /api/feedback/comment    — Comment on an item
  POST /api/feedback/idea       — Submit a new idea
  POST /api/feedback/promote    — Promote an idea to roadmap (admin only)
  POST /api/feedback/unlock     — Validate admin password
  GET  /my-profile              — View/edit own profile

Data storage (lightweight, JSON files in data/):
  acknowledgments.json — list of {timestamp, name, email, ip}
  profiles.json        — {email: {experience, position_size, frequency, goal}}
  audit.json           — all scoring requests for analytics
  feedback.json        — {roadmap_items: [...], ideas: [...]}

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

# Admin password for promoting ideas to roadmap. Override via env var in prod.
ADMIN_PASSWORD = os.environ.get("CIPHER_LENS_ADMIN_PW")

DATA_DIR = _LENS_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ACK_FILE = DATA_DIR / "acknowledgments.json"
PROFILE_FILE = DATA_DIR / "profiles.json"
AUDIT_FILE = DATA_DIR / "audit.json"
FEEDBACK_FILE = DATA_DIR / "feedback.json"

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


def _is_admin() -> bool:
    return bool(session.get("is_admin"))


# ============================================================
# Feedback data helpers
# ============================================================

def _load_feedback():
    """Load feedback data; seed with default roadmap items if file missing."""
    if not FEEDBACK_FILE.exists():
        default = {
            "roadmap_items": _default_roadmap_seed(),
            "ideas": [],
        }
        _write_json(FEEDBACK_FILE, default)
        return default
    data = _read_json(FEEDBACK_FILE, None)
    if not data or "roadmap_items" not in data or "ideas" not in data:
        data = {
            "roadmap_items": _default_roadmap_seed(),
            "ideas": [],
        }
        _write_json(FEEDBACK_FILE, data)
    return data


def _save_feedback(data):
    _write_json(FEEDBACK_FILE, data)


def _default_roadmap_seed():
    now = _now_iso()
    return [
        {
            "id": "rm-watchlists",
            "title": "Watchlists",
            "description": "Save the tickers you care about. Re-score on demand without re-pasting every time. Persistent across sessions and devices.",
            "status_label": "SOON",
            "created_at_utc": now,
            "votes": [],
            "comments": [],
        },
        {
            "id": "rm-byo-api",
            "title": "Bring your own API key",
            "description": "Currently everyone shares the same data source with shared rate limits. Bring your own Finnhub or Alpha Vantage key (both have free tiers) and run as many scans as you want.",
            "status_label": "SOON",
            "created_at_utc": now,
            "votes": [],
            "comments": [],
        },
        {
            "id": "rm-score-history",
            "title": "Score history",
            "description": "See how a stock's score has changed over the last few weeks. Was it green a month ago and now red? That's a story worth knowing before you act.",
            "status_label": "CONSIDERING",
            "created_at_utc": now,
            "votes": [],
            "comments": [],
        },
        {
            "id": "rm-discovery",
            "title": "Discovery mode",
            "description": "Instead of pasting a list you already have, describe what you're looking for (\"large-cap tech with a tailwind, no earnings in the next month\") and get a starting list to research.",
            "status_label": "CONSIDERING",
            "created_at_utc": now,
            "votes": [],
            "comments": [],
        },
        {
            "id": "rm-edu",
            "title": "Educational deep dives",
            "description": "Short writeups on what each scoring category really means, the kinds of mistakes new investors make, and how to think about the signals the framework checks. Built into the tool, not buried in a blog.",
            "status_label": "CONSIDERING",
            "created_at_utc": now,
            "votes": [],
            "comments": [],
        },
        {
            "id": "rm-mobile",
            "title": "Mobile-optimized layout",
            "description": "The current design is desktop-first. Works on mobile, but a layout designed for phones \u2014 quick scans during the day, compact cards, single-thumb navigation \u2014 is on the list.",
            "status_label": "CONSIDERING",
            "created_at_utc": now,
            "votes": [],
            "comments": [],
        },
    ]


def _find_item(data, item_id):
    """Search both roadmap and ideas for an item. Returns (list_name, index, item) or (None, None, None)."""
    for list_name in ("roadmap_items", "ideas"):
        for idx, item in enumerate(data[list_name]):
            if item.get("id") == item_id:
                return list_name, idx, item
    return None, None, None


def _enrich_items_for_view(items, user_email):
    """Add computed fields for the template: vote_count, has_voted, comment_count."""
    out = []
    for it in items:
        votes = it.get("votes", [])
        comments = it.get("comments", [])
        out.append({
            **it,
            "vote_count": len(votes),
            "has_voted": user_email in votes if user_email else False,
            "comment_count": len(comments),
        })
    return out


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
    data = _load_feedback()
    user_email = session.get("user_email")

    # Sort: votes desc, then created_at asc (older items first within same vote count)
    def _sort_key(it):
        return (-len(it.get("votes", [])), it.get("created_at_utc", ""))

    roadmap_items = sorted(data["roadmap_items"], key=_sort_key)
    ideas = sorted(data["ideas"], key=_sort_key)

    return render_template(
        "roadmap.html",
        acknowledged=_is_acknowledged(),
        is_admin=_is_admin(),
        roadmap_items=_enrich_items_for_view(roadmap_items, user_email),
        ideas=_enrich_items_for_view(ideas, user_email),
        user_name=session.get("user_name"),
        user_email=user_email,
    )


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
# Routes — Feedback / Roadmap API
# ============================================================

@app.route("/api/feedback/vote", methods=["POST"])
def api_feedback_vote():
    if not _is_acknowledged():
        return jsonify({"error": "Please acknowledge the terms first."}), 401

    body = request.get_json(silent=True) or {}
    item_id = (body.get("item_id") or "").strip()
    if not item_id:
        return jsonify({"error": "Missing item_id"}), 400

    data = _load_feedback()
    list_name, idx, item = _find_item(data, item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    email = session["user_email"]
    votes = item.setdefault("votes", [])

    # Toggle: if already voted, remove; else add
    if email in votes:
        votes.remove(email)
        action = "unvote"
    else:
        votes.append(email)
        action = "vote"

    _save_feedback(data)
    _save_audit(email, f"feedback_{action}", {"item_id": item_id, "list": list_name})

    return jsonify({
        "ok": True,
        "item_id": item_id,
        "vote_count": len(votes),
        "has_voted": email in votes,
    }), 200


@app.route("/api/feedback/comment", methods=["POST"])
def api_feedback_comment():
    if not _is_acknowledged():
        return jsonify({"error": "Please acknowledge the terms first."}), 401

    body = request.get_json(silent=True) or {}
    item_id = (body.get("item_id") or "").strip()
    text = (body.get("text") or "").strip()
    if not item_id or not text:
        return jsonify({"error": "Missing item_id or text"}), 400
    if len(text) > 1000:
        return jsonify({"error": "Comment too long (max 1000 characters)"}), 400

    data = _load_feedback()
    list_name, idx, item = _find_item(data, item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    email = session["user_email"]
    name = session.get("user_name", "Anonymous")
    comment = {
        "id": str(uuid.uuid4()),
        "author_name": name,
        "author_email": email,
        "text": text,
        "created_at_utc": _now_iso(),
    }
    item.setdefault("comments", []).append(comment)
    _save_feedback(data)
    _save_audit(email, "feedback_comment", {"item_id": item_id, "list": list_name})

    return jsonify({
        "ok": True,
        "item_id": item_id,
        "comment": comment,
        "comment_count": len(item["comments"]),
    }), 200


@app.route("/api/feedback/idea", methods=["POST"])
def api_feedback_idea():
    if not _is_acknowledged():
        return jsonify({"error": "Please acknowledge the terms first."}), 401

    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()

    if not title or len(title) < 3:
        return jsonify({"error": "Please give your idea a clear title (3+ characters)."}), 400
    if len(title) > 120:
        return jsonify({"error": "Title too long (max 120 characters)."}), 400
    if not description or len(description) < 10:
        return jsonify({"error": "Please describe your idea (10+ characters)."}), 400
    if len(description) > 2000:
        return jsonify({"error": "Description too long (max 2000 characters)."}), 400

    data = _load_feedback()
    email = session["user_email"]
    name = session.get("user_name", "Anonymous")

    new_idea = {
        "id": f"idea-{uuid.uuid4().hex[:8]}",
        "title": title,
        "description": description,
        "submitted_by_name": name,
        "submitted_by_email": email,
        "status_label": "NEW",
        "created_at_utc": _now_iso(),
        "votes": [email],  # auto-vote your own idea
        "comments": [],
    }
    data["ideas"].append(new_idea)
    _save_feedback(data)
    _save_audit(email, "feedback_new_idea", {"id": new_idea["id"], "title": title})

    return jsonify({"ok": True, "idea": new_idea}), 200


@app.route("/api/feedback/promote", methods=["POST"])
def api_feedback_promote():
    if not _is_acknowledged():
        return jsonify({"error": "Please acknowledge the terms first."}), 401
    if not _is_admin():
        return jsonify({"error": "Admin access required."}), 403

    body = request.get_json(silent=True) or {}
    item_id = (body.get("item_id") or "").strip()
    if not item_id:
        return jsonify({"error": "Missing item_id"}), 400

    data = _load_feedback()
    # Find in ideas
    for idx, item in enumerate(data["ideas"]):
        if item.get("id") == item_id:
            promoted = data["ideas"].pop(idx)
            promoted["status_label"] = "CONSIDERING"
            promoted["promoted_at_utc"] = _now_iso()
            data["roadmap_items"].append(promoted)
            _save_feedback(data)
            _save_audit(session["user_email"], "feedback_promote",
                        {"item_id": item_id, "title": promoted.get("title")})
            return jsonify({"ok": True, "item_id": item_id}), 200

    return jsonify({"error": "Idea not found"}), 404


@app.route("/api/feedback/unlock", methods=["POST"])
def api_feedback_unlock():
    if not _is_acknowledged():
        return jsonify({"error": "Please acknowledge the terms first."}), 401

    if not ADMIN_PASSWORD:
        log.error("Admin unlock attempted but CIPHER_LENS_ADMIN_PW env var is not set.")
        return jsonify({"error": "Admin feature not configured on this server."}), 503

    body = request.get_json(silent=True) or {}
    pw = (body.get("password") or "").strip()
    if pw == ADMIN_PASSWORD:
        session["is_admin"] = True
        _save_audit(session["user_email"], "admin_unlock", {})
        return jsonify({"ok": True, "is_admin": True}), 200
    return jsonify({"error": "Incorrect password"}), 403


@app.route("/api/feedback/lock", methods=["POST"])
def api_feedback_lock():
    session.pop("is_admin", None)
    return jsonify({"ok": True, "is_admin": False}), 200


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
