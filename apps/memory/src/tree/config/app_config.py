"""
YAML-based application configuration.

Loads app-level tuning parameters (model names, chunk sizes, concurrency, etc.)
from a YAML file.  Infrastructure secrets stay in settings.py / .env.

Resolution order:
    1. Path in APP_CONFIG_PATH env var
    2. configs/default.yaml (memory app root: ``apps/memory/``)
"""

import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "configs" / "default.yaml"


# --- Pydantic models for typed access ---


class LLMConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-2.5-flash-lite"


class EmbeddingConfig(BaseModel):
    """YAML-authoritative embedding config (provider/model/dimensions).

    Used for both :attr:`ModelsConfig.resolution_embedding` and
    :attr:`ModelsConfig.search_embedding`. Only the **search** embedding's
    ``dimensions`` is dimension-coupled to the Atlas Vector Search index
    under ``docker/mongot/``;
    :func:`tree.memory.indexing.core.assert_settings_match_live_vector_index`
    asserts it matches the live ``vector_index`` at boot so a mismatch is a
    hard startup error rather than silent data loss. The **resolution**
    embedding is transient (computed on the entity name and never
    persisted), so its ``dimensions`` is not index-coupled.
    """

    provider: str = Field(default="voyage")
    model: str = Field(default="voyage-3.5")
    dimensions: int = Field(default=1024)


class EmbeddingBatchConfig(BaseModel):
    """Per-request batching caps for Voyage ``/v1/multimodalembeddings``.

    Bound how many texts :func:`tree.memory.embedding_text.embed_in_batches`
    packs into a SINGLE synchronous embed request. Defaults are the Voyage
    per-request caps for ``voyage-multimodal-3``:

    * ``max_inputs`` — max 1,000 inputs per request.
    * ``max_total_tokens`` — total across all inputs per request. Defaults to
      10,000 (the shared free-tier Voyage TPM window, #054); the model's hard
      per-request ceiling is 320,000.
    * ``max_input_tokens`` — each single input ≤ 32,000. The model sends
      ``truncation=True`` so an oversized input is truncated server-side
      rather than 400-ing; this bound only governs how the batcher
      *accounts* a single text against the per-request total.

    This is synchronous request batching, NOT Voyage's async Batch API —
    the async path is rejected because its 12h completion window can't
    drive synchronous mid-flow dedup and it doesn't support
    ``/v1/multimodalembeddings``.
    """

    max_inputs: int = Field(default=1000)
    # Dropped 320_000 → 10_000 (#054 / ADR-002): the shared free-tier Voyage key
    # is capped at 10K TPM, so no single synchronous request may exceed the
    # per-minute token window. The TPM cap is held by this config knob, not a
    # second token-weighted limiter (an explicit deferred follow-up).
    max_total_tokens: int = Field(default=10_000)
    max_input_tokens: int = Field(default=32_000)
    # YAML-only fan-out knob (#054): how many embed requests a single stage may
    # dispatch concurrently. Default 1 keeps dispatch serial; the cross-flow
    # `voyage-embeddings` GCL is the real throttle, this just bounds local fan-out.
    dispatch_concurrency: int = Field(default=1)


class ModelsConfig(BaseModel):
    """Configured models.

    Two embedding blocks:

    * ``resolution_embedding`` — transient, used only by resolution's
      semantic stage (computed on the entity NAME, never persisted).
      Configured separately so a lighter model can be swapped in without
      touching the persisted-vector model.
    * ``search_embedding`` — persisted. Its output is written to the node
      ``embedding`` field and the live mongot ``vector_index`` is
      dimension-coupled to its ``dimensions``. Used for dedup and query.

    ``embedding_batch`` holds the per-request batching caps shared by
    resolution, dedup, and indexing.
    """

    llm: LLMConfig = LLMConfig()
    resolution_embedding: EmbeddingConfig = EmbeddingConfig()
    search_embedding: EmbeddingConfig = EmbeddingConfig()
    embedding_batch: EmbeddingBatchConfig = EmbeddingBatchConfig()


class ResolutionConfig(BaseModel):
    """Resolver chain (Alias → Exact → Fuzzy → Semantic) tuning."""

    fuzzy_threshold: float = 0.85
    semantic_threshold: float = 0.80
    type_strict: bool = True
    max_candidates_per_type: int = 1000
    embedding_cache_max_size: int = 10_000


