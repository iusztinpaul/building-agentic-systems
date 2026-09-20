"""Pure-function tests for the **Embedding catalog** helpers (ADR-009 §2/§3).

Everything here runs against the REAL shipped catalog
(``configs/default.yaml``) — it is the object both the deploy driver (#139)
and the client (#140) read, so a test against a hand-rolled fixture would
prove nothing about what an operator boots.
"""

import math
import subprocess
import sys

import pytest
from pydantic import SecretStr

from tree.config.app_config import ModalEmbeddingModelConfig
from tree.models import modal_catalog
from tree.models.exceptions import ModelError
from tree.models.modal_catalog import (
    DEPLOY_SPEC_ENV,
    EMBEDDING_SERVER_NAME,
    HF_TOKEN_HINT,
    MODAL_ROUTING_REGION,
    EmbeddingDeploySpec,
    build_deploy_spec,
    build_server_args,
    get_catalog_entry,
    hf_token_args,
    hf_token_env,
    modal_cli_command,
    modal_proxy_bearer,
    prompt_for,
    redact_argv,
    resolve_serving,
    truncate_embedding,
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


class TestProxyBearer:
    """**Proxy token** auth (ADR-009 §4): the ONE credential every Serving
    path is reached through, joined exactly the way Modal documents it."""

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
    """The argv the driver runs. Token-free BY CONSTRUCTION (ADR-009 §9), so
    it is safe to log and to assert on."""

    def test_endpoint_without_custom_weights(self, qwen_entry) -> None:
        """Story 1: ``repo_id == base_model``, so Modal serves its own catalog
        model — no ``--custom-hf-*`` may appear."""

        assert modal_cli_command("deploy", qwen_entry, "endpoint") == [
            "modal",
            "endpoint",
            "create",
            "--name",
            "qwen3-embedding-0-6b",
            "--model",
            _QWEN,
            "--routing-region",
            "eu-west",
        ]

    def test_endpoint_with_custom_weights(self, voyage_entry) -> None:
        """Story 2: a repo_id that differs from base_model is served as CUSTOM
        weights on the base model's recipe."""

        assert modal_cli_command("deploy", voyage_entry, "endpoint") == [
            "modal",
            "endpoint",
            "create",
            "--name",
            "voyage-4-nano",
            "--model",
            _QWEN,
            "--custom-hf-repo",
            _VOYAGE,
            "--custom-hf-revision",
            _VOYAGE_SHA,
            "--routing-region",
            "eu-west",
        ]

    def test_never_public_never_token(self, qwen_entry, voyage_entry, mocker) -> None:
        """Two invariants in one: proxy auth is never waived
        (``--unauthenticated``), and the token joins the argv at the
        ``subprocess.run`` boundary ONLY — even with one set."""

        mocker.patch.object(
            modal_catalog.settings, "hf_token", SecretStr("hf_secret123")
        )

        for action in ("deploy", "stop"):
            for entry in (qwen_entry, voyage_entry):
                for serving in ("endpoint", "sglang", "vllm"):
                    argv = modal_cli_command(action, entry, serving)

                    assert "--unauthenticated" not in argv
                    assert "--custom-hf-token" not in argv
                    assert "hf_secret123" not in argv

    def test_script_and_stop_commands(self, qwen_entry, voyage_entry) -> None:
        """The two fallback paths deploy a script FILE, and every path is
        stopped by the name it was created with."""

        assert modal_cli_command("deploy", voyage_entry, "vllm") == [
            "modal",
            "deploy",
            "deploy/modal_vllm_embedding.py",
        ]
        assert modal_cli_command("deploy", qwen_entry, "sglang") == [
            "modal",
            "deploy",
            "deploy/modal_sglang_embedding.py",
        ]
        assert modal_cli_command("stop", qwen_entry, "endpoint") == [
            "modal",
            "endpoint",
            "stop",
            "-y",
            "qwen3-embedding-0-6b",
        ]
        # `-y` verified NECESSARY on the pinned client (modal 1.5.5,
        # modal/cli/app.py:573 `if not yes: ... confirm_or_suggest_yes`):
        # without it `modal app stop` pauses for a confirmation no driver can
        # answer.
        assert modal_cli_command("stop", qwen_entry, "sglang") == [
            "modal",
            "app",
            "stop",
            "-y",
            "ep-qwen3-embedding-0-6b",
        ]

    def test_the_routing_region_is_pinned(self) -> None:
        assert MODAL_ROUTING_REGION == "eu-west"


class TestHfTokenArgs:
    """The ONE place the Hugging Face token joins an argv (ADR-009 §9)."""

    def test_custom_weights_on_an_endpoint_get_the_token(self, voyage_entry) -> None:
        assert hf_token_args(voyage_entry, "endpoint", "hf_secret123") == [
            "--custom-hf-token",
            "hf_secret123",
        ]

    @pytest.mark.parametrize(
        "entry_name,serving,token",
        [
            ("qwen_entry", "endpoint", "hf_secret123"),
            ("voyage_entry", "vllm", "hf_secret123"),
            ("voyage_entry", "sglang", "hf_secret123"),
            ("voyage_entry", "endpoint", ""),
        ],
        ids=["no-custom-weights", "vllm-path", "sglang-path", "no-token"],
    )
    def test_every_other_case_adds_nothing(
        self, request, entry_name: str, serving: str, token: str
    ) -> None:
        """Modal documents ``--custom-hf-token`` as the token "for private
        --custom-hf-repo", the fallback scripts receive it as a Secret (#142),
        and an empty token must change no argv at all."""

        entry = request.getfixturevalue(entry_name)

        assert hf_token_args(entry, serving, token) == []


class TestHfTokenEnv:
    """What the fallback scripts' ephemeral ``modal.Secret`` carries (§9).

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
        argv = ["modal", "endpoint", "create", "--name", "voyage-4-nano"]

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
