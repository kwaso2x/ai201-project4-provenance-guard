# Provenance Guard

A small content-attribution service. You send it a piece of text (a poem, a story, a blog post),
and it estimates whether a **human** or an **AI** wrote it — but instead of pretending to be certain,
it gives a **confidence score**, shows a plain-English **transparency label**, logs every decision to
an **audit log**, and lets creators **appeal** a classification they think is wrong.

The whole design is built around one idea: I can never actually be 100% sure who wrote something, so
the system shouldn't act like it is. That's why there's a confidence score, an "uncertain" label, and
an appeals path — see [planning.md](planning.md) for the full design write-up.

---

## How to run it

```powershell
# 1. Create the virtual environment (use standard Python 3.13, NOT the free-threaded 3.13t build)
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your Groq API key to a .env file in the project root
#    GROQ_API_KEY=your_key_here

# 4. Run the app
python app.py
```

The server runs at `http://127.0.0.1:5000`. Test it from a **second** terminal (the first one is busy
running the server). Signal 2 needs a valid `GROQ_API_KEY`; if the key is missing or the API fails,
the system **degrades gracefully** to Signal 1 alone with a capped confidence (`degraded: true`).

### API endpoints

| Method & path         | Body                                             | Returns                                                        |
|-----------------------|--------------------------------------------------|----------------------------------------------------------------|
| `POST /submit`        | `{ "text", "creator_id"?, "title"? }`            | attribution, confidence, label, both signal scores             |
| `POST /appeal`        | `{ "content_id", "creator_reasoning" }`          | appeal_id, status `under_review`                               |
| `GET /log`            | —                                                | the full structured audit log (newest first)                   |
| `GET /content/<id>`   | —                                                | current status of one submission                               |
| `GET /health`         | —                                                | `{ "status": "ok" }`                                           |

---

## Architecture

```
Submission flow:
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

Appeal flow:
   {content_id, reasoning}        look it up         set status = under_review
Client ───────────────────▶ [Appeal handler] ──▶ [Content store] ──▶ [Audit log] ── save appeal
```

On **submission**, text passes the rate limiter, gets validated and given an id, runs through both
signals, gets fused into a single score + confidence, gets a label, and the whole decision is written
to the audit log before the response goes back. On **appeal**, the creator sends their `content_id`
and reasoning; the system flips that content's status to `under_review`, logs the appeal next to the
original decision, and confirms — a human reviews later, nothing is re-scored automatically.

Code map: [app.py](app.py) (endpoints), [signals.py](signals.py) (both signals),
[scorer.py](scorer.py) (fusion), [labels.py](labels.py) (transparency labels),
[audit_log.py](audit_log.py) (structured log).

---

## Detection signals (multi-signal pipeline)

I use **two signals that are different in kind** — one is plain math over the tokens, one is a
model's opinion. That matters because they have different blind spots, so they cover for each other.

### Signal 1 — Statistics (burstiness + lexical repetition)

- **What it measures:** *burstiness* = how much sentence lengths vary (coefficient of variation), and
  *lexical repetition* = the type-token ratio (unique words ÷ total words).
- **Why it separates human from AI:** humans write unevenly — a short punchy sentence next to a long
  rambling one — and reuse phrases unevenly. Autoregressive models tend to produce **uniform,
  medium-length sentences** and smoother vocabulary. Low variation + smooth repetition reads AI-like.
- **Blind spot:** it doesn't understand *meaning*. Plain, even human writing (technical docs, ESL
  writers) looks AI to it, and it's unreliable on short text or line-structured poems.

### Signal 2 — LLM judge (Groq)

- **What it measures:** a Groq model's holistic read on style — clichés ("it is important to note"),
  generic transitions, over-tidy structure — returned as a probability the text is AI.
- **Why it separates human from AI:** the model has seen huge amounts of both kinds of text and
  recognizes the *gestalt* of AI writing, which pure statistics miss.
- **Blind spot:** it can be **confidently wrong**, is biased toward calling polished human writing
  "AI," is sensitive to prompt wording, and needs the network (so it can fail).

**Why both:** Signal 1 is cheap and explainable but semantically blind; Signal 2 is semantically rich
but opaque. When they **agree**, I have two independent lines of evidence. When they **disagree**,
that disagreement itself tells me to be less confident — which feeds directly into the score.

---

## Confidence scoring & uncertainty

Both signals output a probability-of-AI in `[0,1]`. They're fused like this (constants live in
[scorer.py](scorer.py)):

```
p_ai       = 0.40 * p_stat + 0.60 * p_llm          # LLM weighted higher (stronger signal)
agreement  = 1 - abs(p_stat - p_llm)
distance   = clamp(2 * abs(p_ai - 0.5) * 1.5)      # how far from the 50/50 line
length_factor    = clamp(word_count / 50, 0.4, 1.0)
agreement_factor = 0.5 + 0.5 * agreement
confidence = distance * length_factor * agreement_factor
```

