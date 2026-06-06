# SLM Fine-Tuning Spec — Knowledge-Graph Entity & Relationship Extraction

**Audience:** research team fine-tuning a small language model (SLM) to replace Gemini in the
memory pipeline's graph-extraction step.

**Status of decisions (agreed with Paul):**

| Decision | Choice |
|---|---|
| Training data | Distill from Gemini, then human-correct a subset |
| Ontology delivery at inference | **In-context** — the SLM keeps receiving the full ontology in the system prompt, exactly like Gemini today |
| v1 scope | **Full parity** — node/edge *types*, subtypes, semantic types **and** all properties |
| Output contract | **Byte-compatible drop-in** for the existing parser — the SLM's JSON must match what Gemini produces today so `_parse_extraction` consumes it unchanged |

This document specifies **only the LLM-extracted surface** — the `{nodes, edges}` the model
emits from a single text chunk. Everything structural (documents, chunks, `part_of` / `next` /
`mentions` edges, embeddings, IDs, reverse edges, dedup/resolution, bi-temporal supersession) is
produced deterministically by the pipeline **after** the model returns and is explicitly **out of
scope** for the model.

---

## 1. Where this sits in the system

```
document text
   │
   ├─► chunk_document()                 # token-bounded chunks (pipeline)
   │
   └─► for each chunk:
          extract_entities(llm, chunk)  # ◄── THE MODEL CALL (Gemini today → SLM)
              │  system = SYSTEM_PROMPT.format(ontology=<ontology JSON>)
              │  user   = <chunk text>
              │  return = JSON {nodes:[...], edges:[...]}
              ▼
          _parse_extraction()           # strict envelope + lenient field validation (pipeline)
              ▼
          build_structural_entries()    # document/chunk/part_of/next/mentions (pipeline, NOT model)
              ▼
          resolution + dedup + upsert   # IDs, embeddings, supersession (pipeline, NOT model)
```

Source of truth in the repo:
- Model call + system prompt: `apps/memory/src/tree/memory/extraction/core.py`
- Ontology (single source of truth): `apps/memory/src/tree/entities/ontology.py`
- Tree subtype extensions: `apps/memory/src/tree/entities/ontology_tree_extensions.py`
- Transit schema (parsed output): `apps/memory/src/tree/memory/types.py`
- **Exact ontology JSON injected into the prompt:**
  `apps/memory/tests/unit/entities/snapshots/ontology_schema.json` ← *train against this file*

---

## 2. The inference contract

The model is called with a **system prompt** and a **user message**, and must return **JSON only**
(`response_mime_type=application/json`).

### 2.1 Input — system prompt

The system prompt is a fixed template with the full ontology JSON interpolated into it. Verbatim
template (`core.py`, `_SYSTEM_PROMPT`):

```
You are a knowledge-graph extraction engine.

Given a chunk of text, extract entities (nodes) and relationships (edges)
according to the ontology below.  Return **only** valid JSON that matches
the output schema.

## Ontology
{ontology}                      ← the JSON in §5 / appendix is spliced in here

## Output schema
{ ... see §2.3 ... }

Rules:
- Node names MUST be lowercase.
- Only use node types and edge types listed in the ontology.
- When the ontology lists a `subtypes` array for a node type, pick the best matching subtype
  value from that array and put it on the node's `subtype` field. When the type has no
  `subtypes` array, omit `subtype` (or set it to null).
- Respect edge constraints (source_type → target_type).
- For `related_to` edges (the umbrella domain-relation edge), you MUST set `semantic_type` to one
  of the keys under `edge_types.related_to.semantic_types`. The (source_type, target_type) pair
  MUST appear in that semantic's `allowed_pairs`. Use the semantic's `properties` schema to
  populate the edge's `properties`.
- For every other edge type, omit `semantic_type` (or set it to null).
- If no entities or relationships are found, return empty lists.

## Emitting facts vs. typed relations vs. preferences
<the decision tree — reproduced in full in §6>
```

> The `{ontology}` block is `json.dumps(get_ontology_schema(), indent=2)`. It is deterministic
> (alphabetically sorted) and pinned by a golden-file test, so it is **byte-stable** across runs.
> The current frozen copy is `ontology_schema.json` (see appendix). When the ontology changes,
> this snapshot changes and the SLM must be re-trained — which is the accepted trade-off of the
> "in-context, full-parity" choice.

### 2.2 Input — user message

The raw chunk text. Nothing else — no IDs, no metadata, no prior graph state. The model sees one
chunk in isolation.

Chunking is token-based (`cl100k_base` tokenizer) with configurable size/overlap
(`app_config.extraction.chunk_size` / `chunk_overlap`). The research team should sample real chunk
sizes from production config so training inputs match inference inputs.

