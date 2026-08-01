"""Shared tag constants for Prefect and Opik.

Call sites NEVER hand-write tag strings; they import a combination constant
from here so the tags are enforced in one place. Two families:

 1. PIPELINE-IDENTITY tags — the data / memory-extraction / memory-indexing
    pipelines carry these, and they match their Prefect deployment / flow-run
    tags 1:1: the SAME constant feeds both ``prefect.tags(...)`` and the Opik
    ``span(tags=...)``, so a Prefect run and its Opik trace read identically.
 2. The MCP-SURFACE family — two orthogonal axes for the MCP tools + dream:
    WHAT the work does (``ingestion`` writes vs ``retrieval`` reads) × WHERE it
    runs (``batch`` offline Prefect vs ``mcp`` online tool).

``metadata={"pipeline": "<name>"}`` still carries the finer pipeline name on
the span/trace (see :func:`tree.observability.pipeline_metadata`).
"""

TAG_INGESTION = "ingestion"
TAG_RETRIEVAL = "retrieval"
TAG_BATCH = "batch"
TAG_MCP = "mcp"

# --- Pipeline-identity tags: shared 1:1 by Prefect (deployment + flow-run tags)
# and Opik (span/trace tags). Data ETL splits by mode (offline batch vs online
# single-source); extraction/indexing have no mode (same deployment both ways). ---
TAG_DATA_PIPELINE = "data-pipeline"
TAG_MEMORY_PIPELINE = "memory-pipeline"
TAG_EXTRACTION = "extraction"
TAG_INDEXING = "indexing"
TAG_OFFLINE = "offline"
TAG_ONLINE = "online"

TAGS_DATA_OFFLINE = [TAG_DATA_PIPELINE, TAG_OFFLINE]  # offline data ETL (config batch)
TAGS_DATA_ONLINE = [
    TAG_DATA_PIPELINE,
    TAG_ONLINE,
]  # online ingest (url/file/conversation)
TAGS_EXTRACTION = [
    TAG_MEMORY_PIPELINE,
    TAG_EXTRACTION,
]  # memory extraction (both modes)
TAGS_INDEXING = [TAG_MEMORY_PIPELINE, TAG_INDEXING]  # memory indexing

# Dream consolidation — the remaining batch pipeline still on the MCP-surface family.
TAGS_INGESTION_BATCH = [TAG_INGESTION, TAG_BATCH]
# MCP ingest tools (ingest_url / ingest_file / ingest_conversation / search_web).
TAGS_INGESTION_MCP = [TAG_INGESTION, TAG_MCP]
# MCP retrieval tools (query_memory / search_memory / deep_search_memory and any
# utility tool that reads memory, e.g. visualize_memory_graph).
TAGS_RETRIEVAL_MCP = [TAG_RETRIEVAL, TAG_MCP]
# MCP utility tools that neither read nor write memory (scrape_web, review_*,
# memory_dashboard) — just the surface marker.
TAGS_MCP = [TAG_MCP]
