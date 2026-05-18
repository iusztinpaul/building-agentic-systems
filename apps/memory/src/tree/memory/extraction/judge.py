"""Small LLM judge: are two rows semantically contradictory? (#032)

The contradiction judge is invoked by the supersession resolver branch
in :mod:`tree.memory.extraction.preference_supersession`. Given two
rows in the same partition (two preferences with the same
``(user_id, category)`` slice or two facts with the same
``(user_id, subject, predicate)``), it decides whether the new row
**supersedes** the old one (contradiction) or **paraphrases** it
(falls through to dedup).

The function is intentionally cheap - a single Gemini call with two
short statements - because it runs O(n) times per extraction batch
(once per same-partition candidate above the embedding cosine
threshold).

Output contract:
    Returns ``(is_contradiction: bool, confidence: float)`` where
    ``confidence`` is the LLM's self-reported confidence in
    ``[0.0, 1.0]``. A malformed / non-JSON response degrades to
    ``(False, 0.0)`` - the safe default is "not a contradiction" so
    we never accidentally write a ``superseded_by`` edge based on a
    parse error.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tree.models.base import BaseLLM

logger = logging.getLogger(__name__)


_JUDGE_SYSTEM_PROMPT = """\
You are a strict semantic-contradiction judge.

You receive two short statements (A = new, B = old) describing the
same kind of fact or preference about the same subject. Decide
whether A semantically CONTRADICTS B (the two cannot both be true
simultaneously, or A explicitly retracts / replaces B) or whether
they are PARAPHRASES / COMPATIBLE / REFINEMENTS of the same claim.

Return strict JSON only, matching this schema:
{
  "is_contradiction": <true | false>,
  "confidence": <float in [0.0, 1.0]>,
  "reasoning": "<one short sentence>"
}

Decision rules - read carefully, these are common LLM mis-calls:

1. NARROWING / SCOPING is NOT a contradiction.
   - A: "prefers dark mode for editors"  B: "prefers dark mode"
     -> is_contradiction=false. A is a more-specific scope of B; both
     can be simultaneously true.
   - A: "prefers tea in the morning"     B: "prefers tea"
     -> is_contradiction=false.

2. PARAPHRASE / SYNONYM is NOT a contradiction.
   - A: "really likes python"            B: "prefers python"
     -> is_contradiction=false.
   - A: "loves Mexican food"             B: "prefers Mexican food"
     -> is_contradiction=false.

3. OPPOSING OBJECTS in the same slot ARE a contradiction.
   - A: "prefers dark mode"              B: "prefers light mode"
     -> is_contradiction=true. Mutually exclusive UI choices.
   - A: "prefers vegetarian food"        B: "prefers meat"
     -> is_contradiction=true.

4. MUTUALLY EXCLUSIVE FACTUAL OBJECTS ARE a contradiction.
   - A: "earth orbits sun"               B: "earth orbits mars"
     -> is_contradiction=true.
   - A: "paris is capital of france"     B: "paris is capital of brazil"
     -> is_contradiction=true.

5. CONFIDENCE CALIBRATION. Use the FULL [0.0, 1.0] range:
   - 0.95-1.0: textbook-clear contradiction or textbook-clear
     paraphrase (the dark/light or python paraphrase cases above).
   - 0.7-0.9: confident but the underlying scope or modality is
     fuzzy.
   - 0.3-0.6: genuinely ambiguous.
   - 0.0-0.3: very unsure.
   Returning 1.0 on every case is a calibration bug - it loses the
   audit-trail value. Vary your confidence with how clear-cut the
   case actually is.

Rules:
- Output ONLY the JSON object. No prose, no markdown, no
  surrounding text.
- Be conservative: when in doubt, return is_contradiction=false.
  The downstream resolver falls through to dedup (safe); a false
  positive writes a permanent ``superseded_by`` edge (unsafe).
"""


async def judge_contradiction(
    *,
    llm: BaseLLM,
    new_statement: str,
    old_statement: str,
) -> tuple[bool, float]:
    """Ask the LLM whether two short statements semantically contradict.

    Args:
        llm: An :class:`BaseLLM` instance. The caller owns the model
            handle (the supersession resolver receives it from the
            pipeline so the run cost is bounded by the same
            concurrency semaphore as the main extraction call).
        new_statement: The incoming row's canonical statement (for
            preferences: ``properties.statement``; for facts: the
            ``properties.object`` value).
        old_statement: The candidate prior row's canonical statement.

    Returns:
        ``(is_contradiction, confidence)``. ``confidence`` is clamped
        to ``[0.0, 1.0]``. A malformed response degrades to
        ``(False, 0.0)`` so the resolver falls through to the dedup
        branch rather than writing a spurious supersession.
    """

    prompt = (
        f"Statement A (new): {new_statement!r}\n"
        f"Statement B (old): {old_statement!r}\n"
        "\nDecide whether A contradicts B. Output the JSON object only."
    )
    try:
        raw: dict[str, Any] = await llm.generate_json(
            prompt, system=_JUDGE_SYSTEM_PROMPT
        )
    except Exception:  # noqa: BLE001 - never let the judge break the pipeline.
        logger.warning(
            "judge_contradiction: LLM call failed, defaulting to "
            "(is_contradiction=False, confidence=0.0)",
            exc_info=True,
        )
        return False, 0.0

    # Some LLMs may wrap the JSON in a "result" key or return a string;
    # normalise defensively.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            logger.warning(
                "judge_contradiction: non-JSON string response %r; defaulting",
                raw,
            )
            return False, 0.0

    if not isinstance(raw, dict):
        logger.warning("judge_contradiction: non-dict response %r; defaulting", raw)
        return False, 0.0

    is_contradiction_raw = raw.get("is_contradiction")
    confidence_raw = raw.get("confidence", 0.0)
    is_contradiction = bool(is_contradiction_raw)
    try:
        confidence = float(confidence_raw)
    except TypeError, ValueError:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    logger.debug(
        "judge_contradiction: new=%r old=%r -> is_contradiction=%s confidence=%.2f",
        new_statement,
        old_statement,
        is_contradiction,
        confidence,
    )
    return is_contradiction, confidence
