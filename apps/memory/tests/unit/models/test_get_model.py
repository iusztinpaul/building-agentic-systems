import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from tree.config.app_config import (
    EmbeddingConfig,
    LLMConfig,
    app_config as real_app_config,
    load_app_config,
)
from tree.models import modal_catalog
from tree.models.fake_model import MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM
from tree.models.exceptions import ModelError
from tree.models.get_model import (
    _build_embedding_model,
    get_embedding_model,
    get_llm,
    get_resolution_embedding_model,
    get_search_embedding_model,
    llm_identity,
    search_embedding_identity,
)
from tree.models.modal_embedding import ModalEmbeddingModel
from tree.models.modal_llm import ModalLLM
from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel
from tree.models.voyage_embedding import VoyageTextEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel

# An **Embedding catalog** id: the Modal provider resolves its entry at
# construction, so the Modal tests cannot use an arbitrary model name.
_CATALOG_MODEL = "voyageai/voyage-4-nano"

# The LLM half of the same catalog (``modal.llm_models``). ``get_llm`` resolves
# the entry at construction too, so the id must be a real one.
_CATALOG_LLM = "LiquidAI/LFM2.5-350M"


@pytest.fixture(autouse=True)
def _mock_settings(mocker) -> None:
    mock_settings = MagicMock()
    mock_settings.google_api_key.get_secret_value.return_value = "fake-google-key"
    mock_settings.voyage_api_key.get_secret_value.return_value = "fake-voyage-key"
    mocker.patch("tree.models.get_model.settings", mock_settings)
    # The Modal branch reads the **Proxy token** through
    # ``modal_catalog.modal_proxy_bearer()``, which holds its OWN ``settings``
    # binding — patching only ``get_model.settings`` would let the factory read
    # the developer's real token out of the ``make``-exported ``.env``.
    mocker.patch.object(
        modal_catalog.settings, "modal_proxy_token_id", SecretStr("wk-1")
    )
    mocker.patch.object(
        modal_catalog.settings, "modal_proxy_token_secret", SecretStr("ws-2")
    )


@pytest.fixture(autouse=True)
def _mock_app_config(mocker) -> None:
    mock_config = MagicMock()
    mock_config.models.llm.provider = "gemini"
    mock_config.models.llm.model = "gemini-2.0-flash"
    # #039: get_embedding_model() reads the SEARCH model (the persisted /
    # behavior-identical path for existing call sites).
    mock_config.models.search_embedding.provider = "mock"
    mock_config.models.search_embedding.model = "text-embedding-004"
    mock_config.models.search_embedding.dimensions = 256
    mocker.patch("tree.models.get_model.app_config", mock_config)


def test_importing_get_model_does_not_eagerly_load_torch() -> None:
    """Serverless cold-start regression guard.

    Importing the model factory must NOT pull ``torch`` / ``sentence_transformers``
    (a ~7s import). On Prefect Horizon that eager import blew the 60s port-readiness
    window and the runner was killed mid-import. The heavy providers are lazy-imported
    inside their dispatch branches; this asserts the common boot path stays light.

    Runs in a fresh interpreter so other tests' top-level imports of
    ``sentence_transformer`` don't pollute ``sys.modules`` and mask a regression.
    """

    probe = (
        "import sys, tree.models.get_model; "
        "assert 'torch' not in sys.modules, 'torch'; "
        "assert 'sentence_transformers' not in sys.modules, 'sentence_transformers'"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )

    assert result.returncode == 0, (
        f"get_model import eagerly loaded a heavy ML lib: {result.stderr}"
    )


