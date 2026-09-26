import datetime
import json
import textwrap

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from tree.config.app_config import (
    AppConfig,
    ChunkingConfig,
    ChunkLevelConfig,
    ClusteringConfig,
    ClusterSamplingConfig,
    ClusterSummariesConfig,
    ConcurrencyConfig,
    DreamConfig,
    EmbeddingConfig,
    HdbscanConfig,
    MemoryConfig,
    MODAL_NAME_PREFIX,
    ModalConfig,
    ModalEmbeddingModelConfig,
    ModalLLMModelConfig,
    ObservabilityConfig,
    QueryConfig,
    UmapConfig,
    YouTubeConfig,
    _DEFAULT_CONFIG_PATH,
    load_app_config,
)
from tree.config.sources import (
    HuggingFaceDatasetSource,
    SourceEntry,
)

# A minimal VALID Embedding catalog entry. Each rejection case below mutates
# exactly one key of it, so the test names the mistake, not the boilerplate.
_VALID_ENTRY = {
    "repo_id": "voyageai/voyage-4-nano",
    # 2048 = the real native width of this repo_id (see ::test_seed_entries),
    # so the fixture never contradicts the shipped catalog it borrows its id from.
    "native_dimensions": 2048,
}

_VALID_LLM_ENTRY = {"repo_id": "LiquidAI/LFM2.5-350M"}