### 2.3 Output — JSON envelope

```json
{
  "nodes": [
    {
      "name": "<canonical lowercase name>",
      "type": "<node type from ontology>",
      "subtype": "<subtype from the type's subtypes list, or null>",
      "properties": { }
    }
  ],
  "edges": [
    {
      "source_node_id": "<name of source node>",
      "source_type": "<node type of source>",
      "target_node_id": "<name of target node>",
      "target_type": "<node type of target>",
      "type": "related_to",
      "semantic_type": "<a semantic_types key when type=related_to, else null>",
      "properties": { }
    }
  ]
}
```

- Empty extraction → `{"nodes": [], "edges": []}`.
- `edges[].source_node_id` / `target_node_id` reference a node's `name` (the pipeline resolves
  them to IDs later — the model does **not** emit IDs).
- The **only** edge `type` the model ever emits is `related_to` (everything else is structural,
  pipeline-emitted — see §5.4).

### 2.4 What the model emits vs. what the pipeline adds

| Field | Emitted by model? | Notes |
|---|---|---|
| `nodes[].name` | ✅ | lowercase, stripped |
| `nodes[].type` | ✅ | one of the 7 extractable node types (§5.1) |
| `nodes[].subtype` | ✅ | from the type's closed subtype set, or `null` for `preference`/`fact` |
| `nodes[].properties` | ✅ | per the type's property schema (§5) |
| `edges[].source_node_id` / `target_node_id` | ✅ | node names, lowercase |
| `edges[].source_type` / `target_type` | ✅ | node types |
| `edges[].type` | ✅ | always `related_to` |
| `edges[].semantic_type` | ✅ | one of 16 semantics, required when `type=related_to` |
| `edges[].properties` | ✅ | per the semantic's property schema |
| `chunk_id`, `_id`, `user_id`, `sources`, `embedding`, `created_at`, `updated_at`, `confidence`, `canonical_name`, `extractor`, `valid_from`/`valid_until` | ❌ pipeline | stamped after the model returns |

The model may **optionally** emit the common fields `description`, `valid_from`, `valid_until`
(ISO-8601 strings) alongside `properties` when the text states them; the pipeline never requires
them. The `extractor` field is always server-stamped — the model must not emit it.

---

## 3. The ontology at a glance (POLE + O + support types)

| Group | Node type | Subtypes (closed unless noted) | Model emits? |
|---|---|---|---|
| **P** | `person` | `individual`, `alias`, `persona` | ✅ |
| **O**rg | `organization` | `company`, `nonprofit`, `government`, `educational`, `political`, `religious`, `military` | ✅ |
| **L** | `location` | `address`, `city`, `region`, `country`, `landmark`, `coordinates` | ✅ |
| **E** | `event` | `incident`, `meeting`, `transaction`, `communication`, `travel`, `employment`, `observation` | ✅ |
| **O**bj | `object` | `vehicle`, `phone`, `email`, `document`, `device`, `software`, **`task`**, **`topic`**, **`project`** (last 3 Tree ext.) | ✅ |
| support | `preference` | none (freeform; `subtype=null`) | ✅ |
| support | `fact` | none (freeform; `subtype=null`) | ✅ |
| structural | `document`, `chunk` | — | ❌ pipeline-only |

**Relationships:** there is exactly **one** model-emitted edge type, `related_to`, discriminated by
a `semantic_type` field that takes one of **16** values (§5.3). All other edge types
(`part_of`, `next`, `mentions`, `referenced`, `has`, `same_as`, `superseded_by`) are structural and
**must never** be emitted by the model.

---

## 4. Node property schemas (model-filled)

All fields below are **optional** (default `null` / empty) unless noted **required**. The model
should fill only what the text supports — do **not** hallucinate identifiers, dates, or
coordinates. Dates are ISO-8601, and **partial dates are accepted** — `YYYY`, `YYYY-MM`, or
`YYYY-MM-DD` (the real fixtures use values like `"2024-03"`). Times are `HH:MM:SS`, durations
ISO-8601 (`PT1H30M`), coordinates a `"lat,lon"` string.

- **person** — `aliases: string[]`, `email`, `date_of_birth`, `nationality`, `occupation`
- **organization** — `aliases: string[]`, `jurisdiction`, `registration_number`
- **location** — `aliases: string[]`, `address`, `city`, `country`, `coordinates` (`"lat,lon"` string)
- **event** — `aliases: string[]`, `date`, `time` (`HH:MM:SS`), `duration` (ISO-8601 e.g. `PT1H30M`), `outcome`
- **object** — `aliases: string[]`, `identifier`, `make`, `model`, `serial_number`
  - subtype `project` additionally accepts an `external_ref` handle (`{system, id, url}`) — but
    that is **set by sync jobs, not by extraction**; the model leaves it out.
