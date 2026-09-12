"""Unit tests for ``tree.offline`` — the offline end-to-end glue flow.

``offline_pipeline`` composes the **Offline phase**s (data coordinator, then one
extraction coordinator per target user, then one ``memory_indexing`` subflow per
target user, then — on request only — one ``memory_clustering`` subflow per
target user) without re-implementing any of them; these tests pin that
composition contract: pass-through of source selectors, the per-user fan-out of
each phase, the sequential phase blocks (every user extracted BEFORE any user is
indexed), and per-user failure isolation. The phase bodies themselves are covered
in their own suites.

They also pin the single-step surface — the ``run_data`` / ``run_extraction`` /
``run_indexing`` / ``run_clustering`` phase flags (#098, extended by ADR-007
Decision 5) that let the
step CLIs funnel through this ONE flow, the ``document_ids`` / ``source_uris``
narrowing forwarded to every per-user extraction call (the latter resolved to ids
ONCE at flow entry — the **Ingest receipt**'s retry key, ADR-008 §1), and the
single-tenant guard that fires at BOTH edges (flow and fire-and-forget
dispatcher).
"""

import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from prefect.client.schemas.objects import State, StateType

from tree.memory.pipeline import ClusteringStats
from tree.offline import dispatch_offline_pipeline, offline_pipeline

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_OTHER_USER_ID = PydanticObjectId("507f1f77bcf86cd799439012")
_DOC_ID = "68a1f1f77bcf86cd799439ab"
_OTHER_DOC_ID = "68a1f1f77bcf86cd799439ac"
# The natural key an **Ingest receipt** hands the operator back (ADR-008 §1).
_SOURCE_URI = "https://www.youtube.com/watch?v=abc"
_OTHER_SOURCE_URI = "file:///tmp/a.md"


@dataclass
class _FakeStats:
    shards_total: int = 1
    succeeded: int = 1
    failed: int = 0
    failures: dict = field(default_factory=dict)


# The embedded-row count a stubbed ``memory_indexing`` phase reports back.
_EMBEDDED = 7

# What a stubbed ``memory_clustering`` phase reports back (ADR-007 phase 4).
_CLUSTERING_STATS = ClusteringStats(
    run_id="run-1", chunks_total=40, clustered=35, noise=5, clusters=2
)


def _patch_coordinators(
    mocker,
) -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    """Stub every phase body + user resolution.

    Returns ``(data, extract, index, cluster, users)``.
    """

    data = mocker.patch(
        "tree.offline.data_etl_coordinator",
        new_callable=AsyncMock,
        return_value=_FakeStats(),
    )
    extract = mocker.patch(
        "tree.offline.memory_extract_etl_coordinator",
        new_callable=AsyncMock,
        return_value=_FakeStats(),
    )
    index = mocker.patch(
        "tree.offline.memory_indexing",
        new_callable=AsyncMock,
        return_value=_EMBEDDED,
    )
    cluster = mocker.patch(
        "tree.offline.memory_clustering",
        new_callable=AsyncMock,
        return_value=_CLUSTERING_STATS,
    )
    users = mocker.patch(
        "tree.offline.resolve_target_user_ids",
        new_callable=AsyncMock,
        return_value=[_USER_ID],
    )
    return data, extract, index, cluster, users


def _patch_document_lookup(mocker, documents: list[SimpleNamespace]) -> MagicMock:
    """Stub the Mongo boundary the URI→id resolution reads.

    ``init_mongodb`` (Beanie init) and ``Document.find`` are the ONLY I/O the
    flow itself does; the cursor stub mirrors ``to_list()``'s shape.
    """

    mocker.patch("tree.offline.init_mongodb", new_callable=AsyncMock)
    return mocker.patch(
        "tree.offline.Document.find",
        return_value=MagicMock(to_list=AsyncMock(return_value=documents)),
    )


def _document(document_id: str, source_uri: str) -> SimpleNamespace:
    """A ``documents`` row as the resolution reads it: its id and natural key."""

    return SimpleNamespace(id=document_id, source_uri=source_uri)