class TestGetLLM:
    def test_returns_gemini_llm_by_default(self) -> None:
        result = get_llm()

        assert isinstance(result, GeminiLLM)

    def test_returns_gemini_llm_with_explicit_provider(self) -> None:
        result = get_llm(provider="gemini")

        assert isinstance(result, GeminiLLM)

    def test_raises_for_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM provider: unknown"):
            get_llm(provider="unknown")

    def test_returns_modal_llm_for_a_catalog_id(self, mocker) -> None:
        """ADR-009 §10: ``models.llm: {provider: modal, model: <repo_id>}``
        switches the LLM exactly like the embedding blocks."""

        mocker.patch("tree.models.get_model.app_config.models.llm.model", _CATALOG_LLM)

        result = get_llm(provider="modal")

        assert isinstance(result, ModalLLM)
        # Built from the CATALOG entry, not from the raw id.
        assert result.warm_key == "ep-tree-lfm2-5-350m"
        # The joined Proxy token halves — what Modal's edge checks.
        assert result._proxy_token == "wk-1.ws-2"

    def test_the_provider_may_come_from_the_config_alone(self, mocker) -> None:
        """Story 5: ``TREE_MODELS__LLM__PROVIDER=modal
        TREE_MODELS__LLM__MODEL=<repo_id>`` switches ONE run — the factory is
        called with no argument at all."""

        mocker.patch("tree.models.get_model.app_config.models.llm.provider", "modal")
        mocker.patch(
            "tree.models.get_model.app_config.models.llm.model", "Qwen/Qwen3.5-0.8B"
        )

        result = get_llm()

        assert isinstance(result, ModalLLM)
        assert result.warm_key == "ep-tree-qwen3-5-0-8b"

    def test_a_non_catalog_model_fails_before_any_gpu_wakes(self, mocker) -> None:
        """Story 3: the Gemini default model id under ``provider: modal`` is a
        typo, and the catalog says so — no network call, no deploy."""

        with pytest.raises(ModelError, match="Unknown Modal model"):
            get_llm(provider="modal")

    def test_a_half_proxy_token_fails_before_a_client_exists(self, mocker) -> None:
        """A half token is no token: Modal answers 401 before any container
        wakes, so the factory refuses to build the model."""

        mocker.patch.object(
            modal_catalog.settings, "modal_proxy_token_secret", SecretStr("")
        )
        mocker.patch("tree.models.get_model.app_config.models.llm.model", _CATALOG_LLM)

        with pytest.raises(ModelError, match="Modal proxy token is required"):
            get_llm(provider="modal")


class TestGetEmbeddingModel:
    def test_returns_mock_by_default(self) -> None:
        result = get_embedding_model()

        assert isinstance(result, MockEmbeddingModel)

    def test_returns_mock_with_explicit_provider(self) -> None:
        result = get_embedding_model(provider="mock")

        assert isinstance(result, MockEmbeddingModel)

    def test_returns_gemini_embedding(self) -> None:
        result = get_embedding_model(provider="gemini")

        assert isinstance(result, GeminiEmbeddingModel)

    def test_returns_sentence_transformers(self, mocker) -> None:
        mocker.patch(
            "tree.models.sentence_transformer.SentenceTransformer",
        )

        result = get_embedding_model(provider="sentence-transformers")

        assert isinstance(result, SentenceTransformerEmbeddingModel)

    def test_returns_modal_embedding(self, mocker) -> None:
        """The Modal provider needs an **Embedding catalog** id — the factory
        resolves the entry at construction, so a non-catalog model fails."""

        mocker.patch(
            "tree.models.get_model.app_config.models.search_embedding.model",
            _CATALOG_MODEL,
        )
        mocker.patch(
            "tree.models.get_model.app_config.models.search_embedding.dimensions", 1024
        )

        result = get_embedding_model(provider="modal")

        assert isinstance(result, ModalEmbeddingModel)

    def test_returns_voyage_multimodal_for_multimodal_model(self, mocker) -> None:
        """#048 re-introduced model-id routing for the ``voyage`` provider:
        a ``voyage-multimodal-*`` id still resolves to the multimodal client.
        """

        mocker.patch(
            "tree.models.get_model.app_config.models.search_embedding.model",
            "voyage-multimodal-3",
        )
        mocker.patch(
            "tree.models.get_model.app_config.models.search_embedding.dimensions", 1024
        )

        result = get_embedding_model(provider="voyage")

        assert isinstance(result, VoyageMultimodalEmbeddingModel)

    def test_returns_voyage_text_for_text_model(self, mocker) -> None:
        """A non-multimodal voyage id (the #048 default ``voyage-3.5``) routes
        to the text client targeting ``/v1/embeddings``.
        """

        mocker.patch(
            "tree.models.get_model.app_config.models.search_embedding.model",
            "voyage-3.5",
        )
        mocker.patch(
            "tree.models.get_model.app_config.models.search_embedding.dimensions", 1024
        )

        result = get_embedding_model(provider="voyage")

        assert isinstance(result, VoyageTextEmbeddingModel)

    def test_raises_for_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown embedding provider: unknown"):
            get_embedding_model(provider="unknown")


def _set_embedding_blocks(
    mocker,
    *,
    resolution: EmbeddingConfig,
    search: EmbeddingConfig,
) -> None:
    """Point the two YAML embedding blocks at concrete configs.

    The module-level ``_mock_app_config`` fixture installs a ``MagicMock``
    for ``app_config``; here we replace the two embedding sub-blocks with
    real :class:`EmbeddingConfig` instances so each test exercises an
    independent provider/model/dimensions triple.
    """

    mocker.patch(
        "tree.models.get_model.app_config.models.resolution_embedding",
        resolution,
    )
    mocker.patch(
        "tree.models.get_model.app_config.models.search_embedding",
        search,
    )