class TestLoadAppConfig:
    def test_loads_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.models.llm.provider == "gemini"
        assert config.models.llm.model == "gemini-3.1-flash-lite"
        # #048 flipped the default from the multimodal ``voyage-multimodal-3``
        # to a TEXT model (routed to /v1/embeddings); ADR-009 then pinned the
        # current ``voyage-4``, still 1024-d, so the dim never moved. #039
        # split the single ``embedding`` block into a transient
        # ``resolution_embedding`` and a persisted ``search_embedding``; both
        # point at the same model/dim.
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-4"
        assert config.models.resolution_embedding.dimensions == 1024
        assert config.models.search_embedding.provider == "voyage"
        assert config.models.search_embedding.model == "voyage-4"
        assert config.models.search_embedding.dimensions == 1024
        # #044: real-time request-batching caps. #054/ADR-002 dropped
        # max_total_tokens 320_000 → 10_000 (the shared free-tier Voyage TPM
        # window) and added dispatch_concurrency.
        assert config.models.embedding_batch.max_inputs == 1000
        assert config.models.embedding_batch.max_total_tokens == 10_000
        assert config.models.embedding_batch.max_input_tokens == 32_000
        assert config.models.embedding_batch.dispatch_concurrency == 1
        assert config.extraction.llm_concurrency == 5
        # #054: intra-run fan-out knobs.
        assert config.extraction.doc_concurrency == 1
        assert config.extraction.dedup_concurrency == 8

    def test_embedding_batch_defaults_when_absent(self, tmp_path):
        """A YAML with no ``models.embedding_batch`` block falls back to the
        typed defaults — #044, with the #054/ADR-002 max_total_tokens drop."""

        custom = tmp_path / "no_batch.yaml"
        custom.write_text(
            "models:\n"
            "  search_embedding:\n"
            "    provider: voyage\n"
            "    model: voyage-multimodal-3\n"
            "    dimensions: 1024\n"
        )

        config = load_app_config(custom)

        assert config.models.embedding_batch.max_inputs == 1000
        assert config.models.embedding_batch.max_total_tokens == 10_000
        assert config.models.embedding_batch.max_input_tokens == 32_000
        assert config.models.embedding_batch.dispatch_concurrency == 1

    def test_embedding_batch_caps_loaded_from_yaml(self, tmp_path):
        """Operator-tuned batching caps in YAML are read into the typed
        :class:`EmbeddingBatchConfig` (#044)."""

        custom = tmp_path / "batch.yaml"
        custom.write_text(
            "models:\n"
            "  embedding_batch:\n"
            "    max_inputs: 128\n"
            "    max_total_tokens: 50000\n"
            "    max_input_tokens: 8000\n"
        )

        config = load_app_config(custom)

        assert config.models.embedding_batch.max_inputs == 128
        assert config.models.embedding_batch.max_total_tokens == 50_000
        assert config.models.embedding_batch.max_input_tokens == 8000

    def test_dream_block_loaded_from_default_yaml(self, frozen_config_path):
        """The #051 ``dream:`` block is read into the typed :class:`DreamConfig`.

        Thresholds are NOT duplicated here — they stay in
        ``extraction.dedup``; ``DreamConfig`` carries no threshold field.
        """

        config = load_app_config(frozen_config_path)

        assert config.dream.enabled is True
        assert config.dream.cron == "0 4 * * *"
        # dry_run: true (report-only rollout), agreeing with the safer Pydantic
        # model default.
        assert config.dream.dry_run is True
        assert config.dream.max_pairs == 10_000
        assert config.dream.enable_supersession_judge is False

    def test_prefect_deploy_optional_false_in_default_yaml(self, frozen_config_path):
        """``prefect.deploy_optional`` is false in default.yaml (free-tier safe)."""

        config = load_app_config(frozen_config_path)

        assert config.prefect.deploy_optional is False

    def test_prefect_deploy_optional_defaults_false_when_absent(self, tmp_path):
        custom = tmp_path / "no_prefect.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.prefect.deploy_optional is False

    def test_prefect_deploy_optional_loaded_from_yaml(self, tmp_path):
        """An operator can opt into the optional deployments via YAML."""

        custom = tmp_path / "prefect.yaml"
        custom.write_text("prefect:\n  deploy_optional: true\n")

        config = load_app_config(custom)

        assert config.prefect.deploy_optional is True

    def test_prefect_deploy_optional_env_override(self, tmp_path, monkeypatch):
        """``TREE_PREFECT__DEPLOY_OPTIONAL`` overrides YAML — the documented
        escape hatch reaches sections beyond ``extraction.*`` (it was silently
        extraction-only before, contradicting its own docs)."""

        custom = tmp_path / "prefect.yaml"
        custom.write_text("prefect:\n  deploy_optional: false\n")
        monkeypatch.setenv("TREE_PREFECT__DEPLOY_OPTIONAL", "true")

        config = load_app_config(custom)

        assert config.prefect.deploy_optional is True

    def test_single_segment_tree_env_vars_are_not_overrides(
        self, tmp_path, monkeypatch
    ):
        """``TREE_USER_IDENTIFIER``-style settings vars (no ``__``) must not
        leak into the YAML dict as top-level keys."""

        custom = tmp_path / "empty.yaml"
        custom.write_text("query:\n  top_k: 5\n")
        monkeypatch.setenv("TREE_USER_IDENTIFIER", "someone@example.com")

        config = load_app_config(custom)

        assert not hasattr(config, "user_identifier")

    def test_youtube_block_loaded_from_default_yaml(self, frozen_config_path):
        """The #091 ``youtube:`` block drives the Bright Data collection wait."""

        config = load_app_config(frozen_config_path)

        assert config.youtube.brightdata_timeout_seconds == 600.0
        assert config.youtube.brightdata_poll_interval_seconds == 10.0

    def test_youtube_defaults_when_absent(self, tmp_path):
        custom = tmp_path / "no_youtube.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.youtube == YouTubeConfig()
        assert config.youtube.brightdata_timeout_seconds == 600.0
        assert config.youtube.brightdata_poll_interval_seconds == 10.0

    def test_youtube_timing_knobs_loaded_from_yaml(self, tmp_path):
        """An operator can retune the collection wait without touching code."""

        custom = tmp_path / "youtube.yaml"
        custom.write_text(
            "youtube:\n"
            "  brightdata_timeout_seconds: 90\n"
            "  brightdata_poll_interval_seconds: 3\n"
        )

        config = load_app_config(custom)

        assert config.youtube.brightdata_timeout_seconds == 90.0
        assert config.youtube.brightdata_poll_interval_seconds == 3.0

    def test_dream_defaults_when_absent(self, tmp_path):
        custom = tmp_path / "no_dream.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.dream == DreamConfig()
        assert config.dream.enabled is True
        assert config.dream.dry_run is True  # safe model default
        assert config.dream.max_pairs == 10_000
        assert config.dream.enable_supersession_judge is False

    def test_dream_block_loaded_from_custom_yaml(self, tmp_path):
        custom = tmp_path / "dream.yaml"
        custom.write_text(
            "dream:\n"
            "  enabled: false\n"
            '  cron: "30 2 * * *"\n'
            "  dry_run: true\n"
            "  max_pairs: 42\n"
            "  enable_supersession_judge: true\n"
        )

        config = load_app_config(custom)

        assert config.dream.enabled is False
        assert config.dream.cron == "30 2 * * *"
        assert config.dream.dry_run is True
        assert config.dream.max_pairs == 42
        assert config.dream.enable_supersession_judge is True

    def test_app_config_has_no_sources_field(self):
        """``AppConfig`` is static memory config only — sources moved out to the
        repo-root ``sources/`` files (ADR-003), so the field is gone."""

        assert "sources" not in AppConfig.model_fields

    def test_load_app_config_ignores_a_legacy_sources_block(self, tmp_path):
        """A YAML that still carries a top-level ``sources:`` block loads green —
        the loader no longer eagerly validates it, and no ``sources`` attribute
        is materialised on the config."""

        custom = tmp_path / "with_sources.yaml"
        custom.write_text(
            textwrap.dedent("""\
                sources:
                  - uri: https://example.substack.com/feed
                    type: substack_rss
                query:
                  top_k: 5
            """)
        )

        config = load_app_config(custom)

        assert config.query.top_k == 5
        assert not hasattr(config, "sources")

    def test_loads_custom_yaml(self, tmp_path):
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            textwrap.dedent("""\
                models:
                  llm:
                    model: gemini-2.0-flash
                extraction:
                  llm_concurrency: 9
                  resolution:
                    fuzzy_threshold: 0.9
            """)
        )

        config = load_app_config(custom)

        assert config.models.llm.model == "gemini-2.0-flash"
        assert config.models.llm.provider == "gemini"
        assert config.extraction.llm_concurrency == 9
        assert config.extraction.resolution.fuzzy_threshold == 0.9
        assert config.extraction.doc_concurrency == 1
        # Embedding dimensions fall back to the plain Pydantic default
        # (1024) when the custom YAML doesn't override them (#034/#039).
        assert config.models.resolution_embedding.dimensions == 1024
        assert config.models.search_embedding.dimensions == 1024

    def test_search_embedding_only_defaults_resolution_embedding(self, tmp_path):
        """A YAML that sets only ``search_embedding`` loads cleanly with
        ``resolution_embedding`` falling back to the ``EmbeddingConfig``
        defaults (#039). Neither block is required; both default
        independently.
        """

        custom = tmp_path / "search_only.yaml"
        custom.write_text(
            textwrap.dedent("""\
                models:
                  search_embedding:
                    provider: voyage
                    model: voyage-multimodal-3
                    dimensions: 1024
            """)
        )

        config = load_app_config(custom)

        # search_embedding takes the explicit YAML values.
        assert config.models.search_embedding.provider == "voyage"
        assert config.models.search_embedding.model == "voyage-multimodal-3"
        assert config.models.search_embedding.dimensions == 1024
        # resolution_embedding falls back to the EmbeddingConfig defaults
        # (ADR-009 pinned the code-level default to the text ``voyage-4``).
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-4"
        assert config.models.resolution_embedding.dimensions == 1024

    def test_missing_file_returns_defaults(self, tmp_path):
        config = load_app_config(tmp_path / "nonexistent.yaml")

        assert config == AppConfig()

    def test_empty_yaml_returns_defaults(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("")

        config = load_app_config(empty)

        assert config == AppConfig()

    def test_env_var_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "env.yaml"
        custom.write_text("extraction:\n  llm_concurrency: 11\n")
        monkeypatch.setenv("APP_CONFIG_PATH", str(custom))

        config = load_app_config()

        assert config.extraction.llm_concurrency == 11


class TestConcurrencyConfig:
    """#054 / ADR-002: the top-level ``concurrency:`` block."""

    def test_concurrency_block_loaded_from_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.concurrency.voyage_rpm == 3
        assert config.concurrency.voyage_tpm == 10_000
        assert config.concurrency.runner_global_limit == 4

    def test_concurrency_defaults_when_absent(self, tmp_path):
        custom = tmp_path / "no_concurrency.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.concurrency == ConcurrencyConfig()
        assert config.concurrency.voyage_rpm == 3
        assert config.concurrency.voyage_tpm == 10_000
        assert config.concurrency.runner_global_limit == 4

    def test_concurrency_block_loaded_from_custom_yaml(self, tmp_path):
        custom = tmp_path / "concurrency.yaml"
        custom.write_text(
            "concurrency:\n"
            "  voyage_rpm: 60\n"
            "  voyage_tpm: 1000000\n"
            "  runner_global_limit: 8\n"
        )

        config = load_app_config(custom)

        assert config.concurrency.voyage_rpm == 60
        assert config.concurrency.voyage_tpm == 1_000_000
        assert config.concurrency.runner_global_limit == 8


class TestHuggingFaceWindowFields:
    """#070: the HF offset-window fields ``num_workers`` (YAML-authored fan-out
    width) and ``offset`` (dispatch-time runtime coordinate, never in YAML)."""

    def test_defaults_preserve_todays_behavior(self):
        """A HF source built with only a ``uri`` has ``num_workers == 1`` and
        ``offset is None`` — a single whole-``max_samples`` window, no skip."""

        entry = HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot")

        assert entry.num_workers == 1
        assert entry.offset is None

    def test_explicit_values_are_carried(self):
        """An explicitly-constructed HF source carries the exact authored
        fan-out width and runtime offset."""

        entry = HuggingFaceDatasetSource(
            uri="librarian-bots/arxiv-metadata-snapshot",
            num_workers=4,
            offset=500,
        )

        assert entry.num_workers == 4
        assert entry.offset == 500

    def test_num_workers_must_be_at_least_one(self):
        """``num_workers`` is a fan-out width; values below 1 are rejected."""

        with pytest.raises(ValidationError):
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot", num_workers=0
            )

    def test_offset_defaults_to_none_in_yaml_authored_entry(self):
        """An operator may author ``num_workers`` in YAML; ``offset`` stays
        ``None`` because it is never authored — only set at dispatch."""

        entry = HuggingFaceDatasetSource.model_validate(
            {
                "type": "huggingface_dataset",
                "uri": "librarian-bots/arxiv-metadata-snapshot",
                "num_workers": 4,
            }
        )

        assert entry.num_workers == 4
        assert entry.offset is None

    def test_runtime_offset_set_via_model_copy(self):
        """#072 sets ``offset`` ONLY at dispatch via
        ``entry.model_copy(update={"offset": ...})`` — the authored entry is
        left untouched."""

        authored = HuggingFaceDatasetSource(
            uri="librarian-bots/arxiv-metadata-snapshot", num_workers=4
        )

        dispatched = authored.model_copy(update={"offset": 250})

        assert authored.offset is None
        assert dispatched.offset == 250
        assert dispatched.num_workers == 4

    def test_discriminated_union_round_trip_preserves_window_fields(self):
        """``model_dump()`` → JSON → ``TypeAdapter(list[SourceEntry])`` preserves
        the new window fields — the round-trip the coordinator dispatches
        through ``run_deployment`` flow-run params. Covers both the ``None``
        and a set-int offset case."""

        adapter: TypeAdapter[list[SourceEntry]] = TypeAdapter(list[SourceEntry])
        entries = [
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=1000,
                fetch_content=True,
                batch_size=64,
                concurrency=8,
                num_workers=4,
                offset=None,
            ),
            HuggingFaceDatasetSource(
                uri="librarian-bots/arxiv-metadata-snapshot",
                max_samples=1000,
                num_workers=4,
                offset=250,
            ),
        ]

        serialized = json.loads(json.dumps([e.model_dump() for e in entries]))
        reparsed = adapter.validate_python(serialized)

        assert all(isinstance(e, HuggingFaceDatasetSource) for e in reparsed)

        first = reparsed[0]
        assert first.type == "huggingface_dataset"
        assert first.uri == "librarian-bots/arxiv-metadata-snapshot"
        assert first.max_samples == 1000
        assert first.fetch_content is True
        assert first.batch_size == 64
        assert first.concurrency == 8
        assert first.num_workers == 4
        assert first.offset is None

        second = reparsed[1]
        assert second.num_workers == 4
        assert second.offset == 250


