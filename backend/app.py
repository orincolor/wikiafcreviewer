"""WikiAfCReviewer backend.

POST /review {"title": "Draft:Example"}
  -> fetch the draft wikitext (politely), run the agentic review, return JSON.

The Anthropic API key lives only here (server-side), never in the browser. CORS is
restricted to the English Wikipedia origin so the companion userscript can call it.
The service is strictly read-only against the wikis — it never edits anything.
"""

import logging
import os

import anthropic
from flask import Flask, jsonify, request
from flask_cors import CORS

import reviewer
from polite_wiki import PoliteWiki

# Load a local .env into the environment if present (dev convenience). No-op in
# production, where the host sets env vars directly. Must run before reading env.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Origin the companion userscript runs from. Override via ALLOWED_ORIGIN if needed.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://en.wikipedia.org")

app = Flask(__name__)
# Restrict CORS to /review and the wiki origin; flask-cors answers the preflight.
CORS(app, resources={r"/review": {"origins": ALLOWED_ORIGIN}}, methods=["POST"])

wiki = PoliteWiki()
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment.


@app.post("/review")
def do_review():
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify({"error": "missing 'title'"}), 400

    wikitext = wiki.get_wikitext(title)
    if wikitext is None:
        return jsonify({"error": f"page not found: {title}"}), 404

    try:
        result = reviewer.review(title, wikitext, wiki, client=client)
    except anthropic.RateLimitError:
        return jsonify({"error": "rate limited, try again shortly"}), 503
    except anthropic.APIError as exc:
        log.exception("Anthropic API error")
        return jsonify({"error": f"review backend error: {exc.__class__.__name__}"}), 502
    except Exception:  # noqa: BLE001
        log.exception("review failed")
        return jsonify({"error": "review failed"}), 500

    return jsonify(result)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
