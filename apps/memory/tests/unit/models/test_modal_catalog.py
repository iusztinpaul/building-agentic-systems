"""Pure-function tests for the **Embedding catalog** helpers (ADR-009 §2/§3).

Everything here runs against the REAL shipped catalog
(``configs/default.yaml``) — it is the object both the deploy driver (#139)
and the client (#140) read, so a test against a hand-rolled fixture would
prove nothing about what an operator boots.
"""

import subprocess
import sys

import pytest

from tree.config.app_config import ModalEmbeddingModelConfig
from tree.models.exceptions import ModelError
from tree.models.modal_catalog import (
    DEPLOY_SPEC_ENV,
    EMBEDDING_SERVER_NAME,
    EmbeddingDeploySpec,
    build_deploy_spec,
    build_server_args,
    get_catalog_entry,
    prompt_for,
    resolve_serving,
)

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_QWEN_SHA = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
_VOYAGE_SHA = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"


@pytest.fixture
def qwen_entry() -> ModalEmbeddingModelConfig:
    return get_catalog_entry(_QWEN)


@pytest.fixture
def voyage_entry() -> ModalEmbeddingModelConfig:
    return get_catalog_entry(_VOYAGE)


class TestLookup:
    def test_returns_the_entry_for_an_exact_repo_id(self, voyage_entry) -> None:
        assert voyage_entry.repo_id == _VOYAGE
        assert voyage_entry.serving == "vllm"

    def test_unknown_model_lists_the_catalog_ids_sorted(self) -> None:
        """Story 4: a mistyped id fails loudly, naming every id it COULD have
        meant and the one file to edit."""

        with pytest.raises(ModelError) as excinfo:
            get_catalog_entry("BAAI/bge-m3")

        message = str(excinfo.value)
        assert "BAAI/bge-m3" in message
        assert f"Embedding catalog ids: {_QWEN}, {_VOYAGE}." in message
        assert "modal.embedding_models" in message

    def test_a_near_miss_is_not_resolved_by_prefix(self) -> None:
        """Story 4's exact input: matching is EXACT, never fuzzy — a truncated
        id must not silently resolve to the real model."""

        with pytest.raises(ModelError) as excinfo:
            get_catalog_entry("voyageai/voyage-4-nan")

        assert "Unknown Modal embedding model 'voyageai/voyage-4-nan'" in str(
            excinfo.value
        )


class TestResolveServing:
    def test_no_override_uses_the_yaml_path(self, qwen_entry) -> None:
        assert resolve_serving(qwen_entry, None) == "endpoint"

    def test_empty_override_uses_the_yaml_path(self, qwen_entry) -> None:
        """``SERVING=`` (an unset Make variable) is not an override."""

        assert resolve_serving(qwen_entry, "") == "endpoint"

    def test_override_walks_the_ladder_without_editing_yaml(
        self, qwen_entry, voyage_entry
    ) -> None:
        assert resolve_serving(qwen_entry, "sglang") == "sglang"
        # voyage-4-nano's YAML path is vllm, but it carries a base_model so the
        # endpoint path can be attempted (#141) for one command.
        assert resolve_serving(voyage_entry, "endpoint") == "endpoint"

    def test_unknown_path_names_the_three(self, qwen_entry) -> None:
        with pytest.raises(ModelError) as excinfo:
            resolve_serving(qwen_entry, "tgi")

        message = str(excinfo.value)
        assert "tgi" in message
        assert "endpoint, sglang, vllm" in message

    def test_endpoint_override_without_base_model_is_refused(self) -> None:
        """The base-model rule is enforced at OVERRIDE time too — a Dedicated
        endpoint has nothing to pass as ``--model`` without it."""

        entry = ModalEmbeddingModelConfig(
            repo_id="acme/my-embedder", serving="vllm", native_dimensions=768
        )

        with pytest.raises(ModelError) as excinfo:
            resolve_serving(entry, "endpoint")

        message = str(excinfo.value)
        assert "acme/my-embedder" in message
        assert "base_model" in message


