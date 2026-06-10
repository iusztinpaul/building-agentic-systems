"""NL-to-MongoDB: translate natural language to aggregation pipelines via LLM."""

import json
import logging
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient
from pymongo.errors import OperationFailure

from tree.config.app_config import app_config
from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.entities.ontology import EDGE_CONSTRAINTS, NODE_PROPERTIES
from tree.models.base import BaseEmbeddingModel, BaseLLM
from tree.models.exceptions import PipelineValidationError
from tree.observability import span, track

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"
_EMBED_PLACEHOLDER = "__EMBED__"

# Stages that carry nested sub-pipelines are deliberately omitted from the
# allow-list. ``_inject_user_id`` only walks the TOP level of the outer
# pipeline, so any sub-pipeline-bearing stage is a tenant-isolation escape
# hatch: the inner stages never receive a ``user_id`` predicate and Mongo
# happily scans the entire collection across every tenant. The known
# offenders, all explicitly NOT in the allow-list:
#
#   * ``$lookup`` — both forms leak. The sub-pipeline form
#     (``{"from": ..., "pipeline": [...], "as": ...}``) is never walked,
#     and the ``localField``/``foreignField`` form has no place to attach
#     a ``user_id`` predicate, so the join silently matches across every
#     tenant in the collection.
#   * ``$facet`` — each output field is itself a sub-pipeline. The
#     adversarial shape ``[$match, $facet{leaked: [$lookup{...}]}, $limit]``
#     was demonstrated to leak rows from other tenants at runtime, even
#     after the cycle-1 ``$lookup`` fix, because the ``$facet`` sub-pipeline
#     is never walked. Removing ``$facet`` (rather than recursively
#     validating it) keeps the validator simple and the blast radius low.
#   * ``$unionWith`` — same shape: its ``pipeline`` argument is never
#     walked, so a union against ``knowledge_graph`` would merge in
#     documents from every tenant.
#
# ``$graphLookup`` IS in the allow-list because it does NOT take a
# sub-pipeline — it carries a flat ``restrictSearchWithMatch`` predicate
# that ``_inject_user_id`` enforces. It is the supported join shape.
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
        "$graphLookup",
        "$addFields",
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
        constraints = EDGE_CONSTRAINTS.get(et)
        if constraints:
            for constraint in constraints:
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
- DO NOT use any sub-pipeline-bearing stage — they are all rejected by the \
validator because their nested pipelines bypass the tenant filter. The \
specific forbidden stages are:
  - `$lookup` — both the sub-pipeline form and the \
`localField`/`foreignField` form are rejected. Use `$graphLookup` for \
joins; it is properly tenant-scoped via `restrictSearchWithMatch`.
  - `$facet` — its per-field sub-pipelines are not tenant-scoped. Run \
separate flat queries instead of faceting, or use `$group` for \
single-pipeline aggregations.
  - `$unionWith` — its sub-pipeline is not tenant-scoped. Run separate \
queries and combine client-side instead.
- Keep pipelines flat: no nested `pipeline` arrays anywhere.
- For `$graphLookup`, the `from` field MUST be `"{_KG_COLLECTION}"`.
- Always include a `$limit` stage to cap results.
- Do NOT include `embedding` in returned fields.
- Tenant scoping is enforced by the server: a leading `{{"$match": \
{{"user_id": <bound>}}}}` is prepended automatically when your pipeline \
doesn't begin with `$match` or `$vectorSearch`, and the bound `user_id` \
overwrites any value you supply in `$match`, `$vectorSearch.filter`, or \
`$graphLookup.restrictSearchWithMatch`. You may still emit your own \
leading `$match` for clarity; just don't try to set `user_id` yourself.
"""


# ---------------------------------------------------------------------------
# Pipeline validation
# ---------------------------------------------------------------------------


def validate_pipeline(
    pipeline: list[dict[str, Any]],
    user_id: PydanticObjectId,
    *,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    """Validate that a pipeline is read-only and safe.

    Raises ``PipelineValidationError`` on any violation.
    Returns the (possibly modified) pipeline with safety guards injected.

    Tenant scoping (see ``_inject_user_id`` for the full contract):

    - If the pipeline doesn't already lead with ``$match`` or
      ``$vectorSearch`` (the only stages whose injected filter scopes the
      initial collection scan), a fresh ``{"$match": {"user_id": user_id}}``
      is **prepended** as the new leading stage. This stops a leading
      ``$group`` / ``$sample`` / ``$sort`` / ``$project`` / ``$unwind`` /
      ``$bucket`` / ``$count`` / ``$sortByCount`` from running against
      the unfiltered collection and surfacing cross-tenant rows.
    - In every walked stage (``$match`` / ``$vectorSearch.filter`` /
      ``$graphLookup.restrictSearchWithMatch``), any attacker-supplied
      ``user_id`` is overwritten with the bound ``user_id`` — we never
      trust the LLM to pick the right tenant.
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

        # $graphLookup must target our collection. (``$lookup`` is rejected
        # earlier by the allow-list check; see the ``_ALLOWED_STAGES``
        # comment for the rationale.)
        if stage_name == "$graphLookup":
            from_col = stage[stage_name].get("from", "")
            if from_col != _KG_COLLECTION:
                raise PipelineValidationError(
                    f"{stage_name} 'from' must be '{_KG_COLLECTION}', got '{from_col}'"
                )

        if stage_name == "$limit":
            has_limit = True

    # Return a new list with safety guards injected (avoid mutating the input).
    safe_pipeline = _inject_user_id(list(pipeline), user_id)
    if not has_limit:
        safe_pipeline.append({"$limit": max_results})
    else:
        # Clamp any existing $limit that exceeds max_results.
        for stage in safe_pipeline:
            if "$limit" in stage and stage["$limit"] > max_results:
                stage["$limit"] = max_results
    safe_pipeline.append({"$project": {"embedding": 0}})

    return safe_pipeline