class DedupConfig(BaseModel):
    """Read-only dedup-decision tuning. Mirrors
    :class:`tree.memory.extraction.dedup.DeduplicationConfig` field-for-field
    so the YAML can drive both.

    Post-#034 this is the *sole* source of truth for dedup behavior:
    the ``DEDUP_*`` env-prefixed BaseSettings model has been
    decommissioned. Operators who need a one-off override use
    ``TREE_EXTRACTION__DEDUP__<KEY>`` (see :func:`_apply_env_overrides`).

    ``supersession_candidate_cap`` lives here too (was env-only on the
    retired ``settings.DedupConfig``): it bounds the bound-candidate set
    the supersession resolver feeds to the contradiction judge per
    ``(user_id, category)`` partition.
    """

    enabled: bool = True
    auto_merge_threshold: float = 0.95
    flag_threshold: float = 0.85
    use_fuzzy_matching: bool = True
    fuzzy_threshold: float = 0.90
    max_candidates: int = 10
    match_same_type_only: bool = True
    merge_strategy: Literal["keep_primary", "merge_properties", "keep_aliases"] = (
        "keep_primary"
    )
    supersession_candidate_cap: int = 8


class ExtractionConfig(BaseModel):
    chunk_size: int = 512
    chunk_overlap: int = 64
    llm_concurrency: int = 5
    # Intra-run fan-out knobs (#054). Both inherit the ``TREE_EXTRACTION__*``
    # override hatch via :func:`_apply_env_overrides`.
    # ``doc_concurrency`` — how many documents one extraction run processes in
    # parallel (default 1 = serial; the cross-run fan-out is the
    # ``memory-extract-etl-orchestrator`` dispatching worker shards, see ADR-002).
    doc_concurrency: int = 1
    # ``dedup_concurrency`` — how many entities the dedup stage may resolve in
    # parallel within a run.
    dedup_concurrency: int = 8
    resolution: ResolutionConfig = ResolutionConfig()
    dedup: DedupConfig = DedupConfig()

    @model_validator(mode="after")
    def _check_type_alignment(self) -> "ExtractionConfig":
        """Cross-key invariant: resolver and dedup must agree on type-strictness.

        Misalignment would let resolution match by surface name across types
        while dedup keeps the vector search within a single type (or vice
        versa). The flow refuses to start in that state.
        """

        if self.resolution.type_strict != self.dedup.match_same_type_only:
            raise ValueError(
                "Misconfigured extraction: "
                "extraction.resolution.type_strict and "
                "extraction.dedup.match_same_type_only must agree "
                "(both True or both False). Found "
                f"resolution.type_strict={self.resolution.type_strict}, "
                f"dedup.match_same_type_only={self.dedup.match_same_type_only}."
            )
        return self


class DreamConfig(BaseModel):
    """Dream-consolidation pipeline tuning (#051).

    The dream pipeline re-runs the existing three-tier dedup across the
    knowledge graph **incrementally** (watermark-bounded), catching
    near-duplicate nodes that parallel ingestion's inline write-time dedup
    missed. It owns NO thresholds of its own — the auto-merge / flag /
    fuzzy cut-offs stay in :class:`DedupConfig` (``extraction.dedup``) so
    the inline and the dream surfaces can never drift.

    Fields:

    * ``enabled`` — master on/off switch.
    * ``cron`` — schedule consumed by #052's deployment. Defined here so the
      block is complete; this task does NOT register the deployment.
    * ``dry_run`` — when ``True`` the sweep reports the duplicate pairs it
      WOULD merge/flag but performs NO writes (no merges, no SAME_AS edges,
      no watermark advance). Safe first-rollout default.
    * ``max_pairs`` — cap on the number of candidate pairs examined per run.
      Once the cap is hit the sweep stops driving and records ``cap_hit`` in
      its stats.
    * ``enable_supersession_judge`` — gate for the LLM contradiction /
      supersession sweep that lands in #052. Always ``False`` here; this
      task ships only the semantic + fuzzy sweep and leaves a clean seam.
    """

    enabled: bool = True
    cron: str = "0 4 * * *"
    dry_run: bool = True
    max_pairs: int = 10_000
    enable_supersession_judge: bool = False


