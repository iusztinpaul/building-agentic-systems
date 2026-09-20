"""Pure-function tests for the **Modal catalog** helpers (ADR-009 §2/§3).

Everything here runs against the REAL shipped catalog
(``configs/default.yaml``) — it is the object both the deploy driver and the
clients read, so a test against a hand-rolled fixture would prove nothing
about what an operator boots.

The catalog knows WHICH model and what kind it is; it knows nothing about how
the model ends up served. That decision — and the argv the router picks — is
tested in ``test_modal_router.py`` and ``tests/unit/scripts``.
"""

import math
import subprocess
import sys

import pytest
from pydantic import SecretStr

from tree.config.app_config import (
    ModalEmbeddingModelConfig,
    ModalLLMModelConfig,
    app_config,
)
from tree.models import modal_catalog
from tree.models.exceptions import ModelError
from tree.models.modal_catalog import (
    EMBEDDING_DEPLOY_SPEC_ENV,
    EMBEDDING_SERVER_NAME,
    HF_TOKEN_HINT,
    LLM_DEPLOY_SPEC_ENV,
    MODAL_ROUTING_REGION,
    EmbeddingDeploySpec,
    LLMDeploySpec,
    app_script,
    build_deploy_spec,
    build_llm_deploy_spec,
    build_llm_server_args,
    build_server_args,
    get_catalog_entry,
    get_embedding_entry,
    get_llm_entry,
    hf_token_args,
    hf_token_env,
    looks_gated,
    modal_cli_command,
    modal_proxy_bearer,
    prompt_for,
    redact_argv,
    redact_text,
    truncate_embedding,
)

_QWEN = "Qwen/Qwen3-Embedding-0.6B"
_VOYAGE = "voyageai/voyage-4-nano"
_LFM = "LiquidAI/LFM2.5-350M"
_QWEN_LLM = "Qwen/Qwen3.5-0.8B"
_QWEN_SHA = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
_FAKE_TOKEN = "hf_secret123"
_VOYAGE_SHA = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
_LFM_SHA = "9e6c6ccf47cd318696e137d381a7ded8fe4df09f"


@pytest.fixture
def qwen_entry() -> ModalEmbeddingModelConfig:
    return get_embedding_entry(_QWEN)


@pytest.fixture
def voyage_entry() -> ModalEmbeddingModelConfig:
    return get_embedding_entry(_VOYAGE)


@pytest.fixture
def lfm_entry() -> ModalLLMModelConfig:
    return get_llm_entry(_LFM)