def _inject_user_id(
    pipeline: list[dict[str, Any]],
    user_id: PydanticObjectId,
) -> list[dict[str, Any]]:
    """Splice ``user_id`` into ``pipeline`` so every stage is tenant-scoped.

    Two complementary mechanisms:

    1. **Prepend a tenant ``$match``** as the new leading stage IFF the
       pipeline doesn't already lead with a tenant-scoping stage
       (``$match`` or ``$vectorSearch``). Without this, a pipeline whose
       first stage is e.g. ``$group``, ``$sample``, ``$sort``,
       ``$project``, ``$unwind``, ``$bucket``, ``$count``, or
       ``$sortByCount`` would run that first stage against the unfiltered
       collection and surface every other tenant's rows in its output
       (``items`` arrays, sample picks, count totals, etc.). Mongo's
       ``$vectorSearch`` must be the first stage, so we cannot prepend
       ahead of it — its ``filter`` is updated in (2) instead.
    2. **Overwrite the tenant key in every walked stage.** The LLM may
       emit a pipeline whose ``$match`` / ``$vectorSearch`` / ``$graphLookup``
       carries an attacker-supplied ``user_id`` (e.g. ``{"$ne": <bound>}``
       or a victim's id). We silently overwrite it with the bound
       ``user_id`` — we never trust the LLM to pick the right tenant.

    Top-level stages walked for overwrite (step 2):

    - ``$match`` — ``user_id`` is merged into the predicate dict
      (overwriting any LLM-supplied ``user_id`` key).
    - ``$vectorSearch`` — ``user_id`` is merged into ``filter``.
    - ``$graphLookup`` — ``user_id`` is merged into
      ``restrictSearchWithMatch`` so cross-tenant traversals are blocked.

    Stages NOT walked are still safe BECAUSE step (1) guarantees a
    tenant ``$match`` runs upstream of them. Sub-pipeline-bearing stages
    (``$lookup``, ``$facet``, ``$unionWith``) are explicitly NOT in the
    allow-list and so never reach this function. The specific stages
    removed for that reason — and why each one would otherwise leak —
    are:

      * ``$lookup`` — its sub-pipeline / ``localField`` join is never
        tenant-scoped; Mongo joins across every tenant in the collection.
        Use ``$graphLookup`` instead.
      * ``$facet`` — each output field is a sub-pipeline. The shape
        ``[$match, $facet{leaked: [$lookup{pipeline: [$match {kind:
        edge}]}]}]`` was demonstrated to leak rows from other tenants at
        runtime even after ``$lookup`` was removed at the top level. The
        per-field sub-pipelines never receive ``user_id``.
      * ``$unionWith`` — its ``pipeline`` argument is never walked, so
        unioning ``knowledge_graph`` against itself merges in documents
        from every tenant.

    Contract for future maintainers: any new stage added to
    ``_ALLOWED_STAGES`` must be flat (no nested ``pipeline`` argument,
    no per-field sub-pipelines) AND not reach into the
    ``knowledge_graph`` collection on its own. If a new stage carries a
    sub-pipeline, EITHER omit it from the allow-list (preferred — keeps
    the validator simple and the blast radius low) OR teach this function
    to walk into it. Silently adding a sub-pipeline-bearing stage breaks
    the tenant-isolation contract.
    """

    # --- Step 1: ensure a tenant filter precedes any other stage ----------
    # If the pipeline doesn't lead with $match or $vectorSearch — the only
    # two stages whose injected filter scopes the collection scan — prepend
    # a fresh tenant $match so downstream operators always see a
    # tenant-scoped subset.
    needs_prepend = True
    if pipeline:
        first_stage_keys = list(pipeline[0].keys())
        if first_stage_keys and first_stage_keys[0] in ("$match", "$vectorSearch"):
            needs_prepend = False

    working = (
        [{"$match": {"user_id": user_id}}, *pipeline] if needs_prepend else pipeline
    )

    # --- Step 2: walk every stage and overwrite any attacker-supplied
    # tenant key. The prepended $match in step 1 is processed by the same
    # loop, which is a no-op for it (the user_id is already correct).
    out: list[dict[str, Any]] = []
    for stage in working:
        if "$vectorSearch" in stage:
            vs = dict(stage["$vectorSearch"])
            existing_filter = vs.get("filter") or {}
            if not isinstance(existing_filter, dict):
                existing_filter = {}
            vs["filter"] = {**existing_filter, "user_id": user_id}
            out.append({"$vectorSearch": vs})
            continue
        if "$match" in stage:
            match = stage["$match"]
            if isinstance(match, dict):
                out.append({"$match": {**match, "user_id": user_id}})
            else:
                out.append(stage)
            continue
        if "$graphLookup" in stage:
            gl = dict(stage["$graphLookup"])
            restrict = gl.get("restrictSearchWithMatch") or {}
            if isinstance(restrict, dict):
                gl["restrictSearchWithMatch"] = {**restrict, "user_id": user_id}
            out.append({"$graphLookup": gl})
            continue
        out.append(stage)
    return out