class TestServerArgs:
    def test_vllm_args_are_the_yaml_extras_plus_the_builder_owned_keys(
        self, voyage_entry
    ) -> None:
        args = build_server_args(voyage_entry, "vllm")

        assert args == {
            "--convert": "embed",
            "--pooler-config": '{"pooling_type":"MEAN"}',
            "--hf-overrides": '{"architectures":["VoyageQwen3BidirectionalEmbedModel"]}',
            "--dtype": "bfloat16",
            "--enforce-eager": "",
            "--trust-remote-code": "",
            "--runner": "pooling",
            "--revision": _VOYAGE_SHA,
            "--served-model-name": _VOYAGE,
            "--max-model-len": "32768",
        }

    def test_sglang_args_omit_context_length_without_max_model_len(
        self, qwen_entry
    ) -> None:
        """A minimal entry sets no ``max_model_len``, so the builder must not
        invent one — the engine's own default wins."""

        assert build_server_args(qwen_entry, "sglang") == {
            "--is-embedding": "",
            "--revision": _QWEN_SHA,
            "--served-model-name": _QWEN,
        }

    def test_the_same_entry_runs_under_either_engine(self, qwen_entry) -> None:
        """One catalog entry, two fallback scripts: the embedding-mode flag is
        the BUILDER's, which is what lets SERVING= walk the ladder."""

        assert build_server_args(qwen_entry, "vllm") == {
            "--runner": "pooling",
            "--revision": _QWEN_SHA,
            "--served-model-name": _QWEN,
        }

    def test_default_revision_is_main(self) -> None:
        """An entry that pins no commit sha serves the branch tip."""

        entry = ModalEmbeddingModelConfig(
            repo_id="BAAI/bge-m3", base_model="BAAI/bge-m3", native_dimensions=1024
        )

        assert build_server_args(entry, "vllm")["--revision"] == "main"

    def test_the_entry_is_not_mutated(self, voyage_entry) -> None:
        """The builder returns a NEW dict; a second call must see the YAML
        extras, not the previous call's builder-owned keys."""

        build_server_args(voyage_entry, "vllm")

        assert "--runner" not in voyage_entry.extra_server_args


class TestDeploySpec:
    @pytest.mark.parametrize(
        "model,engine,engine_version",
        [(_VOYAGE, "vllm", "0.17.1"), (_QWEN, "sglang", "0.5.20")],
    )
    def test_json_round_trip(
        self, model: str, engine: str, engine_version: str
    ) -> None:
        """The spec crosses into the Modal container as ONE JSON env var
        (ADR-009 §3), so it must survive dump -> parse byte-for-byte."""

        spec = build_deploy_spec(model, engine)

        restored = EmbeddingDeploySpec.model_validate_json(spec.model_dump_json())

        assert restored == spec
        assert restored.repo_id == model
        assert restored.engine == engine
        assert restored.engine_version == engine_version
        assert restored.autoinference_utils_version == "0.2.6"
        assert restored.server_name == EMBEDDING_SERVER_NAME == "Server"
        assert restored.server_args == build_server_args(
            get_catalog_entry(model), engine
        )

    def test_the_engine_comes_from_the_caller_not_the_entry(self) -> None:
        """``build_deploy_spec`` takes the SCRIPT's engine, so
        ``SERVING=sglang`` on a ``serving: vllm`` entry deploys SGLang."""

        spec = build_deploy_spec(_VOYAGE, "sglang")

        assert spec.engine == "sglang"
        assert spec.engine_version == "0.5.20"
        assert "--is-embedding" in spec.server_args

    def test_carries_the_hardware_and_the_app_name(self) -> None:
        spec = build_deploy_spec(_VOYAGE, "vllm")

        assert spec.app_name == "ep-voyage-4-nano"
        assert spec.gpu == "A10"
        assert spec.cpu == 4
        assert spec.memory_mb == 16384
        # voyage-4-nano's native width is the custom AutoModel's learned
        # 1024->2048 `linear` head (config.json `num_labels`), not its
        # `hidden_size` — the deploy spec carries what the server RETURNS.
        assert spec.native_dimensions == 2048
        assert spec.revision == _VOYAGE_SHA

    def test_unknown_model_raises_model_error(self) -> None:
        with pytest.raises(ModelError):
            build_deploy_spec("BAAI/bge-m3", "vllm")

    def test_deploy_spec_env_var_name(self) -> None:
        assert DEPLOY_SPEC_ENV == "EMBEDDING_DEPLOY_SPEC"


class TestPrompts:
    def test_voyage_prompts_come_from_the_catalog(self, voyage_entry) -> None:
        """OpenAI-compatible ``/v1/embeddings`` has no role field, so the
        **Embedding role** becomes a client-side prefix (ADR-009 §5)."""

        assert (
            prompt_for(voyage_entry, "query")
            == "Represent the query for retrieving supporting documents: "
        )
        assert (
            prompt_for(voyage_entry, "document")
            == "Represent the document for retrieval: "
        )

    def test_no_role_means_no_prompt(self, voyage_entry) -> None:
        assert prompt_for(voyage_entry, None) == ""

    def test_an_unset_prompt_is_empty(self, qwen_entry) -> None:
        """Qwen3 instructs only the QUERY side; documents are embedded raw."""

        assert prompt_for(qwen_entry, "document") == ""
        assert prompt_for(qwen_entry, "query").startswith("Instruct: ")


def test_catalog_does_not_import_modal() -> None:
    """The helpers are pure config: importing them must NOT pull in the
    ``modal`` SDK, so the MCP boot path and the unit suite never pay for it
    (and the module stays importable without the ``local-models`` extra)."""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tree.models.modal_catalog; assert 'modal' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
