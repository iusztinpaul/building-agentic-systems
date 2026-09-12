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
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    :func:`tree.memory.rag.indexing.assert_settings_match_live_vector_index`
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
    :class:`tree.memory.graph.dedup.DeduplicationConfig` field-for-field
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
    # Chunking knobs live under ``memory.chunking`` (ADR-006 decision 6): the
    # two-level splitter is mode-independent, so a chunk size under
    # ``extraction`` would read as graph-only. The old
    # ``chunk_size``/``chunk_overlap`` pair went out with their only consumer.
    llm_concurrency: int = 5
    # Intra-run fan-out knobs (#054). Both inherit the ``TREE_EXTRACTION__*``
    # override hatch via :func:`_apply_env_overrides`.
    # ``doc_concurrency`` — how many documents one extraction run processes in
    # parallel (default 1 = serial; the cross-run fan-out is the
    # ``memory-extract-etl-coordinator`` dispatching worker shards, see ADR-002).
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
    * ``cron`` — the schedule attached to the ``dream-consolidation-all-users``
      deployment (a core spec in ``orchestrator.py`` since
      ``free-tier-deployments``), so editing it here moves the nightly sweep.
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


class PrefectConfig(BaseModel):
    """Prefect deployment-topology knobs.

    * ``deploy_optional`` — register the deployments marked ``optional=True`` in
      ``orchestrator.py`` on top of the always-on core ones. Prefect Cloud's **free
      tier caps a workspace at 5 deployments** and the core set now spends ALL 5
      (the two workers, both end-to-end pipelines, and the scheduled dream
      consolidation), so NO spec is optional today and this flag registers nothing
      extra — it is kept as the extension point for the next deployment that must
      sit outside the budget. Defaults to ``false``; flip it to ``true`` on a paid
      plan or a self-hosted Prefect server. Both the local serve
      (``make memory-serve-workflows``) and the Cloud deploy path honour it.
      Override per-environment without editing YAML via
      ``TREE_PREFECT__DEPLOY_OPTIONAL=true``. (No flag for the online ingest
      path: ``online-pipeline`` is one of the core deployments, so
      ``dispatch_online_pipeline`` always submits it — there is no in-process
      fallback, and a missing deployment is an error, not a mode.)
    """

    deploy_optional: bool = False


class YouTubeConfig(BaseModel):
    """Static tuning for the YouTube ETL's Bright Data collection (ADR-004).

    Two timing knobs only — credential PRESENCE is the backend switch, so
    there is no ``enabled`` flag and no new env var. The dataset id and the
    Web Scraper API URLs are API identity, not tuning, so they stay module
    constants in ``tree.data.youtube.brightdata_transcript_fetcher`` /
    ``tree.data.web.web_scraper_api``.

    * ``brightdata_timeout_seconds`` — upper bound on the wait for ONE
      collection. A live probe measured ~173 s for a single video; 600 is the
      Bright Data CLI default. Hitting it is a fallback trigger (#092), not a
      task failure.
    * ``brightdata_poll_interval_seconds`` — delay between two ``/progress``
      reads.

    Lives here rather than on ``YouTubeVideoSource`` because ADR-003 made
    source entries operator DATA under ``sources/``; these are static app
    tuning, like ``concurrency:``.
    """

    brightdata_timeout_seconds: float = 600.0
    brightdata_poll_interval_seconds: float = 10.0


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


class MCPConfig(BaseModel):
    max_retries: int = 1
    max_results: int = 10


class ChunkLevelConfig(BaseModel):
    """Token budget for ONE chunking level — a parent or a child (ADR-006 §6).

    ``size`` and ``overlap`` are counted in tiktoken ``cl100k_base`` tokens (the
    encoder :mod:`tree.memory.rag.chunking` bounds every chunk with), never in
    characters. ``overlap`` is the number of trailing tokens of a chunk that are
    repeated at the head of the next chunk at the SAME level, so a sentence cut
    by a boundary is still whole in one of the two chunks.

    ``overlap < size`` is a hard invariant: at ``overlap >= size`` a chunk would
    carry over everything it just emitted and the splitter would stop making
    forward progress. Good: ``size=256, overlap=32`` (12% carry-over). Bad:
    ``size=256, overlap=256``.
    """

    size: int = Field(gt=0, description="Chunk budget in cl100k_base tokens.")
    overlap: int = Field(
        default=0,
        ge=0,
        description=(
            "Tokens of the previous chunk repeated at the head of the next "
            "chunk at the same level. Must be < size."
        ),
    )

    @model_validator(mode="after")
    def _check_overlap_below_size(self) -> "ChunkLevelConfig":
        if self.overlap >= self.size:
            raise ValueError(
                "Misconfigured chunking level: overlap must be smaller than "
                f"size. Found size={self.size}, overlap={self.overlap}."
            )
        return self


