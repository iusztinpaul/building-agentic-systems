"""Executable copy of the retry-placement convention (ADR-002 amendment #096).

Per-file tests pin each task's own retry VALUES. This module pins the part no single
file owns: which flows must carry NO ``retries`` at all, and the two single-item load
tasks that have no other metadata test.

The rule these guard is rule 5 — **never stack**. A flow whose tasks already retry must
not itself retry, because attempts MULTIPLY (flow 2 x task 3 = up to 12 executions of the
load). The most likely future regression is someone adding ``retries=`` to one of these
flows "for consistency", which is exactly what the convention forbids.
"""

from __future__ import annotations

import pytest

from tree.data.conversation.conversation_pipeline import (
    ingest_conversation,
    load_conversation_document_task,
)
from tree.data.file.file_pipeline import ingest_file, load_file_document_task
from tree.data.huggingface.arxiv_dataset_pipeline import ingest_arxiv_dataset
from tree.data.offline_pipeline import data_etl_coordinator, data_etl_worker
from tree.data.substack.substack_pipeline_batch import ingest_substack_batch
from tree.data.web.web_pipeline import ingest_web_url_batch
from tree.data.youtube.youtube_pipeline import ingest_youtube_video
from tree.data.youtube.youtube_pipeline_batch import ingest_youtube_batch

# (flow, why it must not carry retries) — the rule that forbids it, per ADR-002 #096.
_FLOWS_WITHOUT_RETRIES = [
    # Rule 1 — dispatchers: a replay re-dispatches every shard.
    pytest.param(data_etl_coordinator, id="data-etl-coordinator"),
    pytest.param(data_etl_worker, id="data-etl-worker"),
    # Rule 2 — batch flows: the retry lives on the batch-grain tasks.
    pytest.param(ingest_web_url_batch, id="ingest-web-url-batch-etl"),
    pytest.param(ingest_substack_batch, id="ingest-substack-batch-etl"),
    pytest.param(ingest_youtube_batch, id="ingest-youtube-batch-etl"),
    # Rule 2 + the accepted streamed-Extract exception.
    pytest.param(ingest_arxiv_dataset, id="ingest-arxiv-dataset-etl"),
    # Rule 3b — the body owns ONE Opik trace; a flow retry emits one trace per attempt.
    pytest.param(ingest_file, id="ingest-file-etl"),
    pytest.param(ingest_conversation, id="ingest-conversation-etl"),
    # Rule 3c — the core delegates to shared batch tasks that already retry, and a
    # replay would re-bill the Bright Data transcript collection.
    pytest.param(ingest_youtube_video, id="ingest-youtube-video-etl"),
]


@pytest.mark.parametrize("flow", _FLOWS_WITHOUT_RETRIES)
def test_flow_does_not_stack_retries_on_its_tasks(flow) -> None:
    assert not flow.retries, (
        f"{flow.name} must not set retries — its tasks already retry, and stacking "
        "multiplies attempts (ADR-002 amendment #096, rule 5)."
    )


# Tier F, 3 x 5 s = 15 s: single-item idempotent Mongo writes, sized to outlast a
# primary election (~10-30 s). These two have no other metadata test.
@pytest.mark.parametrize(
    "task",
    [
        pytest.param(load_file_document_task, id="load-file-document"),
        pytest.param(load_conversation_document_task, id="load-conversation-document"),
    ],
)
def test_single_item_load_tasks_are_tier_f(task) -> None:
    assert task.retries == 3
    assert task.retry_delay_seconds == 5
