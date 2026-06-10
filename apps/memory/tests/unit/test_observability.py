"""Unit tests for :mod:`tree.observability` — Opik monitoring seam.

Covers the load-bearing contracts of the observability layer:

* **No-op without key** — :func:`configure_opik` does NOT call the SDK and
  :func:`track_genai_client` returns the client unchanged when ``OPIK_API_KEY``
  is unset. This is the behavior that keeps the whole suite / CI green with no
  key (DoD #1).
* **Cost math** — :meth:`ObservabilityConfig.cost_for` computes USD from the
  per-1M-token price map and returns 0 for unknown models (DoD #3 / #8).
* **Fail-open** — every public helper swallows SDK failures so telemetry can
  never break a caller (DoD #8).

The SDK boundary (``opik.configure``, ``opik.flush_tracker``,
``opik.integrations.genai.track_genai``, ``opik_context.*``) is mocked — we
never hit the network.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

import tree.observability as obs
from tree.config.app_config import ObservabilityConfig


@pytest.fixture(autouse=True)
def _reset_configured_memo() -> None:
    """Reset the process-local ``_CONFIGURED`` memo around every test.

    :func:`tree.observability.configure_opik` is idempotent per process via the
    module-level ``_CONFIGURED`` flag (so flows can call it at entry without
    re-paying). Tests that assert on repeated configure / flush behavior must
    start from a clean memo, else a prior test's configuration short-circuits the
    SDK call under assertion.
    """

    obs._CONFIGURED = False
    yield
    obs._CONFIGURED = False


@pytest.fixture
def no_key(mocker) -> None:
    """Force the observability layer into its no-key (disabled) state."""

    mocker.patch.object(obs.settings, "opik_api_key", SecretStr(""))


@pytest.fixture
def with_key(mocker) -> None:
    """Force the observability layer into its configured (key-present) state.

    The whole suite sets ``OPIK_TRACK_DISABLE=true`` (root ``conftest.py``) so no
    test pollutes the production Opik project. ``is_opik_configured()`` folds that
    env var in and returns ``False`` whenever it's set — so to exercise the
    *configured* branch (the contracts under test here) we must clear the kill
    switch for these tests' duration. ``mocker`` (monkeypatch) restores it after.
    """

    mocker.patch.dict("os.environ", {"OPIK_TRACK_DISABLE": "false"})
    mocker.patch.object(obs.settings, "opik_api_key", SecretStr("test-key"))
    mocker.patch.object(obs.settings, "opik_workspace", "test-ws")
    mocker.patch.object(obs.settings, "opik_project_name", "tree-memory")


class TestConfigureOpikNoKey:
    def test_configure_is_noop_without_key(self, no_key, mocker) -> None:
        # Arrange
        configure = mocker.patch("tree.observability.opik.configure")

        # Act
        obs.configure_opik()

        # Assert — the SDK is never touched when no key is set.
        configure.assert_not_called()

    def test_is_opik_configured_false_without_key(self, no_key) -> None:
        assert obs.is_opik_configured() is False


class TestConfigureOpikWithKey:
    def test_configure_calls_sdk_with_force_and_project(self, with_key, mocker) -> None:
        # Arrange
        configure = mocker.patch("tree.observability.opik.configure")

        # Act
        obs.configure_opik()

        # Assert
        configure.assert_called_once()
        kwargs = configure.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["workspace"] == "test-ws"
        assert kwargs["use_local"] is False
        assert kwargs["force"] is True
        assert kwargs["automatic_approvals"] is True
        assert kwargs["project_name"] == "tree-memory"

    def test_configure_is_fail_open_on_sdk_error(self, with_key, mocker) -> None:
        # Arrange — SDK raises (bad key, network). Must NOT propagate.
        mocker.patch(
            "tree.observability.opik.configure", side_effect=RuntimeError("boom")
        )

        # Act / Assert — no exception escapes.
        obs.configure_opik()

    def test_is_opik_configured_true_with_key(self, with_key) -> None:
        assert obs.is_opik_configured() is True

    def test_track_disable_env_overrides_present_key(self, with_key, mocker) -> None:
        # Arrange — a key IS present (with_key), but the kill switch is on. This is
        # the test-suite state: OPIK_API_KEY from .env + OPIK_TRACK_DISABLE from the
        # root conftest. The seam must report DISABLED so span()/track_genai_client
        # no-op before the SDK and no test trace reaches the production project.
        mocker.patch.dict("os.environ", {"OPIK_TRACK_DISABLE": "true"})

        # Act / Assert
        assert obs.is_opik_configured() is False

    def test_configure_is_noop_when_track_disabled(self, with_key, mocker) -> None:
        # Arrange — key present, kill switch on: configure must NOT touch the SDK.
        mocker.patch.dict("os.environ", {"OPIK_TRACK_DISABLE": "true"})
        configure = mocker.patch("tree.observability.opik.configure")

        # Act
        obs.configure_opik()

        # Assert
        configure.assert_not_called()


class TestTrackGenaiClient:
    def test_returns_client_unchanged_without_key(self, no_key) -> None:
        # Arrange
        sentinel = object()

        # Act
        result = obs.track_genai_client(sentinel)

        # Assert — untouched passthrough on the no-key path.
        assert result is sentinel

    def test_wraps_client_with_key(self, with_key, mocker) -> None:
        # Arrange
        wrapped = object()
        client = object()
        track_genai = mocker.patch(
            "opik.integrations.genai.track_genai", return_value=wrapped
        )

        # Act
        result = obs.track_genai_client(client)

        # Assert
        track_genai.assert_called_once_with(client)
        assert result is wrapped

    def test_returns_client_unchanged_on_wrap_error(self, with_key, mocker) -> None:
        # Arrange — wrapping blows up; the original client must survive.
        client = object()
        mocker.patch(
            "opik.integrations.genai.track_genai",
            side_effect=RuntimeError("wrap failed"),
        )

        # Act
        result = obs.track_genai_client(client)

        # Assert
        assert result is client


class TestUpdateSpanTraceFailOpen:
    def test_update_current_span_is_fail_open(self, mocker) -> None:
        # Arrange — no active span → SDK raises; helper must swallow it.
        mocker.patch(
            "tree.observability.opik_context.update_current_span",
            side_effect=RuntimeError("no active span"),
        )

        # Act / Assert
        obs.update_current_span(provider="voyage", total_cost=0.1)

    def test_update_current_trace_is_fail_open(self, mocker) -> None:
        # Arrange
        mocker.patch(
            "tree.observability.opik_context.update_current_trace",
            side_effect=RuntimeError("no active trace"),
        )

        # Act / Assert
        obs.update_current_trace(thread_id="abc")

    def test_update_current_span_forwards_kwargs(self, mocker) -> None:
        # Arrange
        spy = mocker.patch("tree.observability.opik_context.update_current_span")

        # Act
        obs.update_current_span(provider="voyage", total_cost=0.5)

        # Assert
        spy.assert_called_once_with(provider="voyage", total_cost=0.5)


class TestFlushOpik:
    def test_flush_is_noop_without_key(self, no_key, mocker) -> None:
        # Arrange
        flush = mocker.patch("tree.observability.opik.flush_tracker")

        # Act
        obs.flush_opik()

        # Assert
        flush.assert_not_called()

    def test_flush_calls_sdk_with_key(self, with_key, mocker) -> None:
        # Arrange
        flush = mocker.patch("tree.observability.opik.flush_tracker")

        # Act
        obs.flush_opik()

        # Assert
        flush.assert_called_once()

    def test_flush_is_fail_open(self, with_key, mocker) -> None:
        # Arrange
        mocker.patch(
            "tree.observability.opik.flush_tracker",
            side_effect=RuntimeError("flush boom"),
        )

        # Act / Assert — no exception escapes.
        obs.flush_opik()


class TestEmbeddingCostMath:
    def test_voyage_3_5_cost_for_one_million_tokens(self) -> None:
        # Arrange
        config = ObservabilityConfig()

        # Act — 1M tokens at $0.06/1M.
        cost = config.cost_for("voyage-3.5", 1_000_000)

        # Assert
        assert cost == pytest.approx(0.06)

    def test_voyage_3_5_cost_is_linear(self) -> None:
        # Arrange
        config = ObservabilityConfig()

        # Act — 500K tokens → half the per-1M price.
        cost = config.cost_for("voyage-3.5", 500_000)

        # Assert
        assert cost == pytest.approx(0.03)

    @pytest.mark.parametrize(
        "model,price_per_1m",
        [
            ("voyage-3.5", 0.06),
            ("voyage-3", 0.06),
            ("voyage-3.5-lite", 0.02),
            ("voyage-3-large", 0.18),
            ("voyage-multimodal-3", 0.12),
        ],
    )
    def test_price_map_matches_voyage_docs(
        self, model: str, price_per_1m: float
    ) -> None:
        # Arrange
        config = ObservabilityConfig()

        # Act
        cost = config.cost_for(model, 1_000_000)

        # Assert
        assert cost == pytest.approx(price_per_1m)

    def test_unknown_model_costs_zero(self) -> None:
        # Arrange
        config = ObservabilityConfig()

        # Act — self-hosted / unknown model → no cost, but no error.
        cost = config.cost_for("voyageai/voyage-4-nano", 1_000_000)

        # Assert
        assert cost == 0.0

    def test_zero_tokens_costs_zero(self) -> None:
        # Arrange
        config = ObservabilityConfig()

        # Act
        cost = config.cost_for("voyage-3.5", 0)

        # Assert
        assert cost == 0.0


class TestLazyIdempotentConfiguration:
    """The configured flag does NOT survive into Prefect flow-run subprocesses,
    so configure-dependent helpers self-configure on first use, once per
    process."""

    def test_configure_is_idempotent(self, with_key, mocker) -> None:
        # Arrange
        configure = mocker.patch("tree.observability.opik.configure")

        # Act — two calls; the SDK is hit exactly once (memoized).
        obs.configure_opik()
        obs.configure_opik()

        # Assert
        configure.assert_called_once()

    def test_track_genai_client_self_configures(self, with_key, mocker) -> None:
        # Arrange — simulate a fresh process: configured memo reset (autouse),
        # nobody called configure_opik() yet.
        configure = mocker.patch("tree.observability.opik.configure")
        mocker.patch("opik.integrations.genai.track_genai", return_value=object())

        # Act — wrapping the Gemini client must trigger configuration itself.
        obs.track_genai_client(object())

        # Assert — this is the fix for "no Gemini spans in the flow subprocess".
        configure.assert_called_once()

    def test_record_embedding_usage_self_configures(self, with_key, mocker) -> None:
        # Arrange
        configure = mocker.patch("tree.observability.opik.configure")
        mocker.patch("tree.observability.opik_context.update_current_span")

        # Act
        obs.record_embedding_usage(
            provider="voyage", model="voyage-3.5", total_tokens=10, total_cost=0.1
        )

        # Assert
        configure.assert_called_once()


class TestDistributedTraceHeaders:
    def test_returns_none_without_key(self, no_key) -> None:
        assert obs.get_distributed_trace_headers() is None

    def test_returns_headers_with_key(self, with_key, mocker) -> None:
        # Arrange
        mocker.patch("tree.observability.opik.configure")
        mocker.patch(
            "tree.observability.opik_context.get_distributed_trace_headers",
            return_value={"opik_trace_id": "t1", "opik_parent_span_id": "s1"},
        )

        # Act
        headers = obs.get_distributed_trace_headers()

        # Assert — a plain JSON-serializable dict (safe as a Prefect parameter).
        assert headers == {"opik_trace_id": "t1", "opik_parent_span_id": "s1"}
        assert isinstance(headers, dict)

    def test_is_fail_open(self, with_key, mocker) -> None:
        # Arrange — SDK raises; helper degrades to None, never propagates.
        mocker.patch("tree.observability.opik.configure")
        mocker.patch(
            "tree.observability.opik_context.get_distributed_trace_headers",
            side_effect=RuntimeError("no active trace"),
        )

        # Act / Assert
        assert obs.get_distributed_trace_headers() is None


class TestSpanContextManager:
    def test_span_is_noop_without_key(self, no_key, mocker) -> None:
        # Arrange — SDK must not be touched on the no-key path.
        start = mocker.patch("tree.observability.opik.start_as_current_span")

        # Act — the body still runs.
        ran = False
        with obs.span("x"):
            ran = True

        # Assert
        assert ran is True
        start.assert_not_called()

    def test_span_forwards_distributed_headers_and_disables_dup_root(
        self, with_key, mocker
    ) -> None:
        # Arrange
        mocker.patch("tree.observability.opik.configure")
        start = mocker.patch("tree.observability.opik.start_as_current_span")

        headers = {"opik_trace_id": "t1", "opik_parent_span_id": "s1"}

        # Act
        with obs.span("task-span", tags=["ingestion"], trace_headers=headers):
            pass

        # Assert — the span attaches to the distributed parent AND suppresses the
        # duplicate root-span noise the human flagged.
        start.assert_called_once()
        _args, kwargs = start.call_args
        assert kwargs["opik_distributed_trace_headers"] == headers
        assert kwargs["create_duplicate_root_span"] is False
        assert kwargs["tags"] == ["ingestion"]

    def test_span_no_headers_omits_distributed_kwarg(self, with_key, mocker) -> None:
        # Arrange — in-process (no headers): attach via contextvars, so we must
        # NOT pass opik_distributed_trace_headers at all.
        mocker.patch("tree.observability.opik.configure")
        start = mocker.patch("tree.observability.opik.start_as_current_span")

        # Act
        with obs.span("task-span"):
            pass

        # Assert
        _args, kwargs = start.call_args
        assert "opik_distributed_trace_headers" not in kwargs

    def test_span_is_fail_open_on_sdk_error(self, with_key, mocker) -> None:
        # Arrange — opening the span blows up; the body must still run.
        mocker.patch("tree.observability.opik.configure")
        mocker.patch(
            "tree.observability.opik.start_as_current_span",
            side_effect=RuntimeError("span boom"),
        )

        # Act
        ran = False
        with obs.span("x"):
            ran = True

        # Assert
        assert ran is True

    def test_span_propagates_body_exception(self, with_key, mocker) -> None:
        # Arrange
        mocker.patch("tree.observability.opik.configure")
        mocker.patch("tree.observability.opik.start_as_current_span")

        # Act / Assert — a real error in the wrapped body is NOT swallowed.
        with pytest.raises(ValueError, match="boom"):
            with obs.span("x"):
                raise ValueError("boom")


class TestTrackedSpanDecorator:
    async def test_pulls_headers_from_kwargs(self, with_key, mocker) -> None:
        # Arrange — the decorator opens a span attached to the headers the caller
        # passed as ``opik_trace_headers``.
        mocker.patch("tree.observability.opik.configure")
        start = mocker.patch("tree.observability.opik.start_as_current_span")

        @obs.tracked_span("my-task", tags=["ingestion"])
        async def task(x: int, opik_trace_headers=None) -> int:
            return x + 1

        headers = {"opik_trace_id": "t1", "opik_parent_span_id": "s1"}

        # Act
        result = await task(1, opik_trace_headers=headers)

        # Assert
        assert result == 2
        _args, kwargs = start.call_args
        assert kwargs["opik_distributed_trace_headers"] == headers

    async def test_runs_body_without_key(self, no_key, mocker) -> None:
        # Arrange
        start = mocker.patch("tree.observability.opik.start_as_current_span")

        @obs.tracked_span("my-task")
        async def task(x: int, opik_trace_headers=None) -> int:
            return x * 2

        # Act
        result = await task(3)

        # Assert — body ran, SDK untouched.
        assert result == 6
        start.assert_not_called()
