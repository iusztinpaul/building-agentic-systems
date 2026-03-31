"""NL-to-MongoDB: translate natural language to aggregation pipelines via LLM."""

import json
import logging
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure

from twin.config.app_config import app_config
from twin.entities.knowledge_graph import EdgeType, NodeType
from twin.entities.ontology import EDGE_CONSTRAINTS, NODE_PROPERTIES
from twin.models.base import BaseEmbeddingModel, BaseLLM
from twin.models.exceptions import PipelineValidationError

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"
_EMBED_PLACEHOLDER = "__EMBED__"

_ALLOWED_STAGES = frozenset(
    {
        "$vectorSearch",
        "$match",
        "$project",
        "$sort",
        "$limit",
        "$skip",
        "$count",
        "$group",
        "$unwind",
        "$lookup",
        "$graphLookup",
        "$addFields",
        "$facet",
        "$sample",
        "$sortByCount",
        "$bucket",
        "$bucketAuto",
        "$redact",
    }
)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------


def build_nl_query_system_prompt() -> str:
    """Build the system prompt dynamically from ALL ontology registries.

    Unlike ``get_ontology_schema()`` (which only covers LLM-extractable types),
    this includes every node type, edge type, property schema, and constraint so
    the LLM can generate queries over the full knowledge graph.
    """

    # Node types and their property schemas.
    node_sections: list[str] = []
    for nt in NodeType:
        props_cls = NODE_PROPERTIES.get(nt)
        if props_cls:
            schema = props_cls.model_json_schema()
            props_json = json.dumps(schema.get("properties", {}), indent=2)
        else:
            props_json = "{}"
        node_sections.append(f"  - {nt.value}: properties schema = {props_json}")
    node_types_block = "\n".join(node_sections)

    # Edge types and their constraints.
    edge_sections: list[str] = []
    for et in EdgeType:
        constraint = EDGE_CONSTRAINTS.get(et)
        if constraint:
            edge_sections.append(
                f"  - {et.value}: {constraint.source_type.value} -> "
                f"{constraint.target_type.value} ({constraint.description})"
            )
        else:
            edge_sections.append(f"  - {et.value}")
    edge_types_block = "\n".join(edge_sections)

    return f"""\
You are a MongoDB aggregation pipeline generator for a knowledge graph.

## Collection: `{_KG_COLLECTION}`

All nodes and edges live in a single collection.

### Node document shape
- `_id`: string, format `"type:name"` (e.g. `"person:alice"`)
- `kind`: `"node"`
- `type`: one of the node types below
- `name`: string
- `properties`: dict (schema varies by node type)
- `embedding`: vector (float array) — do NOT return this field

### Edge document shape
- `_id`: string, format `"source_node_id|edge_type|target_node_id"` \
(e.g. `"person:alice|todo|task:write report"`)
- `kind`: `"edge"`
- `type`: one of the edge types below
- `source_node_id`: string (node `_id`)
- `target_node_id`: string (node `_id`)
- `source_type`: node type of source
- `target_type`: node type of target

## Node types and property schemas
{node_types_block}

## Edge types and constraints (source_type -> target_type)
{edge_types_block}

## Available indexes
- **Text index** on fields: `name`, `properties.content`, `properties.aliases`
- **Vector search index** named `"vector_index"` on field `embedding` (cosine similarity)

## Vector search
To perform vector (semantic) search, use a `$vectorSearch` stage as the **first** \
stage of the pipeline with this exact placeholder:
```json
{{
  "$vectorSearch": {{
    "index": "vector_index",
    "path": "embedding",
    "queryVector": "{_EMBED_PLACEHOLDER}",
    "queryText": "<your search text here>",
    "numCandidates": 100,
    "limit": 10,
    "filter": {{"kind": "node"}}
  }}
}}
```
The system will replace `"{_EMBED_PLACEHOLDER}"` with the actual embedding vector \
computed from `queryText`, then remove the `queryText` field before execution.

## Output format
Return a JSON object with a single key `"pipeline"` containing the aggregation \
pipeline as an array of stage objects:
```json
{{"pipeline": [<stage1>, <stage2>, ...]}}
```

## Safety rules
- ONLY use read operations. NEVER use `$out`, `$merge`, `$delete`, `$drop`, \
`$where`, `$function`, `$accumulator`, or any write operation.
- For `$lookup` and `$graphLookup`, the `from` field MUST be `"{_KG_COLLECTION}"`.
- Always include a `$limit` stage to cap results.
- Do NOT include `embedding` in returned fields.
"""


# ---------------------------------------------------------------------------
# Pipeline validation
# ---------------------------------------------------------------------------