class ChunkingConfig(BaseModel):
    """Two-level (parent/child) chunking knobs for the memory pipeline (ADR-006 §6).

    ``strategy`` picks the SAME splitting algorithm for both levels:

    * ``fixed_tokens`` — the Chapter-4 sliding token window.
    * ``recursive`` — markdown headings -> blank-line paragraphs -> sentences ->
      raw tokens, greedily merged up to ``size`` (the default; it keeps a
      section's prose together and gives every parent a ``heading_path``).

    ``parent`` chunks are the retrieval + LLM-extraction unit (never embedded);
    ``child`` chunks are the embedded search unit. ``child.size < parent.size``
    is a hard invariant — a child at least as large as its parent would make the
    parent level pure overhead (one child per parent, no fan-out) and defeat
    parent-document retrieval.
    """

    strategy: Literal["fixed_tokens", "recursive"] = "recursive"
    parent: ChunkLevelConfig = ChunkLevelConfig(size=4096, overlap=0)
    child: ChunkLevelConfig = ChunkLevelConfig(size=256, overlap=32)

    @model_validator(mode="after")
    def _check_child_smaller_than_parent(self) -> "ChunkingConfig":
        if self.child.size >= self.parent.size:
            raise ValueError(
                "Misconfigured chunking: memory.chunking.child.size must be "
                "smaller than memory.chunking.parent.size. Found "
                f"child.size={self.child.size}, "
                f"parent.size={self.parent.size}."
            )
        return self


class UmapConfig(BaseModel):
    """UMAP knobs, shared by BOTH fits of a **Clustering run** (ADR-007 §1).

    Fit A reduces the 1024-d embeddings to ``n_components`` dimensions and is
    what HDBSCAN clusters on; fit B reduces the SAME raw embeddings to 2-D for
    display only. One block drives both so the picture and the clustering can
    never drift apart on ``metric`` or ``random_state``.
    """

    n_neighbors: int = Field(
        default=15,
        ge=2,
        description=(
            "Local neighbourhood size UMAP balances against global structure. "
            "Clamped down to n-1 at fit time on corpora smaller than this."
        ),
    )
    min_dist: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum distance between points in the embedded space. 0.0 packs "
            "the tightest clumps, which is what a density clusterer wants "
            "(the BERTopic default for the clustering fit)."
        ),
    )
    metric: Literal["cosine", "euclidean"] = Field(
        default="cosine",
        description=(
            "Distance metric on the input embeddings. Voyage embeddings are "
            "cosine-normalised, so 'cosine' is the matching default."
        ),
    )
    n_components: int = Field(
        default=5,
        ge=2,
        description=(
            "Dimensionality of the intermediate space HDBSCAN clusters in. "
            "A handful of dimensions restores the density contrast that 1024-d "
            "cosine space loses; display is always a SEPARATE 2-D fit."
        ),
    )
    random_state: int = Field(
        default=42,
        description=(
            "One seed for both UMAP fits and the sampling RNG, so a re-run on "
            "an unchanged corpus reproduces the same map and the same evidence."
        ),
    )


class HdbscanConfig(BaseModel):
    """``sklearn.cluster.HDBSCAN`` knobs (ADR-007 §1).

    Runs on the ``n_components``-d UMAP intermediate, never on the 2-D display
    projection: clusters read off a 2-D picture are artefacts of the projection
    rather than of the data.
    """

    min_cluster_size: int = Field(
        default=15,
        ge=2,
        description=(
            "Fewest **Child chunk**s that may form a **Memory cluster**. "
            "Everything smaller becomes noise (cluster_id -1). Lower it for a "
            "small corpus, e.g. "
            "TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5."
        ),
    )
    min_samples: int | None = Field(
        default=None,
        ge=1,
        description=(
            "How conservative the density estimate is; higher declares more "
            "points noise. null = min_cluster_size (the scikit-learn default)."
        ),
    )


class ClusterSamplingConfig(BaseModel):
    """How many member chunks the summariser shows the LLM (ADR-007 §4).

    A cluster of 4,000 chunks is summarised from at most ``nearest + random``
    of them, so one LLM call per cluster stays bounded regardless of size.
    """

    nearest: int = Field(
        default=10,
        ge=0,
        description=(
            "Chunks nearest the cluster centroid by cosine distance in the "
            "ORIGINAL 1024-d embedding space (the most typical members)."
        ),
    )
    random: int = Field(
        default=10,
        ge=0,
        description=(
            "Seeded random chunks drawn from the remaining members, so the "
            "sample also shows the cluster's spread — not just its core."
        ),
    )

    @model_validator(mode="after")
    def _check_at_least_one_sample(self) -> "ClusterSamplingConfig":
        """A summary needs evidence: ``nearest + random == 0`` would ask the LLM
        to label a cluster from an empty sample."""

        if self.nearest + self.random < 1:
            raise ValueError(
                "Misconfigured clustering: memory.clustering.sampling.nearest + "
                "memory.clustering.sampling.random must be at least 1. Found "
                f"nearest={self.nearest}, random={self.random}."
            )
        return self


