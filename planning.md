# Provenance Guard — planning.md

My planning doc. The goal here is to figure everything out *before* I write code: my two detection
signals, how I turn them into one confidence score, the exact label text, how appeals work, and the
edge cases I know will trip me up. The last two sections (Architecture + AI Tool Plan) are the stuff
I'll actually feed to the AI tool when I generate code in Milestones 3–5.

---

## 1. Detection signals

I'm using two signals that are different in *kind* — one is plain math on the text, one is a model's
opinion. That matters because they're wrong about different things, so together they cover for each
other.

### Signal 1 — Statistics (burstiness + lexical repetition)

- **Measures:** how much my sentence lengths vary, and how repetitive the vocabulary is.
- **How I actually compute it:**
  - Split into sentences, get the word count of each.
  - **Burstiness** = coefficient of variation of sentence lengths (`stdev / mean`). High = bursty =
    human-ish. I normalize it so low variation → closer to 1 (AI-like).
  - **Lexical repetition** = type-token ratio (`unique_words / total_words`). Lower diversity →
    more AI-like.
  - Combine the two into one number `p_stat` between 0 and 1, where 1 = looks AI.
- **Output:** a score in **[0, 1]** (probability-ish that it's AI), plus the raw numbers so I can
  show my work in the audit log.

### Signal 2 — LLM judge (Groq)

- **Measures:** the model's overall read on style — clichés ("it's important to note"), over-hedging,
  too-tidy structure, generic transitions.
- **How I compute it:** send the text to Groq with a rubric prompt, ask it to return a probability
  it's AI plus a one-line reason. I parse that into a number.
- **Output:** a score in **[0, 1]** (`p_llm`) plus a short rationale string.

### Combining them into one confidence score

Both signals give me a 0–1 "probability it's AI." Here's the math (final values after M4
calibration — these match the constants in `scorer.py`):

```
p_ai       = 0.40 * p_stat + 0.60 * p_llm          # weighted blend (LLM is the stronger signal)
agreement  = 1 - abs(p_stat - p_llm)               # 1.0 = signals agree, 0.0 = totally split
distance   = clamp(2 * abs(p_ai - 0.5) * 1.5)      # 0 at the 50/50 line, saturates near extremes
length_factor   = clamp(word_count / 50, 0.4, 1.0) # short text -> less trust
agreement_factor = 0.5 + 0.5 * agreement           # soft: disagreement only halves, never zeroes
confidence = distance * length_factor * agreement_factor   # then capped
```

- I weight the LLM higher (0.60) because in calibration it reliably caught AI tells the statistics
  missed — but I never let it run alone. If Groq fails I fall back to `p_stat` only and cap
  confidence at 0.50 (one signal can never be "high confidence").
- `agreement` pulls confidence down when the two signals disagree (disagreement = I should be
  unsure). I softened it to `0.5 + 0.5*agreement` after calibration — a hard multiply zeroed out
  too many verdicts.
- `length_factor` shrinks for short text, so a 40-word poem can't score 0.95 confidence.

---

## 2. Uncertainty representation

- **What confidence actually means:** it's how sure I am about the *label I picked*, not the
  probability of AI. `confidence = 0.6` means I'm leaning a direction but I'm close to the boundary —
  the signals only partly agree or the text is on the fence. It should produce a noticeably softer
  label than 0.95.
- **Mapping raw outputs → calibrated score:** raw signal numbers tend to bunch up, so I (a) blend
  them, (b) scale by agreement, and (c) cap by length. I'll **test calibration in M4** by running a
  set of clearly-AI and clearly-human samples and checking the scores actually spread out instead of
  all landing near 0.5 or all near 1.0.
- **Thresholds (on `p_ai`, gated by confidence) — final M4 values:**

  | Condition                                  | Result      |
  |--------------------------------------------|-------------|
  | `confidence < 0.35`                        | `uncertain` |
  | `confidence ≥ 0.35` and `p_ai ≥ 0.62`      | `ai`        |
  | `confidence ≥ 0.35` and `p_ai ≤ 0.38`      | `human`     |
  | anything left in the middle (0.38–0.62)    | `uncertain` |

  Two things to note: it's **not a binary flip at 0.5** — there's a whole uncertain band in the
  middle and a confidence gate on top. And the bar for calling something **AI is stricter** (0.62)
  than the symmetric midpoint, on purpose, because falsely accusing a real creator is the worse error.