- **preference** — **required:** `statement` (≤80 chars), `category` (closed enum below);
  optional: `target`, `over`, `context`, `strength` ∈ {`weak`,`moderate`,`strong`} (default `moderate`)
  - `category` ∈ `ui`, `language`, `food`, `communication`, `work_style`, `time`, `social`,
    `aesthetic`, `other` (use `other` only as last resort)
- **fact** — **required:** `subject`, `predicate`, `object` (all free-text strings)

The exact JSON-Schema (anyOf/null shapes, descriptions, `required` arrays) the model sees is in
`ontology_schema.json` — see appendix. Train field-shape against that file, not this prose
summary.

---

## 5. Edge schema — the `related_to` umbrella

### 5.1 Shape

Every domain relationship is a single edge `type: "related_to"` plus a `semantic_type`. The
`(source_type, target_type)` pair **must** be in that semantic's `allowed_pairs`, and `properties`
follows that semantic's schema.

### 5.2 The 16 semantics

| `semantic_type` | allowed (source → target) | properties |
|---|---|---|
| `knows` | person → person | — |
| `member_of` | person → organization | `role`, `start_date`, `end_date` |
| `employed_by` | person → organization | `role`, `start_date`, `end_date` |
| `owns` | person→object, organization→object | `acquisition_date` |
| `uses` | person→object, organization→object | — |
| `located_at` | object→location, event→location | `from_date`, `to_date` |
| `resides_at` | person → location | `from_date`, `to_date` |
| `headquarters_at` | organization → location | — |
| `participated_in` | person→event, organization→event | `role` |
| `occurred_at` | event → location | — |
| `involved` | object → event | `role` |
| `subsidiary_of` | organization → organization | — |
| `partner_with` | organization → organization | — |
| `alias_of` | person→person, organization→organization, location→location, event→event, object→object | — |
| `has_task` *(Tree ext.)* | person → object (subtype `task`) | `status` |
| `experienced_by` *(Tree ext.)* | person → event | `role` |

### 5.3 Hard constraints the grader enforces

A `related_to` edge is **rejected entirely** (not just trimmed) if any of these fail:
1. `semantic_type` is present and is one of the 16 keys.
2. `(source_type, target_type)` is in that semantic's `allowed_pairs`.
3. Neither endpoint is a `fact` node (fact island rule — §6).

### 5.4 Edges the model must NEVER emit

`part_of`, `next`, `mentions`, `referenced`, `has`, `same_as`, `superseded_by`. These are
pipeline-emitted. (For training labels distilled from Gemini, drop any of these if the teacher
leaks them.)

---

## 6. Decision tree — preference vs. typed relation vs. fact

This is the hardest reasoning the model must learn. Verbatim policy from the system prompt:

For any proposition "subject — predicate — object":

1. **First-person preference** ("I prefer X over Y", "I like dark mode") → emit a `preference`
   **node** with typed slots (`statement`, `category`, optional `target`/`over`/`context`/`strength`).
   Do **not** emit a fact or a typed edge. Do **not** emit any `has` edge (the pipeline adds the
   `has: person:self → preference` edge itself). Contradictions with earlier preferences: just emit
   the new one — a downstream resolver handles supersession.
   - **Strict first-person only.** A third-party preference ("Alice prefers vegetarian food") is
     **not** a preference node → emit a `fact` instead (branch 3).

2. **Both subject and object resolve to POLE+O entities AND the relation matches a registered
   semantic** → emit a `related_to` edge with that `semantic_type`. Do **not** emit a fact.

3. **Otherwise** (free-text subject/object, no matching semantic, or third-party claim) → emit a
   `fact` **node** with `subject`/`predicate`/`object`. **Facts are island nodes: emit NO edges to
   or from a fact.** Contradictory facts: emit the new one, don't retract the old.

---

## 7. Worked examples — one per schema type

Each example is `input chunk → exact model JSON output`. These double as seed training examples.
(`properties: {}` is valid and common — only fill what the text states.)

### 7.1 person

**Input:** `"Ada Lovelace, an English mathematician, is widely regarded as the first programmer."`
```json
{
  "nodes": [
    { "name": "ada lovelace", "type": "person", "subtype": "individual",
      "properties": { "nationality": "English", "occupation": "mathematician" } }
  ],
  "edges": []
}
```

### 7.2 organization

**Input:** `"Anthropic, a US AI-safety company also known as Anthropic PBC, was founded in 2021."`
```json
{
  "nodes": [
    { "name": "anthropic", "type": "organization", "subtype": "company",
      "properties": { "aliases": ["Anthropic PBC"], "jurisdiction": "United States" } }
  ],
  "edges": []
}
```

### 7.3 location

