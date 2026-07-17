# SLM Extraction — Input/Output Schema

A small model reads **one text chunk** and returns **one JSON object** `{nodes, edges}` describing the
entities and relationships in it. The ontology it must follow is handed to the model **in the system
prompt** on every call. This document is the schema + examples; everything operational (pipeline,
validation, building a training set) is in **Side notes** at the end.

Source of truth in the repo:

- System prompt + model call: `apps/memory/src/tree/memory/extraction/core.py`
- Ontology (single source of truth): `apps/memory/src/tree/entities/ontology.py`
- **Exact ontology JSON the model receives:**
  `apps/memory/tests/unit/entities/snapshots/ontology_schema.json`

---

## 1. Input

Two parts:

- **System prompt** — fixed instructions **+ the full ontology JSON** spliced in. Built as
  `_SYSTEM_PROMPT.format(ontology=json.dumps(get_ontology_schema(), indent=2))` and frozen in
  `ontology_schema.json` (see appendix). It tells the model the node/edge types, subtypes, the 16
  relationship semantics, their allowed type-pairs, and the per-type property schemas.
- **User message** — the raw chunk text. Nothing else: no IDs, no metadata, no prior graph state.
  The model sees one chunk in isolation.

The model returns **JSON only**. It fills `name` / `type` / `subtype` / `properties` on nodes and
`source_node_id` / `source_type` / `target_node_id` / `target_type` / `type` / `semantic_type` /
`properties` on edges. The pipeline adds everything else afterward (`_id`, `user_id`, `embedding`,
timestamps, `sources`, …) — the model never emits those.

---

## 2. Output schema

```json
{
  "nodes": [
    { "name": "<lowercase name>", "type": "<node type>", "subtype": "<subtype or null>", "properties": { } }
  ],
  "edges": [
    { "source_node_id": "<a node name>", "source_type": "<node type>",
      "target_node_id": "<a node name>", "target_type": "<node type>",
      "type": "related_to", "semantic_type": "<one of the 16>", "properties": { } }
  ]
}
```

Empty extraction → `{"nodes": [], "edges": []}`. Edge endpoints reference a node's **`name`** (string-
match, same record); the model never emits IDs. `related_to` is the **only** edge type the model ever
emits.

### 2.1 Node types & subtypes

Seven extractable types. The five POLE+O types take a **required** subtype from a closed set;
`preference` / `fact` take `subtype: null`.

| Node type      | Subtypes (closed)                                                                                  |
| -------------- | -------------------------------------------------------------------------------------------------- |
| `person`       | `individual`, `alias`, `persona`                                                                   |
| `organization` | `company`, `nonprofit`, `government`, `educational`, `political`, `religious`, `military`           |
| `location`     | `address`, `city`, `region`, `country`, `landmark`, `coordinates`                                  |
| `event`        | `incident`, `meeting`, `transaction`, `communication`, `travel`, `employment`, `observation`        |
| `object`       | `vehicle`, `phone`, `email`, `document`, `device`, `software`, `task`, `topic`, `project`           |
| `preference`   | — (`subtype: null`)                                                                                |
| `fact`         | — (`subtype: null`)                                                                                |

### 2.2 Node properties