- **What I found when I calibrated (4 milestone inputs + 2 strong-agreement cases):** clearly-human
  text scored `p_ai≈0.12, confidence≈0.74 → human`; a strongly-uniform AI text scored
  `p_ai≈0.76 → ai`; but the milestone's "clearly AI" sample and the "formal human" sample **both**
  landed `p_ai≈0.65, confidence≈0.20 → uncertain`. That's not a miscalibration — the statistical
  signal disagrees with the LLM on both, so the system won't confidently tell polished AI from
  polished formal human writing. The same caution that makes "clearly AI" land uncertain is what
  stops the formal-human paragraph from becoming a false AI accusation. The system is confident when
  it says **human** (both signals agree low) and deliberately cautious when leaning **AI**.

---

## 3. Transparency label design

Three variants, written out exactly. Each maps to one `label.variant` value.

**High-confidence AI** (`high_confidence_ai`) — shown when result is `ai` and confidence is high:
> 🤖 **Likely AI-generated.** Our system found strong signs this text was produced by AI. This is an
> automated estimate, not a certainty — if you created this yourself, you can appeal.

**High-confidence human** (`high_confidence_human`) — shown when result is `human` and confidence is high:
> ✍️ **Likely written by a person.** Our system found no strong signs of AI generation. This is an
> automated estimate and can occasionally be wrong.

**Uncertain** (`uncertain`) — shown whenever the result is `uncertain` (or confidence is low):
> ❔ **Couldn't determine the source.** Our system wasn't able to confidently tell whether a person or
> AI wrote this, so we're not labeling it either way. Treat the origin as unknown.

The point of three variants instead of two: the uncertain one never accuses anyone, and even the
confident ones say "likely" + "automated estimate" + mention the appeal, so a non-technical reader
understands it's a guess, not a verdict.

---

## 4. Appeals workflow

- **Who can appeal:** the creator of the content (anyone who has the `content_id` from their
  submission). No login system in this project, so the `content_id` is the key.