def _flow_run(
    run_id: str = "run-1", state_type: StateType = StateType.SCHEDULED
) -> SimpleNamespace:
    """A just-CREATED flow run, as ``run_deployment(timeout=0)`` returns it."""

    return SimpleNamespace(id=run_id, state=State(type=state_type))


class TestEtlOffline:
    async def test_passes_selectors_through_and_chains_extraction(self, mocker) -> None:
        data, extract, _index, _cluster, _users = _patch_coordinators(mocker)

        result = await offline_pipeline(
            user_id=_USER_ID,
            source_files=["sources/listen.yaml"],
            num_shards=2,
        )

        # Data phase: selectors forwarded untouched — the data coordinator owns
        # source resolution, so end-to-end source semantics stay identical.
        data.assert_awaited_once_with(
            user_id=_USER_ID, source_files=["sources/listen.yaml"], sources=None
        )
        # Memory phase: one extraction coordinator per target user, with the
        # extraction fan-out width forwarded.
        extract.assert_awaited_once_with(_USER_ID, document_ids=None, num_shards=2)
        assert result["data"]["shards_total"] == 1
        assert result["extraction"][str(_USER_ID)]["succeeded"] == 1

    async def test_all_users_mode_extracts_per_active_user(self, mocker) -> None:
        _data, extract, _index, _cluster, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]

        result = await offline_pipeline(user_id=None)

        # user_id=None (the nightly-cron semantics) fans extraction out across
        # every active user the data phase just ingested for.
        users.assert_awaited_once_with(None)
        assert extract.await_count == 2
        assert set(result["extraction"]) == {str(_USER_ID), str(_OTHER_USER_ID)}

    async def test_one_users_extraction_failure_is_isolated(self, mocker) -> None:
        _data, extract, _index, _cluster, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]
        extract.side_effect = [RuntimeError("llm down"), _FakeStats()]

        result = await offline_pipeline(user_id=None)

        # Mirror of the shard-isolation convention: the first user's blown
        # extraction is recorded, the second still runs.
        assert extract.await_count == 2
        assert result["extraction"][str(_USER_ID)] == {"error": "llm down"}
        assert result["extraction"][str(_OTHER_USER_ID)]["succeeded"] == 1


class TestOfflinePipelinePhaseFlags:
    """The single-step surface: ANY of the three phases can be turned off."""

    async def test_run_data_false_skips_ingestion_and_still_extracts(
        self, mocker
    ) -> None:
        data, extract, _index, _cluster, _users = _patch_coordinators(mocker)

        result = await offline_pipeline(user_id=_USER_ID, run_data=False)

        # The data phase is skipped entirely; extraction still runs, and the
        # result carries an explicit "no data phase" marker.
        data.assert_not_awaited()
        extract.assert_awaited_once_with(_USER_ID, document_ids=None, num_shards=1)
        assert result["data"] is None

    async def test_data_only_run_skips_user_resolution_extraction_and_indexing(
        self, mocker
    ) -> None:
        data, extract, index, _cluster, users = _patch_coordinators(mocker)

        result = await offline_pipeline(
            user_id=_USER_ID, run_extraction=False, run_indexing=False
        )

        # Target-user resolution belongs to the per-user phases, so a data-only
        # run must not touch the users collection — nor index anything.
        data.assert_awaited_once_with(user_id=_USER_ID, source_files=None, sources=None)
        users.assert_not_awaited()
        extract.assert_not_awaited()
        index.assert_not_awaited()
        assert result["extraction"] == {}
        assert result["indexing"] == {}

    async def test_all_phases_disabled_is_a_logged_no_op(self, mocker, caplog) -> None:
        data, extract, index, cluster, users = _patch_coordinators(mocker)

        with caplog.at_level(logging.INFO, logger="tree.offline"):
            result = await offline_pipeline(
                user_id=_USER_ID,
                run_data=False,
                run_extraction=False,
                run_indexing=False,
                run_clustering=False,
            )

        # A misconfigured caller gets a Completed run it can read, not a crash.
        assert result == {
            "data": None,
            "extraction": {},
            "indexing": {},
            "clustering": {},
        }
        data.assert_not_awaited()
        users.assert_not_awaited()
        extract.assert_not_awaited()
        index.assert_not_awaited()
        cluster.assert_not_awaited()
        no_op_logs = [
            record
            for record in caplog.records
            if "all phases disabled" in record.getMessage()
        ]
        assert len(no_op_logs) == 1
        assert no_op_logs[0].levelno == logging.INFO

    async def test_document_ids_are_forwarded_to_every_extraction_call(
        self, mocker
    ) -> None:
        _data, extract, _index, _cluster, _users = _patch_coordinators(mocker)

        await offline_pipeline(
            user_id=_USER_ID, document_ids=[_DOC_ID], num_shards=2, run_data=False
        )

        # Narrowing is verbatim pass-through: the coordinator, not this flow,
        # decides what an explicit doc-id set means.
        extract.assert_awaited_once_with(_USER_ID, document_ids=[_DOC_ID], num_shards=2)

    async def test_document_ids_without_user_id_is_rejected(self, mocker) -> None:
        data, extract, _index, _cluster, _users = _patch_coordinators(mocker)

        with pytest.raises(ValueError, match="document_ids is single-tenant"):
            await offline_pipeline(user_id=None, document_ids=[_DOC_ID])

        # Guarded at the edge — no phase runs against the wrong tenant.
        data.assert_not_awaited()
        extract.assert_not_awaited()


