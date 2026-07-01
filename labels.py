"""Transparency label generation for Provenance Guard.

Maps an (attribution, confidence) result to one of three reader-facing label
variants. The exact text matches the "Transparency label design" section of
planning.md. The label is what a non-technical reader sees, so it always says
"likely" / "estimate" and never presents the verdict as certain.
"""

# The three variants, keyed by the attribution the scorer produced. Attribution
# is already confidence-gated (low confidence -> "uncertain"), so the label text
# genuinely changes with the confidence score.
_VARIANTS = {
    "ai": {
        "variant": "high_confidence_ai",
        "text": (
            "🤖 Likely AI-generated. Our system found strong signs this text was "
            "produced by AI. This is an automated estimate, not a certainty — if you "
            "created this yourself, you can appeal."
        ),
    },
    "human": {
        "variant": "high_confidence_human",
        "text": (
            "✍️ Likely written by a person. Our system found no strong signs of AI "
            "generation. This is an automated estimate and can occasionally be wrong."
        ),
    },
    "uncertain": {
        "variant": "uncertain",
        "text": (
            "❔ Couldn't determine the source. Our system wasn't able to confidently "
            "tell whether a person or AI wrote this, so we're not labeling it either "
            "way. Treat the origin as unknown."
        ),
    },
}


def _confidence_phrase(confidence):
    """Plain-language gloss so a non-technical reader understands the number."""
    if confidence >= 0.7:
        return "We're fairly confident in this estimate."
    if confidence >= 0.5:
        return "We're moderately confident in this estimate."
    return "We're not very confident — treat this cautiously."


def build_label(attribution, confidence):
    """Return the label dict for a given attribution + confidence score.

    {
      "variant": "high_confidence_ai" | "high_confidence_human" | "uncertain",
      "text": "<reader-facing sentence>",
      "confidence_pct": int,        # confidence as a percentage
      "confidence_phrase": "<plain-language gloss>",
    }
    """
    base = _VARIANTS.get(attribution, _VARIANTS["uncertain"])
    return {
        "variant": base["variant"],
        "text": base["text"],
        "confidence_pct": round(confidence * 100),
        "confidence_phrase": _confidence_phrase(confidence),
    }
