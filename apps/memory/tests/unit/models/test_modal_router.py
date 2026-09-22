"""The router's pure half: what Modal's text MEANS, and what the Hub knows.

The routing DECISION (which argv runs, which exit code, which ``Routing …``
line) is tested through the driver in
``tests/unit/scripts/test_modal_model_script.py``; here live the three
functions that read text nobody promised us:
:func:`classify_endpoint_refusal`, :func:`parse_endpoint_catalog` and
:func:`hf_base_models`.

The two refusals are VENDORED from the live CLI (2026-09-20, modal 1.5.5) into
``fixtures/real_*_boxed.txt`` and read byte for byte: a refusal arrives inside
a Rich box, so the fixture carries the borders and the wrapping — which is
exactly what a naive ``in`` check would miss — and the bullet list is Modal's
own 44 models, not a synthetic list of the same shape. Two hand-written
fixtures remain, each one narrower than the terminal the real output was
captured at, so the marker sentence itself breaks across lines. Every Hugging
Face call is an ``httpx.MockTransport``: no test here touches the network (the
shared ``hub`` fixture, ``tests/unit/models/modal_fixtures.py``).
"""

import ast
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tree.config.app_config import ModalEmbeddingModelConfig
from tree.models import modal_router
from tree.models.modal_router import (
    classify_endpoint_refusal,
    hf_base_models,
    parse_endpoint_catalog,
    run_deploy,
)

_FAKE_TOKEN = "hf_secret123"

# The model both vendored refusals were captured against.
_VOYAGE = "voyageai/voyage-4-nano"

# The substring the classifier matches — asserted ABSENT from the wrapped
# fixture, so the test proves normalisation and not a lucky `in`.
_ON_ONE_LINE = "is not available for dedicated Endpoints"
_NOT_SERVABLE_ON_ONE_LINE = "is not a servable checkpoint of base model"

# --- The live refusal texts (2026-09-20), boxed exactly as the CLI prints ---

# VENDORED from the real `modal endpoint create` runs of tasks/145, byte for
# byte: the whole design rests on two substrings of Modal's prose and on a
# best-effort parse of its bullet list, so the fixtures are the OUTPUT, not a
# hand-written imitation of it. They carry public model ids and Modal's own
# messages — no URL, no workspace name, no credential.
_FIXTURES = Path(__file__).resolve().parent / "fixtures"

NOT_IN_CATALOG = (_FIXTURES / "real_not_in_catalog_boxed.txt").read_text(
    encoding="utf-8"
)
NOT_SERVABLE = (_FIXTURES / "real_not_servable_boxed.txt").read_text(encoding="utf-8")

# The same verdict with the sentence WRAPPED by the box: `dedicated` and
# `Endpoints` end up on different lines, so a substring match on the raw text
# fails and only the normalised one holds.
NOT_IN_CATALOG_WRAPPED = """
╭─────────────────────────── Error ────────────────────────────╮
│ 'voyageai/voyage-4-nano' is not available for dedicated      │
│ Endpoints.                                                   │
╰──────────────────────────────────────────────────────────────╯
"""

# The same verdict at a NARROWER terminal, where the marker sentence itself
# breaks: `base` and `model` land on different lines with two borders between
# them, so a substring match on the raw text fails.
NOT_SERVABLE_WRAPPED = """
╭──────────────────────────── Error ─────────────────────────╮
│ The custom model is not a servable checkpoint of base      │
│ model 'Qwen/Qwen3-Embedding-0.6B': Custom model            │
│ hidden_size=2560 does not match base model                 │
│ hidden_size=1024                                           │
╰────────────────────────────────────────────────────────────╯
"""

# How many models Modal's endpoint catalog held that day (ADR-009 Context) —
# 2 of them embedding models, which is why our Apps are the general path.
_CATALOG_SIZE = 44


def _card(
    base_model: object = None, relation: str | None = None, **extra: object
) -> dict:
    """One Hugging Face model card, shaped like the real API answer."""

    data: dict[str, object] = dict(extra)
    if base_model is not None:
        data["base_model"] = base_model
    if relation is not None:
        data["base_model_relation"] = relation
    return {"modelId": "acme/model", "cardData": data}