All optional unless marked **required**. Fill only what the text supports — never invent IDs, dates,
or coordinates. Dates are ISO-8601 and **partials are fine** (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`).

- **person** — `aliases[]`, `email`, `date_of_birth`, `nationality`, `occupation`
- **organization** — `aliases[]`, `jurisdiction`, `registration_number`
- **location** — `aliases[]`, `address`, `city`, `country`, `coordinates` (`"lat,lon"` string)
- **event** — `aliases[]`, `date`, `time` (`HH:MM:SS`), `duration` (ISO-8601, e.g. `PT1H30M`), `outcome`
- **object** — `aliases[]`, `identifier`, `make`, `model`, `serial_number`
- **preference** — **required:** `statement` (≤80 chars), `category`; optional: `target`, `over`,
  `context`, `strength` ∈ {`weak`,`moderate`,`strong`} (default `moderate`).
  `category` ∈ `ui`, `language`, `food`, `communication`, `work_style`, `time`, `social`, `aesthetic`, `other`.
- **fact** — **required:** `subject`, `predicate`, `object` (free text)

### 2.3 Edges — `related_to` + a `semantic_type`

Every relationship is `type: "related_to"` plus one of 16 `semantic_type`s. The
`(source_type → target_type)` pair must be one of the allowed pairs; `properties` follows the
semantic.

| `semantic_type`  | allowed (source → target)                                                              | properties                  |
| ---------------- | -------------------------------------------------------------------------------------- | --------------------------- |
| `knows`          | person → person                                                                        | —                           |
| `member_of`      | person → organization                                                                  | `role`, `start_date`, `end_date` |
| `employed_by`    | person → organization                                                                  | `role`, `start_date`, `end_date` |
| `owns`           | person → object, organization → object                                                 | `acquisition_date`          |
| `uses`           | person → object, organization → object                                                 | —                           |
| `located_at`     | object → location, event → location                                                    | `from_date`, `to_date`      |
| `resides_at`     | person → location                                                                      | `from_date`, `to_date`      |
| `headquarters_at`| organization → location                                                                | —                           |
| `participated_in`| person → event, organization → event                                                   | `role`                      |
| `occurred_at`    | event → location                                                                       | —                           |
| `involved`       | object → event                                                                         | `role`                      |
| `subsidiary_of`  | organization → organization                                                            | —                           |
| `partner_with`   | organization → organization                                                            | —                           |
| `alias_of`       | person→person, organization→organization, location→location, event→event, object→object | —                         |
| `has_task`       | person → object (subtype `task`)                                                       | `status`                    |
| `experienced_by` | person → event                                                                         | `role`                      |

### 2.4 The 3-way rule: preference vs. related_to vs. fact

For any "subject — predicate — object" statement:

1. **First-person preference** ("I prefer X over Y") → a **`preference` node** with the typed slots
   above. Not a fact, not an edge. A third-party preference ("Alice prefers vegetarian food") is
   **not** first-person → emit a `fact` (branch 3).
2. **Both endpoints are POLE+O entities and the relation matches a semantic** → a **`related_to`
   edge** with that `semantic_type`.
3. **Otherwise** (free-text endpoint, no matching semantic, or third-party claim) → a **`fact` node**
   (`subject`/`predicate`/`object`). Facts are **islands**: no edge ever touches a fact.

---

## 3. Examples (real rows from the production graph)

Actual extracted rows (teacher `gemini-3.1-flash-lite`), shown on the model's output surface (IDs,
embeddings, timestamps stripped; the model's verbatim lowercase values kept).

**One node per type:**

```json
{ "name": "jeremy howard", "type": "person", "subtype": "individual",
  "properties": { "occupation": "ai researcher" } }
{ "name": "anthropic", "type": "organization", "subtype": "company", "properties": {} }
{ "name": "san francisco", "type": "location", "subtype": "city",
  "properties": { "city": "san francisco", "country": "united states" } }
{ "name": "finetuning-deprecation", "type": "event", "subtype": "transaction",
  "properties": { "outcome": "openai deprecation of finetuning apis" } }
{ "name": "ainews", "type": "object", "subtype": "project", "properties": {} }
{ "name": "building rag systems", "type": "object", "subtype": "task", "properties": {} }
{ "name": "prefers-custom-task-tracker-over-current-fragmented-setup",
  "type": "preference", "subtype": null,
  "properties": { "statement": "prefers custom task tracker over current fragmented setup",
                  "category": "work_style", "target": "custom solution",
                  "over": "trello, spreadsheets, and telegram bot", "strength": "moderate" } }
{ "name": "fact-saturating-benchmarks", "type": "fact", "subtype": null,
  "properties": { "subject": "polynoamial", "predicate": "argues",
                  "object": "benchmarks with uniformly high scores should be retired in favor of frontier-challenging tests" } }
```

**A few real edges** (`*_node_id` = the matched node `name`):

```json
{ "source_node_id": "demis hassabis", "source_type": "person",
  "target_node_id": "isomorphic labs", "target_type": "organization",
  "type": "related_to", "semantic_type": "member_of", "properties": { "role": "leader" } }
{ "source_node_id": "alexey", "source_type": "person",
  "target_node_id": "ai shipping labs", "target_type": "organization",
  "type": "related_to", "semantic_type": "employed_by", "properties": { "role": "founder" } }
{ "source_node_id": "ai engineer", "source_type": "person",
  "target_node_id": "building rag systems", "target_type": "object",
  "type": "related_to", "semantic_type": "has_task", "properties": { "status": "in_progress" } }
{ "source_node_id": "a100-gpu", "source_type": "object",
  "target_node_id": "dpo-training-run", "target_type": "event",
  "type": "related_to", "semantic_type": "involved", "properties": { "role": "hardware_requirement" } }
{ "source_node_id": "openai", "source_type": "organization",
  "target_node_id": "finetuning-deprecation", "target_type": "event",
  "type": "related_to", "semantic_type": "participated_in", "properties": { "role": "initiator" } }
{ "source_node_id": "anthropic", "source_type": "organization",
  "target_node_id": "san francisco", "target_type": "location",
  "type": "related_to", "semantic_type": "headquarters_at", "properties": {} }
```

**One chunk → multiple rows together:** input *"In March 2024 Paul joined Anthropic as a research
engineer. Anthropic is headquartered in San Francisco, and Paul lives in Berkeley."*

```json
{
  "nodes": [
    { "name": "paul", "type": "person", "subtype": "individual", "properties": {} },
    { "name": "anthropic", "type": "organization", "subtype": "company", "properties": {} },
    { "name": "san francisco", "type": "location", "subtype": "city", "properties": {} },
    { "name": "berkeley", "type": "location", "subtype": "city", "properties": {} }
  ],
  "edges": [
    { "source_node_id": "paul", "source_type": "person", "target_node_id": "anthropic",
      "target_type": "organization", "type": "related_to", "semantic_type": "employed_by",
      "properties": { "role": "research engineer", "start_date": "2024-03" } },
    { "source_node_id": "anthropic", "source_type": "organization", "target_node_id": "san francisco",
      "target_type": "location", "type": "related_to", "semantic_type": "headquarters_at", "properties": {} },
    { "source_node_id": "paul", "source_type": "person", "target_node_id": "berkeley",
      "target_type": "location", "type": "related_to", "semantic_type": "resides_at", "properties": {} }
  ]
}
```

A one-line trigger example for **each** of the 16 semantics is in **Side note F**.

---
---

# Side notes

Operational reference — not needed to understand the schema, but needed to build a training set and
match the pipeline's behaviour.

## A. Where this sits in the pipeline

```
document text
   └─► chunk_document()                 # 512-token / 64-overlap chunks (cl100k_base)
        └─► for each chunk:
              extract_entities(llm, chunk)     # ◄── THE MODEL CALL → {nodes, edges} JSON
                 └─► validate (envelope + fields)         # Side note B
                       └─► build structural rows + resolution + dedup + upsert   # pipeline, not model
```

Structural node/edge types the **model must never emit** (the pipeline creates them): `document`,
`chunk`, and the edges `part_of`, `next`, `mentions`, `referenced`, `has`, `same_as`,
`superseded_by`.

## B. Validation — what gets dropped

The pipeline applies **strict envelope, lenient field** (`validate_envelope` + `validate_properties`
in `apps/memory/src/tree/memory/extraction/validation.py`; full flow in `pipeline.py` ~L565–694):

- **Whole row dropped** if: type not registered/extractable; `related_to` `semantic_type` missing,
  unknown, or pair not allowed; any edge endpoint pair disallowed; any edge endpoint is a `fact`;
  a POLE+O node has a missing or non-member subtype.
- **Field dropped, row kept** if: an unknown property key, or a value that fails its type/enum/length.

Reason tokens (useful to log): `unknown_type`, `missing_name`, `missing_subtype`, `unknown_subtype`,
`missing_endpoint_type`, `missing_semantic_type`, `unknown_semantic`, `disallowed_pair`,
`semantic_on_non_related_to`, `fact_endpoint_disallowed`, `unknown_kind`; fields — `unknown_field` or
a Pydantic message.

## C. Naming & value conventions

The pipeline only **lowercases and strips** `name` (no slugging). The rest is model behaviour observed
in the live graph — reproduce the tendency, it is not a strict grammar:

- **Hard invariant:** `name` is lowercase, and each edge `source_node_id` / `target_node_id`
  string-matches a node `name` in the same record.
- `person` / `organization` / `location`: lowercase surface form, spaces kept (`"jeremy howard"`,
  `"san francisco"`).
- `event` / `object`: often kebab-slugged (`"finetuning-deprecation"`), sometimes spaced
  (`"building rag systems"`) — either is accepted.
- `preference`: kebab-slug of the `statement`. `fact`: a short slug, often `fact-…` or `fact-N`.
- **Property values come back lowercase free text**, not normalized — `"country": "united states"`
  (not ISO `US`). The §2.2 field hints guide the model; there is no post-hoc normalization.

## D. Building a training set (distill + human-correct)

**Teacher.** `gemini-3.1-flash-lite` (`configs/default.yaml → models.llm`; any capable model works).
Called with `response_mime_type="application/json"`, `system_instruction =` the assembled prompt,
`contents =` the chunk, no `response_schema` (so the teacher *will* emit some invalid rows — that's
what the cleaner below is for). Use low temperature (≈0–0.3) for label stability.

**Dump the exact prompt once** (do not hand-reassemble from this doc):

```python
import json
from tree.memory.extraction.core import _SYSTEM_PROMPT
from tree.entities.ontology import get_ontology_schema
open("frozen_system_prompt.txt", "w").write(
    _SYSTEM_PROMPT.format(ontology=json.dumps(get_ontology_schema(), indent=2)))
```

**Training record** — one JSONL line per chunk; the system message is the frozen prompt (identical on
every line), the assistant message is the cleaned label:

```json
{"messages":[
  {"role":"system","content":"<frozen_system_prompt.txt>"},
  {"role":"user","content":"<raw chunk text>"},
  {"role":"assistant","content":"{\"nodes\":[...],\"edges\":[...]}"}
]}
```

**Clean every teacher emission** (label = post-validator JSON). The production clean lives inside a
Prefect/DB flow; this is a faithful standalone extract (verified to reproduce the example below):

```python
# build_label.py — raw teacher JSON -> training label. Mirrors pipeline.py:565-694.
from tree.memory.extraction.core import _parse_extraction
from tree.memory.extraction.validation import (
    validate_envelope, validate_properties,
    get_node_property_schemas, get_edge_property_schema,
)

def build_label(raw: dict) -> dict:
    parsed = _parse_extraction(raw)  # enum-coerce + drop grossly-invalid rows
    nodes = []
    for n in parsed.nodes:
        t = n.type.value
        if not validate_envelope(kind="node", type=t, subtype=n.subtype, name=n.name).ok:
            continue  # e.g. missing/unknown subtype -> whole node dropped
        parent, extras = get_node_property_schemas(type=t, subtype=n.subtype)
        props, _ = validate_properties(n.properties or {}, parent, extras)
        nodes.append({"name": n.name, "type": t, "subtype": n.subtype, "properties": props})
    edges = []
    for e in parsed.edges:
        t, st, tt = e.type.value, e.source_type.value, e.target_type.value
        if not validate_envelope(kind="edge", type=t, source_type=st,
                                 target_type=tt, semantic_type=e.semantic_type).ok:
            continue
        schema = get_edge_property_schema(type=t, semantic_type=e.semantic_type)
        props, _ = validate_properties(e.properties or {}, schema, None)
        edges.append({"source_node_id": e.source_node_id, "source_type": st,
                      "target_node_id": e.target_node_id, "target_type": tt,
                      "type": t, "semantic_type": e.semantic_type, "properties": props})
    return {"nodes": nodes, "edges": edges}
```

Cleaning example — teacher emits `{"occupation":"engineer","favorite_color":"blue"}` on a person
(`favorite_color` dropped as `unknown_field`), a `located_at` edge between `(person, organization)`
(dropped, `disallowed_pair`), and a `mentions` edge (dropped, not model-emittable). The label keeps
only the valid person + the valid `employed_by` edge.

**End-to-end driver** — documents export → chunk → teacher → clean → JSONL:

```python
# generate_dataset.py  —  uv run python generate_dataset.py documents.jsonl > train.jsonl
# documents.jsonl: one JSON object per line with a "content" field
#   mongoexport --uri "$MONGO_URI" --collection documents --fields content --type json --out documents.jsonl
import asyncio, json, sys
from tree.entities.ontology import get_ontology_schema
from tree.memory.extraction.core import _SYSTEM_PROMPT, chunk_document
from tree.models.get_model import get_llm
from build_label import build_label  # the function above

SYSTEM = _SYSTEM_PROMPT.format(ontology=json.dumps(get_ontology_schema(), indent=2))

async def main(path: str) -> None:
    teacher = get_llm()  # the configured teacher
    for line in open(path):
        text = (json.loads(line).get("content") or "").strip()
        if not text:
            continue
        for chunk in chunk_document(text):           # 512 / 64, cl100k_base
            raw = await teacher.generate_json(chunk, system=SYSTEM)
            label = build_label(raw)
            print(json.dumps({"messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": chunk},
                {"role": "assistant", "content": json.dumps(label, separators=(",", ":"), ensure_ascii=False)},
            ]}, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

Route a stratified sample (rejected rows, the 3-way decision, rare cells) to human correction; the
corrected JSON replaces the teacher's.

## E. Coverage & balance

Cover every cell: each node subtype; each `semantic_type` × **each** of its allowed pairs (e.g. `owns`
needs both `person→object` and `organization→object`); the 3-way decision; and **negatives** — a
deliberate ~15–30% of chunks that legitimately yield `{"nodes":[],"edges":[]}` (boilerplate, vague
chatter) so the model learns restraint. Real corpora under-represent the tail semantics
(`headquarters_at`, `alias_of`, …) — synthesize inputs for those. Rough target: ≥50–100 cleaned
examples per cell, more for the 3-way decision.

## F. Real-data distribution + one example per semantic

From the live graph (793 model-extracted rows): node types are dominated by `object` and `fact`;
`location` is rare. Among semantics, `owns` / `involved` / `participated_in` / `uses` dominate while
`headquarters_at` / `alias_of` are rare — so the tail is exactly what to over-sample.

| semantic          | trigger chunk                                  | edge                                                |
| ----------------- | ---------------------------------------------- | --------------------------------------------------- |
| `knows`           | "Paul and Maria are old friends."              | person `paul` → person `maria`                      |
| `member_of`       | "Paul sits on the board of the EFF."           | person `paul` → org `eff` `{role:"board member"}`   |
| `employed_by`     | "Paul joined Anthropic in March 2024."         | person `paul` → org `anthropic` `{role,start_date}` |
| `owns`            | "Paul owns a Tesla Model 3."                   | person `paul` → object `tesla model 3`              |
| `uses`            | "Maria uses Linear daily."                     | person `maria` → object `linear`                    |
| `located_at`      | "The server rack is in the Dublin datacenter." | object `server rack` → location `dublin datacenter` |
| `resides_at`      | "Maria lives in Lisbon."                       | person `maria` → location `lisbon`                  |
| `headquarters_at` | "Stripe is HQ'd in South San Francisco."       | org `stripe` → location `south san francisco`       |
| `participated_in` | "Paul spoke at PyCon 2023."                    | person `paul` → event `pycon 2023` `{role:"speaker"}` |
| `occurred_at`     | "The outage happened in us-east-1."            | event `outage` → location `us-east-1`               |
| `involved`        | "The knife was used in the robbery."           | object `knife` → event `robbery` `{role:"weapon"}`  |
| `subsidiary_of`   | "Instagram is a subsidiary of Meta."           | org `instagram` → org `meta`                        |
| `partner_with`    | "Spotify partnered with Shopify."              | org `spotify` → org `shopify`                        |
| `alias_of`        | "FDR, i.e. Franklin Delano Roosevelt…"         | person `fdr` → person `franklin delano roosevelt`   |
| `has_task`        | "Paul needs to file the report."               | person `paul` → object `file the report` `{status}` |
| `experienced_by`  | "Paul lived through the 2008 crash."           | person `paul` → event `2008 crash`                  |

---

## Appendix — the ontology JSON file

The model receives this verbatim inside the system prompt's `## Ontology` section:

```
apps/memory/tests/unit/entities/snapshots/ontology_schema.json
```

Three top-level keys: `node_types` (the 7 types with full JSON-Schema `properties`, `required`,
`subtypes`), `edge_types` (`related_to` with the 16 `semantic_types`, their `allowed_pairs` and
`properties`), and `common_fields` (`description` / `valid_from` / `valid_until` — optional ISO-8601
fields the model may add). Regenerate with `tree.entities.ontology.get_ontology_schema()`; it is
golden-file pinned, so it changes only when the ontology changes.
```