def validate_pipeline(
    pipeline: list[dict[str, Any]],
    *,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    """Validate that a pipeline is read-only and safe.

    Raises ``PipelineValidationError`` on any violation.
    Returns the (possibly modified) pipeline with safety guards injected.
    """

    max_results = max_results if max_results is not None else app_config.mcp.max_results

    if not pipeline:
        raise PipelineValidationError("Pipeline is empty")

    has_limit = False
    for idx, stage in enumerate(pipeline):
        stage_keys = list(stage.keys())
        if not stage_keys:
            raise PipelineValidationError(f"Stage {idx} is empty")

        stage_name = stage_keys[0]

        if stage_name not in _ALLOWED_STAGES:
            raise PipelineValidationError(
                f"Stage '{stage_name}' is not allowed. "
                f"Allowed stages: {sorted(_ALLOWED_STAGES)}"
            )

        # $vectorSearch must be the first stage.
        if stage_name == "$vectorSearch" and idx != 0:
            raise PipelineValidationError(
                "$vectorSearch must be the first stage of the pipeline"
            )

        # $lookup / $graphLookup must target our collection.
        if stage_name in ("$lookup", "$graphLookup"):
            from_col = stage[stage_name].get("from", "")
            if from_col != _KG_COLLECTION:
                raise PipelineValidationError(
                    f"{stage_name} 'from' must be '{_KG_COLLECTION}', got '{from_col}'"
                )

        if stage_name == "$limit":
            has_limit = True

    # Return a new list with safety guards injected (avoid mutating the input).
    safe_pipeline = list(pipeline)
    if not has_limit:
        safe_pipeline.append({"$limit": max_results})
    safe_pipeline.append({"$project": {"embedding": 0}})

    return safe_pipeline


# ---------------------------------------------------------------------------
# Embedding placeholder replacement
# ---------------------------------------------------------------------------


async def _replace_embedding_placeholder(
    pipeline: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> list[dict[str, Any]]:
    """Replace the ``__EMBED__`` placeholder with the actual embedding vector."""

    for stage in pipeline:
        if "$vectorSearch" not in stage:
            continue

        vs = stage["$vectorSearch"]
        if vs.get("queryVector") != _EMBED_PLACEHOLDER:
            continue

        query_text = vs.pop("queryText", None)
        if not query_text:
            raise PipelineValidationError(
                "$vectorSearch has __EMBED__ placeholder but no queryText field"
            )

        vectors = await embedding_model.embed([query_text])
        vs["queryVector"] = vectors[0]

    return pipeline


# ---------------------------------------------------------------------------
# NL -> pipeline
# ---------------------------------------------------------------------------


async def nl_to_pipeline(
    llm: BaseLLM,
    query: str,
) -> list[dict[str, Any]]:
    """Translate a natural language query to a MongoDB aggregation pipeline."""

    system_prompt = build_nl_query_system_prompt()
    result = await llm.generate_json(query, system=system_prompt)

    pipeline = result.get("pipeline")
    if not isinstance(pipeline, list):
        raise PipelineValidationError(
            f"LLM response missing 'pipeline' key or not a list: {result}"
        )

    return pipeline


# ---------------------------------------------------------------------------
# End-to-end execution
# ---------------------------------------------------------------------------


async def execute_nl_query(
    client: AsyncMongoClient,
    database: str,
    query: str,
    llm: BaseLLM,
    embedding_model: BaseEmbeddingModel,
    *,
    max_retries: int | None = None,
) -> list[dict[str, Any]]:
    """End-to-end: NL -> pipeline -> validate -> execute -> results.

    On failure, feeds the error back to the LLM for self-correction
    (up to ``max_retries`` attempts).
    """

    max_retries = max_retries if max_retries is not None else app_config.mcp.max_retries
    collection = client[database][_KG_COLLECTION]

    current_prompt = query
    last_error: Exception | None = None

    for attempt in range(1 + max_retries):
        try:
            pipeline = await nl_to_pipeline(llm, current_prompt)
            pipeline = validate_pipeline(pipeline)
            pipeline = await _replace_embedding_placeholder(pipeline, embedding_model)

            logger.info(
                "Executing NL query (attempt %d/%d): %s",
                attempt + 1,
                1 + max_retries,
                json.dumps(pipeline, default=str),
            )

            cursor = await collection.aggregate(pipeline, maxTimeMS=10_000)
            results: list[dict[str, Any]] = []
            async for doc in cursor:
                results.append(doc)

            logger.info("NL query returned %d documents", len(results))
            return results

        except (PipelineValidationError, OperationFailure) as exc:
            last_error = exc
            logger.warning("NL query attempt %d failed: %s", attempt + 1, exc)
            current_prompt = (
                f"Original query: {query}\n\n"
                f"The pipeline failed with this error: {exc}\n\n"
                f"Fix the pipeline to avoid this error."
            )

    raise last_error  # type: ignore[misc]
