"""Talking to a served **Embedding catalog** model — the same way on every
**Serving path** (ADR-009 §3/§4).

ONE URL lookup, ONE authenticated health check, ONE served-model discovery and
ONE smoke test, shared by the deploy driver (``scripts/modal_embedding_model.py``)
and the ``ModalEmbeddingModel`` client (#140). They rest on assumption H1 — a
**Dedicated endpoint** named ``N`` is the Modal app ``ep-N`` with a server class
``Server``, which the fallback scripts copy — so the caller never reads
``serving``. Every ``N`` of ours is ``tree-<slug>``, hence every app is
``ep-tree-<slug>``: H1 needs the shape, not a particular name. ``tasks/141`` proves H1 live and owns the correction if it is false.

This module imports the ``modal`` SDK, so it is imported by the driver and the
client only — never by :mod:`tree.models.modal_catalog` and never at MCP boot.
"""

import logging
import math
import time

import aiohttp
import modal
from pydantic import BaseModel, Field

from tree.config.app_config import ModalEmbeddingModelConfig
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import (
    EMBEDDING_SERVER_NAME,
    get_catalog_entry,
    modal_proxy_bearer,
    prompt_for,
    truncate_embedding,
)

logger = logging.getLogger(__name__)

# The three texts every smoke test embeds: one question, the document that
# answers it and one that does not. A wrong architecture or pooling returns
# well-shaped floats, so the ONLY cheap check is that the relevant document
# scores higher than the unrelated one (ADR-009 §2).
SMOKE_QUERY = "how do I reset my password?"
SMOKE_RELEVANT = "To reset your password, open Settings and choose Reset password."
SMOKE_UNRELATED = "The Eiffel Tower is 330 metres tall."

# How far a truncated vector may sit from unit length before we call the
# client-side Matryoshka truncation broken.
_NORM_TOLERANCE = 1e-3


class SmokeTestReport(BaseModel):
    """What one smoke test measured — the evidence ``tasks/141`` records."""

    url: str = Field(description="Root URL of the served model (no /v1).")
    served_model: str = Field(description="The id discovered from /v1/models.")
    dimensions: int = Field(description="Vector width on the wire.")
    truncated_dimensions: int | None = Field(
        description="Width after client-side truncation, or None when the "
        "entry lists no Matryoshka size below its native width."
    )
    cold_start_seconds: float = Field(description="Seconds until /health was 200.")
    cos_relevant: float
    cos_unrelated: float
    unauthenticated_status: int = Field(
        description="What /health answers WITHOUT the Proxy token — 401."
    )


async def resolve_server_url(entry: ModalEmbeddingModelConfig) -> str:
    """The root URL of the Modal app serving ``entry`` (no trailing ``/v1``).

    Resolved without blocking the event loop (``get_url.aio()`` on the pinned
    client, modal 1.5.5).

    Raises:
        ModelError: the lookup failed or the server has no URL — most often
            because the model is not deployed, or a Dedicated endpoint is
            still provisioning.
    """

    failure = (
        f"Failed to resolve Modal server {entry.app_name}/{EMBEDDING_SERVER_NAME}. "
        "Is the model deployed (a Dedicated endpoint may still be provisioning "
        "— check `modal endpoint list`)? Run: "
        f"make memory-deploy-embedding-model MODEL={entry.repo_id}"
    )

    try:
        server = modal.Server.from_name(entry.app_name, EMBEDDING_SERVER_NAME)
        url = await server.get_url.aio()
    except Exception as exc:  # noqa: BLE001 — every lookup failure is the same operator action
        raise ModelError(failure) from exc

    if not url:
        raise ModelError(failure)
    return url.rstrip("/").removesuffix("/v1").rstrip("/")