class TestSourceUris:
    """The retry selector an **Ingest receipt** hands back (ADR-008 §1).

    A receipt carries ``source_uri``, never a ``Document._id``, so the operator
    retrying a failed online extraction types the URI they already have. The
    flow resolves it to document ids ONCE at entry and feeds the unchanged
    ``document_ids`` path — an unknown URI fails loud, because a silent
    "0 documents" is what makes a broken retry look successful.
    """

    async def test_resolves_uris_to_document_ids(self, mocker) -> None:
        _data, extract, _index, _cluster, _users = _patch_coordinators(mocker)
        find = _patch_document_lookup(mocker, [_document(_DOC_ID, _SOURCE_URI)])

        await offline_pipeline(
            user_id=_USER_ID, source_uris=[_SOURCE_URI], run_data=False
        )

        # ONE tenant-scoped ``$in`` lookup, and the id it yields is what the
        # extraction Coordinator is narrowed to.
        find.assert_called_once_with(
            {"user_id": _USER_ID, "source_uri": {"$in": [_SOURCE_URI]}}
        )
        extract.assert_awaited_once_with(_USER_ID, document_ids=[_DOC_ID], num_shards=1)

    async def test_unions_with_document_ids(self, mocker) -> None:
        _data, extract, _index, _cluster, _users = _patch_coordinators(mocker)
        # Cursor order is NOT the selector order — the resolved ids follow the
        # URIs the operator typed, and the id they ALSO passed is not repeated.
        _patch_document_lookup(
            mocker,
            [
                _document(_OTHER_DOC_ID, _OTHER_SOURCE_URI),
                _document(_DOC_ID, _SOURCE_URI),
            ],
        )

        await offline_pipeline(
            user_id=_USER_ID,
            document_ids=[_DOC_ID],
            source_uris=[_SOURCE_URI, _OTHER_SOURCE_URI],
            run_data=False,
        )

        extract.assert_awaited_once_with(
            _USER_ID, document_ids=[_DOC_ID, _OTHER_DOC_ID], num_shards=1
        )

    async def test_unknown_uri_fails_loud(self, mocker) -> None:
        data, extract, _index, _cluster, _users = _patch_coordinators(mocker)
        _patch_document_lookup(mocker, [_document(_DOC_ID, _SOURCE_URI)])

        with pytest.raises(
            ValueError,
            match=f"No document for source_uri {re.escape(_OTHER_SOURCE_URI)}",
        ):
            await offline_pipeline(
                user_id=_USER_ID,
                source_uris=[_SOURCE_URI, _OTHER_SOURCE_URI],
                run_data=False,
            )

        # A typo must fail at entry, not extract the OTHER URI and read green.
        data.assert_not_awaited()
        extract.assert_not_awaited()

    async def test_source_uris_require_user_id(self, mocker) -> None:
        data, extract, _index, _cluster, _users = _patch_coordinators(mocker)
        find = _patch_document_lookup(mocker, [])

        with pytest.raises(ValueError, match="source_uris is single-tenant"):
            await offline_pipeline(user_id=None, source_uris=[_SOURCE_URI])

        # Single-tenant like ``document_ids``: fanned across every active user
        # the lookup would reach another tenant's documents.
        find.assert_not_called()
        data.assert_not_awaited()
        extract.assert_not_awaited()

    async def test_a_run_without_source_uris_never_touches_mongo(self, mocker) -> None:
        _data, extract, _index, _cluster, _users = _patch_coordinators(mocker)
        init = mocker.patch("tree.offline.init_mongodb", new_callable=AsyncMock)
        find = mocker.patch("tree.offline.Document.find")

        await offline_pipeline(user_id=_USER_ID, document_ids=[_DOC_ID])

        # Resolution connects only when asked for — the nightly cron and every
        # doc-id run keep the exact I/O they had before the selector existed.
        init.assert_not_awaited()
        find.assert_not_called()
        extract.assert_awaited_once_with(_USER_ID, document_ids=[_DOC_ID], num_shards=1)