# ---------------------------------------------------------------------------
# Embedding placeholder replacement
# ---------------------------------------------------------------------------


async def _replace_embedding_placeholder(
    pipeline: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> list[dict[str, Any]]:
    """Replace the ``__EMBED__`` placeholder with the actual embedding vector.

    When the LLM emitted a ``$vectorSearch`` stage, the query text is embedded
    here inside an ``embed_query_vector`` span so the embedding model's own
    ``llm``-type span (Voyage usage + cost, or Modal usage) nests as a named
    child of the NL-query trace. Pipelines with no ``$vectorSearch`` stage open
    no span (no embedding call happens), so a pure text/graph query isn't
    cluttered with an empty embed node.
    """

    needs_embedding = any(
        "$vectorSearch" in stage
        and stage["$vectorSearch"].get("queryVector") == _EMBED_PLACEHOLDER
        for stage in pipeline
    )
    if not needs_embedding:
        return pipeline

    with span("embed_query_vector"):
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


@track(name="nl_to_pipeline")
async def nl_to_pipeline(
    llm: BaseLLM,
    query: str,
) -> list[dict[str, Any]]:
    """Translate a natural language query to a MongoDB aggregation pipeline.

    Wrapped in an Opik ``nl_to_pipeline`` span so the headline NL→aggregation
    LLM call nests as a named child of the ``query_memory`` tool trace. The
    Gemini ``generate_content`` call below runs through the
    ``track_genai``-wrapped client (see :class:`tree.models.gemini.GeminiLLM`),
    so a nested ``llm``-type span with native Gemini token usage + cost attaches
    under THIS span — that is where Paul sees the NL-translation spend. The
    system-prompt build is its own short ``build_system_prompt`` span so the
    (cheap but non-trivial) ontology-schema assembly is visible separately from
    the model latency.
    """

    with span("build_system_prompt"):
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


@track(name="execute_nl_query")
async def execute_nl_query(
    client: AsyncMongoClient,
    database: str,
    query: str,
    llm: BaseLLM,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
    *,
    max_retries: int | None = None,
    max_results: int | None = None,
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
            pipeline = validate_pipeline(pipeline, user_id, max_results=max_results)
            pipeline = await _replace_embedding_placeholder(pipeline, embedding_model)

            logger.info(
                "Executing NL query (attempt %d/%d): %s",
                attempt + 1,
                1 + max_retries,
                json.dumps(pipeline, default=str),
            )

            with span("execute_aggregation"):
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