class TestClassifyEndpointRefusal:
    """Two substrings decide the whole route — so they are pinned against the
    exact texts Modal printed on 2026-09-20."""

    def test_the_live_not_in_catalog_refusal(self) -> None:
        """The vendored CLI output, byte for byte."""

        assert classify_endpoint_refusal(NOT_IN_CATALOG) == "not_in_catalog"

    def test_a_wrapped_not_in_catalog_refusal(self) -> None:
        """Rich wraps the sentence at the box width: `dedicated` and
        `Endpoints` land on different lines with two borders between them.

        Synthetic, and deliberately so: the vendored refusal happens to fit on
        one line at the width it was captured at, so only this fixture proves
        the normalisation for THIS verdict. The vendored ``not_servable`` one
        is genuinely wrapped and covers the other half.
        """

        assert _ON_ONE_LINE not in NOT_IN_CATALOG_WRAPPED
        assert classify_endpoint_refusal(NOT_IN_CATALOG_WRAPPED) == "not_in_catalog"

    def test_the_live_architecture_mismatch(self) -> None:
        """The vendored custom-weights refusal, byte for byte — four config
        mismatches inside a box, which is what "custom weights = a
        SAME-ARCHITECTURE fine-tune" looks like from the CLI."""

        assert "hidden_size=2560 does not match" in " ".join(NOT_SERVABLE.split())
        assert classify_endpoint_refusal(NOT_SERVABLE) == "not_servable"

    def test_a_wrapped_architecture_mismatch(self) -> None:
        """Synthetic, like its not-in-catalog twin: at the width the vendored
        output was captured, the marker sentence happens to fit on one line,
        so only this fixture splits it across two."""

        assert _NOT_SERVABLE_ON_ONE_LINE not in NOT_SERVABLE_WRAPPED
        assert classify_endpoint_refusal(NOT_SERVABLE_WRAPPED) == "not_servable"

    def test_the_unwrapped_architecture_mismatch(self) -> None:
        assert (
            classify_endpoint_refusal(
                "The custom model is not a servable checkpoint of base model "
                "'Qwen/Qwen3-Embedding-0.6B': hidden_size 2560 != 1024"
            )
            == "not_servable"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Token missing. Could not authenticate client.",
            "Error: quota exceeded for GPU A10 in this workspace",
            "Connection error.",
            "",
        ],
        ids=["auth", "quota", "network", "empty"],
    )
    def test_everything_else_is_other(self, text: str) -> None:
        """An unknown failure is not a verdict: `other` aborts the deploy with
        Modal's own message instead of deploying an App on a guess."""

        assert classify_endpoint_refusal(text) == "other"


class TestParseEndpointCatalog:
    def test_the_full_live_list(self) -> None:
        """Every bullet line after the header, borders and padding stripped —
        on the REAL output, not a synthetic one of the same shape.

        The four ids ADR-009, the glossary and the seeds rest on are asserted
        by name: the two embedding models of 44 are exactly why our Apps are
        the general path, and the two LLMs are the catalog's seeds.
        """

        ids = parse_endpoint_catalog(NOT_IN_CATALOG)

        assert len(ids) == _CATALOG_SIZE
        assert ids == sorted(ids), "Modal prints the list sorted"
        for repo_id in (
            "Qwen/Qwen3-Embedding-0.6B",
            "Qwen/Qwen3-Embedding-8B",
            "openai/gpt-oss-120b",
            "Qwen/Qwen3.5-0.8B",
        ):
            assert repo_id in ids

    def test_only_two_of_the_forty_four_are_embedding_models(self) -> None:
        """The number ADR-009's Context rests on, read off the live list."""

        ids = parse_endpoint_catalog(NOT_IN_CATALOG)

        assert [repo_id for repo_id in ids if "Embedding" in repo_id] == [
            "Qwen/Qwen3-Embedding-0.6B",
            "Qwen/Qwen3-Embedding-8B",
        ]

    def test_the_refused_model_itself_is_not_in_the_list(self) -> None:
        """The refusal names the model ABOVE the header; only what follows the
        header is a servable id."""

        assert "Qwen/Qwen3-Embedding-4B" not in parse_endpoint_catalog(NOT_IN_CATALOG)

    @pytest.mark.parametrize(
        "bullet", ["-", "*", "•"], ids=["dash", "asterisk", "unicode-bullet"]
    )
    def test_other_bullets_parse(self, bullet: str) -> None:
        text = (
            "Models available for dedicated Endpoints:\n"
            f"  {bullet} Qwen/Qwen3-Embedding-0.6B\n"
            f"  {bullet} openai/gpt-oss-120b\n"
        )

        assert parse_endpoint_catalog(text) == [
            "Qwen/Qwen3-Embedding-0.6B",
            "openai/gpt-oss-120b",
        ]

    def test_rich_markup_parses(self) -> None:
        text = (
            "[bold]Models available for dedicated Endpoints:[/bold]\n"
            "- [green]Qwen/Qwen3-Embedding-0.6B[/green]\n"
        )

        assert parse_endpoint_catalog(text) == ["Qwen/Qwen3-Embedding-0.6B"]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "'x/y' is not available for dedicated Endpoints.",
            "Models available:\n- Qwen/Qwen3-Embedding-0.6B",
            "Models available for dedicated Endpoints:\n- not a repo id\n-\n- a/b/c",
            "\x00\x01 garbage ][ \x00",
        ],
        ids=["empty", "no-header", "other-header", "unparsable-lines", "garbage"],
    )
    def test_anything_unparsable_is_an_empty_list(self, text: str) -> None:
        """Best-effort: an empty list only SKIPS the custom-weights attempt
        (one App deploy instead of one endpoint). It must never raise."""

        assert parse_endpoint_catalog(text) == []