- **What a confidence of 0.6 means:** it's how sure the system is *about the label it picked*, not the
  probability of AI. ~0.6 means "leaning a direction but close to the boundary." It produces a
  meaningfully softer label than 0.95 — see the label variants below.
- **Not a binary flip at 0.5.** There's a whole uncertain band in the middle **plus** a confidence
  gate on top:

  | Condition                              | Result      |
  |----------------------------------------|-------------|
  | `confidence < 0.35`                    | `uncertain` |
  | `confidence ≥ 0.35` and `p_ai ≥ 0.62`  | `ai`        |
  | `confidence ≥ 0.35` and `p_ai ≤ 0.38`  | `human`     |
  | middle (`p_ai` 0.38–0.62)              | `uncertain` |

  The AI threshold (0.62) is **stricter** than the midpoint on purpose: falsely accusing a real
  creator is the worse error, so I make the system work harder to say "AI."
- **Graceful degradation:** if the Groq signal is unavailable, the score falls back to Signal 1 alone
  and confidence is capped at 0.50 — one signal can never produce a "high confidence" verdict.

### How I tested that the scores are meaningful

I ran 6 deliberately-chosen inputs (4 from the assignment + 2 strong-agreement cases) and checked the
scores actually spread out and matched intuition:

| Input                                   | stat | llm  | p_ai | confidence | attribution |
|-----------------------------------------|-----:|-----:|-----:|-----------:|-------------|
| Clearly human (casual ramen review)     | 0.15 | 0.10 | 0.12 | **0.97**   | human       |
| Strong-agreement AI (uniform/repetitive)| 0.54 | 0.90 | 0.76 | **0.54**   | ai          |
| Clearly AI (assignment sample)          | 0.32 | 0.90 | 0.66 | **0.20**   | uncertain   |
| Formal human (monetary policy)          | 0.41 | 0.80 | 0.64 | **0.20**   | uncertain   |
| Lightly-edited AI (remote work)         | 0.33 | 0.20 | 0.25 | **0.56**   | human       |

The most interesting finding: the "clearly AI" sample and the "formal human" sample land **almost
identically** (`uncertain`, p_ai ≈ 0.65, confidence ≈ 0.20). That's not a bug — the cheap statistical
signal disagrees with the LLM on both, so the system refuses to confidently tell polished AI from
polished formal human writing. The same caution that makes obvious-AI land `uncertain` is exactly what
stops the human economist's paragraph from becoming a false AI accusation. The system is **confident
when it says human** (both signals agree low) and **deliberately cautious when leaning AI**.

### Two worked examples (high vs low confidence)

These are real responses and show the score is a genuine variable, not a constant.

**High-confidence case** — casual, bursty human writing (the ramen review):
```
input : "ok so i finally tried that new ramen place downtown and honestly? underwhelming..."
stat_score = 0.15   llm_score = 0.10   agreement = 0.95
p_ai = 0.12   ->   attribution = human   confidence = 0.97
label: "✍️ Likely written by a person..."  (We're fairly confident in this estimate.)
```
Both signals independently agree it's human and the text is long enough to trust, so agreement is
high, `p_ai` is far from 0.5, and confidence lands at **0.97**.

**Lower-confidence case** — the assignment's "clearly AI" paragraph:
```
input : "Artificial intelligence represents a transformative paradigm shift in modern society..."
stat_score = 0.32   llm_score = 0.90   agreement = 0.42
p_ai = 0.66   ->   attribution = uncertain   confidence = 0.20
label: "❔ Couldn't determine the source..."  (We're not very confident — treat this cautiously.)
```
Here the LLM is sure it's AI but the statistical signal disagrees (the paragraph has decent sentence
variation). Because the two signals split, confidence collapses to **0.20** and the system honestly
says "uncertain" rather than making a shaky call. Same label machinery, very different number — a
0.97 and a 0.20 produce two different variants, exactly as intended.

### Why this scoring approach, and what I'd change for a real deployment

I chose a **weighted blend gated by agreement and length** (rather than, say, just trusting the LLM)
because the whole point of two signals is that their *disagreement* is information. A single-signal
system would confidently mislabel the cases where its one blind spot is triggered; making
disagreement lower the confidence is what buys the false-positive protection.

If I were deploying this for real I'd change three things: (1) replace the hand-tuned statistical
heuristic with a proper **perplexity/burstiness model** and **calibrate the thresholds on a labeled
dataset** instead of eyeballing 6 examples; (2) the confidence weights are currently intuition-tuned —
I'd fit them against ground-truth so the number is a real calibrated probability; (3) I'd add a second
LLM judge (or the same one at a different temperature) and treat the *variance* of their answers as an
extra uncertainty signal, since a single LLM call is a single point of failure.