class TestRunnerGlobalLimitBump:
    """#070: ``runner_global_limit`` is raised 4→6 in ``default.yaml`` ONLY; the
    typed default on :class:`ConcurrencyConfig` stays at 4."""

    def test_default_yaml_raises_runner_global_limit_to_six(self):
        """The real, human-tuned ``configs/default.yaml`` admits up to 6 runs."""

        config = load_app_config(_DEFAULT_CONFIG_PATH)

        assert config.concurrency.runner_global_limit == 6

    def test_typed_default_runner_global_limit_unchanged(self):
        """A bare ``ConcurrencyConfig()`` (no YAML) still reports 4 — the bump is
        YAML-only, so configs that omit the block are unchanged."""

        assert ConcurrencyConfig().runner_global_limit == 4


class TestExtractionConcurrencyKnobs:
    """#054: the new intra-run fan-out knobs on ``extraction`` +
    ``models.embedding_batch``, including the env-override hatch."""

    def test_extraction_fanout_knobs_loaded_from_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.extraction.doc_concurrency == 1
        assert config.extraction.dedup_concurrency == 8

    def test_dispatch_concurrency_loaded_from_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.models.embedding_batch.dispatch_concurrency == 1
        assert config.models.embedding_batch.max_total_tokens == 10_000

    def test_dedup_concurrency_env_override(self, tmp_path, monkeypatch):
        """``TREE_EXTRACTION__DEDUP_CONCURRENCY=4`` overrides the YAML default
        (8) — proves the existing override hatch reaches the new knob."""

        custom = tmp_path / "extraction.yaml"
        custom.write_text("extraction:\n  dedup_concurrency: 8\n")
        monkeypatch.setenv("TREE_EXTRACTION__DEDUP_CONCURRENCY", "4")

        config = load_app_config(custom)

        assert config.extraction.dedup_concurrency == 4

    def test_doc_concurrency_env_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "extraction.yaml"
        custom.write_text("extraction:\n  doc_concurrency: 1\n")
        monkeypatch.setenv("TREE_EXTRACTION__DOC_CONCURRENCY", "3")

        config = load_app_config(custom)

        assert config.extraction.doc_concurrency == 3


class TestMemoryModeConfig:
    """ADR-006 §5 / #105: the ONE ``memory.mode`` switch (``rag | graphrag``).

    Nothing branches on it yet — these tests pin that it is *readable*, that an
    unchanged checkout keeps today's behaviour (``graphrag``), and that the
    existing ``TREE_<SECTION>__<KEY>`` hatch reaches it with no new mechanism.
    """

    def test_memory_mode_is_graphrag_in_frozen_config(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.memory.mode == "graphrag"

    def test_memory_mode_is_graphrag_in_default_yaml(self):
        """The real, human-tuned ``configs/default.yaml`` ships the graph mode,
        so an unchanged checkout behaves exactly as before ADR-006."""

        config = load_app_config(_DEFAULT_CONFIG_PATH)

        assert config.memory.mode == "graphrag"

    def test_memory_mode_defaults_to_graphrag_when_section_absent(self, tmp_path):
        custom = tmp_path / "no_memory.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.memory.mode == "graphrag"

    def test_typed_default_memory_mode_is_graphrag(self):
        assert MemoryConfig().mode == "graphrag"

    def test_memory_mode_rag_loaded_from_yaml(self, tmp_path):
        custom = tmp_path / "memory.yaml"
        custom.write_text("memory:\n  mode: rag\n")

        config = load_app_config(custom)

        assert config.memory.mode == "rag"

    def test_memory_mode_env_override_selects_rag(self, tmp_path, monkeypatch):
        """``TREE_MEMORY__MODE=rag`` flips the mode without editing YAML."""

        custom = tmp_path / "memory.yaml"
        custom.write_text("memory:\n  mode: graphrag\n")
        monkeypatch.setenv("TREE_MEMORY__MODE", "rag")

        config = load_app_config(custom)

        assert config.memory.mode == "rag"

    def test_unknown_memory_mode_raises_naming_both_allowed_values(
        self, tmp_path, monkeypatch
    ):
        """A mistyped mode fails the load loudly and tells the operator the two
        allowed values, rather than silently falling back to a default."""

        custom = tmp_path / "memory.yaml"
        custom.write_text("memory:\n  mode: graphrag\n")
        monkeypatch.setenv("TREE_MEMORY__MODE", "hybrid")

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)

        message = str(excinfo.value)
        assert "memory" in message
        assert "mode" in message
        assert "'rag'" in message
        assert "'graphrag'" in message


