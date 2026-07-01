"""Confidence scoring / signal fusion for Provenance Guard.

Takes the two signal outputs and combines them into a single AI-likelihood
score (`p_ai`), a `confidence` value, and an `attribution` label, following the
formulas + thresholds in planning.md. Constants are named so they can be checked
directly against the spec.
"""

# --- Spec constants (must match planning.md "Uncertainty representation") ---
WEIGHT_STAT = 0.40            # statistical signal weight in the blend
WEIGHT_LLM = 0.60             # LLM judge weight (a bit higher; it's the stronger signal)
CONF_GATE = 0.35              # below this confidence -> always "uncertain"
AI_THRESHOLD = 0.62           # p_ai at/above this -> "ai" (stricter than midpoint)
HUMAN_THRESHOLD = 0.38        # p_ai at/below this -> "human"
SINGLE_SIGNAL_CEILING = 0.50  # if only one signal ran, confidence can't exceed this
CONFIDENCE_GAIN = 1.5         # p_ai rarely hits the 0/1 extremes, so scale up the
                              # distance term; otherwise even clear verdicts read low


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _length_factor(word_count):
    """Short text -> less trust. Full trust at ~50+ words, floored at 0.4."""
    return _clamp(word_count / 50.0, 0.4, 1.0)


def combine(stat_result, llm_result):
    """Fuse the two signals.

    Returns a dict:
        {
          "p_ai": float,           # combined probability the text is AI
          "confidence": float,     # how much to trust the verdict
          "attribution": str,      # "ai" | "human" | "uncertain"
          "agreement": float,      # 1 = signals agree, 0 = opposite
          "signals_used": [str],
          "degraded": bool,        # True if the LLM signal was unavailable
        }
    """
    p_stat = stat_result["score"]
    word_count = stat_result["word_count"]
    length_factor = _length_factor(word_count)

    llm_ok = llm_result.get("available") and llm_result.get("score") is not None

    if llm_ok:
        p_llm = llm_result["score"]
        p_ai = WEIGHT_STAT * p_stat + WEIGHT_LLM * p_llm
        agreement = 1.0 - abs(p_stat - p_llm)
        signals_used = ["statistical", "llm_judge"]
        degraded = False
        ceiling = 1.0
    else:
        # Graceful degradation: Signal 1 only. Never allow high confidence.
        p_ai = p_stat
        agreement = 1.0  # nothing to disagree with
        signals_used = ["statistical"]
        degraded = True
        ceiling = SINGLE_SIGNAL_CEILING

    # Confidence: how far the verdict is from the 50/50 line, scaled down when
    # the signals disagree and when the text is too short to trust.
    distance = _clamp(2 * abs(p_ai - 0.5) * CONFIDENCE_GAIN)  # 0 at p_ai=0.5, saturates near extremes
    agreement_factor = 0.5 + 0.5 * agreement       # soft: disagreement floors at 0.5x
    confidence = distance * length_factor * agreement_factor
    confidence = _clamp(confidence, 0.0, ceiling)

    # Short text can never be confidently labelled (planning.md edge case).
    if stat_result.get("short_text"):
        confidence = min(confidence, CONF_GATE - 0.05)

    # Attribution: confidence gate first, then the p_ai bands.
    if confidence < CONF_GATE:
        attribution = "uncertain"
    elif p_ai >= AI_THRESHOLD:
        attribution = "ai"
    elif p_ai <= HUMAN_THRESHOLD:
        attribution = "human"
    else:
        attribution = "uncertain"

    return {
        "p_ai": round(p_ai, 4),
        "confidence": round(confidence, 4),
        "attribution": attribution,
        "agreement": round(agreement, 4),
        "signals_used": signals_used,
        "degraded": degraded,
    }