**Input:** `"The conference was in San Francisco, California (37.7749,-122.4194)."`
```json
{
  "nodes": [
    { "name": "san francisco", "type": "location", "subtype": "city",
      "properties": { "city": "San Francisco", "country": "US",
                      "coordinates": "37.7749,-122.4194" } }
  ],
  "edges": []
}
```

### 7.4 event

**Input:** `"The product launch on 2024-05-10 ran 90 minutes and ended with a standing ovation."`
```json
{
  "nodes": [
    { "name": "product launch", "type": "event", "subtype": "meeting",
      "properties": { "date": "2024-05-10", "duration": "PT1H30M",
                      "outcome": "standing ovation" } }
  ],
  "edges": []
}
```

### 7.5 object — canonical subtype (device)

**Input:** `"She uses a MacBook Pro M3 with serial C02XL0ABJGH7 for editing."`
```json
{
  "nodes": [
    { "name": "macbook pro m3", "type": "object", "subtype": "device",
      "properties": { "make": "Apple", "model": "MacBook Pro M3",
                      "serial_number": "C02XL0ABJGH7" } }
  ],
  "edges": []
}
```

### 7.6 object — Tree subtype `task`

**Input:** `"I need to ship the demo by Friday."`
```json
{
  "nodes": [
    { "name": "ship the demo", "type": "object", "subtype": "task",
      "properties": { "aliases": [] } }
  ],
  "edges": []
}
```
*(The `person:self → task` link is added by the pipeline via `has`, or by the model via a
`related_to`/`has_task` edge when a person is named — see 7.11.)*

### 7.7 object — Tree subtype `topic`

**Input:** `"The talk covered distributed consensus and Raft."`
```json
{
  "nodes": [
    { "name": "distributed consensus", "type": "object", "subtype": "topic", "properties": {} },
    { "name": "raft", "type": "object", "subtype": "topic", "properties": {} }
  ],
  "edges": []
}
```

### 7.8 object — Tree subtype `project`

**Input:** `"We tracked the migration under the 'Atlas' project."`
```json
{
  "nodes": [
    { "name": "atlas", "type": "object", "subtype": "project", "properties": {} }
  ],
  "edges": []
}
```
*(`external_ref` is set by sync jobs, never by extraction.)*

### 7.9 preference (first-person, comparative)

**Input:** `"Honestly I much prefer dark mode over light mode when I'm coding."`
```json
{
  "nodes": [
    { "name": "prefers-dark-mode", "type": "preference", "subtype": null,
      "properties": { "statement": "prefers dark mode", "category": "ui",
                      "target": "dark mode", "over": "light mode",
                      "context": "when coding", "strength": "strong" } }
  ],
  "edges": []
}
```

### 7.10 fact (free-text / third-party — island node)

**Input:** `"Earth orbits the Sun once every 365.25 days. Alice prefers vegetarian food."`
```json
{
  "nodes": [
    { "name": "earth-orbits-sun", "type": "fact", "subtype": null,
      "properties": { "subject": "earth", "predicate": "orbits", "object": "sun" } },
    { "name": "alice-prefers-vegetarian", "type": "fact", "subtype": null,
      "properties": { "subject": "alice", "predicate": "prefers", "object": "vegetarian food" } }
  ],
  "edges": []
}
```
*Note: "Alice prefers…" is third-party → `fact`, NOT a `preference` node. No edges touch facts.*

### 7.11 relationships — one full multi-edge example

**Input:** `"In March 2024 Paul joined Anthropic as a research engineer. Anthropic is headquartered
in San Francisco, and Paul lives in Berkeley."`
```json
{
  "nodes": [
    { "name": "paul", "type": "person", "subtype": "individual", "properties": {} },
    { "name": "anthropic", "type": "organization", "subtype": "company", "properties": {} },
    { "name": "san francisco", "type": "location", "subtype": "city", "properties": {} },
    { "name": "berkeley", "type": "location", "subtype": "city", "properties": {} }
  ],
  "edges": [
    { "source_node_id": "paul", "source_type": "person",
      "target_node_id": "anthropic", "target_type": "organization",
      "type": "related_to", "semantic_type": "employed_by",
      "properties": { "role": "research engineer", "start_date": "2024-03" } },
    { "source_node_id": "anthropic", "source_type": "organization",
      "target_node_id": "san francisco", "target_type": "location",
      "type": "related_to", "semantic_type": "headquarters_at", "properties": {} },
    { "source_node_id": "paul", "source_type": "person",
      "target_node_id": "berkeley", "target_type": "location",
      "type": "related_to", "semantic_type": "resides_at", "properties": {} }
  ]
}
```

### 7.12 relationships — one minimal example per remaining semantic