class TestQueryConfig:
    """ADR-008 §3-4 / #125: ``query.min_vector_score``, the vector-leg bar.

    The ONE absolute score in retrieval (Atlas normalises cosine to
    ``(1 + cos) / 2``), so it is the only place a "nothing relevant" decision
    can sit. PROVISIONAL at 0.70 — re-pinned on voyage-4 by the two live
    queries in ``tasks/141``'s log (nonsense top=0.649, on-topic top=0.735, so
    the voyage-3.5 pin of 0.75 from ``tasks/125`` kept nothing on topic), and
    Chapter 7's evals own it from here. Which is exactly why it is a knob.
    """

    def test_min_vector_score_default_override_and_bounds(self, tmp_path, monkeypatch):
        # Default: the typed default, the frozen fixture and the real
        # configs/default.yaml all agree on the pinned 0.70.
        assert QueryConfig().min_vector_score == 0.70
        assert load_app_config(_DEFAULT_CONFIG_PATH).query.min_vector_score == 0.70

        # Override: an operator raises the bar for a noisy corpus with the same
        # TREE_<SECTION>__<KEY> hatch every other knob uses — no YAML edit.
        custom = tmp_path / "query.yaml"
        custom.write_text("query:\n  min_vector_score: 0.75\n")
        monkeypatch.setenv("TREE_QUERY__MIN_VECTOR_SCORE", "0.9")
        assert load_app_config(custom).query.min_vector_score == 0.9

        # Bounds: the score is a normalised similarity, so a value outside
        # [0, 1] is a typo that would silently gate EVERY hit away.
        monkeypatch.setenv("TREE_QUERY__MIN_VECTOR_SCORE", "1.5")
        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)
        assert "min_vector_score" in str(excinfo.value)

    def test_min_vector_score_loaded_from_frozen_config(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.query.min_vector_score == 0.70

    def test_min_vector_score_defaults_when_key_absent(self, tmp_path):
        """A YAML ``query:`` block written before #125 keeps the typed default
        instead of failing the load."""

        custom = tmp_path / "query.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        assert load_app_config(custom).query.min_vector_score == 0.70


class TestChunkingConfig:
    """ADR-006 §6 / #107: the two-level (parent/child) chunking knobs.

    Nothing wires the splitter into the pipeline yet (#108) — these tests pin
    that the block is READABLE from both YAML files, that its two cross-key
    invariants fail loudly, and that the existing ``TREE_<SECTION>__<KEY>``
    hatch reaches a nested level with no new mechanism.
    """

    def test_chunking_block_loaded_from_frozen_config(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.memory.chunking.strategy == "recursive"
        assert config.memory.chunking.parent.size == 4096
        assert config.memory.chunking.parent.overlap == 0
        assert config.memory.chunking.child.size == 256
        assert config.memory.chunking.child.overlap == 32

    def test_chunking_block_loaded_from_default_yaml(self):
        """The real, human-tuned ``configs/default.yaml`` ships the same block,
        so an unchanged checkout chunks the way ADR-006 describes."""

        config = load_app_config(_DEFAULT_CONFIG_PATH)

        assert config.memory.chunking.strategy == "recursive"
        assert config.memory.chunking.parent.size == 4096
        assert config.memory.chunking.child.size == 256
        assert config.memory.chunking.child.overlap == 32

    def test_chunking_defaults_when_section_absent(self, tmp_path):
        custom = tmp_path / "no_chunking.yaml"
        custom.write_text("memory:\n  mode: rag\n")

        config = load_app_config(custom)

        assert config.memory.chunking.strategy == "recursive"
        assert config.memory.chunking.parent == ChunkLevelConfig(size=4096, overlap=0)
        assert config.memory.chunking.child == ChunkLevelConfig(size=256, overlap=32)

    def test_typed_defaults_match_the_yaml(self):
        chunking = ChunkingConfig()

        assert chunking.strategy == "recursive"
        assert (chunking.parent.size, chunking.parent.overlap) == (4096, 0)
        assert (chunking.child.size, chunking.child.overlap) == (256, 32)

    def test_child_size_env_override(self, tmp_path, monkeypatch):
        """``TREE_MEMORY__CHUNKING__CHILD__SIZE=128`` reaches a THIRD-level key
        through the unchanged hatch — no new mechanism for nested blocks."""

        custom = tmp_path / "chunking.yaml"
        custom.write_text(
            "memory:\n"
            "  mode: graphrag\n"
            "  chunking:\n"
            "    strategy: recursive\n"
            "    parent:\n"
            "      size: 4096\n"
            "      overlap: 0\n"
            "    child:\n"
            "      size: 256\n"
            "      overlap: 32\n"
        )
        monkeypatch.setenv("TREE_MEMORY__CHUNKING__CHILD__SIZE", "128")

        config = load_app_config(custom)

        assert config.memory.chunking.child.size == 128
        # Untouched siblings keep their YAML values.
        assert config.memory.chunking.child.overlap == 32
        assert config.memory.chunking.parent.size == 4096

    def test_overlap_at_or_above_size_raises(self):
        with pytest.raises(ValidationError) as excinfo:
            ChunkingConfig(
                parent={"size": 4096, "overlap": 0},
                child={"size": 300, "overlap": 400},
            )

        message = str(excinfo.value)
        assert "overlap" in message
        assert "size" in message

    def test_child_size_at_or_above_parent_size_raises(self):
        with pytest.raises(ValidationError) as excinfo:
            ChunkingConfig(parent={"size": 200}, child={"size": 256})

        message = str(excinfo.value)
        assert "child.size" in message
        assert "parent.size" in message

    def test_child_size_env_override_above_parent_fails_the_load(
        self, tmp_path, monkeypatch
    ):
        """The operator story: ``TREE_MEMORY__CHUNKING__CHILD__SIZE=8192`` with a
        4096-token parent must refuse to boot, naming both keys."""

        custom = tmp_path / "chunking.yaml"
        custom.write_text("memory:\n  mode: graphrag\n")
        monkeypatch.setenv("TREE_MEMORY__CHUNKING__CHILD__SIZE", "8192")

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)

        message = str(excinfo.value)
        assert "child.size" in message
        assert "parent.size" in message

    @pytest.mark.parametrize("size", [0, -1])
    def test_non_positive_size_raises(self, size):
        with pytest.raises(ValidationError):
            ChunkLevelConfig(size=size)

    def test_negative_overlap_raises(self):
        with pytest.raises(ValidationError):
            ChunkLevelConfig(size=256, overlap=-1)

    def test_unknown_strategy_raises_naming_both_allowed_values(self):
        with pytest.raises(ValidationError) as excinfo:
            ChunkingConfig(strategy="semantic")

        message = str(excinfo.value)
        assert "'fixed_tokens'" in message
        assert "'recursive'" in message

    def test_extraction_chunk_knobs_are_gone(self, frozen_config_path):
        """#108 removed ``extraction.chunk_size``/``chunk_overlap`` together with
        their only consumer — chunking is configured under ``memory.chunking``."""

        config = load_app_config(frozen_config_path)

        assert not hasattr(config.extraction, "chunk_size")
        assert not hasattr(config.extraction, "chunk_overlap")
        assert config.memory.chunking.parent.size == 4096
        assert config.memory.chunking.child.size == 256


class TestClusteringConfig:
    """ADR-007 §1 / #115: the ``memory.clustering`` knobs.

    Nothing computes yet (#116) — these tests pin that the BERTopic-shaped
    recipe is READABLE from both YAML files with the numbers the ADR states,
    that every bound fails loudly, and that the section can never grow a silent
    ``enabled`` switch (the ON/OFF switch is the ``run_clustering`` flow
    parameter, ADR-007 §5).
    """

    def test_clustering_block_loaded_from_frozen_config(self, frozen_config_path):
        config = load_app_config(frozen_config_path)
        clustering = config.memory.clustering

        assert clustering.umap.n_neighbors == 15
        assert clustering.umap.min_dist == 0.0
        assert clustering.umap.metric == "cosine"
        assert clustering.umap.n_components == 5
        assert clustering.umap.random_state == 42
        assert clustering.hdbscan.min_cluster_size == 15
        assert clustering.hdbscan.min_samples is None
        assert (clustering.sampling.nearest, clustering.sampling.random) == (10, 10)
        assert clustering.summaries.llm_concurrency == 5

    def test_clustering_block_loaded_from_default_yaml(self):
        """The real, human-tuned ``configs/default.yaml`` ships the same recipe,
        so an unchanged checkout clusters the way ADR-007 §1 describes."""

        clustering = load_app_config(_DEFAULT_CONFIG_PATH).memory.clustering

        assert clustering.umap.n_components == 5
        assert clustering.umap.metric == "cosine"
        assert clustering.hdbscan.min_cluster_size == 15
        assert clustering.hdbscan.min_samples is None
        assert (clustering.sampling.nearest, clustering.sampling.random) == (10, 10)
        assert clustering.summaries.llm_concurrency == 5

    def test_clustering_defaults_when_section_absent(self, tmp_path):
        custom = tmp_path / "no_clustering.yaml"
        custom.write_text("memory:\n  mode: rag\n")

        clustering = load_app_config(custom).memory.clustering

        assert clustering == ClusteringConfig()
        assert clustering.umap.n_components == 5
        assert clustering.hdbscan.min_cluster_size == 15

    def test_typed_defaults_match_the_yaml(self):
        clustering = ClusteringConfig()

        assert (clustering.umap.n_neighbors, clustering.umap.min_dist) == (15, 0.0)
        assert clustering.umap.metric == "cosine"
        assert (clustering.umap.n_components, clustering.umap.random_state) == (5, 42)
        assert clustering.hdbscan.min_cluster_size == 15
        assert clustering.hdbscan.min_samples is None
        assert (clustering.sampling.nearest, clustering.sampling.random) == (10, 10)
        assert clustering.summaries.llm_concurrency == 5

    def test_min_cluster_size_env_override(self, tmp_path, monkeypatch):
        """Story 1: an operator loosens clustering for a small corpus with
        ``TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5``, no YAML edit."""

        custom = tmp_path / "clustering.yaml"
        custom.write_text("memory:\n  mode: graphrag\n")
        monkeypatch.setenv("TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE", "5")

        config = load_app_config(custom)

        assert config.memory.clustering.hdbscan.min_cluster_size == 5
        # Untouched siblings keep their defaults.
        assert config.memory.clustering.umap.n_components == 5
        assert config.memory.clustering.sampling.nearest == 10

    def test_unknown_umap_metric_raises_naming_both_allowed_values(
        self, tmp_path, monkeypatch
    ):
        custom = tmp_path / "clustering.yaml"
        custom.write_text("memory:\n  mode: graphrag\n")
        monkeypatch.setenv("TREE_MEMORY__CLUSTERING__UMAP__METRIC", "manhattan")

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)

        message = str(excinfo.value)
        assert "'cosine'" in message
        assert "'euclidean'" in message

    def test_an_enabled_key_is_a_hard_validation_error(self, tmp_path):
        """Story 2: ``memory.clustering.enabled`` must never become a second,
        contradicting switch — ``extra='forbid'`` rejects it at boot."""

        custom = tmp_path / "clustering.yaml"
        custom.write_text("memory:\n  clustering:\n    enabled: false\n")

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)

        assert "enabled" in str(excinfo.value)

    def test_an_enabled_env_override_is_a_hard_validation_error(
        self, tmp_path, monkeypatch
    ):
        custom = tmp_path / "clustering.yaml"
        custom.write_text("memory:\n  mode: graphrag\n")
        monkeypatch.setenv("TREE_MEMORY__CLUSTERING__ENABLED", "true")

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)

        assert "enabled" in str(excinfo.value)

    @pytest.mark.parametrize("n_neighbors", [1, 0, -1])
    def test_umap_n_neighbors_below_two_raises(self, n_neighbors):
        with pytest.raises(ValidationError):
            UmapConfig(n_neighbors=n_neighbors)

    @pytest.mark.parametrize("min_dist", [-0.1, 1.1])
    def test_umap_min_dist_outside_the_unit_interval_raises(self, min_dist):
        with pytest.raises(ValidationError):
            UmapConfig(min_dist=min_dist)

    def test_umap_n_components_below_two_raises(self):
        with pytest.raises(ValidationError):
            UmapConfig(n_components=1)

    def test_hdbscan_min_cluster_size_below_two_raises(self):
        with pytest.raises(ValidationError):
            HdbscanConfig(min_cluster_size=1)

    def test_hdbscan_min_samples_below_one_raises(self):
        with pytest.raises(ValidationError):
            HdbscanConfig(min_samples=0)

    def test_hdbscan_min_samples_none_is_allowed(self):
        """``null`` means "= min_cluster_size" (the sklearn default), which is
        what the shipped YAML says."""

        assert HdbscanConfig(min_samples=None).min_samples is None

    def test_sampling_with_no_chunks_at_all_raises(self):
        """A cluster summary needs at least one sampled chunk — ``0 + 0`` would
        send the LLM an empty evidence set."""

        with pytest.raises(ValidationError) as excinfo:
            ClusterSamplingConfig(nearest=0, random=0)

        message = str(excinfo.value)
        assert "nearest" in message
        assert "random" in message

    @pytest.mark.parametrize(
        "nearest,random_",
        [(0, 1), (1, 0), (20, 0)],
        ids=["random-only", "nearest-only", "nearest-only-20"],
    )
    def test_sampling_allows_one_empty_side(self, nearest, random_):
        sampling = ClusterSamplingConfig(nearest=nearest, random=random_)

        assert sampling.nearest + sampling.random >= 1

    @pytest.mark.parametrize("field", ["nearest", "random"])
    def test_negative_sampling_counts_raise(self, field):
        with pytest.raises(ValidationError):
            ClusterSamplingConfig(**{field: -1})

    def test_summaries_llm_concurrency_below_one_raises(self):
        with pytest.raises(ValidationError):
            ClusterSummariesConfig(llm_concurrency=0)

    @pytest.mark.parametrize(
        "model",
        [
            ClusteringConfig,
            UmapConfig,
            HdbscanConfig,
            ClusterSamplingConfig,
            ClusterSummariesConfig,
        ],
        ids=lambda model: model.__name__,
    )
    def test_every_clustering_field_documents_itself(self, model):
        """Each knob is read by an operator in the YAML, so each carries a
        ``Field(description=...)`` (the ``test_field_descriptions.py`` rule)."""

        properties = model.model_json_schema()["properties"]

        for name in model.model_fields:
            description = properties[name].get("description")
            assert description and description.strip(), (
                f"{model.__name__}.{name} is missing Field(description=...)"
            )


