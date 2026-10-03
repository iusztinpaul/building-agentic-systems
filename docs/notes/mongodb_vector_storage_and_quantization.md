# MongoDB vector storage and quantization — from BSON arrays to binary vectors

Note on **how many bytes one embedding costs in MongoDB**, why task 175 cut it ~3.2×, and what the
next steps (real quantization) would buy. Written after the 2026-10 prod backfill hit the Atlas M0
512 MB cap: with vectors stored as BSON arrays, the memory pipeline's bulk load died with
`AutoReconnect … connection closed` at the same row count on every retry.

Grounding: `apps/memory/src/tree/entities/memory.py` (`to_stored_vector` / `from_stored_vector`),
task `tasks/done/175-binary-float32-embedding-storage.md` (measurements), ADR-006 §4,
`models.search_embedding` in `apps/memory/src/tree/config/default.yaml` (voyage-4, 1024 dims).

---

## TL;DR — the progression for one 1024-dim vector

Bytes are the `embedding` field alone (the rest of a child row — content, title, heading path,
metadata — is ~1.5 KB and unchanged). Corpus column = × 17,305 child chunks, the full local
rehearsal of the prod corpus (10,085 documents).

| # | Method | Per element | Per vector | vs raw | 17,305 vectors | Retrieval quality | Status |
|---|---|---|---|---|---|---|---|
| 1 | **Raw**: BSON array of float64 doubles | 8 B value + 1 B type + ~3.9 B key | **13,231 B** | 1.0× | 229 MB | reference | before task 175 |
| 2 | + **float32 values** (accounting step: array overhead kept, payload halved) | 4 B value + 4.9 B overhead | 9,135 B | 1.4× | 158 MB | identical (cosine drift ~2e-9) | not storable on its own — BSON arrays only hold doubles |
| 3 | + **drop the per-element overhead**: packed float32 `binData` (vector subtype 9) | 4 B | **4,098 B** | **3.2×** | **71 MB** | identical | ✅ **what we ship** (task 175) |
| 4 | **int8 quantization** stored (`BinaryVectorDtype.INT8`) | 1 B | 1,026 B | 12.9× | 18 MB | small loss | option, not used |
| 5 | **binary quantization** stored (`BinaryVectorDtype.PACKED_BIT`) | 1 bit | 130 B | 102× | 2.2 MB | larger loss; needs rescoring | option, not used |
| 6 | **Quantize only the search index** (`"quantization": "scalar"` / `"binary"` on the Atlas vector index) | docs keep 4 B | 4,098 B on disk (unchanged) | 3.2× on disk | 71 MB on disk | ~identical with rescoring | option, not used |

Row 6 does not shrink the collection at all: it shrinks the **mongot vector index in RAM** (MongoDB's
docs quote ~3.75× less for scalar and ~24× less for binary), while the full-fidelity float32 vectors
stay in the documents for rescoring. It is the lever for search-node memory, not for the M0 storage
cap.

**Prod impact of step 3**, measured on the full local rehearsal: `memory` = 98 MiB storage + 74 MiB
indexes, the whole `tree` database ≈ 197 MiB on disk, ≈ 2.4× headroom under the 512 MB cap. With
step 1 the same corpus extrapolated to ~610 MB storage + ~120 MB indexes — over the cap, which is
why the backfill kept failing.

---

## The arithmetic

### 1. Raw — BSON array of doubles (13,231 B)

A BSON array is a document whose keys are the indexes as strings. Every one of the 1,024 elements
pays:

- 1 B element type (`0x01` = double)
- the key as a C string: `"0"`–`"9"` 2 B, `"10"`–`"99"` 3 B, `"100"`–`"999"` 4 B,
  `"1000"`–`"1023"` 5 B → 4,010 B in total, ~3.9 B average
- 8 B IEEE-754 double

1,024 × (1 + 8) + 4,010 + 5 B array framing = **13,231 B**. Only 8,192 B of that (62%) is the numbers.

Python floats are 64-bit, so pymongo writes a `list[float]` this way with no option to do otherwise.

### 2. float32 values (accounting step)

Embedding models compute in float32 (or lower), so the extra 29 bits of mantissa in a double carry
noise, not signal. Halving the payload saves 4,096 B. BSON has no 32-bit float element type, so this
saving is **only reachable together with step 3** — the row exists to show where the bytes go.

### 3. Packed float32 binData — what we ship (4,098 B)

`Binary.from_vector(vector, BinaryVectorDtype.FLOAT32)` (pymongo ≥ 4.10) writes ONE `binData`
element of subtype 9 (BSON vector): a 2-byte header (dtype `0x27` + padding) followed by the 1,024
float32 values back to back: 2 + 1,024 × 4 = **4,098 B**. No per-element type byte, no per-element
key.

- **Not quantization.** Each dimension is still a full IEEE-754 float (~7 significant digits). The
  tester measured a float64→float32 cosine drift of ~2.2e-9 over 200 random 1024-dim vectors, no
  score crossing `min_vector_score` 0.70, and identical rankings for 12 queries.
- **Atlas Vector Search reads it natively** — same index definition (`path: "embedding"`,
  `numDimensions: 1024`, cosine), and `$vectorSearch` still takes a `list[float]` `queryVector`.
- In code: every write goes through `to_stored_vector`, every read that uses the numbers through
  `from_stored_vector` (which rejects anything that is not a float32 vector). A row with no vector
  has no `embedding` field — "pending" is `{"embedding": None}`, "embedded" is
  `{"embedding": {"$type": "binData"}}` — because `$size` / `embedding.0` do not work on binData.

### 4. int8 quantization (1,026 B)

Each dimension mapped to one of 256 levels: 2 + 1,024 × 1 = 1,026 B. Two ways to get there: ask
Voyage for `output_dtype="int8"` (the model is trained for it), or quantize our float32 vectors
ourselves. Both lose some retrieval quality, so this would need an eval run against the current
float32 baseline, an Embedding reset, and a vector index with the int8 type.

### 5. Binary quantization (130 B)

One bit per dimension (the sign): 2 + 1,024 / 8 = 130 B, ~102× smaller than raw. The quality loss is
large enough that it is normally paired with a rescoring pass over full-precision vectors — which
then have to be stored somewhere anyway.

### 6. Quantize only the search index

Add `"quantization": "scalar"` (int8) or `"binary"` to the vector field of the Atlas index definition
(`_build_vector_index_definition` in `apps/memory/src/tree/memory/rag/indexing.py`). mongot keeps
the quantized copy in memory for the ANN search and rescores candidates against the float32 vectors
the documents still hold. Document storage is unchanged; search-node RAM drops. This is the first
lever to pull if vector-search memory, not disk, becomes the limit.

---

## When to move past step 3

- **Disk cap again** (a bigger corpus on M0): step 4 (int8 stored) is the next 4×, after an eval.
  Halving `models.search_embedding.dimensions` to 512 (voyage-4 is Matryoshka-trained) is the
  alternative 2× without changing the dtype.
- **Search memory / latency** on a dedicated tier: step 6 first — it keeps full-precision vectors
  for rescoring and needs no re-embedding, only an index rebuild.
- **Don't** go back to arrays (step 1): there is no read path that needs them.