class TestHfBaseModels:
    """``cardData.base_model`` is a string on some cards and a list on others
    — verified live on 2026-09-20 (see the module docstring of the router)."""

    def test_a_string_base_model(self, hub) -> None:
        hub.cards = {
            "Octen/Octen-Embedding-0.6B": _card("Qwen/Qwen3-Embedding-0.6B"),
        }

        assert hf_base_models("Octen/Octen-Embedding-0.6B") == [
            "Qwen/Qwen3-Embedding-0.6B"
        ]

    def test_a_list_base_model(self, hub) -> None:
        hub.cards = {"Qwen/Qwen3.5-0.8B": _card(["Qwen/Qwen3.5-0.8B-Base"])}

        assert hf_base_models("Qwen/Qwen3.5-0.8B") == ["Qwen/Qwen3.5-0.8B-Base"]

    def test_a_missing_base_model(self, hub) -> None:
        """voyage-4-nano's card names none — the common case, and the reason
        the router then deploys the App."""

        hub.cards = {"voyageai/voyage-4-nano": _card()}

        assert hf_base_models("voyageai/voyage-4-nano") == []

    @pytest.mark.parametrize("relation", ["adapter", "merge", "quantized"])
    def test_relations_that_are_not_fine_tuned_weights_are_skipped(
        self, hub, relation: str
    ) -> None:
        """Modal's custom weights are a same-architecture CHECKPOINT: a LoRA
        adapter, a merge and a quantization are none of those."""

        hub.cards = {
            "acme/thing": _card("Qwen/Qwen3-Embedding-0.6B", relation=relation)
        }

        assert hf_base_models("acme/thing") == []

    def test_the_walk_stops_after_two_hops(self, hub) -> None:
        """model -> base -> base's base, and no further: a lineage nobody
        curated must not cost a deploy an unbounded number of GETs."""

        hub.cards = {
            "acme/third": _card("acme/second"),
            "acme/second": _card("acme/first"),
            "acme/first": _card("acme/zeroth"),
        }

        assert hf_base_models("acme/third") == ["acme/second", "acme/first"]
        assert len(hub.requests) == 2

    @pytest.mark.parametrize("status", [404, 401, 500])
    def test_an_http_failure_degrades_to_no_lineage(
        self, hub, caplog, status: int
    ) -> None:
        hub.status = {"acme/thing": status}

        with caplog.at_level(logging.WARNING):
            assert hf_base_models("acme/thing") == []

        message = "\n".join(record.getMessage() for record in caplog.records)
        assert "Could not read the Hugging Face lineage of acme/thing" in message
        assert f"HTTP {status}" in message

    def test_a_timeout_degrades_to_no_lineage(self, hub, caplog) -> None:
        hub.error = httpx.ConnectTimeout("timed out")

        with caplog.at_level(logging.WARNING):
            assert hf_base_models("acme/thing") == []

        assert "skipping the custom-weights attempt" in "\n".join(
            record.getMessage() for record in caplog.records
        )

    def test_invalid_json_degrades_to_no_lineage(self, hub, caplog) -> None:
        hub.body = b"<html>not json</html>"

        with caplog.at_level(logging.WARNING):
            assert hf_base_models("acme/thing") == []

        assert "Could not read the Hugging Face lineage" in "\n".join(
            record.getMessage() for record in caplog.records
        )

    def test_a_token_is_sent_as_a_bearer_header_and_never_logged(
        self, hub, caplog
    ) -> None:
        """ADR-009 §9: the Hub GET is the ONE place the token leaves the
        process besides a custom-weights create."""

        hub.cards = {"acme/thing": _card("Qwen/Qwen3-Embedding-0.6B")}

        with caplog.at_level(logging.DEBUG):
            hf_base_models("acme/thing", _FAKE_TOKEN)

        assert hub.requests[0].headers["Authorization"] == f"Bearer {_FAKE_TOKEN}"
        assert _FAKE_TOKEN not in "\n".join(
            record.getMessage() for record in caplog.records
        )

    def test_without_a_token_no_authorization_header_is_sent(self, hub) -> None:
        hub.cards = {"acme/thing": _card()}

        hf_base_models("acme/thing")

        assert "Authorization" not in hub.requests[0].headers