class ConcurrencyConfig(BaseModel):
    """Pipeline-parallelism + Voyage rate-limiting knobs (ADR-002).

    These govern how aggressively the memory pipeline runs concurrently and
    how the shared free-tier Voyage embedding key is throttled across separate
    flow runs:

    * ``voyage_rpm`` — requests/minute the shared free-tier Voyage key allows.
      Drives the server-side ``voyage-embeddings`` Prefect global concurrency
      limit (limit = ``voyage_rpm``, slot-decay-per-second = ``voyage_rpm / 60``),
      created with ``prefect gcl create voyage-embeddings --limit <voyage_rpm>
      --slot-decay-per-second <voyage_rpm/60>``.
    * ``voyage_tpm`` — tokens/minute the key allows. Held by config (the
      ``max_total_tokens`` cap), not yet a second token-weighted limiter.
    * ``runner_global_limit`` — admission control for ``serve(limit=...)``;
      kept close to ``voyage_rpm`` so we don't admit far more runs than the
      embed budget can feed.
    """

    voyage_rpm: int = 3
    voyage_tpm: int = 10_000
    runner_global_limit: int = 4


class QueryConfig(BaseModel):
    top_k: int = 10
    max_hops: int = 1
    rrf_k: int = 60
    embedding_batch_size: int = 64


class ObservabilityConfig(BaseModel):
    """Opik observability tuning (monitoring only — cost + retrieval threads).

    * ``enabled`` — master flag for the YAML side of observability. The actual
      no-op gate is the absence of ``OPIK_API_KEY`` (see
      :mod:`tree.observability`); this flag is the operator's documented YAML
      switch.
    * ``embedding_price_per_1m_tokens`` — per-model USD price per 1,000,000
      tokens, used to compute the manual ``total_cost`` on Voyage embedding
      spans (Opik does not natively cost Voyage). Prices verified against
      https://docs.voyageai.com/docs/pricing (June 2026): voyage-3.5 $0.06,
      voyage-3 $0.06, voyage-3.5-lite $0.02, voyage-3-large $0.18,
      voyage-3-lite $0.02, voyage-code-3 $0.18, voyage-multimodal-3 $0.12,
      voyage-finance-2 $0.12, voyage-law-2 $0.12. A model absent from the map
      yields ``total_cost=0`` (token usage is still recorded) rather than an
      error — telemetry is fail-open.
    """

    enabled: bool = True
    embedding_price_per_1m_tokens: dict[str, float] = Field(
        default_factory=lambda: {
            "voyage-3.5": 0.06,
            "voyage-3": 0.06,
            "voyage-3.5-lite": 0.02,
            "voyage-3-large": 0.18,
            "voyage-3-lite": 0.02,
            "voyage-code-3": 0.18,
            "voyage-finance-2": 0.12,
            "voyage-law-2": 0.12,
            "voyage-multimodal-3": 0.12,
            "voyage-multimodal-3.5": 0.12,
        }
    )

    def cost_for(self, model: str, total_tokens: int) -> float:
        """USD cost for ``total_tokens`` of ``model``.

        Returns ``0.0`` when the model is not in the price map (self-hosted or
        unknown) — the span still carries token usage, just no cost.
        """

        price_per_1m = self.embedding_price_per_1m_tokens.get(model)
        if price_per_1m is None:
            return 0.0
        return (total_tokens / 1_000_000) * price_per_1m


# --- Source variants (discriminated union) ---


class _ConfiguredSource(BaseModel):
    """Fields shared by every configured offline source.

    ``scheduled`` opts a source into the nightly scheduled run
    (``data-etl-orchestrator``'s cron sets ``scheduled_only=True``, which ingests
    ONLY flagged sources, for every active user). A manual ``make
    run-data-pipeline`` ignores the flag and ingests everything. Default
    ``false`` — set it ``true`` on feeds that gain new items over time (e.g.
    ``substack_rss`` / ``youtube_rss``); leave it off for one-shot
    articles/videos/datasets that never change after first ingest.
    """

    scheduled: bool = False


class SubstackRssSource(_ConfiguredSource):
    """A Substack RSS feed URL."""

    type: Literal["substack_rss"] = "substack_rss"
    uri: str = Field(min_length=1)


class SubstackArticleSource(_ConfiguredSource):
    """A Substack article URL (may live on a custom domain)."""

    type: Literal["substack_article"] = "substack_article"
    uri: str = Field(min_length=1)