async def wait_until_healthy(url: str, bearer: str, timeout: float) -> float:
    """Seconds until ``GET {url}/health`` answered 200 through proxy auth.

    ONE long-held request rather than a poll loop: Modal's edge holds the
    request while a scaled-to-zero container wakes, which is the cold start we
    are measuring.

    Raises:
        ModelError: 401 — the **Proxy token** is wrong or missing, which no
            amount of waiting fixes.
        ExtractionError: any other status or a transport failure (transient:
            a provisioning endpoint, a crashed engine).
    """

    started = time.monotonic()
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {bearer}"}
        ) as session:
            async with session.get(
                f"{url}/health", timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                status = response.status
    except Exception as exc:  # noqa: BLE001 — a transport failure is retryable, like a 5xx
        raise ExtractionError(f"Health check on {url}/health failed: {exc}") from exc

    elapsed = time.monotonic() - started
    if status == 200:
        return elapsed
    if status == 401:
        raise ModelError(
            f"Modal answered 401 for {url}/health — the Proxy token is wrong "
            "or missing. Check MODAL_PROXY_TOKEN_ID and "
            "MODAL_PROXY_TOKEN_SECRET in .env."
        )
    raise ExtractionError(
        f"Health check on {url}/health answered {status}.", status_code=status
    )


async def served_model_id(url: str, bearer: str, default: str) -> str:
    """The model id the server answers to, discovered from ``/v1/models``.

    We do not control the name a managed recipe serves under (custom weights
    especially) and vLLM rejects an unknown ``model``, so the id is read from
    the server. Best-effort: a failing call or an empty list falls back to
    ``default`` with a WARNING, because the embeddings POST is where a wrong
    name surfaces loudly anyway.
    """

    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {bearer}"}
        ) as session:
            async with session.get(f"{url}/v1/models") as response:
                if response.status != 200:
                    logger.warning(
                        "GET %s/v1/models answered %d — using %s as the model id.",
                        url,
                        response.status,
                        default,
                    )
                    return default
                payload = await response.json()
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort by design
        logger.warning(
            "Could not read %s/v1/models (%s) — using %s as the model id.",
            url,
            exc,
            default,
        )
        return default

    served = [item.get("id") for item in payload.get("data", []) if item.get("id")]
    if not served:
        logger.warning(
            "GET %s/v1/models listed no model — using %s as the model id.",
            url,
            default,
        )
        return default
    return served[0]


async def smoke_test(model: str, health_timeout: float = 1200.0) -> SmokeTestReport:
    """Prove one served model end-to-end, on whatever path serves it.

    Health through proxy auth, the DISCOVERED model id, three prompted inputs,
    the native vector width, a relevant-vs-unrelated ordering, the same
    ordering at the truncated width the memory stores, and a 401 without the
    token.

    Raises:
        ModelError: any assertion failed (the message carries the numbers).
        ExtractionError: the server was unreachable or unhealthy.
    """

    entry = get_catalog_entry(model)
    bearer = modal_proxy_bearer()

    url = await resolve_server_url(entry)
    elapsed = await wait_until_healthy(url, bearer, health_timeout)
    logger.info("health 200 after %.1fs", elapsed)

    served = await served_model_id(url, bearer, entry.repo_id)
    logger.info("served model id: %s", served)

    texts = [
        prompt_for(entry, "query") + SMOKE_QUERY,
        prompt_for(entry, "document") + SMOKE_RELEVANT,
        prompt_for(entry, "document") + SMOKE_UNRELATED,
    ]
    vectors = await _embed(url, bearer, served, texts)

    for vector in vectors:
        if len(vector) != entry.native_dimensions:
            raise ModelError(
                f"expected {entry.native_dimensions} dims, got {len(vector)} — "
                "the Embedding catalog's native_dimensions is wrong for this "
                "Serving path, or the served architecture dropped the model's "
                "projection head"
            )
    logger.info("%d embeddings, %d dims", len(vectors), entry.native_dimensions)

    cos_relevant, cos_unrelated = _rank(vectors)
    if cos_relevant <= cos_unrelated:
        raise ModelError(
            f"sanity check failed: cos(query, relevant)={cos_relevant:.2f} <= "
            f"cos(query, unrelated)={cos_unrelated:.2f} — the served "
            "architecture or pooling is probably wrong"
        )
    logger.info(
        "sanity: cos(query, relevant)=%.2f > cos(query, unrelated)=%.2f",
        cos_relevant,
        cos_unrelated,
    )

    truncated_dimensions = _truncation_target(entry)
    if truncated_dimensions is not None:
        _check_truncation(vectors, entry.native_dimensions, truncated_dimensions)

    unauthenticated_status = await _unauthenticated_health_status(url)
    if unauthenticated_status != 401:
        raise ModelError(
            f"unauthenticated /health answered {unauthenticated_status}, "
            "expected 401 — the server is public"
        )
    logger.info("unauthenticated health -> %d", unauthenticated_status)
    logger.info("Smoke test passed")

    return SmokeTestReport(
        url=url,
        served_model=served,
        dimensions=entry.native_dimensions,
        truncated_dimensions=truncated_dimensions,
        cold_start_seconds=elapsed,
        cos_relevant=cos_relevant,
        cos_unrelated=cos_unrelated,
        unauthenticated_status=unauthenticated_status,
    )


