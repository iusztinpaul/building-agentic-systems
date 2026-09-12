import json
import textwrap

import pytest
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
    HdbscanConfig,
    MemoryConfig,
    UmapConfig,
    YouTubeConfig,
    _DEFAULT_CONFIG_PATH,
    load_app_config,
)
from tree.config.sources import (
    HuggingFaceDatasetSource,
    SourceEntry,
)


class TestLoadAppConfig:
    def test_loads_default_yaml(self, frozen_config_path):
        config = load_app_config(frozen_config_path)

        assert config.models.llm.provider == "gemini"
        assert config.models.llm.model == "gemini-3.1-flash-lite"
        # #048 flipped the default from the multimodal ``voyage-multimodal-3`` to
        # the TEXT model ``voyage-3.5`` (routed to /v1/embeddings); ``voyage-3.5``
        # is also 1024-d, so the dim stays put. #039 split the single
        # ``embedding`` block into a transient ``resolution_embedding`` and a
        # persisted ``search_embedding``; both point at the same model/dim.
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-3.5"
        assert config.models.resolution_embedding.dimensions == 1024
        assert config.models.search_embedding.provider == "voyage"
        assert config.models.search_embedding.model == "voyage-3.5"
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
        # (#048 flipped the code-level default model to the text ``voyage-3.5``).
        assert config.models.resolution_embedding.provider == "voyage"
        assert config.models.resolution_embedding.model == "voyage-3.5"
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

    def test_default_yaml_comment_block_explains_the_switch_and_the_2d_rule(self):
        """The two decisions an operator MUST not have to read the ADR for: the
        switch is the ``run_clustering`` flow parameter, and clustering happens
        in the 5-d intermediate — never on the 2D projection."""

        yaml_text = _DEFAULT_CONFIG_PATH.read_text()

        assert "run_clustering" in yaml_text
        assert "never on the 2D" in yaml_text

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