class HuggingFaceDatasetSource(_ConfiguredSource):
    """A HuggingFace dataset id (NOT a URL).

    The ``uri`` is the dataset id (``namespace/name``) and is used to
    dispatch to a per-dataset ETL pipeline registered in
    ``tree.data.offline_pipeline``. Unknown dataset ids raise at dispatch time.

    Two of these fields draw an authored-vs-runtime split (#070, the config
    foundation for HF offset-window fan-out):

    * ``num_workers`` — operator-AUTHORED YAML (like ``batch_size``). The
      offset-window fan-out width: #072 dispatches ``num_workers``
      ``data-etl-worker`` runs, each ingesting one disjoint offset-window of the
      dataset. Default ``1`` ⇒ a single window covering the whole
      ``max_samples`` ⇒ today's behavior. Must be ``>= 1``.
    * ``offset`` — a dispatch-time RUNTIME coordinate, NOT authored in YAML and
      never present in ``default.yaml``. The orchestrator sets it ONLY at
      dispatch via ``entry.model_copy(update={"offset": ...})`` (#072), and #071
      makes the ingest skip the first ``offset`` rows. Default ``None`` ⇒ no
      skip ⇒ today's behavior.

    The discriminated-union round-trip MUST preserve both fields: the
    orchestrator serializes shards through ``run_deployment`` flow-run params
    (``model_dump()`` → JSON → ``TypeAdapter(list[SourceEntry])``), so a set
    ``offset`` round-trips as the int and ``offset=None`` round-trips as ``None``.
    """

    type: Literal["huggingface_dataset"] = "huggingface_dataset"
    uri: str = Field(min_length=1)
    max_samples: int = 10
    fetch_content: bool = False
    batch_size: int = 50
    concurrency: int = 10
    # YAML-authored offset-window fan-out width (#070). See class docstring.
    num_workers: int = Field(default=1, ge=1)
    # Dispatch-time runtime coordinate (#070), never authored in YAML. See
    # class docstring.
    offset: int | None = None


class YouTubeVideoSource(_ConfiguredSource):
    """A YouTube video URL (or 11-char video id)."""

    type: Literal["youtube_video"] = "youtube_video"
    uri: str = Field(min_length=1)


class YouTubeRssSource(_ConfiguredSource):
    """A YouTube channel feed: ``youtube.com/feeds/videos.xml?channel_id=…``."""

    type: Literal["youtube_rss"] = "youtube_rss"
    uri: str = Field(min_length=1)


class WebSource(_ConfiguredSource):
    """A generic web URL ingested via the URL dispatcher."""

    type: Literal["web"] = "web"
    uri: str = Field(min_length=1)


SourceEntry = Annotated[
    Union[
        SubstackRssSource,
        SubstackArticleSource,
        HuggingFaceDatasetSource,
        YouTubeVideoSource,
        YouTubeRssSource,
        WebSource,
    ],
    Field(discriminator="type"),
]


_YOUTUBE_HOSTS: frozenset[str] = frozenset({"youtube.com", "m.youtube.com", "youtu.be"})


def _is_youtube_host(host: str) -> bool:
    """True iff ``host`` is a recognized YouTube host (``www.`` already stripped)."""

    return host in _YOUTUBE_HOSTS


def _is_substack_subdomain(host: str) -> bool:
    """True iff ``host`` is ``substack.com`` or any ``*.substack.com`` subdomain.

    Strips a leading ``www.`` for tolerance.
    """

    if not host:
        return False
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "substack.com" or host.endswith(".substack.com")


def _host_of(uri: str) -> str:
    """Lower-cased ``netloc`` of ``uri`` with any ``www.`` prefix stripped.

    Returns an empty string if ``uri`` has no parseable host (e.g. a
    HuggingFace dataset id).
    """

    host = (urlparse(uri).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _collect_typed_substack_hosts(raw_entries: list[Any]) -> set[str]:
    """Hosts of entries explicitly typed as a Substack variant.

    Used to coerce later untyped entries on the same custom domain.
    """

    hosts: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type not in ("substack_rss", "substack_article"):
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str):
            continue
        host = _host_of(uri)
        if host:
            hosts.add(host)
    return hosts


def _normalize_untyped_entry(
    entry: dict[str, Any], substack_hosts: set[str]
) -> dict[str, Any]:
    """Add a ``type`` to an entry that has none, based on its ``uri``.

    Rules:
        - URL on a YouTube host AND path is ``/feeds/videos.xml`` AND query has
          ``channel_id`` → ``youtube_rss``.
        - URL on a YouTube host that looks like a video URL (``/watch``,
          ``/shorts/...``, or ``youtu.be/<id>``) → ``youtube_video``.
        - URL on ``*.substack.com`` (or ``substack.com``) → ``substack_article``.
        - URL whose host matches another typed Substack source's host → ``substack_article``.
        - Anything else (HTTP/HTTPS URL or otherwise) → ``web``.
    """

    uri = entry.get("uri")
    if not isinstance(uri, str):
        # Let Pydantic raise the proper validation error downstream.
        return entry

    parsed = urlparse(uri)
    host = _host_of(uri)
    path = parsed.path or ""
    query = parsed.query or ""

    if _is_youtube_host(host):
        if path == "/feeds/videos.xml" and "channel_id=" in query:
            return {**entry, "type": "youtube_rss"}
        if host == "youtu.be" or path == "/watch" or path.startswith("/shorts/"):
            return {**entry, "type": "youtube_video"}

    if _is_substack_subdomain(host) or (host and host in substack_hosts):
        inferred_type = "substack_article"
    else:
        inferred_type = "web"

    return {**entry, "type": inferred_type}