class TestDualEmbeddingGetters:
    def test_both_getters_return_voyage_at_1024(self, mocker) -> None:
        """Both blocks set to voyage / voyage-multimodal-3 / 1024 → each
        getter returns a VoyageMultimodalEmbeddingModel at 1024 dims."""

        voyage = EmbeddingConfig(
            provider="voyage", model="voyage-multimodal-3", dimensions=1024
        )
        _set_embedding_blocks(mocker, resolution=voyage, search=voyage)

        resolution_model = get_resolution_embedding_model()
        search_model = get_search_embedding_model()

        assert isinstance(resolution_model, VoyageMultimodalEmbeddingModel)
        assert resolution_model.dimensions == 1024
        assert isinstance(search_model, VoyageMultimodalEmbeddingModel)
        assert search_model.dimensions == 1024

    def test_getters_read_independent_config_blocks(self, mocker) -> None:
        """resolution_embedding=mock + search_embedding=voyage proves the
        two getters select from independent YAML blocks."""

        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=256),
            search=EmbeddingConfig(
                provider="voyage", model="voyage-multimodal-3", dimensions=1024
            ),
        )

        resolution_model = get_resolution_embedding_model()
        search_model = get_search_embedding_model()

        assert isinstance(resolution_model, MockEmbeddingModel)
        assert isinstance(search_model, VoyageMultimodalEmbeddingModel)

    def test_resolution_getter_builds_from_resolution_block(self, mocker) -> None:
        """The resolution getter must read resolution_embedding, not the
        search block."""

        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(provider="mock", dimensions=512),
        )

        resolution_model = get_resolution_embedding_model()

        assert isinstance(resolution_model, MockEmbeddingModel)
        assert resolution_model.dimensions == 128

    def test_search_getter_builds_from_search_block(self, mocker) -> None:
        """The search getter must read search_embedding, not the
        resolution block."""

        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(provider="mock", dimensions=512),
        )

        search_model = get_search_embedding_model()

        assert isinstance(search_model, MockEmbeddingModel)
        assert search_model.dimensions == 512

    def test_legacy_getter_aliases_search_model(self, mocker) -> None:
        """get_embedding_model() returns the same model type as
        get_search_embedding_model() (the persisted-vector model)."""

        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(
                provider="voyage", model="voyage-multimodal-3", dimensions=1024
            ),
        )

        legacy_model = get_embedding_model()
        search_model = get_search_embedding_model()

        assert type(legacy_model) is type(search_model)
        assert isinstance(legacy_model, VoyageMultimodalEmbeddingModel)


class TestVoyageModelIdRouting:
    """``_build_embedding_model`` dispatches the voyage provider by model id.

    ``voyage-multimodal-*`` → multimodal client (``/v1/multimodalembeddings``);
    every other voyage id → the text client (``/v1/embeddings``). Both clients
    coexist after #048's partial revert of #038.
    """

    @pytest.mark.parametrize(
        "model_id",
        [
            "voyage-4",
            "voyage-4-large",
            "voyage-4-lite",
            "voyage-code-4",
            "voyage-3",
            "voyage-3.5",
            "voyage-3-lite",
            "voyage-code-3",
        ],
    )
    def test_text_models_route_to_text_client(self, mocker, model_id: str) -> None:
        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(provider="voyage", model=model_id, dimensions=1024),
        )

        result = get_search_embedding_model()

        assert isinstance(result, VoyageTextEmbeddingModel)

    @pytest.mark.parametrize(
        "model_id",
        ["voyage-multimodal-3", "voyage-multimodal-3.5"],
    )
    def test_multimodal_models_route_to_multimodal_client(
        self, mocker, model_id: str
    ) -> None:
        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(provider="voyage", model=model_id, dimensions=1024),
        )

        result = get_search_embedding_model()

        assert isinstance(result, VoyageMultimodalEmbeddingModel)


class TestSearchEmbeddingIdentity:
    """ADR-009 decisions 5 + 6: ONE helper renders the identity every embed
    cache key carries, so a model / dimension / **Embedding role** swap can
    never replay vectors from the old embedding space.
    """

    def test_renders_provider_model_dimensions_and_role(self, mocker) -> None:
        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(
                provider="voyage", model="voyage-4", dimensions=1024
            ),
        )

        # The role is the PERSISTED one: both cached embed tasks write vectors
        # that are stored, and a persisted vector is always ``document``.
        assert search_embedding_identity() == "voyage:voyage-4:1024:document"

    def test_reads_the_config_at_call_time(self, mocker) -> None:
        """An operator pinning a legacy model through the
        ``TREE_MODELS__SEARCH_EMBEDDING__MODEL`` escape hatch must move the
        identity — otherwise the override silently reuses voyage-4 cache
        entries."""

        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(
                provider="voyage", model="voyage-3.5", dimensions=1024
            ),
        )

        assert search_embedding_identity() == "voyage:voyage-3.5:1024:document"

    def test_tracks_the_dimension_too(self, mocker) -> None:
        _set_embedding_blocks(
            mocker,
            resolution=EmbeddingConfig(provider="mock", dimensions=128),
            search=EmbeddingConfig(provider="voyage", model="voyage-4", dimensions=512),
        )

        assert search_embedding_identity() == "voyage:voyage-4:512:document"