class TestOfflineIndexingPhase:
    """Phase 3: indexing is its own **Offline phase**, not the Coordinator's job.

    ADR-007 Decision 5 moved ``memory_indexing`` out of ``_fan_out_extraction``
    into ``offline_pipeline``, so these pin what the fan-out tests used to: the
    flow runs ONE indexing subflow per target user, AFTER every extraction, and
    isolates a per-user failure.
    """

    async def test_indexes_the_target_user_once_after_extraction(self, mocker) -> None:
        _data, extract, index, _cluster, _users = _patch_coordinators(mocker)
        manager = MagicMock()
        manager.attach_mock(extract, "extract")
        manager.attach_mock(index, "index")

        result = await offline_pipeline(user_id=_USER_ID)

        # Exactly one indexing subflow for the tenant, and it reports the
        # embedded-row count the flow returned.
        index.assert_awaited_once_with(user_id=_USER_ID)
        assert result["indexing"][str(_USER_ID)] == {"embedded": _EMBEDDED}
        # Ordering is the load-bearing part: extraction first, then indexing.
        assert [call[0] for call in manager.mock_calls] == ["extract", "index"]

    async def test_all_users_mode_indexes_every_user_after_all_extractions(
        self, mocker
    ) -> None:
        _data, extract, index, _cluster, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]
        manager = MagicMock()
        manager.attach_mock(extract, "extract")
        manager.attach_mock(index, "index")

        result = await offline_pipeline(user_id=None)

        # Phases are sequential BLOCKS: every user is extracted before any user
        # is indexed (not extract→index interleaved per user).
        assert [call[0] for call in manager.mock_calls] == [
            "extract",
            "extract",
            "index",
            "index",
        ]
        assert [call.kwargs["user_id"] for call in index.await_args_list] == [
            _USER_ID,
            _OTHER_USER_ID,
        ]
        assert set(result["indexing"]) == {str(_USER_ID), str(_OTHER_USER_ID)}

    async def test_run_indexing_false_never_indexes(self, mocker) -> None:
        _data, extract, index, _cluster, _users = _patch_coordinators(mocker)

        result = await offline_pipeline(user_id=_USER_ID, run_indexing=False)

        # An extraction-only run (``run-memory-pipeline`` with the phase off)
        # leaves the embeddings backfill for a later run.
        extract.assert_awaited_once()
        index.assert_not_awaited()
        assert result["indexing"] == {}

    async def test_indexing_only_run_still_resolves_target_users(self, mocker) -> None:
        data, extract, index, _cluster, users = _patch_coordinators(mocker)

        result = await offline_pipeline(
            user_id=_USER_ID, run_data=False, run_extraction=False
        )

        # The ``run-indexing-pipeline`` shape: no data, no extraction, but the
        # per-user phase still needs its target users resolved.
        data.assert_not_awaited()
        extract.assert_not_awaited()
        users.assert_awaited_once_with(_USER_ID)
        index.assert_awaited_once_with(user_id=_USER_ID)
        assert result["indexing"][str(_USER_ID)] == {"embedded": _EMBEDDED}

    async def test_one_users_indexing_failure_is_isolated(self, mocker) -> None:
        _data, _extract, index, _cluster, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]
        index.side_effect = [RuntimeError("boom"), _EMBEDDED]

        result = await offline_pipeline(user_id=None)

        # Same convention as the extraction phase: one tenant's blown index
        # (e.g. mongot down for its namespace) never sinks the nightly run.
        assert index.await_count == 2
        assert result["indexing"][str(_USER_ID)] == {"error": "boom"}
        assert result["indexing"][str(_OTHER_USER_ID)] == {"embedded": _EMBEDDED}

    def test_the_phase_flags_read_in_execution_order(self) -> None:
        """Parameter ORDER is the contract: phases read as 1 → 2 → 3 → 4."""

        parameters = inspect.signature(offline_pipeline).parameters
        names = list(parameters)
        assert names.index("run_data") + 1 == names.index("run_extraction")
        assert names.index("run_extraction") + 1 == names.index("run_indexing")
        assert names.index("run_indexing") + 1 == names.index("run_clustering")
        assert names.index("run_clustering") + 1 == names.index("document_ids")
        # The two doc selectors read side by side — both narrow extraction.
        assert names.index("document_ids") + 1 == names.index("source_uris")
        assert parameters["run_indexing"].default is True