---

## Transparency label (three variants)

The label the reader sees changes with the confidence result. Every variant says "likely" +
"estimate" so a non-technical reader understands it's a guess, and each carries a confidence
percentage + a plain-language phrase. Exact text ([labels.py](labels.py)):

**High-confidence AI** (`high_confidence_ai`, shown when attribution = `ai`):
> 🤖 Likely AI-generated. Our system found strong signs this text was produced by AI. This is an
> automated estimate, not a certainty — if you created this yourself, you can appeal.

**High-confidence human** (`high_confidence_human`, shown when attribution = `human`):
> ✍️ Likely written by a person. Our system found no strong signs of AI generation. This is an
> automated estimate and can occasionally be wrong.

**Uncertain** (`uncertain`, shown when attribution = `uncertain`):
> ❔ Couldn't determine the source. Our system wasn't able to confidently tell whether a person or AI
> wrote this, so we're not labeling it either way. Treat the origin as unknown.

Each label also includes `confidence_pct` (e.g. `54`) and a `confidence_phrase`, which is one of:
"We're fairly confident in this estimate." (≥0.7) / "We're moderately confident in this estimate."
(≥0.5) / "We're not very confident — treat this cautiously." (<0.5).

---

## Appeals workflow

- **Who can appeal:** the creator (anyone with the `content_id` from their submission — there's no
  login system in this project, so the id is the key).
- **What they provide:** `content_id` + `creator_reasoning` (free text).
- **What the system does:** looks up the original decision, changes that content's status from
  `classified` → `under_review`, appends an **appeal record** to the audit log linked to the original
  decision (so the original scores stay visible next to the complaint), and returns an `appeal_id`.
  Re-classification is **not** automated — a human reviews.
- **What a reviewer sees:** from `GET /log`, the appeal record includes the creator's reasoning plus
  the original attribution, confidence, and both signal scores — everything needed to uphold or
  overturn.

Example:
```powershell
$body = @{ content_id = "PASTE-ID"; creator_reasoning = "I wrote this myself..." } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:5000/appeal -Method Post -ContentType "application/json" -Body $body
# -> { "appeal_id": "...", "content_id": "...", "status": "under_review", "message": "Appeal received..." }
```

---

## Rate limiting

Applied to `POST /submit` with Flask-Limiter (in-memory storage): **`10 per minute; 100 per day`** per
IP address. `POST /appeal` has a separate `20 per hour` limit.

**Reasoning:** a real writer checking their own work submits a handful of pieces and occasionally
re-checks an edit — 10/minute is far more than a human needs but low enough to stop a script from
flooding the endpoint (each submit triggers a paid Groq API call, so abuse has a real cost). The
100/day ceiling caps sustained automated hammering that stays under the per-minute limit. Appeals are
rarer human actions, so 20/hour is plenty while still bounding abuse.

**Evidence** — 12 rapid requests to `/submit` (first 10 pass, then the limit trips):
```
1->200  2->200  3->200  4->200  5->200  6->200  7->200  8->200  9->200  10->200  11->429  12->429
```

---

## Audit log

Every decision and every appeal is written to [audit_log.jsonl](audit_log.jsonl) as structured JSON
(one object per line), and surfaced via `GET /log`. Each **classification** entry captures: timestamp,
content id, attribution, confidence, both individual signal scores (`statistical_score`, `llm_score`),
their agreement, which signals ran, the label variant, and status. Each **appeal** entry captures the
creator's reasoning and the original decision, and sets status to `under_review`.

Real entries from a run (3 classifications + 1 appeal):

```json
{"type": "classification", "content_id": "984accdd-...", "creator_id": "demo-ai", "timestamp": "2026-07-01T05:14:32.120Z", "attribution": "ai", "confidence": 0.5436, "p_ai": 0.7567, "statistical_score": 0.5417, "llm_score": 0.9, "agreement": 0.6417, "signals_used": ["statistical", "llm_judge"], "label_variant": "high_confidence_ai", "status": "classified"}
{"type": "classification", "content_id": "13d8f79b-...", "creator_id": "demo-human", "timestamp": "2026-07-01T05:14:32.674Z", "attribution": "human", "confidence": 0.9735, "p_ai": 0.1212, "statistical_score": 0.153, "llm_score": 0.1, "agreement": 0.947, "signals_used": ["statistical", "llm_judge"], "label_variant": "high_confidence_human", "status": "classified"}
{"type": "classification", "content_id": "5fa0e288-...", "creator_id": "demo-uncertain", "timestamp": "2026-07-01T05:14:33.212Z", "attribution": "uncertain", "confidence": 0.2286, "p_ai": 0.6409, "statistical_score": 0.2522, "llm_score": 0.9, "agreement": 0.3522, "signals_used": ["statistical", "llm_judge"], "label_variant": "uncertain", "status": "classified"}
{"type": "appeal", "appeal_id": "625a18e6-...", "content_id": "13d8f79b-...", "creator_id": "demo-human", "timestamp": "2026-07-01T05:14:33.218Z", "appeal_reasoning": "I wrote this myself from personal experience. As a non-native English speaker my style can read as more formal than usual.", "original_attribution": "human", "original_confidence": 0.9735, "status": "under_review"}
```