class TestLLMIdentity:
    """ADR-009 decision 6's rule ("caches carry the identity of the model that
    produced them"), applied to the decision-10 ``models.llm`` switch: ONE
    helper renders the identity the two cached LLM tasks carry, so flipping
    ``gemini`` -> ``modal`` (or one model id -> another) is a cache MISS
    instead of a replay of the old LLM's JSON.
    """

    def test_default_identity(self, mocker) -> None:
        """The shipped ``configs/default.yaml`` renders
        ``gemini:gemini-3.1-flash-lite``.

        The module-level ``_mock_app_config`` fixture installs a ``MagicMock``
        for ``app_config``; this test puts the REAL import-time singleton back
        so the assertion is about the shipped YAML, not about the double.
        """

        mocker.patch("tree.models.get_model.app_config", real_app_config)

        assert llm_identity() == "gemini:gemini-3.1-flash-lite"

    def test_reads_the_config_at_call_time(self, mocker) -> None:
        """Nothing is frozen at import time: Prefect re-imports this module in
        flow-run subprocesses, so a ``models.llm`` switch made after import
        must still move the identity."""

        mocker.patch(
            "tree.models.get_model.app_config.models.llm",
            LLMConfig(provider="modal", model=_CATALOG_LLM),
        )

        assert llm_identity() == f"modal:{_CATALOG_LLM}"

    def test_env_override_moves_the_identity(self, mocker, tmp_path, monkeypatch):
        """Story 3: ``TREE_MODELS__LLM__MODEL=gemini-2.5-flash`` for ONE run
        neither reads nor pollutes the default model's cache entries."""

        custom = tmp_path / "models.yaml"
        custom.write_text(
            "models:\n  llm:\n    provider: gemini\n    model: gemini-3.1-flash-lite\n"
        )
        monkeypatch.setenv("TREE_MODELS__LLM__MODEL", "gemini-2.5-flash")

        mocker.patch("tree.models.get_model.app_config", load_app_config(custom))

        assert llm_identity() == "gemini:gemini-2.5-flash"

    def test_two_parts_only(self, mocker) -> None:
        """An LLM has no dimensions and no **Embedding role** — the identity is
        ``provider:model`` and nothing else, unlike the 4-part embedding one."""

        mocker.patch("tree.models.get_model.app_config", real_app_config)

        assert llm_identity().count(":") == 1


class TestModalBranch:
    """ADR-009 §3/§4: the Modal client is built from the catalog entry, the
    YAML ``dimensions`` and the **Proxy token** — and the branch stays lazy."""

    def test_the_client_gets_the_yaml_dimensions_and_the_bearer(self) -> None:
        """``cfg.dimensions`` used to be dropped, so a YAML asking for 512-d
        vectors silently got the model's native width."""

        cfg = EmbeddingConfig(provider="modal", model=_CATALOG_MODEL, dimensions=512)

        result = _build_embedding_model(cfg)

        assert isinstance(result, ModalEmbeddingModel)
        assert result.dimensions == 512
        # The joined Proxy token halves — what Modal's edge checks.
        assert result._proxy_token == "wk-1.ws-2"

    def test_a_half_proxy_token_fails_before_a_client_exists(self, mocker) -> None:
        """Story 5: a half token is no token — Modal would answer 401 before
        any container wakes, so the factory refuses to build the model."""

        mocker.patch.object(
            modal_catalog.settings, "modal_proxy_token_secret", SecretStr("")
        )
        cfg = EmbeddingConfig(provider="modal", model=_CATALOG_MODEL)

        with pytest.raises(ModelError, match="Modal proxy token is required"):
            _build_embedding_model(cfg)

    def test_importing_the_factory_does_not_import_modal(self) -> None:
        """MCP cold-boot budget: the Modal SDK is imported inside the branch,
        never at module level — for BOTH Modal clients. Run in a fresh
        interpreter so another test's import of a client cannot mask a
        regression."""

        probe = (
            "import sys, tree.models.get_model; "
            "assert 'modal' not in sys.modules, 'modal'; "
            "assert 'tree.models.modal_llm' not in sys.modules, 'modal_llm'; "
            "assert 'tree.models.modal_embedding' not in sys.modules, 'modal_embedding'"
        )

        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True
        )

        assert result.returncode == 0, (
            f"get_model import eagerly loaded the Modal SDK: {result.stderr}"
        )