class TestOfflineClusteringPhase:
    """Phase 4: clustering — the ONE phase that is OFF by default (ADR-007 §5).

    The default matters as much as the behaviour: the nightly cron runs with
    these defaults, and a clustering run costs a ~40 s cold ``import umap`` plus
    one Gemini call per cluster. The switch is the flow PARAMETER — there is
    deliberately no ``memory.clustering.enabled`` YAML key that could disagree
    with it.
    """

    async def test_clusters_the_target_user_once_after_indexing(self, mocker) -> None:
        _data, _extract, index, cluster, _users = _patch_coordinators(mocker)
        manager = MagicMock()
        manager.attach_mock(index, "index")
        manager.attach_mock(cluster, "cluster")

        result = await offline_pipeline(user_id=_USER_ID, run_clustering=True)

        # Clustering reads the embeddings indexing just backfilled, so it runs
        # LAST — a map of the whole corpus, not of yesterday's part of it.
        cluster.assert_awaited_once_with(user_id=_USER_ID)
        assert [call[0] for call in manager.mock_calls] == ["index", "cluster"]
        assert result["clustering"][str(_USER_ID)] == _CLUSTERING_STATS.model_dump()

    async def test_is_off_by_default(self, mocker) -> None:
        _data, _extract, _index, cluster, _users = _patch_coordinators(mocker)

        result = await offline_pipeline(user_id=_USER_ID)

        # The nightly cron's shape: three phases on, clustering untouched — so
        # no worker ever imports the UMAP stack for a run that never asked.
        cluster.assert_not_awaited()
        assert result["clustering"] == {}

    def test_run_clustering_defaults_to_false_on_both_edges(self) -> None:
        for entry_point in (offline_pipeline, dispatch_offline_pipeline):
            parameter = inspect.signature(entry_point).parameters["run_clustering"]
            assert parameter.default is False, entry_point.__name__

    async def test_a_clustering_only_run_still_resolves_target_users(
        self, mocker
    ) -> None:
        data, extract, index, cluster, users = _patch_coordinators(mocker)

        result = await offline_pipeline(
            user_id=_USER_ID,
            run_data=False,
            run_extraction=False,
            run_indexing=False,
            run_clustering=True,
        )

        # The ``run-clustering-pipeline`` shape: no data, no extraction, no
        # indexing — but the per-user phase still needs its target users.
        data.assert_not_awaited()
        extract.assert_not_awaited()
        index.assert_not_awaited()
        users.assert_awaited_once_with(_USER_ID)
        cluster.assert_awaited_once_with(user_id=_USER_ID)
        assert result["clustering"][str(_USER_ID)]["clusters"] == 2

    async def test_all_users_mode_clusters_every_user(self, mocker) -> None:
        _data, _extract, _index, cluster, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]

        result = await offline_pipeline(user_id=None, run_clustering=True)

        assert [call.kwargs["user_id"] for call in cluster.await_args_list] == [
            _USER_ID,
            _OTHER_USER_ID,
        ]
        assert set(result["clustering"]) == {str(_USER_ID), str(_OTHER_USER_ID)}

    async def test_one_users_clustering_failure_is_isolated(self, mocker) -> None:
        _data, _extract, _index, cluster, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]
        cluster.side_effect = [RuntimeError("umap blew up"), _CLUSTERING_STATS]

        result = await offline_pipeline(user_id=None, run_clustering=True)

        # Same convention as the other per-user phases: one tenant's blown
        # clustering never sinks the run for everyone else.
        assert cluster.await_count == 2
        assert result["clustering"][str(_USER_ID)] == {"error": "umap blew up"}
        assert result["clustering"][str(_OTHER_USER_ID)]["clusters"] == 2

    async def test_the_result_is_json_safe(self, mocker) -> None:
        """The flow-run result is serialized by Prefect — no Pydantic models."""

        _patch_coordinators(mocker)

        result = await offline_pipeline(user_id=_USER_ID, run_clustering=True)

        assert (
            json.loads(json.dumps(result["clustering"]))[str(_USER_ID)]["run_id"]
            == "run-1"
        )