Note the last two: the `human` submission (`13d8f79b`) was appealed, and the appeal record links back
to it with `status: under_review` and the full reasoning.

---

## Known limitations

**Repetitive, simple poetry gets misread as AI.** This is the clearest failure and it's tied directly
to a property of Signal 1. Burstiness is measured as the *variation in sentence/line length*. A poem
with short, even lines and a deliberately spare vocabulary has **low burstiness and low lexical
diversity** — the exact fingerprint my statistical signal reads as "AI." Worse, the Groq judge is also
biased toward calling polished verse "AI," so on this kind of input *both* signals can fail in the
same direction. My mitigations soften it (short/line-structured text caps confidence, and the strict
AI threshold pushes borderline cases to `uncertain` instead of a false accusation), but a genuine poem
that happens to look uniform will still, at best, land `uncertain` — I can't confidently defend a real
poet's work here. The root cause is that burstiness is a proxy for authorship, and poetry breaks the
proxy: its regularity comes from *form*, not from a language model.

Two other known-weak cases, same root idea: **very short text** (under ~25 words there aren't enough
sentences to measure burstiness, so it's flagged short and forced to `uncertain`), and **mixed
authorship** (a human draft polished by AI, or vice-versa) — there's no single true label, so it
lands in the middle, which is honest but not actionable.

---

## Spec reflection

**One way the spec helped:** the requirement that confidence be a real score with an "uncertain" band
— *not* a binary flip at 0.5 — forced me to design the confidence gate and the middle band **before**
writing the scorer. That constraint is what led to the whole "disagreement lowers confidence" idea,
which turned out to be the most important part of the system (it's what protects formal human writing
from false AI accusations). If the spec had just asked for "AI or human," I'd have built something more
confident and more wrong.

**One way my implementation diverged:** my planning.md originally specified fusion weights of
`0.45/0.55` and `confidence = raw_conf * agreement * length_factor`. When I actually calibrated in
Milestone 4, that formula **over-compressed** the scores — a hard agreement multiply zeroed out too
many verdicts, and even my clearest AI example read as low confidence. I diverged to `0.40/0.60`
weights, a *softened* agreement factor (`0.5 + 0.5*agreement`), and a confidence gain of `1.5` so
clear two-signal verdicts aren't under-reported. I updated planning.md to match rather than letting the
doc and code drift apart. The lesson: the plan was directionally right but the specific constants had
to be earned from real numbers, not guessed.

---

## AI usage

I used an AI coding assistant throughout, but reviewed and corrected its output at each step rather
than pasting blindly. Specific instances:

1. **Statistical signal + Flask skeleton (M3).** I gave it my detection-signals section and asked for
   the `POST /submit` route and the Signal 1 function. It produced a working burstiness/TTR function,
   but its default minimum-length threshold (40 words) flagged normal paragraphs — including the
   assignment's own test text — as "short." I **overrode it to 25 words** after testing the function
   directly on sample inputs and seeing everything come back `uncertain`.

2. **Groq judge + fusion (M4).** I asked it to write the LLM signal and the scorer. The judge crashed
   with `KeyError: '"p_ai"'`; I traced it to the AI's use of `str.format()` on a prompt that contained
   literal `{}` JSON braces, and **fixed it by switching to a `__TEXT__` placeholder + `.replace()`**.
   It also implemented the scoring with reasonable-looking constants that **silently diverged from my
   planning.md thresholds** — I caught this by comparing against my spec and re-tuning (see Spec
   Reflection).

3. **Label coherence (M5).** The generated label mapper let the `high_confidence_ai` variant fire at a
   confidence of 0.36 while the phrase read "not very confident" — a contradiction. I **added a
   confidence gain to the scorer** so a clear verdict reports a coherent number, then re-verified all
   three variants were still reachable.

---

## Portfolio walkthrough

> _(Recording link goes here.)_ A short, unpolished ~2-minute tour: start the app, submit a clearly-
> human and a clearly-AI-style text to show the different labels + confidence scores, file an appeal on
> one and show it flip to `under_review` in `GET /log`, and talk through the one design decision I'm
> most proud of — using signal *disagreement* to drive uncertainty so the system doesn't falsely accuse
> real writers.
