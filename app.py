"""Provenance Guard — Flask app.

Endpoints:
  POST /submit   -> run both detection signals, fuse into a confidence score,
                    return a transparency label, write a structured audit entry.
  POST /appeal   -> a creator contests a classification; status -> under_review,
                    the appeal is logged alongside the original decision.
  GET  /log      -> the structured audit log (newest first).
  GET  /content/<id> -> current status of one submission.
  GET  /health   -> liveness check.
"""

import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import audit_log
import labels
import scorer
from signals import llm_signal, statistical_signal

load_dotenv()  # loads .env (GROQ_API_KEY) for the Groq signal

app = Flask(__name__)

# Rate limiting (see README for the reasoning behind these numbers).
# 10/minute stops a script from flooding the endpoint; 100/day is a generous
# ceiling for a real writer submitting their own work.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# In-memory store of content so /appeal can look submissions up by content_id.
_content = {}


def _rebuild_content_from_log():
    """Reconstruct the content store from the persisted audit log on startup.

    The log survives restarts but the in-memory store doesn't, so without this a
    content_id from before a restart (e.g. the debug auto-reload) would 404 on
    appeal. We replay classification entries, then apply any appeals on top.
    """
    for entry in reversed(audit_log.get_log()):  # oldest first
        cid = entry.get("content_id")
        if not cid:
            continue
        if entry.get("type") == "appeal":
            if cid in _content:
                _content[cid]["status"] = "under_review"
                _content[cid]["appeal_filed"] = True
        else:
            _content[cid] = {
                "content_id": cid,
                "creator_id": entry.get("creator_id"),
                "text": None,  # raw text isn't persisted to the log
                "attribution": entry.get("attribution"),
                "confidence": entry.get("confidence"),
                "statistical_score": entry.get("statistical_score"),
                "llm_score": entry.get("llm_score"),
                "status": entry.get("status", "classified"),
                "appeal_filed": _content.get(cid, {}).get("appeal_filed", False),
            }


_rebuild_content_from_log()


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = data.get("creator_id")

    if not text:
        return jsonify({"error": "Field 'text' is required and cannot be empty."}), 400

    content_id = str(uuid.uuid4())

    # --- Run both signals ---
    stat = statistical_signal(text)   # Signal 1: statistical (local)
    llm = llm_signal(text)            # Signal 2: Groq LLM judge (may be unavailable)

    # --- Fuse into one confidence score + attribution ---
    result = scorer.combine(stat, llm)
    attribution = result["attribution"]
    confidence = result["confidence"]

    # --- Transparency label (one of three variants, varies by confidence) ---
    label = labels.build_label(attribution, confidence)

    # Save the content so /appeal can find it later.
    _content[content_id] = {
        "content_id": content_id,
        "creator_id": creator_id,
        "text": text,
        "title": data.get("title"),
        "attribution": attribution,
        "confidence": confidence,
        "statistical_score": stat["score"],
        "llm_score": llm["score"],
        "status": "classified",
        "appeal_filed": False,
    }

    # --- Structured audit entry: both signal scores + combined result ---
    audit_log.append(
        {
            "type": "classification",
            "content_id": content_id,
            "creator_id": creator_id,
            "timestamp": audit_log.now_iso(),
            "attribution": attribution,
            "confidence": confidence,
            "p_ai": result["p_ai"],
            "statistical_score": stat["score"],
            "llm_score": llm["score"],
            "agreement": result["agreement"],
            "signals_used": result["signals_used"],
            "degraded": result["degraded"],
            "label_variant": label["variant"],
            "signal_details": {"statistical": stat, "llm_judge": llm},
            "status": "classified",
        }
    )

    return jsonify(
        {
            "content_id": content_id,
            "attribution": attribution,
            "confidence": confidence,
            "p_ai": result["p_ai"],
            "label": label,
            "signals": {"statistical": stat, "llm_judge": llm},
            "agreement": result["agreement"],
            "degraded": result["degraded"],
            "status": "classified",
        }
    )


@app.post("/appeal")
@limiter.limit("20 per hour")
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    # Accept either field name; the assignment's example uses creator_reasoning.
    reasoning = (data.get("creator_reasoning") or data.get("reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "Field 'content_id' is required."}), 400
    if not reasoning:
        return jsonify({"error": "Field 'creator_reasoning' is required."}), 400

    content = _content.get(content_id)
    if content is None:
        return jsonify({"error": f"No content found with id '{content_id}'."}), 404

    # Update the content's status. Re-classification is intentionally manual.
    content["status"] = "under_review"
    content["appeal_filed"] = True

    appeal_id = str(uuid.uuid4())

    # Log the appeal alongside the original decision so a reviewer sees both.
    audit_log.append(
        {
            "type": "appeal",
            "appeal_id": appeal_id,
            "content_id": content_id,
            "creator_id": content.get("creator_id"),
            "timestamp": audit_log.now_iso(),
            "appeal_reasoning": reasoning,
            "original_attribution": content.get("attribution"),
            "original_confidence": content.get("confidence"),
            "original_statistical_score": content.get("statistical_score"),
            "original_llm_score": content.get("llm_score"),
            "status": "under_review",
        }
    )

    return jsonify(
        {
            "appeal_id": appeal_id,
            "content_id": content_id,
            "status": "under_review",
            "message": "Appeal received. This content is now under review by a human moderator.",
        }
    )


@app.get("/content/<content_id>")
def get_content(content_id):
    content = _content.get(content_id)
    if content is None:
        return jsonify({"error": f"No content found with id '{content_id}'."}), 404
    return jsonify(
        {
            "content_id": content_id,
            "attribution": content.get("attribution"),
            "confidence": content.get("confidence"),
            "status": content.get("status"),
            "appeal_filed": content.get("appeal_filed", False),
        }
    )


@app.get("/log")
def log():
    # In a real system this would be auth-protected; here it's for grading/visibility.
    return jsonify({"entries": audit_log.get_log()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