class TestOfflinePhaseLogLines:
    """What the operator's TERMINAL says each phase did.

    ``tree.cli.wait_for_flow_run`` streams the PARENT run's logs only, so a
    subflow's own summary (``clustering run …: 37 clusters, …``) never reaches
    the terminal — it lands in the ``serve-workflows`` process / the Prefect UI.
    These pin ONE parent-level line per user per phase, which does reach it:
    a success line, a skip WARNING, and the isolated per-user failure.
    """

    @staticmethod
    def _messages(caplog) -> list[str]:
        return [record.getMessage() for record in caplog.records]

    async def test_indexing_logs_the_embedded_count_for_each_user(
        self, mocker, caplog
    ) -> None:
        _data, _extract, _index, _cluster, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]

        with caplog.at_level(logging.INFO):
            await offline_pipeline(user_id=None, run_data=False, run_extraction=False)

        # #114 Story 1 step 3: "the CLI streams … Embedded N nodes".
        messages = self._messages(caplog)
        assert f"indexing: user_id={_USER_ID} embedded={_EMBEDDED}" in messages
        assert f"indexing: user_id={_OTHER_USER_ID} embedded={_EMBEDDED}" in messages

    async def test_extraction_logs_its_fan_out_counts_for_each_user(
        self, mocker, caplog
    ) -> None:
        _data, _extract, _index, _cluster, _users = _patch_coordinators(mocker)

        with caplog.at_level(logging.INFO):
            await offline_pipeline(user_id=_USER_ID, run_data=False, run_indexing=False)

        # Same shape as the other two phases: one line, ``phase: key=value``.
        assert (
            f"extraction: user_id={_USER_ID} shards=1 succeeded=1 failed=0"
            in self._messages(caplog)
        )

    async def test_clustering_logs_the_run_summary(self, mocker, caplog) -> None:
        _data, _extract, _index, _cluster, _users = _patch_coordinators(mocker)

        with caplog.at_level(logging.INFO):
            await offline_pipeline(
                user_id=_USER_ID,
                run_data=False,
                run_extraction=False,
                run_indexing=False,
                run_clustering=True,
            )

        # #117 Story 3: the counts the subflow logs, repeated where the
        # operator is actually looking.
        assert (
            f"clustering: user_id={_USER_ID} run_id=run-1 clusters=2 "
            "clustered=35 noise=5 fallbacks=0" in self._messages(caplog)
        )

    async def test_a_skipped_clustering_run_warns_with_its_reason(
        self, mocker, caplog
    ) -> None:
        _data, _extract, _index, cluster, _users = _patch_coordinators(mocker)
        cluster.return_value = ClusteringStats(
            run_id="run-2",
            chunks_total=3,
            skipped_reason="3 child embeddings < min_cluster_size 15",
        )

        with caplog.at_level(logging.INFO):
            await offline_pipeline(
                user_id=_USER_ID,
                run_data=False,
                run_extraction=False,
                run_indexing=False,
                run_clustering=True,
            )

        # The corpus-too-small exit is a NON-failure, so the run still completes
        # — without this line the terminal reads exactly like a 37-cluster run.
        skips = [
            record
            for record in caplog.records
            if record.getMessage()
            == (
                f"clustering SKIPPED: user_id={_USER_ID} reason=3 child "
                "embeddings < min_cluster_size 15"
            )
        ]
        assert len(skips) == 1
        assert skips[0].levelno == logging.WARNING
        assert not any(
            message.startswith(f"clustering: user_id={_USER_ID}")
            for message in self._messages(caplog)
        )

    async def test_a_failed_phase_is_reported_at_the_parent_level(
        self, mocker, caplog
    ) -> None:
        _data, _extract, index, cluster, _users = _patch_coordinators(mocker)
        index.side_effect = RuntimeError("mongot down")
        cluster.side_effect = RuntimeError("umap blew up")

        with caplog.at_level(logging.INFO):
            await offline_pipeline(
                user_id=_USER_ID,
                run_data=False,
                run_extraction=False,
                run_clustering=True,
            )

        # Isolated per user, but never silent: the failure reaches the same
        # stream as the success lines, at ERROR.
        failures = {
            record.getMessage(): record.levelno
            for record in caplog.records
            if "failed for user" in record.getMessage()
        }
        assert failures == {
            f"offline-pipeline: indexing failed for user {_USER_ID}": logging.ERROR,
            f"offline-pipeline: clustering failed for user {_USER_ID}": logging.ERROR,
        }
        # A blown phase logs no success line for that user.
        assert not any(
            message.startswith(("indexing: user_id", "clustering: user_id"))
            for message in self._messages(caplog)
        )


