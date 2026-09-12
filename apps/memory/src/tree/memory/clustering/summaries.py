"""One LLM call per **Memory cluster**: prompt, call, and the fail-open fallback.

ADR-007 §4. A **Clustering run** shows the model at most
``sampling.nearest + sampling.random`` (20) member chunks per cluster — picked
by :func:`tree.memory.clustering.core.sample_cluster_members` — and asks for the
three things a legend entry needs: a ``label``, a ``summary`` and ``keywords``.
The bounds live on :class:`~tree.memory.clustering.types.ClusterSummary`, so a
chatty model produces a ``ValidationError`` (and a task retry) rather than a
label that wraps over the map.

This module is PURE in the same sense as ``core.py``: no Prefect, no Mongo, no
config reads, no model factory. It takes a :class:`~tree.models.base.BaseLLM`
and a list of strings. The flow (``tree.memory.pipeline``) owns the semaphore,
the retries, the cache and the ``get_llm()`` call.
"""

from __future__ import annotations

from tree.memory.clustering.types import ClusterSummary
from tree.models.base import BaseLLM

SUMMARY_PROMPT_VERSION = "v1"
"""Bumped whenever the prompt text below changes.

It is a task PARAMETER of ``summarise-cluster``, so it is part of the task's
``INPUTS`` cache key: editing the prompt without bumping this would serve
90-day-old labels written by the OLD prompt from Prefect's cache.
"""

_SAMPLE_CHAR_LIMIT = 600
"""How much of each sampled chunk the model sees.

20 samples x 600 characters is ~3k tokens of evidence — enough to name a topic,
and bounded so one huge chunk cannot crowd the other 19 out of the prompt.
Child chunks are ~256 tokens by default, so the cut only bites on outliers.
"""

_SYSTEM_PROMPT = """\
You are a topic labeller for a personal knowledge base.

You are given text chunks that an embedding-clustering run grouped together.
Name what they have in common. Return ONLY valid JSON matching this schema:

{
  "label": "<the topic, at most 6 words>",
  "summary": "<what the chunks share, at most 100 words>",
  "keywords": ["<3-5 short lowercase phrases>"]
}

Rules:
- Describe only what the chunks actually say; never invent a topic.
- The label names the topic itself, not the fact that it is a cluster
  (write "Agent memory architectures", not "Cluster about agent memory").
- If the chunks are genuinely unrelated, say so in the summary and give the
  broadest honest label.\
"""


def build_summary_prompt(samples: list[str]) -> str:
    """Render the user prompt for one cluster's sampled chunks.

    The samples are NUMBERED so the model reads them as N separate pieces of
    evidence rather than one run-on document, and each is truncated to
    :data:`_SAMPLE_CHAR_LIMIT` characters.

    Args:
        samples: The member chunks' ``properties.content``, in the order
            :func:`~tree.memory.clustering.core.sample_cluster_members` returned
            them (nearest the centroid first, then the seeded random tail).

    Returns:
        The prompt text. Deterministic for a given sample list — which is what
        makes the task's ``INPUTS`` cache worth having.
    """

    numbered = "\n\n".join(
        f"{position}. {sample[:_SAMPLE_CHAR_LIMIT]}"
        for position, sample in enumerate(samples, start=1)
    )
    return (
        f"These are {len(samples)} text chunks from one cluster of a personal "
        "knowledge base.\n\n"
        f"{numbered}\n\n"
        'Return JSON {"label": …, "summary": …, "keywords": […]}: `label` names '
        "the topic in at most 6 words; `summary` describes what the chunks "
        "share in at most 100 words; `keywords` is 3–5 short lowercase phrases."
    )


async def summarise_cluster(llm: BaseLLM, samples: list[str]) -> ClusterSummary:
    """Ask the model to name and describe ONE cluster.

    Exactly one ``generate_json`` call — clusters are summarised in parallel by
    the flow, under its own semaphore, so this stays a single round trip.

    Args:
        llm: The model handle; the caller owns its lifecycle.
        samples: The sampled member chunks (see :func:`build_summary_prompt`).

    Returns:
        The validated summary.

    Raises:
        pydantic.ValidationError: The model ignored the bounds (a 9-word label,
            2 keywords, a 300-word summary). Deliberately NOT caught here: the
            ``summarise-cluster`` task retries, and a cluster that keeps failing
            falls back to :func:`fallback_summary` in the flow.
    """

    raw = await llm.generate_json(build_summary_prompt(samples), system=_SYSTEM_PROMPT)
    return ClusterSummary.model_validate(raw)


def fallback_summary(cluster_id: int) -> ClusterSummary:
    """The fail-open value for a cluster whose summary call kept failing.

    ADR-007 §4: one bad cluster never fails a **Clustering run** — the map still
    draws its points, and the legend reads ``Cluster 4`` instead of a name.

    Built with ``model_construct`` (NO validation) on purpose: ``keywords=[]``
    and ``summary=""`` are exactly what :class:`ClusterSummary` refuses from a
    model, and that refusal is the point. Widening the contract to let the
    fallback through would also let a lazy model through.
    """

    return ClusterSummary.model_construct(
        label=f"Cluster {cluster_id}", summary="", keywords=[]
    )