class TestEmbeddingDefaults:
    """ADR-009 decision 1: both embedding blocks are pinned to ``voyage-4`` at
    1024-d — the SAME dimension as legacy ``voyage-3.5``, so the live mongot
    ``vector_index`` is untouched by the swap."""

    def test_both_blocks_default_to_voyage_4_1024(self) -> None:
        # Arrange / Act — the REAL shipped config, not the frozen fixture: this
        # is what an operator boots.
        config = load_app_config(_DEFAULT_CONFIG_PATH)

        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-4"
        assert config.models.resolution_embedding.dimensions == 1024
        assert config.models.search_embedding.provider == "voyage"
        assert config.models.search_embedding.model == "voyage-4"
        assert config.models.search_embedding.dimensions == 1024
        # A YAML that omits the blocks entirely must land on the same pin.
        assert EmbeddingConfig().provider == "voyage"
        assert EmbeddingConfig().model == "voyage-4"
        assert EmbeddingConfig().dimensions == 1024


class TestModalCatalog:
    """The **Modal catalog** — ``modal.embedding_models`` + ``modal.llm_models``
    (ADR-009 §2/§3).

    One YAML entry per Hugging Face model that MAY be served on Modal. An
    entry says WHICH model and the facts about it; it never says HOW it is
    served — the **Serving path** is the deploy driver's runtime decision, so
    there is no ``serving`` and no ``base_model`` field to read. The seeds are
    asserted against the REAL shipped ``configs/default.yaml`` (what an
    operator boots), not the frozen fixture.
    """

    def test_seed_entries(self) -> None:
        """Four seeds, two per list, each carrying only facts about the model
        itself. Which of them becomes an endpoint is Modal's answer at deploy
        time, and appears nowhere in this file."""

        config = load_app_config(_DEFAULT_CONFIG_PATH)

        embeddings = config.modal.embedding_models
        assert [e.repo_id for e in embeddings] == [
            "Qwen/Qwen3-Embedding-0.6B",
            "voyageai/voyage-4-nano",
        ]

        qwen, voyage = embeddings
        assert qwen.kind == "embedding"
        # HF API sha for `main`, read 2026-09-19.
        assert qwen.revision == "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
        # App-only fields are OPTIONAL: these come from the typed defaults, and
        # are what the router needs the moment Modal refuses the model.
        assert qwen.gpu == "A10"
        assert qwen.cpu == 4
        assert qwen.memory_mb == 16384
        assert qwen.max_model_len is None
        # MRL is on the card (32-1024), but nothing offline proves the
        # Modal-picked recipe honours `dimensions` — stays empty until #141.
        assert qwen.matryoshka_dimensions == []

        assert voyage.kind == "embedding"
        assert voyage.revision == "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
        assert voyage.max_model_len == 32768
        assert voyage.matryoshka_dimensions == [256, 512, 1024, 2048]

        # `native_dimensions` is what the server returns with NO `dimensions`
        # asked for, so only Qwen3 lands in the 1024-d mongot vector_index
        # untruncated. voyage-4-nano is natively 2048-d — its custom
        # modeling_qwen3_bidirectional.py AutoModel applies a learned
        # nn.Linear(1024, 2048) head (config.json `num_labels`, safetensors
        # `linear.weight` [2048, 1024]) that modules.json cannot show — so the
        # client MUST truncate to the index's 1024.
        assert qwen.native_dimensions == 1024
        assert voyage.native_dimensions == 2048

        llms = config.modal.llm_models
        assert [e.repo_id for e in llms] == [
            "Qwen/Qwen3.5-0.8B",
            "LiquidAI/LFM2.5-350M",
        ]

        qwen_llm, lfm = llms
        assert qwen_llm.kind == "llm"
        # HF API shas for `main`, read 2026-09-20. Qwen3.5-0.8B is in Modal's
        # endpoint catalog and ungated (Apache-2.0), unlike the equally listed
        # google/gemma-3-1b-it (`gated: manual`).
        assert qwen_llm.revision == "2fc06364715b967f1860aea9cf38778875588b17"
        assert qwen_llm.n_gpus == 1
        assert qwen_llm.max_model_len is None
        assert qwen_llm.app_name == "ep-tree-qwen3-5-0-8b"
        # A THINKING model: live, with thinking ON, it spent the smoke test's
        # 256-token budget reasoning and answered with empty `content`
        # (tasks/141 round 1). Both knobs are CLIENT-side request fields.
        assert qwen_llm.max_tokens == 4096
        assert qwen_llm.chat_template_kwargs == {"enable_thinking": False}

        assert lfm.kind == "llm"
        assert lfm.revision == "9e6c6ccf47cd318696e137d381a7ded8fe4df09f"
        assert lfm.gpu == "A10"
        assert lfm.n_gpus == 1
        # The card's context is 128000; 32K keeps the KV cache small on one A10.
        assert lfm.max_model_len == 32768
        assert lfm.app_name == "ep-tree-lfm2-5-350m"
        # Not a thinking model: a budget that bounds a runaway constrained-JSON
        # generation, and no chat-template kwarg at all.
        assert lfm.max_tokens == 4096
        assert lfm.chat_template_kwargs == {}

    def test_the_kind_comes_from_the_list_not_from_the_model(self) -> None:
        """``kind`` is a class fact, not a value YAML may assert: an entry
        under ``llm_models`` IS an LLM, however its repo is named."""

        assert ModalEmbeddingModelConfig.kind == "embedding"
        assert ModalLLMModelConfig.kind == "llm"

        with pytest.raises(ValidationError):
            ModalLLMModelConfig(repo_id="acme/thing", kind="embedding")

    def test_retired_fields_are_rejected(self, tmp_path) -> None:
        """``serving:`` and ``base_model:`` asked the operator to predict an
        answer only Modal has (ADR-009 §2). A YAML that still carries one must
        fail at LOAD time, not be silently ignored."""

        for field, value in (("serving", "vllm"), ("base_model", "Qwen/Qwen3-0.6B")):
            custom = tmp_path / f"{field}.yaml"
            custom.write_text(
                yaml.safe_dump(
                    {"modal": {"embedding_models": [{**_VALID_ENTRY, field: value}]}}
                )
            )

            with pytest.raises(ValidationError) as excinfo:
                load_app_config(custom)

            message = str(excinfo.value)
            assert field in message
            assert "Extra inputs are not permitted" in message

    def test_qwen_query_prompt_is_byte_exact(self) -> None:
        """The Qwen3 instruction prompt is prepended client-side, so its BYTES
        are the contract: a real newline, and NO space after ``Query:`` (the
        model card's ``f'Instruct: {task}\nQuery:{query}'`` helper)."""

        config = load_app_config(_DEFAULT_CONFIG_PATH)

        qwen = config.modal.embedding_models[0]
        assert qwen.query_prompt == (
            "Instruct: Given a web search query, retrieve relevant passages "
            "that answer the query\nQuery:"
        )
        assert qwen.document_prompt == ""

    def test_engine_pins(self, frozen_config_path) -> None:
        """Engine versions are per ENGINE, not per model (ADR-009 §3), and the
        ``autoinference-utils`` pin is shared by both App scripts.

        vLLM is pinned to Modal's own embedding recipe (``vllm==0.26.0``, the
        ``serve.py`` it generates for its embedding endpoints, read
        2026-09-20) in the YAML, in the code default AND in the frozen
        fixture: a bump that moves only one of the three would build the image
        with a version no test describes.

        SGLang's pin is a DOCKER TAG of ``lmsysorg/sglang`` (``v0.5.18``,
        Modal's own LLM recipe), not a PyPI version — the SGLang App runs the
        official image instead of pip-installing the engine, so a bare
        ``0.5.18`` here would name no image at all.
        """

        config = load_app_config(_DEFAULT_CONFIG_PATH)

        assert config.modal.autoinference_utils_version == "0.2.6"
        assert config.modal.engines["vllm"].version == "0.26.0"
        assert config.modal.engines["sglang"].version == "v0.5.18"
        assert ModalConfig().engines["vllm"].version == "0.26.0"
        assert ModalConfig().engines["sglang"].version == "v0.5.18"
        frozen = load_app_config(frozen_config_path).modal
        assert frozen.engines["vllm"].version == "0.26.0"
        assert frozen.engines["sglang"].version == "v0.5.18"

    def test_modal_warmup_deadline_default_and_override(
        self, tmp_path, monkeypatch, frozen_config_path
    ) -> None:
        """ADR-009 §11: ONE budget for waiting out a cold start — the client's
        300 s and the smoke test's 1200 s are gone. 600 s is ~3x the slowest
        boot measured live (199 s, a 35B LLM)."""

        # Default: the typed default, the frozen fixture and the real
        # configs/default.yaml all agree on 600.
        assert ModalConfig().warmup_deadline_s == 600.0
        assert load_app_config(frozen_config_path).modal.warmup_deadline_s == 600.0
        assert load_app_config(_DEFAULT_CONFIG_PATH).modal.warmup_deadline_s == 600.0

        # Override: a first deploy that also downloads 16 GB of weights gets a
        # longer budget for ONE command, with no file edit.
        custom = tmp_path / "modal.yaml"
        custom.write_text("modal:\n  warmup_deadline_s: 600\n")
        monkeypatch.setenv("TREE_MODAL__WARMUP_DEADLINE_S", "45")
        assert load_app_config(custom).modal.warmup_deadline_s == 45.0

        # Bounds: a 0 s budget would fail every cold start on its first poll,
        # which is the bug this knob exists to fix.
        monkeypatch.setenv("TREE_MODAL__WARMUP_DEADLINE_S", "0")
        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)
        assert "warmup_deadline_s" in str(excinfo.value)

    def test_modal_request_timeout_default_and_override(
        self, tmp_path, monkeypatch, frozen_config_path
    ) -> None:
        """ADR-009 §11: ONE bound on a SINGLE request — the twin of the warm-up
        budget, and a different wait: that one waits for a COLD server, this
        one waits for ONE answer from a living one.

        The SDK's own defaults are 600 s and two SILENT retries, so one
        thinking model held a caller for 13 minutes before it was killed
        (``tasks/141``, cycle 4d). 300 s is half the SDK's default and the same
        order as the chat smoke test's accidental ``aiohttp`` default.
        """

        # Default: the typed default, the frozen fixture and the real
        # configs/default.yaml all agree on 300.
        assert ModalConfig().request_timeout_s == 300.0
        assert load_app_config(frozen_config_path).modal.request_timeout_s == 300.0
        assert load_app_config(_DEFAULT_CONFIG_PATH).modal.request_timeout_s == 300.0
        assert "request_timeout_s: 300" in frozen_config_path.read_text()

        # Override: an operator serving a 35B model whose answers take seven
        # minutes widens it for ONE serving process, with no file edit.
        custom = tmp_path / "modal.yaml"
        custom.write_text("modal:\n  request_timeout_s: 300\n")
        monkeypatch.setenv("TREE_MODAL__REQUEST_TIMEOUT_S", "45")
        assert load_app_config(custom).modal.request_timeout_s == 45.0

        # Bounds: a 0 s timeout would fail every call before the server could
        # answer at all.
        monkeypatch.setenv("TREE_MODAL__REQUEST_TIMEOUT_S", "0")
        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)
        assert "request_timeout_s" in str(excinfo.value)

    def test_a_minimal_entry_is_a_repo_id_and_its_facts(self) -> None:
        """Story 1: nothing about serving, nothing about hardware — the App
        fields default, and a Dedicated endpoint never reads them."""

        entry = ModalEmbeddingModelConfig(repo_id="BAAI/bge-m3", native_dimensions=1024)

        assert entry.kind == "embedding"
        assert entry.revision == "main"
        assert entry.endpoint_name == "tree-bge-m3"
        assert entry.app_name == "ep-tree-bge-m3"
        assert entry.extra_server_args == {}

    def test_a_minimal_llm_entry_defaults_to_one_gpu(self) -> None:
        entry = ModalLLMModelConfig(repo_id="acme/small-llm")

        assert entry.kind == "llm"
        assert entry.n_gpus == 1
        assert entry.gpu == "A10"
        assert entry.app_name == "ep-tree-small-llm"

    def test_the_frozen_fixture_carries_the_request_knobs(
        self, frozen_config_path
    ) -> None:
        """The fixture mirrors the SHAPE of the shipped catalog. A knob that
        lived only in ``configs/default.yaml`` would leave every test that
        loads the fixture proving nothing about the request the memory sends."""

        qwen_llm, lfm = load_app_config(frozen_config_path).modal.llm_models

        assert qwen_llm.max_tokens == 4096
        assert qwen_llm.chat_template_kwargs == {"enable_thinking": False}
        assert lfm.max_tokens == 4096
        assert lfm.chat_template_kwargs == {}

    def test_the_request_knobs_are_off_by_default(self) -> None:
        """ADR-009 §10: both knobs are OPT-IN. An entry that names neither
        sends the request it sent before they existed — ``None`` is "send no
        `max_tokens`", not "send 0"."""

        entry = ModalLLMModelConfig(repo_id="acme/small-llm")

        assert entry.max_tokens is None
        assert entry.chat_template_kwargs == {}

    @pytest.mark.parametrize("max_tokens", [0, -1], ids=["zero", "negative"])
    def test_a_non_positive_max_tokens_is_refused(self, max_tokens: int) -> None:
        """A 0-token budget makes every completion empty — the exact live
        failure this knob exists to fix (``tasks/141`` round 1)."""

        with pytest.raises(ValidationError) as excinfo:
            ModalLLMModelConfig(repo_id="acme/small-llm", max_tokens=max_tokens)

        assert "max_tokens" in str(excinfo.value)

    def test_chat_template_kwargs_must_be_a_json_object(self) -> None:
        """It goes on the wire as a JSON OBJECT (SGLang's
        ``chat_template_kwargs: Optional[Dict]``, ``protocol.py:844`` at
        v0.5.18), so a bare string would be a 400 from a booted GPU."""

        with pytest.raises(ValidationError) as excinfo:
            ModalLLMModelConfig(repo_id="acme/small-llm", chat_template_kwargs="x")

        assert "chat_template_kwargs" in str(excinfo.value)

    def test_a_chat_template_kwarg_key_may_not_be_empty(self) -> None:
        """``{"": false}`` names no template variable at all — a YAML typo,
        caught at load time instead of on a running GPU."""

        with pytest.raises(ValidationError) as excinfo:
            ModalLLMModelConfig(
                repo_id="acme/small-llm", chat_template_kwargs={"": False}
            )

        assert "chat_template_kwargs" in str(excinfo.value)

    @pytest.mark.parametrize(
        "value,offending_key",
        [
            ({"cutoff": datetime.date(2026, 1, 1)}, "cutoff"),
            ({"limits": {"cutoff": datetime.date(2026, 1, 1)}}, "limits"),
            ({"temperature": float("nan")}, "temperature"),
            ({"temperature": float("inf")}, "temperature"),
            ({"seen": {"a"}}, "seen"),
        ],
        ids=["a-yaml-date", "a-nested-yaml-date", "nan", "infinity", "a-set"],
    )
    def test_a_chat_template_kwarg_value_that_is_not_json_is_refused(
        self, value: dict[str, object], offending_key: str
    ) -> None:
        """The knob is POSTed as a JSON object by both chat paths, so a value
        `json.dumps` cannot write is not a knob — it is a crash before the
        request (`json.dumps` in the smoke test's pre-POST log line) on one
        path and a silently re-interpreted value (`"2026-01-01"`) on the
        other, which is the parity guarantee itself breaking. NaN and Infinity
        are floats Python writes and JSON has no syntax for, so they are
        refused too. A NESTED offender names the TOP-LEVEL key that holds it —
        the one an operator edits in the YAML."""

        with pytest.raises(ValidationError) as excinfo:
            ModalLLMModelConfig(repo_id="acme/small-llm", chat_template_kwargs=value)

        message = str(excinfo.value)
        assert "chat_template_kwargs" in message
        assert offending_key in message
        assert "quote" in message

    def test_an_unquoted_yaml_date_is_refused_at_load_time(self) -> None:
        """The footgun in the operator's own words: YAML parses a BARE
        `2026-01-01` into a `datetime.date`, not a string — so the rejection
        has to survive the real parser, not just a hand-built dict."""

        parsed = yaml.safe_load("cutoff: 2026-01-01")
        assert parsed == {"cutoff": datetime.date(2026, 1, 1)}

        with pytest.raises(ValidationError) as excinfo:
            ModalLLMModelConfig(repo_id="acme/small-llm", chat_template_kwargs=parsed)

        assert "cutoff" in str(excinfo.value)

    def test_every_json_native_chat_template_kwarg_is_accepted(self) -> None:
        """The refusal above may not cost the knob its range: SGLang passes the
        object to a Jinja chat template, and every JSON type is a legal value
        there. Asserted by ROUND-TRIPPING what was stored, so a validator that
        quietly dropped or mangled a value could not pass."""

        kwargs = {
            "enable_thinking": False,
            "name": "qwen",
            "top_k": 20,
            "temperature": 0.7,
            "stop": None,
            "tags": ["a", 1, None],
            "nested": {"deep": {"ok": True, "list": [1.5, "x"]}},
        }

        entry = ModalLLMModelConfig(
            repo_id="acme/small-llm", chat_template_kwargs=kwargs
        )

        assert entry.chat_template_kwargs == kwargs
        assert json.loads(json.dumps(entry.chat_template_kwargs, allow_nan=False)) == (
            kwargs
        )

    @pytest.mark.parametrize(
        "knob,value",
        [("max_tokens", 512), ("chat_template_kwargs", {"enable_thinking": False})],
        ids=["max_tokens", "chat_template_kwargs"],
    )
    def test_the_request_knobs_are_llm_only(self, knob: str, value: object) -> None:
        """Story 5: ``/v1/embeddings`` has neither a completion budget nor a
        chat template, so ``extra="forbid"`` refuses the knob on an EMBEDDING
        entry — at load time, naming the field."""

        with pytest.raises(ValidationError) as excinfo:
            ModalEmbeddingModelConfig(**{**_VALID_ENTRY, knob: value})

        assert "Extra inputs are not permitted" in str(excinfo.value)
        assert knob in str(excinfo.value)

    def test_n_gpus_must_be_positive(self) -> None:
        """It becomes SGLang's ``tp`` and the ``:N`` of the GPU string, so a
        0 would deploy a server with no GPU at all."""

        for n_gpus in (0, -1):
            with pytest.raises(ValidationError) as excinfo:
                ModalLLMModelConfig(repo_id="acme/small-llm", n_gpus=n_gpus)

            assert "n_gpus" in str(excinfo.value)

    @pytest.mark.parametrize("flag", ["--tp", "--tp-size", "--tensor-parallel-size"])
    def test_tp_flags_are_builder_owned(self, flag: str) -> None:
        """Parallelism is derived from ``n_gpus``; an entry that also set it
        by hand could disagree with the GPU string the App asks Modal for."""

        with pytest.raises(ValidationError) as excinfo:
            ModalLLMModelConfig(repo_id="acme/small-llm", extra_server_args={flag: "2"})

        assert f"{flag!r} is owned by the deploy builder" in str(excinfo.value)

    @pytest.mark.parametrize(
        "revision",
        ["main", "refs/pr/3", "v1.2.0", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"],
    )
    def test_accepts_every_shape_a_hub_revision_takes(self, revision: str) -> None:
        """A sha, a branch, a tag and a PR ref — the charset excludes only
        whitespace and shell punctuation, not legal Hub revisions."""

        entry = ModalEmbeddingModelConfig(
            repo_id="BAAI/bge-m3", native_dimensions=1024, revision=revision
        )

        assert entry.revision == revision

    @pytest.mark.parametrize(
        "repo_id,endpoint_name",
        [
            ("voyageai/voyage-4-nano", "tree-voyage-4-nano"),
            ("Qwen/Qwen3-Embedding-0.6B", "tree-qwen3-embedding-0-6b"),
            ("Qwen/Qwen3.5-0.8B", "tree-qwen3-5-0-8b"),
            ("LiquidAI/LFM2.5-350M", "tree-lfm2-5-350m"),
            ("BAAI/bge-m3", "tree-bge-m3"),
            ("Org/My_Model..v2", "tree-my-model-v2"),
        ],
    )
    def test_name_derivation(self, repo_id: str, endpoint_name: str) -> None:
        """Every name we create carries the ``tree-`` namespace (ADR-009 §3):
        Modal names a hand-made endpoint's app ``ep-<slug>``, ours is
        ``ep-tree-<slug>``, so a deploy of ours can never land on it.

        ONE derivation for BOTH kinds — it lives on the shared base class, so
        a correction to either half is one line."""

        entry = ModalEmbeddingModelConfig(repo_id=repo_id, native_dimensions=1024)
        llm_entry = ModalLLMModelConfig(repo_id=repo_id)

        assert MODAL_NAME_PREFIX == "tree"
        assert entry.endpoint_name == llm_entry.endpoint_name == endpoint_name
        assert entry.app_name == llm_entry.app_name == f"ep-{endpoint_name}"

    @pytest.mark.parametrize(
        "slug_length,fits",
        [(55, True), (56, False)],
        ids=["55-characters-fit", "56-characters-do-not"],
    )
    def test_app_name_length_limit(self, slug_length: int, fits: bool) -> None:
        """``ep-tree-`` spends 8 of Modal's 63 characters, so a slug may be at
        most 55. Caught at LOAD time instead of after an image build."""

        repo_id = f"acme/{'a' * slug_length}"

        if fits:
            entry = ModalEmbeddingModelConfig(repo_id=repo_id, native_dimensions=1024)
            assert len(entry.app_name) == 63
            return

        with pytest.raises(ValidationError) as excinfo:
            ModalEmbeddingModelConfig(repo_id=repo_id, native_dimensions=1024)

        message = str(excinfo.value)
        assert "at most 63" in message
        assert "64 characters" in message

    def test_empty_slug_is_still_rejected(self) -> None:
        """The prefix must not mask an empty derivation: ``tree-`` is truthy,
        so the validator reads the SLUG."""

        with pytest.raises(ValidationError) as excinfo:
            ModalEmbeddingModelConfig(repo_id="a/---", native_dimensions=1024)

        assert "derives an empty endpoint name" in str(excinfo.value)

    def test_uniqueness_spans_both_lists(self, tmp_path) -> None:
        """One model, one entry, one Modal app — ACROSS the lists. A repo id
        in both would make ``get_catalog_entry`` (and therefore the KIND)
        order-dependent; two ids deriving one app name would share one Modal
        app, so deploying one would stop the other."""

        duplicate_id = tmp_path / "duplicate_id.yaml"
        duplicate_id.write_text(
            yaml.safe_dump(
                {
                    "modal": {
                        "embedding_models": [_VALID_ENTRY],
                        "llm_models": [{"repo_id": _VALID_ENTRY["repo_id"]}],
                    }
                }
            )
        )

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(duplicate_id)

        message = str(excinfo.value)
        assert "duplicate repo_id 'voyageai/voyage-4-nano'" in message
        assert "modal.embedding_models and modal.llm_models" in message

        duplicate_app = tmp_path / "duplicate_app.yaml"
        duplicate_app.write_text(
            yaml.safe_dump(
                {
                    "modal": {
                        "embedding_models": [_VALID_ENTRY],
                        "llm_models": [{"repo_id": "acme/voyage-4-nano"}],
                    }
                }
            )
        )

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(duplicate_app)

        message = str(excinfo.value)
        assert (
            "'voyageai/voyage-4-nano' and 'acme/voyage-4-nano' both derive" in message
        )
        assert "'ep-tree-voyage-4-nano'" in message
        assert "modal.embedding_models and modal.llm_models" in message

    @pytest.mark.parametrize(
        "entries,expected_fragments",
        [
            pytest.param(
                [{**_VALID_ENTRY, "url": "https://acme--x.modal.run"}],
                ["url", "Extra inputs are not permitted"],
                id="unknown-field",
            ),
            pytest.param(
                [{**_VALID_ENTRY, "repo_id": "no-slash"}],
                ["repo_id", "should match pattern"],
                id="repo-id-without-org",
            ),
            pytest.param(
                [{k: v for k, v in _VALID_ENTRY.items() if k != "native_dimensions"}],
                ["native_dimensions", "Field required"],
                id="embedding-without-native-dimensions",
            ),
            pytest.param(
                # Not in the AC: found by QA. `-` passes _REPO_ID_PATTERN but
                # the derivation collapses it away, so the app would be `ep-`.
                [{**_VALID_ENTRY, "repo_id": "acme/---"}],
                [
                    "'acme/---'",
                    "derives an empty endpoint name",
                    # The derivation is ASCII-only (`[^a-z0-9]+` -> `-`), so
                    # the message must not promise that a non-ASCII letter
                    # survives it.
                    "at least one ASCII letter or digit",
                ],
                id="repo-id-deriving-an-empty-endpoint-name",
            ),
            pytest.param(
                # Not in the AC: found by QA on #139. The revision travels as
                # ONE element of the argv the driver logs with a plain
                # `" ".join(...)`, so a value carrying whitespace would split
                # into two tokens in a log line meant to be re-runnable.
                [{**_VALID_ENTRY, "revision": "main branch"}],
                ["revision", "should match pattern"],
                id="revision-with-whitespace",
            ),
            pytest.param(
                [{**_VALID_ENTRY, "revision": "main;rm -rf /"}],
                ["revision", "should match pattern"],
                id="revision-with-shell-punctuation",
            ),
            pytest.param(
                [{**_VALID_ENTRY, "extra_server_args": {"runner": "pooling"}}],
                ["'runner'", "must start with '--'"],
                id="server-arg-key-without-dashes",
            ),
            pytest.param(
                [
                    {
                        **_VALID_ENTRY,
                        "extra_server_args": {
                            "--pooler-config": '{"pooling_type": "MEAN"}'
                        },
                    }
                ],
                ["value for --pooler-config contains whitespace", "compact JSON"],
                id="server-arg-value-with-whitespace",
            ),
            pytest.param(
                [{**_VALID_ENTRY, "extra_server_args": {"--port": "8000"}}],
                ["'--port'", "owned by the deploy builder"],
                id="reserved-server-arg-key",
            ),
            pytest.param(
                [{**_VALID_ENTRY, "extra_server_args": {"--is-embedding": ""}}],
                ["'--is-embedding'", "owned by the deploy builder"],
                id="builder-owned-server-arg-key",
            ),
            pytest.param(
                [_VALID_ENTRY, _VALID_ENTRY],
                [
                    "duplicate repo_id 'voyageai/voyage-4-nano'",
                    "one entry per model",
                ],
                id="duplicate-repo-id",
            ),
            pytest.param(
                [_VALID_ENTRY, {**_VALID_ENTRY, "repo_id": "acme/voyage-4-nano"}],
                [
                    "'voyageai/voyage-4-nano' and 'acme/voyage-4-nano' both derive",
                    "'ep-tree-voyage-4-nano'",
                    "one app per model",
                ],
                id="duplicate-app-name",
            ),
        ],
    )
    def test_rejects_invalid_entries(
        self,
        tmp_path,
        entries: list[dict],
        expected_fragments: list[str],
    ) -> None:
        """Every catalog mistake is a LOAD-TIME error naming the offending key,
        never a container that dies minutes later on a mangled flag."""

        custom = tmp_path / "modal.yaml"
        custom.write_text(yaml.safe_dump({"modal": {"embedding_models": entries}}))

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)

        message = str(excinfo.value)
        for fragment in expected_fragments:
            assert fragment in message

    def test_an_llm_entry_takes_no_embedding_fields(self, tmp_path) -> None:
        """The halves are not interchangeable: dimensions and prompts are
        embedding facts, and an LLM entry that carried them would imply a
        client that does not exist."""

        custom = tmp_path / "llm.yaml"
        custom.write_text(
            yaml.safe_dump(
                {
                    "modal": {
                        "llm_models": [{**_VALID_LLM_ENTRY, "native_dimensions": 1024}]
                    }
                }
            )
        )

        with pytest.raises(ValidationError) as excinfo:
            load_app_config(custom)

        assert "native_dimensions" in str(excinfo.value)

    def test_modal_section_is_optional(self, tmp_path) -> None:
        """A YAML with no ``modal:`` block boots with an EMPTY catalog: the
        code default adds no model, the YAML seeds it."""

        custom = tmp_path / "no_modal.yaml"
        custom.write_text("query:\n  top_k: 5\n")

        config = load_app_config(custom)

        assert config.modal.embedding_models == []
        assert config.modal.llm_models == []
        assert AppConfig().modal.embedding_models == []
        assert AppConfig().modal.llm_models == []

    def test_frozen_config_carries_the_catalog(self, frozen_config_path) -> None:
        """The frozen fixture gains both lists too, so the loader assertions
        cover a catalog without depending on operator edits to default.yaml."""

        config = load_app_config(frozen_config_path)

        assert [e.repo_id for e in config.modal.embedding_models] == [
            "Qwen/Qwen3-Embedding-0.6B",
            "voyageai/voyage-4-nano",
        ]
        assert [e.repo_id for e in config.modal.llm_models] == [
            "Qwen/Qwen3.5-0.8B",
            "LiquidAI/LFM2.5-350M",
        ]


def test_yaml_price_map_matches_code_default() -> None:
    """The YAML price map and the Pydantic default price map are ONE table.

    A model id present in only one of them costs $0 in half the deployments
    (YAML-less boot vs shipped config) — a silent telemetry gap, so the two are
    pinned identical here.
    """

    yaml_map = yaml.safe_load(_DEFAULT_CONFIG_PATH.read_text())["observability"][
        "embedding_price_per_1m_tokens"
    ]

    assert yaml_map == ObservabilityConfig().embedding_price_per_1m_tokens
