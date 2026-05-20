# Feature Plan: Embedding split and batching

## Summary

Today the memory pipeline has a single embedding concept used everywhere:
RESOLUTION embeds entity names, DEDUP embeds names too and persists that
name-vector on new nodes, while INDEXING backfills missing vectors from
NODE-TEXT — leaving the persisted graph an inconsistent mix of
name-embeddings and node-text-embeddings, with dedup decisions made in a
different space than search queries. This feature splits the embedding
config into a transient, name-only **`resolution_embedding_model`** and a
persisted **`search_embedding_model`** (used for dedup AND search),
extracts node-text embedding into one shared function used by both dedup
and indexing, makes DEDUP embed node-text and reuse that vector when it
creates a node (so indexing never recomputes), and adds real-time request
batching across all three stages to cut latency and the free-tier 429s the
operator hit. Net effect: every persisted node vector becomes
node-text-via-search-model (generic types) in one consistent space, and
embedding gets materially faster.

## Tasks (in order)

1. **#039** — Split embedding model config into `resolution_embedding` +
   `search_embedding` — YAML + `app_config.py` + migrate all
   `app_config.models.embedding` consumers; dim-guard pinned to the search
   model. Behavior-preserving (both blocks identical).
2. **#040** — Dual embedding-model factory —
   `get_resolution_embedding_model()` + `get_search_embedding_model()` in
   `get_model.py`; legacy `get_embedding_model()` aliases the search model.
   Depends on #039.
3. **#041** — Shared node-text embed function — extract
   `indexing.core._node_to_text` into a new `tree/memory/embedding_text.py`
   (`node_to_embedding_text`), route indexing through it; preference/fact
   statement-embedding stays separate. Depends on #040.
4. **#042** — Dedup on node-text via search model + reuse vector on
   new-node creation — `add_entity` and extraction tasks ④/⑤/⑥ embed
   node-text (not name), persist that vector; resolves the
   name-vs-node-text inconsistency. Carries the re-extract migration note.
   Depends on #041.
5. **#043** — Resolution semantic stage uses `resolution_embedding_model`
   (name-only, transient) — `_build_resolver` + flow entry points hold two
   distinct handles; supersession stays on the search model. Depends on
   #040. (Independent of #041/#042; ordered after for a clean sequence.)
6. **#044** — Real-time request batching for embeddings (resolution +
   dedup + indexing) — pack texts into multi-input synchronous
   `/v1/multimodalembeddings` requests bounded by 1000 inputs / 320K
   tokens, preserve the 429 backoff. Explicitly rejects Voyage's async
   Batch API. Depends on #042, #043.
7. **#045** — E2E acceptance — slow + mongot-dependent integration test +
   manual runbook proving consistent node-text vectors, vector-space
   agreement, and a lower embed-request count, on the Paul Iusztin seed
   user. Depends on #044.

## Out of scope (intentional)

- **Voyage async Batch API discount path.** Rejected for #044 because
  (a) its 12-hour completion window can't drive a synchronous mid-flow
  dedup decision, and (b) it doesn't support `/v1/multimodalembeddings`,
  the endpoint our pinned `voyage-multimodal-3` model uses. Using it would
  require switching the search/dedup model back to a text model on
  `/v1/embeddings` AND decoupling embedding from the synchronous pipeline —
  a much larger change. Surfaced as an operator decision below.
- **A new migration script.** The stale-vector convergence reuses the
  existing `RESET_ONTOLOGY=1` runbook in `CLAUDE.md`; #042 only documents
  the re-extract requirement.
- **Actually configuring a lighter resolution model.** The split makes it
  *possible*; both blocks ship pointing at `voyage-multimodal-3`. Choosing
  and validating a lighter model is a future task.
- **Pre-existing bug:** `indexing/core.py:339` has invalid Py3 syntax
  (`except TypeError, ValueError:`). Not introduced by this feature and out
  of scope; flagged here so it isn't mistaken for our change. File
  separately if it bites.

## Documentation updates (this grooming round)

This project has no `docs/adr/` and no `docs/glossary.md` (documentation
discipline is opted-out per scaffold). No ADRs authored, no glossary
edits. The architectural decision that would otherwise warrant an ADR —
"batching means real-time request batching, NOT the async Batch API" — is
captured in #044's Scope and "Rejected alternatives" criterion instead.

## Open questions / Operator decision points (Step-3 approval gate)

1. **Async Batch API rejection (confirm).** #044 implements real-time
   request batching against the synchronous `/v1/multimodalembeddings`
   endpoint and explicitly does NOT use Voyage's async Batch API
   (12h window + endpoint incompatible with `voyage-multimodal-3`). If you
   actually wanted the async batch DISCOUNT, that's a different, larger
   feature (switch search/dedup to a text model on `/v1/embeddings` and
   decouple embedding from the synchronous pipeline) — say so now and we
   re-plan.
2. **Re-embed migration consequence (acknowledge).** After #042, the
   persisted node vector becomes node-text-via-search-model everywhere.
   Existing rows are stale (dedup-created rows hold name-vectors;
   voyage-3-era rows are in a different semantic space). Convergence
   requires a re-extract + re-index via the existing `RESET_ONTOLOGY=1`
   migration — there will be a `$vectorSearch`-degraded window during the
   rebuild (text + graph search keep working). OK to require operators run
   that migration after merge?
3. **Batching config knobs (preference).** #044 may surface
   `max_inputs` / `max_total_tokens` as YAML knobs (defaults at the Voyage
   caps) or keep them as code constants. Any preference, or leave it to the
   implementer?
4. **E2E tier (logistics).** #045's `[HUMAN]` live-run criterion needs
   either a paid Voyage key or a small `DOC_IDS` subset on free tier
   (3 RPM). Which will the Tester use?