class SourcesConfig(BaseModel):
    """Flat list of typed data sources for the ingestion pipelines."""

    sources: list[SourceEntry] = []

    @model_validator(mode="before")
    @classmethod
    def _normalize_untyped_sources(cls, data: Any) -> Any:
        """Pre-validation hook: infer ``type`` for entries that lack one.

        Runs before discriminated-union validation so untyped raw dicts can
        be coerced into a typed variant. Also coerces a bare list of source
        entries into ``{"sources": <list>}`` so the YAML can write the flat
        shape directly under ``AppConfig.sources``. See module-level helpers
        for the inference rules.
        """

        # Accept the flat YAML shape (``sources: [...]`` at the AppConfig
        # level) by wrapping a bare list as ``{"sources": <list>}``.
        if isinstance(data, list):
            data = {"sources": data}

        if not isinstance(data, dict):
            return data
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, list):
            return data

        substack_hosts = _collect_typed_substack_hosts(raw_sources)

        normalized: list[Any] = []
        for entry in raw_sources:
            if isinstance(entry, dict) and "type" not in entry:
                normalized.append(_normalize_untyped_entry(entry, substack_hosts))
            else:
                normalized.append(entry)

        return {**data, "sources": normalized}


class MCPConfig(BaseModel):
    max_retries: int = 1
    max_results: int = 10


class AppConfig(BaseModel):
    sources: SourcesConfig = SourcesConfig()
    models: ModelsConfig = ModelsConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    query: QueryConfig = QueryConfig()
    mcp: MCPConfig = MCPConfig()
    dream: DreamConfig = DreamConfig()
    concurrency: ConcurrencyConfig = ConcurrencyConfig()
    observability: ObservabilityConfig = ObservabilityConfig()


_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _coerce_env_value(raw: str) -> Any:
    """Convert an env-var string to bool/int/float when it parses cleanly."""

    lowered = raw.strip().lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Layer ``TREE_<SECTION>__<KEY>...`` env vars on top of the YAML dict.

    Only overrides paths under ``extraction.resolution.*`` and
    ``extraction.dedup.*``. Other config sections stay YAML-driven.

    Reads ``TREE_EXTRACTION__RESOLUTION__TYPE_STRICT`` style keys, splitting
    on ``__`` and lower-casing each segment. Missing intermediate dicts are
    created on demand.
    """

    prefix = "TREE_"
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path = [seg.lower() for seg in env_key[len(prefix) :].split("__") if seg]
        if not path:
            continue
        # Restrict to the extraction.* subtree we care about.
        if path[0] != "extraction" or len(path) < 2:
            continue
        cursor: dict[str, Any] = raw
        for segment in path[:-1]:
            existing = cursor.get(segment)
            if not isinstance(existing, dict):
                existing = {}
                cursor[segment] = existing
            cursor = existing
        cursor[path[-1]] = _coerce_env_value(env_value)
    return raw


def load_app_config(path: str | Path | None = None) -> AppConfig:
    """Load application config from a YAML file.

    Args:
        path: Explicit path to a YAML file. Falls back to APP_CONFIG_PATH
              env var, then configs/default.yaml.

    ``TREE_EXTRACTION__RESOLUTION__*`` and ``TREE_EXTRACTION__DEDUP__*`` env
    vars override the corresponding YAML keys; this lets operators flip a
    single dedup/resolution knob without editing the YAML and ensures the
    cross-key validator on :class:`ExtractionConfig` sees the actual runtime
    values.
    """

    config_path = Path(path or os.environ.get("APP_CONFIG_PATH", _DEFAULT_CONFIG_PATH))

    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    raw = _apply_env_overrides(raw)
    config = AppConfig.model_validate(raw)
    return config


app_config = load_app_config()
