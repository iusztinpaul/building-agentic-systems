import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from tree.config.app_config import EmbeddingConfig
from tree.models.fake_model import MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM
from tree.models.get_model import (
    get_embedding_model,
    get_llm,
    get_resolution_embedding_model,
    get_search_embedding_model,
)
from tree.models.modal_embedding import ModalEmbeddingModel
from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel
from tree.models.voyage_embedding import VoyageTextEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel


@pytest.fixture(autouse=True)
def _mock_settings(mocker) -> None:
    mock_settings = MagicMock()
    mock_settings.google_api_key.get_secret_value.return_value = "fake-google-key"
    mock_settings.modal_embedding_api_key.get_secret_value.return_value = (
        "fake-modal-key"
    )
    mock_settings.voyage_api_key.get_secret_value.return_value = "fake-voyage-key"
    mocker.patch("tree.models.get_model.settings", mock_settings)


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

    def test_returns_modal_embedding(self) -> None:
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
        ["voyage-3", "voyage-3.5", "voyage-3-lite", "voyage-code-3"],
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