class TestDispatchOfflineIngest:
    """The caller-edge dispatcher: ONE path — submit the core deployment.

    ``offline-pipeline`` is always registered, so dispatch simply requires a
    reachable Prefect API: no in-process fallback, and submission failures
    reach the caller instead of turning into a silent blocking local run.
    """

    async def test_submits_the_deployment_fire_and_forget(self, mocker) -> None:
        mock_run = mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(),
        )
        mock_flow = mocker.patch(
            "tree.offline.offline_pipeline", new_callable=AsyncMock
        )

        result = await dispatch_offline_pipeline(
            user_id=_USER_ID, source_files=["sources/listen.yaml"], num_shards=2
        )

        assert result == {"status": "scheduled", "flow_run_id": "run-1"}
        mock_run.assert_awaited_once()
        assert mock_run.await_args.args == ("offline-pipeline/offline-pipeline",)
        assert mock_run.await_args.kwargs["timeout"] == 0
        assert mock_run.await_args.kwargs["parameters"] == {
            "user_id": str(_USER_ID),
            "source_files": ["sources/listen.yaml"],
            "sources": None,
            "num_shards": 2,
            "run_data": True,
            "run_extraction": True,
            "run_indexing": True,
            "run_clustering": False,
            "document_ids": None,
            "source_uris": None,
        }
        mock_flow.assert_not_awaited()

    async def test_forwards_the_clustering_phase_flag(self, mocker) -> None:
        """``run-clustering-pipeline`` is glue over THIS: the flag must survive.

        A dropped flag here would submit a run with clustering off — which
        completes green, having done nothing.
        """

        mock_run = mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(),
        )

        await dispatch_offline_pipeline(
            user_id=_USER_ID,
            run_data=False,
            run_extraction=False,
            run_indexing=False,
            run_clustering=True,
        )

        parameters = mock_run.await_args.kwargs["parameters"]
        assert parameters["run_clustering"] is True
        assert not any(
            parameters[phase]
            for phase in ("run_data", "run_extraction", "run_indexing")
        )

    @pytest.mark.parametrize(
        ("state_type", "expected"),
        [(StateType.SCHEDULED, "scheduled"), (StateType.PENDING, "pending")],
    )
    async def test_status_reports_the_flow_runs_own_state(
        self, mocker, state_type: StateType, expected: str
    ) -> None:
        mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(state_type=state_type),
        )

        result = await dispatch_offline_pipeline(user_id=_USER_ID)

        # The status is Prefect's, not ours: a caller can look the run up
        # under that exact state name.
        assert result["status"] == expected

    async def test_submission_failure_propagates(self, mocker) -> None:
        mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Failed to reach API"),
        )

        # No fallback swallows it: an unreachable API, a missing deployment or
        # bad parameters must surface to the caller.
        with pytest.raises(RuntimeError, match="Failed to reach API"):
            await dispatch_offline_pipeline(user_id=_USER_ID)

    async def test_forwards_the_phase_flags_and_document_ids_to_the_deployment(
        self, mocker
    ) -> None:
        mock_run = mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run("run-2"),
        )

        await dispatch_offline_pipeline(
            user_id=_USER_ID,
            run_data=False,
            run_extraction=True,
            run_indexing=False,
            document_ids=[_DOC_ID],
        )

        # A narrowed single-step run must survive the trip through the
        # deployment parameters, not silently fall back to the defaults.
        parameters = mock_run.await_args.kwargs["parameters"]
        assert parameters["run_data"] is False
        assert parameters["run_extraction"] is True
        assert parameters["run_indexing"] is False
        assert parameters["document_ids"] == [_DOC_ID]

    async def test_dispatch_forwards_source_uris(self, mocker) -> None:
        """The retry selector must survive the trip to the deployment.

        Dropped here, ``SOURCE_URIS=`` would submit an un-narrowed run that
        re-extracts every PENDING document instead of the one that failed.
        """

        mock_run = mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run("run-3"),
        )

        await dispatch_offline_pipeline(
            user_id=_USER_ID,
            source_uris=[_SOURCE_URI],
            run_data=False,
        )

        parameters = mock_run.await_args.kwargs["parameters"]
        assert parameters["source_uris"] == [_SOURCE_URI]
        assert parameters["document_ids"] is None

    async def test_source_uris_without_user_id_never_creates_a_flow_run(
        self, mocker
    ) -> None:
        mock_run = mocker.patch("tree.offline.run_deployment", new_callable=AsyncMock)

        with pytest.raises(ValueError, match="source_uris is single-tenant"):
            await dispatch_offline_pipeline(user_id=None, source_uris=[_SOURCE_URI])

        # Same both-edges guard as ``document_ids``: the dispatcher is
        # fire-and-forget, so this is the caller's only synchronous signal.
        mock_run.assert_not_awaited()

    async def test_document_ids_without_user_id_never_creates_a_flow_run(
        self, mocker
    ) -> None:
        mock_run = mocker.patch("tree.offline.run_deployment", new_callable=AsyncMock)

        with pytest.raises(ValueError, match="document_ids is single-tenant"):
            await dispatch_offline_pipeline(user_id=None, document_ids=[_DOC_ID])

        # Dispatch is fire-and-forget, so edge validation is the ONLY way the
        # caller sees this synchronously — it must precede run_deployment.
        mock_run.assert_not_awaited()