| semantic | example chunk | edge (abbreviated) |
|---|---|---|
| `knows` | "Paul and Maria are old friends." | person `paul` → person `maria` |
| `member_of` | "Paul sits on the board of the EFF." | person `paul` → org `eff`, `{role:"board member"}` |
| `owns` | "Paul owns a Tesla Model 3." | person `paul` → object `tesla model 3` |
| `uses` | "Maria uses Linear daily." | person `maria` → object `linear` |
| `located_at` | "The server rack is in the Dublin datacenter." | object `server rack` → location `dublin datacenter` |
| `resides_at` | "Maria lives in Lisbon." | person `maria` → location `lisbon` |
| `headquarters_at` | "Stripe is HQ'd in South San Francisco." | org `stripe` → location `south san francisco` |
| `participated_in` | "Paul spoke at PyCon 2023." | person `paul` → event `pycon 2023`, `{role:"speaker"}` |
| `occurred_at` | "The outage happened in the us-east-1 region." | event `outage` → location `us-east-1` |
| `involved` | "The knife was used in the robbery." | object `knife` → event `robbery`, `{role:"weapon"}` |
| `subsidiary_of` | "Instagram is a subsidiary of Meta." | org `instagram` → org `meta` |
| `partner_with` | "Spotify partnered with Shopify." | org `spotify` → org `shopify` |
| `alias_of` | "FDR, i.e. Franklin Delano Roosevelt…" | person `fdr` → person `franklin delano roosevelt` |
| `has_task` | "Paul needs to file the report." | person `paul` → object `file the report` (subtype task), `{status:"pending"}` |
| `experienced_by` | "Paul lived through the 2008 crash." | person `paul` → event `2008 crash` |

---

## 8. Validation / grading rules (the acceptance function the pipeline applies)

The pipeline's parser (`_parse_extraction`) applies a **strict envelope, lenient field** policy.
The research team should grade against the same logic so training targets match what survives.

**Strict — the whole row is dropped if:**
- node/edge `type` missing or not a registered, LLM-extractable type
- (for `related_to`) `semantic_type` missing, unknown, or pair not in `allowed_pairs`
- (for any edge) endpoint pair violates the edge's `allowed_pairs`
- any edge endpoint is a `fact` node (island rule)

**Lenient — the row survives, the offending property is dropped if:**
- an unknown property key is present → dropped (`reason: unknown_field`)
- a property value fails its type/enum/length validator → that field dropped, row kept

**Closed-subtype rule:** for `person`/`organization`/`location`/`event`/`object`, `subtype` is
**required and must be a member** of that type's closed set — a missing subtype drops the whole node
(`reason: missing_subtype`), a non-member drops it (`reason: unknown_subtype`). `preference`/`fact`
take `subtype: null`.

**Exact rejection reason tokens** (log these to measure teacher quality): envelope drops —
`unknown_type`, `missing_name`, `missing_subtype`, `unknown_subtype`, `missing_endpoint_type`,
`missing_semantic_type`, `unknown_semantic`, `disallowed_pair`, `semantic_on_non_related_to`,
`fact_endpoint_disallowed`, `unknown_kind`; field drops — `unknown_field` or a Pydantic message.
Source: `apps/memory/src/tree/memory/extraction/validation.py` (`validate_envelope` +
`validate_properties`).

Suggested distillation hygiene: run Gemini, then **pass every teacher output through this exact
validator** and keep only what survives (plus the human-corrected subset). That guarantees the SLM
never learns to emit rows the pipeline would reject.

---

## 9. Notes for dataset construction (distill + human-correct)

- **Source mix:** chunks come from Substack/YouTube/custom sites/markdown/arxiv/conversations.
  Sample the training set from the same distribution (and the same chunker) so register and chunk
  length match inference.
- **Hard cases to over-sample** (where Gemini is most error-prone and human correction pays off):
  the §6 three-way decision (first-person preference vs. third-party fact vs. typed relation);
  `semantic_type` + `allowed_pairs` selection; subtype selection; property extraction of dates/roles;
  correctly returning empty lists for chunks with no entities.
- **Label = post-validator JSON**, not raw teacher output (see §8).
- **Determinism:** the ontology block is byte-stable; freeze the exact `ontology_schema.json`
  version used for a training run alongside the dataset, since any ontology change invalidates a
  "baked-in"-flavored model and shifts the in-context one.

---

# Part B — Synthetic data generation guide

Part A above is the *contract* (what the model must emit). Part B is the *recipe* (how to build the
training set). It assumes repo access; every step names the live file so you train against code, not
this prose.

## 10. Reproduce the teacher exactly

The current production teacher is **`gemini-3.1-flash-lite`** (configured at
`configs/default.yaml → models.llm`; you may swap in any capable model as teacher — the distillation
target is the SLM, not this specific model). The call (`src/tree/models/gemini.py`,
`GeminiLLM.generate_json`) is:

- `response_mime_type="application/json"` (free-form JSON — **no** `response_schema`, so the teacher
  can and will occasionally emit invalid rows; that is exactly what §12's cleaning step exists for)
- `system_instruction =` the assembled prompt (below)
- `contents =` the raw chunk text
- **no temperature set** → SDK default. For a distillation run you generally want low temperature
  (≈0–0.3) for label stability; record whatever you use alongside the dataset.

### 10.1 The single artifact to train against — the assembled prompt

The system prompt is **not** the abbreviated block in §2.1. The real string is built at call time:

```python
system = _SYSTEM_PROMPT.format(ontology=json.dumps(get_ontology_schema(), indent=2))
```

Do **not** hand-reassemble it from this doc — dump the exact bytes once and version them next to the
dataset:

```python
# scripts-style snippet; run from apps/memory with the project venv
import json
from tree.memory.extraction.core import _SYSTEM_PROMPT
from tree.entities.ontology import get_ontology_schema

prompt = _SYSTEM_PROMPT.format(ontology=json.dumps(get_ontology_schema(), indent=2))
open("frozen_system_prompt.txt", "w").write(prompt)
```

`_SYSTEM_PROMPT` (`src/tree/memory/extraction/core.py`) contains the full decision tree **with inline
examples** that §6 only paraphrases — those examples are part of the prompt and must be present
verbatim for faithful distillation. The `{ontology}` block is the appendix JSON. The assembled
string is byte-stable (golden-file pinned), so it is **identical for every training record** — see
§11.

## 11. Training-record format

One JSONL line per chunk, standard chat-fine-tuning shape:

```json
{"messages":[
  {"role":"system","content":"<the entire frozen_system_prompt.txt — identical on every line>"},
  {"role":"user","content":"<raw chunk text>"},
  {"role":"assistant","content":"{\"nodes\":[...],\"edges\":[...]}"}
]}
```

- The **system content is the same on every record** (in-context, frozen ontology). It is large
  (~the whole ontology); that is expected. If your trainer supports a shared system template or
  prompt-caching, factor it out — but the logical contract is "every example carries the full
  ontology."
- The **assistant content is the cleaned label** (§12), serialized as compact JSON
  (`json.dumps(label, separators=(",",":"), ensure_ascii=False)`) and must parse back into the
  §2.3 envelope.
- Because the ontology lives in the prompt, **re-freeze and re-train whenever the ontology changes**
  (the version this set was built against = the appendix file's current state).

## 12. Labels = post-validator JSON (the cleaning step)

The teacher's raw output is **not** the label. Pass every raw emission through the *same* validator
chain the pipeline uses and keep only what survives — that is the label:

1. `validate_envelope(...)` — strict; drops the whole row (`reason` token from §8).
2. `validate_properties(raw, schema, extras)` — lenient; drops only bad fields, keeps the row.

(`src/tree/memory/extraction/validation.py`; schema lookups via `get_node_property_schemas` /
`get_edge_property_schema` in the same module. `_parse_extraction` in `core.py` shows the exact
order the pipeline applies them, including dropping any non-LLM-extractable edge type the teacher
leaks.)

> ⚠️ The production clean lives **inside a Prefect/DB flow** (`extraction/pipeline.py`, ~L565–694) —
> there is no standalone "clean this JSON" function to import. Reuse the snippet below; it is a
> faithful, dependency-free extract of that block (verified to reproduce §12.1 byte-for-byte). The
> two-step order matters: `_parse_extraction` coerces enums + drops the grossly-malformed rows, then
> `validate_envelope` adds the **strict subtype check** (`_parse_extraction` alone is lenient on
> subtype, so a POLE+O node the teacher emits with no subtype only gets dropped here).

### 12.0 Reference cleaner — run this on every teacher emission

```python
# build_label.py — turn one raw teacher JSON emission into a training label.
# Mirrors apps/memory/src/tree/memory/extraction/pipeline.py:565-694.
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
        props, _drops = validate_properties(n.properties or {}, parent, extras)
        nodes.append({"name": n.name, "type": t, "subtype": n.subtype, "properties": props})

    edges = []
    for e in parsed.edges:
        t, st, tt = e.type.value, e.source_type.value, e.target_type.value
        if not validate_envelope(kind="edge", type=t, source_type=st,
                                 target_type=tt, semantic_type=e.semantic_type).ok:
            continue  # disallowed pair / unknown semantic / fact endpoint -> dropped
        schema = get_edge_property_schema(type=t, semantic_type=e.semantic_type)
        props, _drops = validate_properties(e.properties or {}, schema, None)
        edges.append({"source_node_id": e.source_node_id, "source_type": st,
                      "target_node_id": e.target_node_id, "target_type": tt,
                      "type": t, "semantic_type": e.semantic_type, "properties": props})

    return {"nodes": nodes, "edges": edges}  # <- the assistant-message label (§11)
```

Run from `apps/memory` with the project venv (`uv run python build_label.py`). `_drops` is the
per-field diagnostic list — aggregate it across the corpus to see which keys the teacher hallucinates
most (prompt-iteration signal). If you'd rather not cache raw teacher JSON, `extract_entities(llm,
chunk)` in `core.py` does the LLM call **and** `_parse_extraction` in one step, returning the same
`parsed` object — then apply the two validation loops above.

### 12.1 Worked raw → cleaned example

**Teacher raw output** (note three defects):

```json
{
  "nodes": [
    { "name": "paul", "type": "person", "subtype": "individual",
      "properties": { "occupation": "engineer", "favorite_color": "blue" } },
    { "name": "acme corp", "type": "organization", "subtype": "company", "properties": {} }
  ],
  "edges": [
    { "source_node_id": "paul", "source_type": "person",
      "target_node_id": "acme corp", "target_type": "organization",
      "type": "related_to", "semantic_type": "employed_by", "properties": { "role": "engineer" } },
    { "source_node_id": "paul", "source_type": "person",
      "target_node_id": "acme corp", "target_type": "organization",
      "type": "related_to", "semantic_type": "located_at", "properties": {} },
    { "source_node_id": "doc1", "source_type": "document",
      "target_node_id": "paul", "target_type": "person",
      "type": "mentions", "properties": {} }
  ]
}
```

- `favorite_color` is not a `PersonProperties` field → **field dropped** (`unknown_field`), node kept.
- `located_at` does not allow `(person, organization)` → **edge dropped** (`disallowed_pair`).
- `mentions` is a structural, non-LLM-extractable edge the teacher leaked → **edge dropped**.

**Cleaned label** (what goes in the `assistant` field):

```json
{
  "nodes": [
    { "name": "paul", "type": "person", "subtype": "individual",
      "properties": { "occupation": "engineer" } },
    { "name": "acme corp", "type": "organization", "subtype": "company", "properties": {} }
  ],
  "edges": [
    { "source_node_id": "paul", "source_type": "person",
      "target_node_id": "acme corp", "target_type": "organization",
      "type": "related_to", "semantic_type": "employed_by", "properties": { "role": "engineer" } }
  ]
}
```

> A whole-row envelope drop (e.g. an organization the teacher emitted with **no** subtype →
> `missing_subtype`) is a teacher *error*, not just noise. Prefer routing those chunks to human
> correction (§14) — add the right subtype — rather than silently shipping a chunk whose label lost
> an entity. Log reject counts per `reason` to find the teacher's weak spots.

## 13. Naming & shape conventions (must be reproduced — no code derives them)

The pipeline only **lowercases and strips** `name`; it does **not** slugify. So the teacher/label
must already carry the right surface form. These conventions live only in the prompt + examples:

- **Entity nodes** (`person`/`organization`/`location`/`event`/`object`): `name` = lowercase,
  whitespace-trimmed surface form, **spaces preserved** — `"san francisco"`, `"macbook pro m3"`.
- **`fact`**: `name` = a kebab-slug summarizing subject–predicate–object — `"earth-orbits-sun"`.
- **`preference`**: `name` = a kebab-slug of the `statement` — `"prefers-dark-mode"`.
- Every entity node carries a **non-null subtype** from its closed set (§8). `preference`/`fact`
  use `subtype: null`.
- `edges[].source_node_id` / `target_node_id` must **string-match** a `name` emitted in the same
  record (that is how the pipeline later wires them). Keep them byte-identical to the node `name`.

## 14. Coverage, balance, and the two input paths

You chose "both" for input sourcing. Use distill-over-real-corpus as the spine and synthetic inputs
as a booster for thin cells.

**Path A — distill over the real corpus (primary).** The source text is the `content` field of the
`documents` collection (the `Document` ODM in `tree.entities.documents`; one row per ingested
article/video/file). Pull `documents.content`, chunk it with the production chunker —
`chunk_document(text, chunk_size=512, chunk_overlap=64)`, `cl100k_base` tokenizer (`core.py`) —
sampling across all sources (Substack / YouTube / custom sites / markdown / arxiv / conversations) so
register and length match inference. Run teacher → clean (§12) → record (§11).

**Path B — synthesize inputs for rare cells (booster).** Real corpora under-represent some
(type, subtype) and (semantic, allowed_pair) cells. For each thin cell, prompt a generator LLM to
write a few diverse, natural sentences/paragraphs that *should* trigger it, then label them via the
same teacher+validator path and human-check. This is how you guarantee every subtype and every one
of the 16 semantics' allowed pairs is seen.

**Coverage matrix to fill** (track counts per cell):

- 7 node types × each subtype (incl. Tree `task`/`topic`/`project`).
- 16 semantics × **each** entry in their `allowed_pairs` (e.g. `owns` needs both `person→object`
  and `organization→object`; `alias_of` needs all five self-pairs).
- The three-way §6 decision: first-person `preference` vs. third-party `fact` vs. typed
  `related_to` — the highest-value, most error-prone cell.
- **Negatives:** a deliberate fraction of chunks (~15–30%) that legitimately yield
  `{"nodes":[],"edges":[]}` — boilerplate, navigation text, vague third-party chatter. Without
  these the model learns to always extract something. Include them as real training records.

**Rough minimums (scale to budget):** aim for ≥50–100 cleaned examples per semantic-pair and per
subtype, several hundred for the three-way decision, and the negative fraction above. These are
starting points, not hard numbers — `log()` the final per-cell counts so any thin cell is visible
rather than silently under-covered.

## 15. Human-correction loop

After teacher+validator, stratify a review sample toward: (a) every chunk where the teacher produced
a *rejected* row (§12), (b) the three-way decision cell, (c) synthetic Path-B chunks. Humans fix the
label; the corrected JSON replaces the teacher's. Track inter-annotator agreement on the three-way
decision — it is the metric most predictive of downstream graph quality.

## 16. End-to-end driver (turnkey)

This is §10 + §11 + §12 + §14 glued into one runnable script — the whole Path-A loop. It reads a
documents export, chunks each doc, calls the configured teacher, cleans to a label, and writes one
training record per chunk. (Verified: the loop below runs as written; with a stub teacher it produces
valid `system/user/assistant` JSONL records with the 32 KB frozen prompt as the system message and
auto-cleaned labels.)

Get the corpus with one `mongoexport` (the source field is `documents.content`, §14):

```bash
mongoexport --uri "$MONGO_URI" --collection documents --fields content \
  --type json --out documents.jsonl
```

```python
# generate_dataset.py — Path-A distillation: documents export -> training JSONL.
# Run from apps/memory with the project venv:
#   uv run python generate_dataset.py documents.jsonl > train.jsonl
import asyncio, json, sys

from tree.entities.ontology import get_ontology_schema
from tree.memory.extraction.core import _SYSTEM_PROMPT, chunk_document, _parse_extraction
from tree.memory.extraction.validation import (
    validate_envelope, validate_properties,
    get_node_property_schemas, get_edge_property_schema,
)
from tree.models.get_model import get_llm  # builds the configured teacher (reads the API key)

# Frozen for the whole run — identical system message on every record (§10.1, §11).
SYSTEM = _SYSTEM_PROMPT.format(ontology=json.dumps(get_ontology_schema(), indent=2))


def build_label(raw: dict) -> dict:  # = §12.0
    parsed = _parse_extraction(raw)
    nodes = []
    for n in parsed.nodes:
        t = n.type.value
        if not validate_envelope(kind="node", type=t, subtype=n.subtype, name=n.name).ok:
            continue
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


async def main(path: str) -> None:
    teacher = get_llm()  # gemini-3.1-flash-lite per configs/default.yaml
    with open(path) as fh:
        for line in fh:
            text = (json.loads(line).get("content") or "").strip()
            if not text:
                continue
            for chunk in chunk_document(text):  # 512-token / 64-overlap, cl100k_base
                raw = await teacher.generate_json(chunk, system=SYSTEM)
                label = build_label(raw)
                record = {"messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": chunk},
                    {"role": "assistant",
                     "content": json.dumps(label, separators=(",", ":"), ensure_ascii=False)},
                ]}
                print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

That `train.jsonl` is the fine-tuning set. Then: keep negatives in (don't filter empty-label
records — §14), route the §15 review sample for human correction, and layer Path-B synthetic chunks
for thin cells. Re-run whenever the ontology snapshot changes.

---

## Appendix — the exact ontology JSON injected into the prompt

The model receives this verbatim (pretty-printed, alphabetically sorted) inside the `## Ontology`
section of the system prompt. Train and evaluate against this file:

```
apps/memory/tests/unit/entities/snapshots/ontology_schema.json
```

It contains three top-level keys: `node_types` (the 7 extractable types with full JSON-Schema
`properties`, `required`, and `subtypes`), `edge_types` (just `related_to`, with the 16
`semantic_types` and their per-semantic `allowed_pairs` + `properties`), and `common_fields`
(`description` / `valid_from` / `valid_until`). To regenerate it from code:
`tree.entities.ontology.get_ontology_schema()`.
```