async def _embed(
    url: str, bearer: str, served: str, texts: list[str]
) -> list[list[float]]:
    """``POST /v1/embeddings`` for ``texts`` — never with a ``dimensions`` key.

    vLLM answers 400 to an OpenAI ``dimensions`` parameter unless the model's
    HF config is flagged Matryoshka, and a managed recipe exposes no flag for
    it, so every Serving path is asked for the native width (ADR-009 §3).
    """

    body = {"model": served, "input": texts, "encoding_format": "float"}
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {bearer}"}
        ) as session:
            async with session.post(f"{url}/v1/embeddings", json=body) as response:
                status = response.status
                payload = await response.json() if status == 200 else {}
    except Exception as exc:  # noqa: BLE001 — a transport failure is retryable
        raise ExtractionError(f"POST {url}/v1/embeddings failed: {exc}") from exc

    if status != 200:
        raise ExtractionError(
            f"POST {url}/v1/embeddings answered {status}, expected 200.",
            status_code=status,
        )

    vectors = [item["embedding"] for item in payload.get("data", [])]
    if len(vectors) != len(texts):
        raise ModelError(
            f"expected {len(texts)} embeddings, got {len(vectors)} — the "
            "server dropped or merged inputs"
        )
    return vectors


async def _unauthenticated_health_status(url: str) -> int:
    """What ``GET {url}/health`` answers with NO ``Authorization`` header.

    Modal's edge rejects unauthenticated traffic BEFORE a GPU wakes, so 401 is
    the expected answer and anything else means the server is public.
    """

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{url}/health") as response:
                return response.status
    except Exception as exc:  # noqa: BLE001 — a transport failure is retryable
        raise ExtractionError(
            f"Unauthenticated health probe on {url}/health failed: {exc}"
        ) from exc


def _truncation_target(entry: ModalEmbeddingModelConfig) -> int | None:
    """The widest Matryoshka size BELOW the native width, or ``None``.

    ``None`` means the memory stores what the wire carries — there is nothing
    to prove about a truncation that never happens.
    """

    smaller = [
        size for size in entry.matryoshka_dimensions if size < entry.native_dimensions
    ]
    return max(smaller) if smaller else None


def _check_truncation(vectors: list[list[float]], native: int, target: int) -> None:
    """Prove the vector the memory actually stores (ADR-009 §3).

    The SAME three vectors, truncated client-side with no second request: each
    unit length at ``target``, and the ranking unchanged — the first ``target``
    components are what the 1024-d mongot index sees.

    Raises:
        ModelError: a truncated vector is the wrong width, is not unit length,
            or the ordering flipped under truncation.
    """

    truncated = [truncate_embedding(vector, target) for vector in vectors]

    for vector in truncated:
        if len(vector) != target:
            raise ModelError(
                f"expected {target} dims after truncation, got {len(vector)}"
            )
        norm = math.sqrt(sum(value * value for value in vector))
        if abs(norm - 1.0) > _NORM_TOLERANCE:
            raise ModelError(
                f"truncated norm={norm:.3f}, expected 1.0 ± {_NORM_TOLERANCE}"
            )

    logger.info(
        "truncated %d -> %d dims client-side, norm=%.3f",
        native,
        target,
        math.sqrt(sum(value * value for value in truncated[0])),
    )

    cos_relevant, cos_unrelated = _rank(truncated)
    if cos_relevant <= cos_unrelated:
        raise ModelError(
            f"sanity@{target} check failed: cos(query, relevant)="
            f"{cos_relevant:.2f} <= cos(query, unrelated)={cos_unrelated:.2f} — "
            f"the first {target} components do not carry the model's ranking, "
            f"so the {target}-d vector the memory would store is unusable"
        )
    logger.info(
        "sanity@%d: cos(query, relevant)=%.2f > cos(query, unrelated)=%.2f",
        target,
        cos_relevant,
        cos_unrelated,
    )


def _rank(vectors: list[list[float]]) -> tuple[float, float]:
    """``(cos(query, relevant), cos(query, unrelated))`` for the three vectors."""

    query, relevant, unrelated = vectors
    return _cosine(query, relevant), _cosine(query, unrelated)


def _cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity; ``0.0`` when either side is a zero vector."""

    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (norm_left * norm_right)