class TestGetCatalogEntry:
    """ONE lookup over BOTH lists (ADR-009 §3): the entry's KIND says which
    one it came from, so no caller passes a kind in."""

    def test_returns_the_entry_for_an_exact_repo_id(self, voyage_entry) -> None:
        assert voyage_entry.repo_id == _VOYAGE
        assert voyage_entry.kind == "embedding"

    def test_finds_an_llm_entry_in_the_other_list(self) -> None:
        entry = get_catalog_entry(_LFM)

        assert entry.repo_id == _LFM
        assert entry.kind == "llm"
        assert entry.app_name == "ep-tree-lfm2-5-350m"

    def test_unknown_model_lists_both_groups_sorted(self) -> None:
        """Story 7: a mistyped id fails loudly, naming every id it COULD have
        meant and the one file to edit."""

        with pytest.raises(ModelError) as excinfo:
            get_catalog_entry("BAAI/bge-m3")

        message = str(excinfo.value)
        assert "Unknown Modal model 'BAAI/bge-m3'" in message
        assert (
            f"Modal catalog ids — embeddings: {_QWEN}, {_VOYAGE}; "
            f"llms: {_LFM}, {_QWEN_LLM}." in message
        )
        assert "modal.embedding_models or modal.llm_models" in message

    def test_a_near_miss_is_not_resolved_by_prefix(self) -> None:
        """Matching is EXACT, never fuzzy — a truncated id must not silently
        resolve to the real model."""

        with pytest.raises(ModelError) as excinfo:
            get_catalog_entry("voyageai/voyage-4-nan")

        assert "Unknown Modal model 'voyageai/voyage-4-nan'" in str(excinfo.value)

    def test_an_llm_id_is_refused_where_an_embedding_is_needed(self) -> None:
        """The embedding client and the embedding deploy spec cannot serve a
        chat model — and an LLM entry has no `native_dimensions` at all."""

        with pytest.raises(ModelError) as excinfo:
            get_embedding_entry(_LFM)

        assert (
            f"{_LFM} is an LLM entry (modal.llm_models), not an embedding model."
            in str(excinfo.value)
        )

    def test_an_embedding_id_is_refused_where_an_llm_is_needed(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            get_llm_entry(_VOYAGE)

        assert (
            f"{_VOYAGE} is an embedding entry (modal.embedding_models), not an LLM."
            in str(excinfo.value)
        )

    @pytest.mark.parametrize(
        "getter", [get_embedding_entry, get_llm_entry], ids=["embedding", "llm"]
    )
    def test_an_unknown_id_still_lists_the_catalog(self, getter) -> None:
        """The kind check never shadows the unknown-id message."""

        with pytest.raises(ModelError) as excinfo:
            getter("BAAI/bge-m3")

        assert "Unknown Modal model" in str(excinfo.value)


class TestAppScript:
    def test_the_kind_picks_the_app(self) -> None:
        """One purpose per engine (ADR-009 §2): vLLM serves embeddings,
        SGLang serves LLMs."""

        assert app_script("embedding") == "deploy/modal_vllm_embedding.py"
        assert app_script("llm") == "deploy/modal_sglang_llm.py"


class TestServerArgs:
    def test_vllm_args_are_the_yaml_extras_plus_the_builder_owned_keys(
        self, voyage_entry
    ) -> None:
        args = build_server_args(voyage_entry)

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

    def test_no_entry_flags_matryoshka_server_side(self) -> None:
        """ADR-009 §3: the client truncates client-side on EVERY path.

        Modal's own 8B embedding recipe passes ``--hf-overrides
        {"is_matryoshka":true}`` while its 0.6B recipe does not — server-side
        ``dimensions`` support therefore differs between two managed recipes
        of the same family, which is exactly why no entry of ours asks for it.
        """

        for entry in app_config.modal.embedding_models:
            assert "is_matryoshka" not in str(build_server_args(entry))

    def test_args_omit_the_context_window_without_max_model_len(
        self, qwen_entry
    ) -> None:
        """A minimal entry sets no ``max_model_len``, so the builder must not
        invent one — the engine's own default wins."""

        assert build_server_args(qwen_entry) == {
            "--runner": "pooling",
            "--revision": _QWEN_SHA,
            "--served-model-name": _QWEN,
        }

    def test_a_new_entry_with_no_extras_gets_modals_baseline(self) -> None:
        """Story 1: an operator adds five YAML lines for a model Modal's
        catalog does not have, and the engine starts with exactly the
        baseline Modal's own embedding ``serve.py`` uses — plus the pinned
        revision and the context window the entry asks for."""

        entry = ModalEmbeddingModelConfig(
            repo_id="BAAI/bge-m3",
            revision="5617a9f61b028005a4858fdac845db406aefb181",
            native_dimensions=1024,
            gpu="A10",
            max_model_len=8192,
        )

        assert build_server_args(entry) == {
            "--served-model-name": "BAAI/bge-m3",
            "--runner": "pooling",
            "--revision": "5617a9f61b028005a4858fdac845db406aefb181",
            "--max-model-len": "8192",
        }

    def test_default_revision_is_main(self) -> None:
        """An entry that pins no commit sha serves the branch tip."""

        entry = ModalEmbeddingModelConfig(repo_id="BAAI/bge-m3", native_dimensions=1024)

        assert build_server_args(entry)["--revision"] == "main"

    def test_the_entry_is_not_mutated(self, voyage_entry) -> None:
        """The builder returns a NEW dict; a second call must see the YAML
        extras, not the previous call's builder-owned keys."""

        build_server_args(voyage_entry)

        assert "--runner" not in voyage_entry.extra_server_args


class TestLlmServerArgs:
    """ADR-009 §2: generic-safe SGLang flags — what ANY LLM takes, never the
    per-model tuning Modal's own 120B / 35B-MoE recipes carry."""

    def test_the_seed_gets_the_generic_baseline(self, lfm_entry) -> None:
        """Story 1: `LiquidAI/LFM2.5-350M` is five YAML lines, and the engine
        starts with the served id, the pinned sha, the context window the entry
        asks for and the two tuning defaults."""

        assert build_llm_server_args(lfm_entry) == {
            "--served-model-name": _LFM,
            "--revision": _LFM_SHA,
            "--trust-remote-code": "",
            "--mem-fraction-static": "0.85",
            "--context-length": "32768",
        }

    def test_nothing_modal_tuned_per_model_leaks_in(self, lfm_entry) -> None:
        """Speculative decoding needs a draft model FOR THAT MODEL; the mamba
        and multimodal flags describe Modal's Qwen3.6-35B-A3B-FP8. A generic
        script that assumed any of them would fail on the first model that is
        neither."""

        for key in build_llm_server_args(lfm_entry):
            assert not key.startswith(
                ("--speculative", "--mamba", "--enable-multimodal")
            )

    def test_an_entry_overrides_the_tuning_defaults(self) -> None:
        """Story 3: a bigger reasoning model adds its parsers and lowers the
        memory fraction — both are per-model facts, so the ENTRY wins."""

        entry = ModalLLMModelConfig(
            repo_id="Qwen/Qwen3.6-35B-A3B-FP8",
            revision="a" * 40,
            gpu="H100",
            n_gpus=2,
            extra_server_args={
                "--reasoning-parser": "qwen3",
                "--tool-call-parser": "qwen3_coder",
                "--mem-fraction-static": "0.75",
            },
        )

        assert build_llm_server_args(entry) == {
            "--served-model-name": "Qwen/Qwen3.6-35B-A3B-FP8",
            "--revision": "a" * 40,
            "--reasoning-parser": "qwen3",
            "--tool-call-parser": "qwen3_coder",
            "--trust-remote-code": "",
            "--mem-fraction-static": "0.75",
        }

    def test_an_attached_override_replaces_the_default(self) -> None:
        """``--flag=value`` and ``--flag value`` set the SAME flag
        (``autoinference-utils`` 0.2.6), so a dict-key merge would hand SGLang
        ``--mem-fraction-static 0.85 --mem-fraction-static=0.75``."""

        entry = ModalLLMModelConfig(
            repo_id="acme/llm",
            extra_server_args={"--mem-fraction-static=0.75": ""},
        )

        args = build_llm_server_args(entry)

        assert "--mem-fraction-static" not in args
        assert args["--mem-fraction-static=0.75"] == ""

    def test_the_context_window_is_omitted_without_max_model_len(self) -> None:
        entry = ModalLLMModelConfig(repo_id="acme/llm")

        assert build_llm_server_args(entry) == {
            "--served-model-name": "acme/llm",
            "--revision": "main",
            "--trust-remote-code": "",
            "--mem-fraction-static": "0.85",
        }

    def test_the_entry_is_not_mutated(self, lfm_entry) -> None:
        build_llm_server_args(lfm_entry)

        assert lfm_entry.extra_server_args == {}


class TestDeploySpec:
    @pytest.mark.parametrize("model", [_VOYAGE, _QWEN])
    def test_json_round_trip(self, model: str) -> None:
        """The spec crosses into the Modal container as ONE JSON env var
        (ADR-009 §3), so it must survive dump -> parse byte-for-byte."""

        spec = build_deploy_spec(model)

        restored = EmbeddingDeploySpec.model_validate_json(spec.model_dump_json())

        assert restored == spec
        assert restored.repo_id == model
        assert restored.engine_version == "0.26.0"
        assert restored.autoinference_utils_version == "0.2.6"
        assert restored.server_name == EMBEDDING_SERVER_NAME == "Server"
        assert restored.server_args == build_server_args(get_embedding_entry(model))

    def test_the_engine_is_the_scripts_so_the_spec_names_none(self) -> None:
        """The vLLM script is the only caller, so the spec carries vLLM's
        PINNED VERSION and no engine field at all — there is nothing left to
        choose (ADR-009 §2)."""

        spec = build_deploy_spec(_VOYAGE)

        assert not hasattr(spec, "engine")
        assert spec.engine_version == "0.26.0"
        assert "--runner" in spec.server_args

    def test_carries_the_hardware_and_the_app_name(self) -> None:
        spec = build_deploy_spec(_VOYAGE)

        assert spec.app_name == "ep-tree-voyage-4-nano"
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
            build_deploy_spec("BAAI/bge-m3")

    def test_an_llm_entry_has_no_embedding_deploy_spec(self) -> None:
        """The vLLM script serves embeddings only, so the spec builder refuses
        an LLM entry instead of inventing `native_dimensions` for it."""

        with pytest.raises(ModelError) as excinfo:
            build_deploy_spec(_LFM)

        assert "is an LLM entry" in str(excinfo.value)

    def test_deploy_spec_env_var_names(self) -> None:
        """One env var per KIND: a script that read the other one would fail on
        a missing key instead of on a wrong model."""

        assert EMBEDDING_DEPLOY_SPEC_ENV == "EMBEDDING_DEPLOY_SPEC"
        assert LLM_DEPLOY_SPEC_ENV == "LLM_DEPLOY_SPEC"


class TestBuildLlmDeploySpec:
    """What the SGLang App deploy is driven by (ADR-009 §2/§10)."""

    def test_the_seed_resolves_to_modals_llm_recipe_inputs(self) -> None:
        spec = build_llm_deploy_spec(_LFM)

        assert spec.app_name == "ep-tree-lfm2-5-350m"
        assert spec.repo_id == _LFM
        assert spec.revision == _LFM_SHA
        assert spec.gpu == "A10"
        assert spec.n_gpus == 1
        assert spec.cpu == 4
        assert spec.memory_mb == 16384
        # A DOCKER TAG of lmsysorg/sglang, not a PyPI version: the App runs the
        # official image.
        assert spec.engine_version == "v0.5.18"
        assert spec.autoinference_utils_version == "0.2.6"
        assert spec.server_name == EMBEDDING_SERVER_NAME == "Server"
        assert spec.server_args == {
            "--served-model-name": _LFM,
            "--revision": _LFM_SHA,
            "--trust-remote-code": "",
            "--mem-fraction-static": "0.85",
            "--context-length": "32768",
        }

    def test_json_round_trip(self) -> None:
        """It crosses into the container as ONE JSON env var (ADR-009 §3)."""

        spec = build_llm_deploy_spec(_LFM)

        assert LLMDeploySpec.model_validate_json(spec.model_dump_json()) == spec

    def test_no_credential_is_ever_in_the_spec(self, mocker) -> None:
        """ADR-009 §9: the spec is baked into a cached, inspectable image
        layer, so the Hugging Face token travels as an ephemeral Secret — even
        with one configured, the dump must not mention it."""

        mocker.patch.object(modal_catalog.settings, "hf_token", SecretStr(_FAKE_TOKEN))

        dumped = build_llm_deploy_spec(_LFM).model_dump_json()

        assert "HF_TOKEN" not in dumped
        assert _FAKE_TOKEN not in dumped

    def test_an_embedding_entry_is_refused(self) -> None:
        """Story 5: the LLM script pointed at an embedding model fails BEFORE
        any image is built (through the driver the kind picks the script, so
        this can only happen by hand)."""

        with pytest.raises(ModelError) as excinfo:
            build_llm_deploy_spec(_VOYAGE)

        assert (
            f"{_VOYAGE} is an embedding entry (modal.embedding_models), not an LLM."
            in str(excinfo.value)
        )

    def test_unknown_model_raises_model_error(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            build_llm_deploy_spec("BAAI/bge-m3")

        assert "Unknown Modal model" in str(excinfo.value)

    def test_a_missing_engine_pin_names_the_engines_that_exist(
        self, mocker, lfm_entry
    ) -> None:
        """A YAML edit that drops the pin must fail HERE, not in a container
        that pulled `lmsysorg/sglang:None`."""

        mocker.patch.object(
            app_config.modal, "engines", {"vllm": app_config.modal.engines["vllm"]}
        )

        with pytest.raises(ModelError) as excinfo:
            build_llm_deploy_spec(_LFM)

        assert "No pinned version for engine 'sglang'" in str(excinfo.value)
        assert "Engines in modal.engines: vllm." in str(excinfo.value)


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


class TestProxyBearer:
    """**Proxy token** auth (ADR-009 §4): the ONE credential BOTH Serving
    paths are reached through, joined exactly the way Modal documents it."""

    def test_joins_the_id_and_the_secret_with_a_dot(self, mocker) -> None:
        mocker.patch.object(
            modal_catalog.settings, "modal_proxy_token_id", SecretStr("wk-1")
        )
        mocker.patch.object(
            modal_catalog.settings, "modal_proxy_token_secret", SecretStr("ws-2")
        )

        assert modal_proxy_bearer() == "wk-1.ws-2"

    @pytest.mark.parametrize(
        "token_id,token_secret",
        [("", "ws-2"), ("wk-1", ""), ("", "")],
        ids=["no-id", "no-secret", "neither"],
    )
    def test_a_missing_half_names_both_env_vars(
        self, mocker, token_id: str, token_secret: str
    ) -> None:
        """Half a proxy token is no proxy token: the operator is told the two
        names to put in ``.env``, not handed a 401 from Modal's edge."""

        mocker.patch.object(
            modal_catalog.settings, "modal_proxy_token_id", SecretStr(token_id)
        )
        mocker.patch.object(
            modal_catalog.settings, "modal_proxy_token_secret", SecretStr(token_secret)
        )

        with pytest.raises(ModelError) as excinfo:
            modal_proxy_bearer()

        message = str(excinfo.value)
        assert "MODAL_PROXY_TOKEN_ID" in message
        assert "MODAL_PROXY_TOKEN_SECRET" in message


class TestModalCliCommand:
    """The argv the ROUTER runs. Token-free BY CONSTRUCTION (ADR-009 §9), so
    it is safe to log and to assert on."""

    def test_an_endpoint_create_serves_the_repo_id_itself(self, qwen_entry) -> None:
        """Story 1: no base model is passed, because the catalog no longer
        guesses at one — Modal is asked about the model itself."""

        assert modal_cli_command("deploy", qwen_entry, "endpoint") == [
            "modal",
            "endpoint",
            "create",
            "--name",
            "tree-qwen3-embedding-0-6b",
            "--model",
            _QWEN,
            "--routing-region",
            "eu-west",
        ]

    def test_a_base_model_makes_it_a_custom_weights_create(self, voyage_entry) -> None:
        """Story 3: the base comes from the RUNTIME lineage lookup, so the
        weights are served on that base's recipe."""

        assert modal_cli_command(
            "deploy", voyage_entry, "endpoint", base_model=_QWEN
        ) == [
            "modal",
            "endpoint",
            "create",
            "--name",
            "tree-voyage-4-nano",
            "--model",
            _QWEN,
            "--custom-hf-repo",
            _VOYAGE,
            "--custom-hf-revision",
            _VOYAGE_SHA,
            "--routing-region",
            "eu-west",
        ]

    def test_a_base_equal_to_the_repo_id_is_not_custom_weights(
        self, qwen_entry
    ) -> None:
        """A model that IS its own catalog base needs no ``--custom-hf-*``:
        Modal serves its own snapshot."""

        assert modal_cli_command(
            "deploy", qwen_entry, "endpoint", base_model=_QWEN
        ) == (modal_cli_command("deploy", qwen_entry, "endpoint"))

    def test_the_app_deploy_is_the_script_for_the_kind(
        self, voyage_entry, lfm_entry
    ) -> None:
        """The KIND picks the script — an embedding entry can no longer be
        sent to SGLang, and an LLM entry can no longer be sent to vLLM."""

        assert modal_cli_command("deploy", voyage_entry, "app") == [
            "modal",
            "deploy",
            "deploy/modal_vllm_embedding.py",
        ]
        assert modal_cli_command("deploy", lfm_entry, "app") == [
            "modal",
            "deploy",
            "deploy/modal_sglang_llm.py",
        ]

    def test_the_two_stop_commands(self, qwen_entry, voyage_entry) -> None:
        """``stop`` is path-blind (ADR-009 §2), so BOTH argvs are built for
        every model and tried in order."""

        assert modal_cli_command("stop", qwen_entry, "endpoint") == [
            "modal",
            "endpoint",
            "stop",
            "-y",
            "tree-qwen3-embedding-0-6b",
        ]
        # `-y` verified NECESSARY on the pinned client (modal 1.5.5,
        # modal/cli/app.py:573 `if not yes: ... confirm_or_suggest_yes`):
        # without it `modal app stop` pauses for a confirmation no driver can
        # answer.
        assert modal_cli_command("stop", voyage_entry, "app") == [
            "modal",
            "app",
            "stop",
            "-y",
            "ep-tree-voyage-4-nano",
        ]

    def test_never_public_never_token(
        self, qwen_entry, voyage_entry, lfm_entry, mocker
    ) -> None:
        """Two invariants in one: proxy auth is never waived
        (``--unauthenticated``), and the token joins the argv at the
        ``subprocess.run`` boundary ONLY — even with one set."""

        mocker.patch.object(
            modal_catalog.settings, "hf_token", SecretStr("hf_secret123")
        )

        for action in ("deploy", "stop"):
            for entry in (qwen_entry, voyage_entry, lfm_entry):
                for target in ("endpoint", "app"):
                    for base in (None, _QWEN):
                        argv = modal_cli_command(action, entry, target, base_model=base)

                        assert "--unauthenticated" not in argv
                        assert "--custom-hf-token" not in argv
                        assert "hf_secret123" not in argv

    def test_modal_cli_command_uses_prefixed_names(
        self, qwen_entry, voyage_entry
    ) -> None:
        """One test for the namespace as the CLI sees it: every name in every
        argv — and in the deploy spec the App scripts read — carries
        ``tree-``, so nothing this project runs can name a Dedicated Endpoint
        an operator created by hand (ADR-009 §3)."""

        create = modal_cli_command("deploy", qwen_entry, "endpoint")

        assert create[create.index("--name") + 1] == "tree-qwen3-embedding-0-6b"
        assert modal_cli_command("stop", qwen_entry, "endpoint")[-1] == (
            "tree-qwen3-embedding-0-6b"
        )
        assert modal_cli_command("stop", voyage_entry, "app")[-1] == (
            "ep-tree-voyage-4-nano"
        )
        assert build_deploy_spec(_VOYAGE).app_name == "ep-tree-voyage-4-nano"

    def test_the_routing_region_is_pinned(self) -> None:
        assert MODAL_ROUTING_REGION == "eu-west"


class TestHfTokenArgs:
    """The ONE place the Hugging Face token joins an argv (ADR-009 §9)."""

    def test_a_custom_weights_create_gets_the_token(self) -> None:
        assert hf_token_args(_QWEN, "hf_secret123") == [
            "--custom-hf-token",
            "hf_secret123",
        ]

    @pytest.mark.parametrize(
        "base_model,token",
        [(None, "hf_secret123"), (_QWEN, ""), (None, "")],
        ids=["no-custom-weights", "no-token", "neither"],
    )
    def test_every_other_case_adds_nothing(
        self, base_model: str | None, token: str
    ) -> None:
        """Modal documents ``--custom-hf-token`` as the token "for private
        --custom-hf-repo", our App scripts receive it as a Secret (#142), and
        an empty token must change no argv at all."""

        assert hf_token_args(base_model, token) == []


class TestHfTokenEnv:
    """What the App scripts' ephemeral ``modal.Secret`` carries (§9).

    Every token here is FAKE and ``settings`` is patched on the binding THIS
    module holds: ``make`` exports the developer's real ``.env`` into the test
    process, and a test that read it would leak it and pass for the wrong
    reason.
    """

    def test_a_configured_token_becomes_the_one_env_var(self, mocker) -> None:
        mocker.patch.object(
            modal_catalog.settings, "hf_token", SecretStr("hf_secret123")
        )

        assert hf_token_env() == {"HF_TOKEN": "hf_secret123"}

    def test_no_token_is_an_empty_dict(self, mocker) -> None:
        """``Secret.from_dict({})`` — the list still has exactly one element,
        so the local and container sides of a script have the same shape."""

        mocker.patch.object(modal_catalog.settings, "hf_token", SecretStr(""))

        assert hf_token_env() == {}


class TestRedactArgv:
    """Every argv the driver logs passes through here."""

    def test_the_value_after_the_flag_becomes_stars(self) -> None:
        assert redact_argv(
            ["modal", "endpoint", "create", "--custom-hf-token", "hf_secret123"]
        ) == ["modal", "endpoint", "create", "--custom-hf-token", "***"]

    def test_a_repeated_flag_redacts_every_value(self) -> None:
        """Redaction is a discipline, not a type (ADR-009 §9): it must not
        depend on the argv carrying exactly one token pair."""

        assert redact_argv(
            [
                "modal",
                "--custom-hf-token",
                "hf_secret123",
                "--custom-hf-token",
                "hf_other456",
            ]
        ) == ["modal", "--custom-hf-token", "***", "--custom-hf-token", "***"]

    def test_an_argv_without_the_flag_is_unchanged(self) -> None:
        argv = ["modal", "endpoint", "create", "--name", "tree-voyage-4-nano"]

        assert redact_argv(argv) == argv

    def test_the_input_is_not_mutated(self) -> None:
        """The caller runs the ORIGINAL argv after logging the redacted copy —
        redacting in place would send ``***`` to Modal as the token."""

        argv = ["modal", "--custom-hf-token", "hf_secret123"]

        redact_argv(argv)

        assert argv == ["modal", "--custom-hf-token", "hf_secret123"]

    def test_a_trailing_flag_does_not_raise(self) -> None:
        """A malformed argv must not crash the logging path."""

        assert redact_argv(["modal", "--custom-hf-token"]) == [
            "modal",
            "--custom-hf-token",
        ]


class TestHfTokenHint:
    def test_the_hint_names_the_repo_and_the_env_var(self) -> None:
        hint = HF_TOKEN_HINT.format(repo_id=_VOYAGE)

        assert hint.startswith(f"If {_VOYAGE} is a private or gated")
        assert "HF_TOKEN" in hint
        assert "https://huggingface.co/settings/tokens" in hint


class TestRedactText:
    """Belt and braces for CAPTURED ``modal`` output (ADR-009 §9)."""

    def test_every_occurrence_of_the_token_becomes_stars(self) -> None:
        text = f"downloading with {_FAKE_TOKEN}\nfailed with {_FAKE_TOKEN}"

        redacted = redact_text(text, _FAKE_TOKEN)

        assert _FAKE_TOKEN not in redacted
        assert redacted.count("***") == 2

    def test_an_empty_token_is_a_no_op(self) -> None:
        """``"".replace`` would splice ``***`` between every character —
        turning "no token set" into unreadable output."""

        assert redact_text("modal: all good", "") == "modal: all good"


class TestLooksGated:
    """The gate on the ONE ``HF_TOKEN`` hint (ADR-009 §9). The false cases are
    the live texts that printed the hint before this existed — none of them is
    fixed by a token."""

    @pytest.mark.parametrize(
        "text",
        [
            "401 Client Error",
            "403 Forbidden",
            "GatedRepoError: You must accept the licence",
            "Cannot access gated repo for url https://huggingface.co/...",
            "RepositoryNotFoundError: repo not found",
            "You do not have access to model acme/private",
        ],
    )
    def test_a_gated_looking_failure(self, text: str) -> None:
        assert looks_gated(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "'voyageai/voyage-4-nano' is not available for dedicated Endpoints.",
            "The custom model is not a servable checkpoint of base model "
            "'Qwen/Qwen3-Embedding-0.6B': hidden_size 1024 != 2048",
            "Health check on https://x/health answered 503.",
            "",
        ],
    )
    def test_a_failure_no_token_would_fix(self, text: str) -> None:
        assert looks_gated(text) is False

    def test_the_match_is_case_insensitive(self) -> None:
        assert looks_gated("GATEDREPOERROR") is True


class TestTruncateEmbedding:
    """Client-side Matryoshka truncation (ADR-009 §3) — the ONE helper the
    smoke test and the client share, so what the smoke test proves is exactly
    what reaches the 1024-d mongot index."""

    def test_slices_then_renormalises(self) -> None:
        result = truncate_embedding([3.0, 4.0, 12.0], 2)

        assert result == pytest.approx([0.6, 0.8], abs=1e-9)

    def test_a_native_width_vector_comes_back_unit_length(self) -> None:
        """voyage-4-nano's 2048 -> 1024: the width the memory stores."""

        vector = [1.0 / math.sqrt(2048)] * 2048

        result = truncate_embedding(vector, 1024)

        assert len(result) == 1024
        assert math.sqrt(sum(v * v for v in result)) == pytest.approx(1.0, abs=1e-6)

    def test_truncating_to_the_full_width_renormalises(self) -> None:
        vector = [3.0, 4.0]

        assert truncate_embedding(vector, len(vector)) == pytest.approx([0.6, 0.8])

    def test_a_zero_vector_is_returned_sliced(self) -> None:
        """No division by zero: a degenerate vector is a server problem, and
        the length assertion above it is what catches it."""

        assert truncate_embedding([0.0, 0.0, 0.0], 2) == [0.0, 0.0]

    @pytest.mark.parametrize("dimensions", [0, -1, -1024])
    def test_a_non_positive_width_is_refused(self, dimensions: int) -> None:
        """The width comes from a CALLER (the client's effective
        ``dimensions``), and Python's negative-slice semantics would turn
        ``-1024`` into a silently shorter vector instead of an error."""

        with pytest.raises(ModelError) as excinfo:
            truncate_embedding([1.0] * 2048, dimensions)

        assert f"cannot truncate to {dimensions}-d" in str(excinfo.value)

    def test_widening_is_refused(self) -> None:
        with pytest.raises(ModelError) as excinfo:
            truncate_embedding([1.0] * 1024, 2048)

        assert "cannot truncate a 1024-d vector to 2048-d" in str(excinfo.value)

    def test_the_input_is_not_mutated(self) -> None:
        vector = [3.0, 4.0, 12.0]

        truncate_embedding(vector, 2)

        assert vector == [3.0, 4.0, 12.0]


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