- **What they provide:** the `content_id` and their `reasoning` (free text — e.g. "this is my
  original poem, I have drafts").
- **What the system does when an appeal comes in:**
  1. Look up the original decision by `content_id` (reject if it doesn't exist).
  2. Change that content's **status from `classified` → `under_review`**.
  3. Append an **appeal record** to the audit log, linked to the original decision (so the original
     score/signals stay visible right next to the complaint).
  4. Return a confirmation with an `appeal_id`.
  - No automatic re-classification — a human decides.
- **What a human reviewer sees in the queue:** for each appeal, the original text, the attribution +
  confidence, both signal scores and the LLM's rationale, the timestamp, and the creator's reasoning
  — basically everything needed to agree or overturn, all in one record from `GET /log`.

---

## 5. Anticipated edge cases (where I'll do badly)

1. **Repetitive, simple poem flagged as AI.** A real poem with short even lines and plain vocabulary
   hits Signal 1's blind spot (low burstiness, low lexical diversity → looks AI), and the Groq judge
   is also biased against polished verse. Both fail the same way at once. My defense is the length
   cap + the strict AI threshold pushing it to `uncertain` instead of a confident false accusation.
2. **Very short text (a tweet, a two-line bio).** Not enough sentences to measure burstiness and not
   enough tokens for the LLM to judge well. I flag short input at intake and cap confidence so these
   land in `uncertain` rather than getting a confident label off basically no evidence.
3. **Mixed authorship (human draft polished by AI, or AI draft heavily edited by a human).** There's
   no single true answer, but my system only outputs one label. Realistically this lands somewhere in
   the middle and should read as `uncertain` — which is honest, but worth calling out as a known gap.
4. **Non-native or deliberately plain writing (technical docs, ESL writers).** Flat, even, simple
   style looks AI-like to Signal 1 even though it's fully human. This is exactly the false-positive
   case the appeals flow exists to catch.

---

## Architecture

The diagram I drew in Milestone 1, plus a short description of the two flows.

### Submission flow

```
          text                text                  stat score
Client ─────────▶ [Rate Limiter] ──▶ [Validate] ──┬──▶ [Signal 1: stats] ──┐
  ▲                 │ (429 if over)    │ makes id   │                        │ score 1
  │                 ▼                  │ short flag └──▶ [Signal 2: Groq] ────┤ score 2
  │           blocked                  │                                     ▼
  │                                    │                        [Confidence scorer]
  │                                    │                            │ result + confidence
  │                                    │                            ▼
  │                                    │                      [Label builder]
  │                                    │                            │ label text
  │                                    │                            ▼
  │  response {id, result,             │                       [Audit log] ◀── save decision
  └─ confidence, label, signals} ◀─────┴────────────────────────────┘
```

### Appeal flow

```
   {content_id, reasoning}        look it up         set status = under_review
Client ───────────────────▶ [Appeal handler] ──▶ [Content store] ──▶ [Audit log] ── save appeal
  ▲                                 │                                      │  (linked to original)
  │                                 ▼                                      ▼
  └──── {content_id, status: "under_review", appeal_id} ◀─────────────────┘
```

**Narrative:** On submission, text passes the rate limiter, gets validated and given an id, runs
through both signals, gets fused into a single score + confidence, gets a label, and the whole
decision is written to the audit log before the response goes back. On appeal, the creator sends
their `content_id` and reasoning; the system flips that content's status to `under_review`, logs the
appeal next to the original decision, and confirms — a human reviews later, nothing is re-scored
automatically.

---

## AI Tool Plan

How I'll use the AI tool in each implementation milestone — which parts of this doc I'll paste in,
what I'll ask for, and how I'll check it.

### M3 — Submission endpoint + first signal

- **Spec I'll provide:** the Detection Signals section (esp. Signal 1's compute steps) + the
  Architecture diagram + the `/submit` request/response shape.
- **Ask it to generate:** a Flask app skeleton with `POST /submit` and `GET /health`, plus the
  Signal 1 (statistics) function that returns `p_stat` and the raw burstiness/repetition numbers.
- **How I verify:** call the Signal 1 function directly on a few hand-picked inputs (an obvious AI
  paragraph, a bursty human paragraph, a one-liner) and confirm the scores move in the right
  direction *before* I wire it into the endpoint. Then hit `/submit` with curl/Postman.

### M4 — Second signal + confidence scoring

- **Spec I'll provide:** Detection Signals (Signal 2) + the whole Uncertainty Representation section
  (formulas + thresholds) + the diagram.
- **Ask it to generate:** the Signal 2 Groq judge function (with the rubric prompt + graceful
  fallback if the API fails) and the scoring/fusion function that turns `p_stat` + `p_llm` into
  `p_ai`, `confidence`, and the attribution band.
- **How I check:** run a small labeled set of clearly-AI vs clearly-human texts and confirm the
  scores actually **spread out** and produce different attributions — not everything stuck near 0.5,
  and not a hard flip at 0.5. Also test the Groq-fails path falls back to Signal 1 with low
  confidence.

### M5 — Production layer (labels + appeals)

- **Spec I'll provide:** the Transparency Label section (all three exact variants) + the Appeals
  Workflow section + the diagram.
- **Ask it to generate:** the label-builder function (maps result + confidence → one of the three
  variants) and the `POST /appeal` endpoint + `GET /log`, including the status change and appeal
  logging.
- **How I verify:** craft inputs that reach **all three** label variants (confident AI, confident
  human, uncertain), and submit→appeal one piece end to end, confirming its status becomes
  `under_review` and the appeal shows up in `GET /log` linked to the original decision.

---

## Still to lock in during M2/M3 (notes to self)

- Exact rate-limit numbers + reasoning (goes in README).
- Final tuning of the fusion weights and thresholds after I see real calibration numbers in M4.
- I reviewed the three label variants above and I'm happy with them — they all say "likely" +
  "estimate" and only the AI/human ones make a claim, the uncertain one stays neutral.
```