class TestProvisioningNotice:
    """``modal endpoint create`` returns in ~4 s with the endpoint still
    ``provisioning`` — ``live`` came 2m15s / 9m25s later (2026-09-21). A
    successful create therefore says so, and names the command that waits it
    out; the routing DECISIONS stay tested through the driver.
    """

    @pytest.fixture
    def entry(self) -> ModalEmbeddingModelConfig:
        return ModalEmbeddingModelConfig(repo_id=_VOYAGE, native_dimensions=2048)

    @staticmethod
    def _notices(entry: ModalEmbeddingModelConfig, caplog) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith(f"Endpoint {entry.endpoint_name} ")
        ]

    def test_a_successful_endpoint_create_announces_the_wait(
        self, entry, run, caplog
    ) -> None:
        """Story 1: the operator reads what to run next, not `modal endpoint
        list`."""

        with caplog.at_level(logging.INFO):
            assert run_deploy(entry) == 0

        assert self._notices(entry, caplog) == [
            f"Endpoint {entry.endpoint_name} is provisioning — Modal returns "
            f"before it is live (minutes). make memory-deploy-model-test "
            f"MODEL={_VOYAGE} waits for it."
        ]

    def test_an_app_deploy_announces_nothing(
        self, entry, run, caplog, hub, monkeypatch
    ) -> None:
        """An App has no endpoint row to go `live`; its container cold start is
        the health poller's business."""

        monkeypatch.chdir(Path(__file__).resolve().parents[3])
        run.state.results = [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout=NOT_IN_CATALOG, stderr=""
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        with caplog.at_level(logging.INFO):
            assert run_deploy(entry) == 0

        assert self._notices(entry, caplog) == []

    def test_a_dry_run_announces_nothing(self, entry, run, caplog) -> None:
        with caplog.at_level(logging.INFO):
            assert run_deploy(entry, dry_run=True) == 0

        run.assert_not_called()
        assert self._notices(entry, caplog) == []

    def test_a_refused_create_announces_nothing(self, entry, run, caplog) -> None:
        """Nothing is provisioning after a failure — Modal's own message and
        exit code are the whole answer."""

        run.state.result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="Token missing.", stderr=""
        )

        with caplog.at_level(logging.INFO):
            assert run_deploy(entry) == 1

        assert self._notices(entry, caplog) == []


def test_router_does_not_import_modal_or_subprocess() -> None:
    """The process boundary stays in ``modal_cli``.

    ``subprocess`` is checked on the SOURCE — it is in every interpreter's
    ``sys.modules`` anyway, so only the import statement can be asserted on;
    ``modal`` is checked by IMPORTING the router in a fresh interpreter, which
    also proves the module stays usable without the ``local-models`` extra.
    """

    tree = ast.parse(Path(modal_router.__file__).read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "subprocess" not in imported
    assert "modal" not in imported

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tree.models.modal_router; assert 'modal' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