class ClusterSummariesConfig(BaseModel):
    """Per-cluster LLM summarisation knobs (ADR-007 §4)."""

    llm_concurrency: int = Field(
        default=5,
        ge=1,
        description=(
            "Concurrent per-cluster LLM calls (one call per cluster), bounded "
            "by its own semaphore. Separate from extraction.llm_concurrency: "
            "clustering is mode-orthogonal, extraction is graph-only."
        ),
    )


class ClusteringConfig(BaseModel):
    """The BERTopic-shaped clustering recipe (ADR-007 §1).

    UMAP to a low-dimensional intermediate -> HDBSCAN there -> a separate UMAP
    to 2-D for display -> one LLM summary per cluster. Every number is a knob;
    there is deliberately NO automatic parameter search.

    ``extra="forbid"`` is load-bearing: the ON/OFF switch is the
    ``run_clustering`` flow parameter of ``offline_pipeline`` (ADR-007 §5), so a
    ``memory.clustering.enabled`` key must fail at boot rather than let the YAML
    say "on" while the cron says "off".
    """

    model_config = ConfigDict(extra="forbid")

    umap: UmapConfig = Field(
        default_factory=UmapConfig,
        description="UMAP knobs, shared by the clustering fit and the 2-D display fit.",
    )
    hdbscan: HdbscanConfig = Field(
        default_factory=HdbscanConfig,
        description="HDBSCAN knobs, applied on the UMAP intermediate space.",
    )
    sampling: ClusterSamplingConfig = Field(
        default_factory=ClusterSamplingConfig,
        description="How many member chunks each cluster summary is written from.",
    )
    summaries: ClusterSummariesConfig = Field(
        default_factory=ClusterSummariesConfig,
        description="Per-cluster LLM summarisation knobs.",
    )


class MemoryConfig(BaseModel):
    """The ONE memory-mode switch (ADR-006 decision 5).

    ``mode`` selects how much machinery the memory half of the system runs:

    * ``rag`` — clean -> two-level chunk -> embed children -> load rows, plus
      parent-document hybrid retrieval. Node rows only: no edges, no LLM
      entity extraction. (The Chapter-4 system.)
    * ``graphrag`` — the rag stages PLUS structural edges, LLM entity
      extraction over parent chunks, resolution, dedup, ``mentions`` edges and
      graph expansion at retrieval. (What Chapter 8 adds.)

    Defaults to ``graphrag`` so an unchanged checkout behaves exactly as it did
    before ADR-006. Read ONCE at flow entry / MCP-server boot / CLI start —
    never per request. Operators flip it without editing YAML through the
    existing override hatch (``TREE_MEMORY__MODE=rag``, see
    :func:`_apply_env_overrides`); an unknown value is a hard
    ``ValidationError`` at load time, never a silent fallback.

    Both modes write the SAME ``memory`` collection
    (:data:`tree.entities.memory.MEMORY_COLLECTION`) and both start from
    scratch — there is no migration between them.

    ``chunking`` is mode-independent: BOTH modes split documents into parent and
    child chunks with the same knobs (ADR-006 §6), so chunk rows are
    byte-identical across modes. So is ``clustering`` (ADR-007): a **Clustering
    run** reads the same child chunk rows and writes the same
    ``memory_clusters`` rows in both modes.
    """

    mode: Literal["rag", "graphrag"] = "graphrag"
    chunking: ChunkingConfig = ChunkingConfig()
    clustering: ClusteringConfig = ClusteringConfig()


class AppConfig(BaseModel):
    memory: MemoryConfig = MemoryConfig()
    models: ModelsConfig = ModelsConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    query: QueryConfig = QueryConfig()
    mcp: MCPConfig = MCPConfig()
    dream: DreamConfig = DreamConfig()
    concurrency: ConcurrencyConfig = ConcurrencyConfig()
    prefect: PrefectConfig = PrefectConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    youtube: YouTubeConfig = YouTubeConfig()


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

    The operator escape hatch documented in CLAUDE.md: ANY YAML key can be
    overridden per-environment, e.g. ``TREE_PREFECT__DEPLOY_OPTIONAL=true`` or
    ``TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99``.

    Reads ``TREE_<SECTION>__<KEY>`` style keys, splitting on ``__`` and
    lower-casing each segment. Missing intermediate dicts are created on
    demand. Single-segment keys (no ``__``) are ignored — they are plain
    settings-layer env vars (``TREE_USER_IDENTIFIER``, ``TREE_WORKING_DIR``),
    not YAML overrides.
    """

    prefix = "TREE_"
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path = [seg.lower() for seg in env_key[len(prefix) :].split("__") if seg]
        if len(path) < 2:
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

    ``TREE_<SECTION>__<KEY>`` env vars override the corresponding YAML keys;
    this lets operators flip a single knob (e.g.
    ``TREE_PREFECT__DEPLOY_OPTIONAL``) without editing the YAML and ensures
    cross-key validators (e.g. on :class:`ExtractionConfig`) see the actual
    runtime values.
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
