# `User` Beanie entity + self-person auto-creation hook

Status: pending
Tags: `phase-1`, `multi-tenancy`, `entities`, `user`, `foundation`
Depends on: #016
Blocks: #018, #019, #020, #021

## Scope

Introduce the **`User` Beanie model** at `apps/memory/src/tree/entities/users.py` (new file) and an **`after_insert` hook** that auto-creates the user's `person:self` node inside the `knowledge_graph` collection. This is the tenant-identity ledger; every downstream tenant scoping (`user_id` on `Document`, `KnowledgeGraphEntry`, etc.) refers to a `User._id`.

Per `plan.md` Phase 1 and decision #1: the active user is represented in the KG by a single `person` node with `_id = "{user_id}:person:self"`, `name="self"`, `properties.is_active_user=True`. **There is no `User.self_person_id` field** — the flag on the person node is the single source of truth (eliminates two-source drift).

The self-person node is written through `KnowledgeGraphEntry` directly (no `KGQuery` yet — that's #019); however the **id shape** for this node already follows the multi-tenant convention `"{user_id}:person:self"`. Because #018 lands the `user_id`-aware `build_node_id`, this task **lands a temporary local helper** in `users.py` that hand-builds the self-person `_id` string, and a TODO marker pointing at #018 to swap it for `build_node_id(user_id, NodeType.PERSON, "self")` once that helper exists. The hook code itself is the durable artifact; the local id-builder is a 3-line transitional bridge.

### Files touched

- `apps/memory/src/tree/entities/users.py` — NEW. `User` Beanie model + `after_insert` hook.
- `apps/memory/src/tree/entities/__init__.py` — export `User`.
- `apps/memory/src/tree/db.py` — register `User` in the Beanie initialization document list (alongside `Document`, `KnowledgeGraphEntry`).
- `apps/memory/tests/unit/entities/test_users.py` — NEW. Unit tests for the model and the hook (using a mocked Mongo via `mocker` per testing conventions; no real DB).
- `apps/memory/tests/integration/entities/test_users_self_person_hook.py` — NEW. Integration test against a real local Mongo: insert a user, assert the self-person node lands at the right `_id` with the right properties.

### `User` schema

```python
from datetime import UTC, datetime
from typing import Any

from beanie import Document as BeanieDocument
from beanie import Indexed, PydanticObjectId
from pydantic import Field


class User(BeanieDocument):
    """Tenant identity. Every Document and KnowledgeGraphEntry carries the
    referencing user's _id in its `user_id` field (#018).

    There is NO `self_person_id` field. The user's representation inside
    their own KG is the node at `_id = "{user_id}:person:self"`, identified
    by `properties.is_active_user=True`. Keeping that flag the single source
    of truth eliminates two-source drift.
    """

    identifier: Indexed(str, unique=True)
    """Stable external handle (e.g. email or OIDC `sub`). Free string for now."""

    attributes: dict[str, Any] = Field(default_factory=dict)
    """Display name, locale, prefs, etc. The self-person hook uses
    `attributes.get('name', identifier)` as `canonical_name`."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "users"

    async def after_insert(self) -> None:
        """Idempotent self-person creation. Writes a `person` KG node with
        `_id="{user_id}:person:self"`, `name="self"`, `properties.is_active_user=True`.
        Re-running this hook on an existing user is a no-op (upsert by _id)."""
        ...
```

### Self-person hook contract

The hook writes **exactly one** `KnowledgeGraphEntry` with:

- `id = f"{self.id}:person:self"`  (using a transitional local helper; #018 swaps this for `build_node_id`)
- `kind = "node"`
- `type = NodeType.PERSON`
- `name = "self"`
- `canonical_name = self.attributes.get("name") or self.identifier`
- `properties = {"is_active_user": True, **(self.attributes or {})}`  — flag first; user attributes mirrored (locale, prefs) for downstream "what does the user know about themselves" queries.
- `user_id` field: **set on the KG entry** if and only if #018 has already landed. **In this task #017, `user_id` does NOT yet exist on `KnowledgeGraphEntry`** — Phase 1 lands sequentially, and this hook will be revisited in #018 to add `user_id=self.id` to the write. Mark with a `# TODO(#018)` comment. The integration test in this task ignores `user_id`; the integration test in #018 reasserts the field is present.
- `created_at`, `updated_at` = `datetime.now(UTC)`.

**Idempotency:** the hook uses an upsert on `_id` (`KnowledgeGraphEntry.find_one_and_update({"_id": ...}, {"$setOnInsert": {...}}, upsert=True)`). Re-inserting the same `User` (in tests / migration re-runs) is a no-op for the self-person node. The hook itself only fires on `insert`, but a re-issued `User.insert()` will simply upsert the user again (Beanie `insert` is not idempotent on its own; callers needing idempotency must use `find_one_and_update` or check `User` existence first — the migration script in #021 will do exactly that).

### Behavior guarantees

- `User` lives in collection `users`.
- `User.identifier` is uniquely indexed; inserting two users with the same `identifier` raises `pymongo.errors.DuplicateKeyError`.
- `User.attributes` defaults to `{}`; can hold any JSON-compatible dict.
- After `User.insert()`, querying `KnowledgeGraphEntry.find_one({"_id": f"{user._id}:person:self"})` returns the self-person node with `properties.is_active_user=True` and `name="self"`.
- The hook is async and awaited by Beanie's standard event pipeline.
- All datetimes are tz-aware UTC.

## Acceptance Criteria

- [x] File `apps/memory/src/tree/entities/users.py` exists with the `User` class exactly as described.
- [x] `User.identifier` is a unique indexed field (`Indexed(str, unique=True)`).
- [x] `User.attributes` is `dict[str, Any] = Field(default_factory=dict)`.
- [x] `User` is registered in `tree.db.init_mongodb()` (or wherever the Beanie `document_models` list lives), alongside the existing `Document` and `KnowledgeGraphEntry` entries.
- [x] `User` is exported from `tree.entities` and `tree.entities.users`.
- [x] Unit test: `User(identifier="paul@example.com")` constructs; `model_dump()` shows `attributes == {}`, tz-aware `created_at` / `updated_at`.
- [x] Unit test: two `User` instances with the same `identifier` raise a `DuplicateKeyError` on a mocked Beanie/Motor index path (or via `pytest.raises` on the insert). _(Covered by the integration test `test_duplicate_identifier_raises_duplicate_key_error` — real index path; the unit suite verifies the model shape via mocks.)_
- [x] Integration test (against a real local Mongo): `await User(identifier="paul@example.com", attributes={"name": "Paul"}).insert()` creates the user AND a `KnowledgeGraphEntry` with `_id = f"{user.id}:person:self"`, `type == NodeType.PERSON`, `kind == "node"`, `name == "self"`, `canonical_name == "Paul"`, `properties["is_active_user"] is True`.
- [x] Integration test: calling the hook twice on the same `User.id` does NOT duplicate the self-person node (upsert idempotency).
- [x] Integration test: when `attributes` is missing `name`, `canonical_name` falls back to `identifier`.
- [x] No `User.self_person_id` field exists. Code review check.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` green. _(Only pre-existing unrelated failure: `tests/unit/config/test_app_config.py::TestLoadAppConfig::test_loads_default_yaml` — config drift `gemini-2.5-flash-lite` vs `gemini-3.1-flash-lite`, confirmed present on `feat/multi-tenancy` HEAD before this change.)_
- [x] Targeted integration tests for this module green (full integration suite runs in #021).

## User Stories

### Story: Seed user gets a self-person node automatically
1. Developer runs the seed step of the migration script (#021): `User(identifier="dev@example.com", attributes={"name": "Dev User"})` then `await user.insert()`.
2. The `after_insert` hook writes `KnowledgeGraphEntry(_id=f"{user.id}:person:self", type="person", name="self", canonical_name="Dev User", properties={"is_active_user": True, "name": "Dev User"})`.
3. Subsequent KG operations under this user can immediately attach edges (`has`, etc.) to this `person:self` node — no manual setup.

### Story: Re-running seed is a no-op for the self-person node
1. A developer accidentally re-runs the seed script after fixing an unrelated bug.
2. The user exists already (existence check in the migration script avoids a duplicate insert).
3. If the script does call `User.insert()` directly, the duplicate-identifier index raises — the script knows to skip.
4. If the developer manually re-fires the hook via a one-off `await user.after_insert()`, the self-person node is upserted, not duplicated.

### Story: `is_active_user` flag is the single source of truth
1. Application code never reads `User.self_person_id` (the field does not exist).
2. To find the active-user node, code queries `KnowledgeGraphEntry.find_one({"user_id": user.id, "type": "person", "properties.is_active_user": True})` (the `user_id` filter lands in #018; for now it's `{"type": "person", "name": "self", "_id": {"$regex": f"^{user.id}:"}}`).
3. There is no way for two equally-canonical "active user" claims to drift apart.

## Test plan

**Unit tests:**
- `User` field shape, default values, tz-aware timestamps.
- Hook constructs the right `KnowledgeGraphEntry` payload (assert on the mocked collection's upsert args).
- `canonical_name` fallback: `name` in attributes wins; missing `name` → `identifier`.
- `properties` merges `is_active_user=True` with `attributes` without dropping caller keys.

**Integration tests** (new file under `apps/memory/tests/integration/entities/`):
- Insert a real `User`, then re-fetch the `person:self` node from `knowledge_graph` and assert the full shape.
- Re-run the hook explicitly (`await user.after_insert()`) and assert no duplicate (`count_documents` stays at 1 for the self-person `_id`).
- Insert two distinct users; assert each gets its own self-person node at the right `_id` and neither overwrites the other.
- Duplicate `identifier` insert raises `DuplicateKeyError`.

**Manual verification:**
- `uv --directory apps/memory run python -c "from tree.entities import User; print(User)"` succeeds.

## Out of scope

- `user_id` field on `KnowledgeGraphEntry` / `Document` — that's #018.
- Threading `user_id` through pipelines or MCP tools — that's #019/#020.
- A `User` CRUD MCP tool — not needed for Phase 1.
- Auth wiring for `identifier` (OIDC, etc.) — Phase 1 keeps `identifier` a free string. Auth is later.

---

Refs: `plan.md` Phase 1 §"Recommendation for user modeling", decision #1, decision #6
Blocked by: #016

## Log

### [SWE] 2026-05-16 16:03 — Implementation

**Files modified**
- `apps/memory/src/tree/entities/users.py` — NEW. `User` Beanie model + `@after_event(Insert)` hook that upserts the `person:self` node into `knowledge_graph`. Includes transitional `_build_self_person_id` helper with a `# TODO(#018)` marker for the upcoming `build_node_id(user_id, ...)` swap.
- `apps/memory/src/tree/entities/__init__.py` — exports `Document`, `SourceType`, `KnowledgeGraphEntry`, `NodeType`, `EdgeType`, `build_node_id`, `build_edge_id`, `User`.
- `apps/memory/src/tree/db.py` — adds `User` to `ALL_DOCUMENT_MODELS` so Beanie registers the collection + index on init.
- `apps/memory/tests/unit/entities/test_users.py` — NEW. 15 unit tests: model shape, default values, tz-aware timestamps, the `_build_self_person_id` helper, the hook payload shape (mocked pymongo collection), canonical-name fallback, properties merge, `is_active_user` flag protection, exports, settings.
- `apps/memory/tests/integration/entities/__init__.py` — NEW (empty package marker).
- `apps/memory/tests/integration/entities/test_users_self_person_hook.py` — NEW. 5 integration tests against local Mongo: real `User.insert()` creates the self-person node, hook idempotency (no double-write), canonical-name fallback, two distinct users → two distinct nodes, duplicate-identifier raises `DuplicateKeyError`.

**Tests**
- Unit (entities only): 15/15 green, 0 warnings (`pytest -W error`).
- Unit (whole module, scoped): `tests/unit/entities + tests/integration/entities` → 58/58 green, 0 warnings.
- Unit (full memory suite): 752 passing, 1 unrelated pre-existing failure in `tests/unit/config/test_app_config.py::TestLoadAppConfig::test_loads_default_yaml` (asserts `models.llm.model == "gemini-2.5-flash-lite"` but the pinned config is `gemini-3.1-flash-lite`). Confirmed pre-existing on `feat/multi-tenancy` HEAD via `git stash && pytest …` before this change.
- Integration (this module): 5/5 green.

**Acceptance criteria** — all checked above; the two-`DuplicateKeyError`-on-`identifier` case is exercised by the integration test rather than a separate unit test (the real unique index is what enforces it; mocking it would test the mock, not the contract).

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
…
202 files left unchanged
All checks passed!
202 files already formatted
All checks passed!

$ make pre-commit
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ uv --directory apps/memory run pytest tests/unit/entities tests/integration/entities -W error
collected 58 items
tests/unit/entities/test_documents.py ....                               [  6%]
tests/unit/entities/test_knowledge_graph.py ........................     [ 48%]
tests/unit/entities/test_ontology.py ..........                          [ 65%]
tests/unit/entities/test_users.py ...............                        [ 91%]
tests/integration/entities/test_users_self_person_hook.py .....          [100%]
============================== 58 passed in 0.39s ==============================
```

**Notes for Tester / PM**

1. **Beanie hook registration:** the spec's example schema names the hook `async def after_insert` without a decorator. Beanie does NOT auto-fire methods by name — the hook must be decorated `@after_event(Insert)` (imported from `beanie`). Initial implementation followed the spec literally and silently dropped writes; integration tests caught it immediately. Final implementation uses the decorator, which is the canonical Beanie pattern.
2. **`get_pymongo_collection` (not `get_motor_collection`):** Beanie's current API exposes the underlying async pymongo collection via `get_pymongo_collection()`. The spec's prose mentions Motor — that's stale terminology. No behavioral difference; the call still returns an async collection and `update_one` is awaitable.
3. **`properties` merge order:** the spec says `properties = {"is_active_user": True, **(self.attributes or {})}` — flag first, attrs override. I deliberately reversed the order to `{**attributes, "is_active_user": True}` so a caller cannot shadow the flag from `attributes={"is_active_user": False, ...}`. A unit test (`test_properties_caller_cannot_override_is_active_user_flag`) pins this behavior. If the Tester / PM prefers strict spec adherence, flip the spread order and drop the test — but I think flag-wins is the safer default given the flag is the single source of truth.
4. **`user_id` field on the KG entry:** not stamped (Phase 1 lands sequentially; `KnowledgeGraphEntry.user_id` arrives in #018). A `# TODO(#018)` comment marks the exact line to update.
5. **Pre-existing unit-test failure:** `tests/unit/config/test_app_config.py::TestLoadAppConfig::test_loads_default_yaml` fails on the current branch HEAD (config drift). Verified pre-existing via `git stash`. Out of scope for this task.

**Hand-off**

Code is local, uncommitted. Ready for Tester review.

### [Tester] 2026-05-16 16:13 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests (full memory suite): 752 passed, 1 pre-existing failure (`tests/unit/config/test_app_config.py::TestLoadAppConfig::test_loads_default_yaml` — `gemini-3.1-flash-lite` config vs `gemini-2.5-flash-lite` assertion). Verified pre-existing on branch HEAD (the configs/default.yaml on this branch already pins `gemini-3.1-flash-lite`); unrelated to #017.
- Unit tests (scoped `tests/unit/entities`): 53/53 green, 0 warnings (`pytest -W error`); the 15 new `test_users.py` tests included.
- Integration tests (full memory suite): 134 passed, 11 skipped (mongot-only probes), 0 failed, 0 warnings, 212.89s.
- Integration tests (scoped `tests/integration/entities`): 5/5 green, 0 warnings.

**E2E adversarial pass** (real-Mongo script `/tmp/qa_017_e2e.py`, DB `qa_017_e2e`, dropped after run):
- Happy path: `User(identifier="paul@e2e.test", attributes={"name": "Paul", "locale": "en-US"}).insert()` → self-person node lands at `{user.id}:person:self` with `name="self"`, `type=PERSON`, `kind="node"`, `canonical_name="Paul"`, `properties.is_active_user=True`, attributes mirrored, tz-aware timestamps. **PASS**.
- Break path 1 (boundary — duplicate identifier): inserting a second `User(identifier="paul@e2e.test", ...)` raised `pymongo.errors.DuplicateKeyError` code 11000. **PASS**.
- Break path 2 (hostile input — caller tries to shadow the source-of-truth flag): `User(identifier="adv@e2e.test", attributes={"is_active_user": False, "name": "Adv", "extra": 1}).insert()` → resulting node has `properties.is_active_user=True` AND `properties.extra=1` (caller keys mirrored without dropping the flag). The SWE's deliberate spec deviation works exactly as intended. **PASS**.
- Break path 3 (state edge — hook idempotence): inserted user once, then manually called `await user.after_insert()` 4 more times → `count_documents({"_id": f"{user.id}:person:self"}) == 1`. `$setOnInsert` semantics hold. **PASS**.
- Bonus break paths run for completeness:
  - `attributes={}` → `canonical_name` falls back to `identifier`, `properties == {"is_active_user": True}` (no leakage). **PASS**.
  - Two distinct users → two distinct `_id`s, no cross-tenant collision. **PASS**.

**SWE's deliberate spec deviation (`properties` merge order)** — assessment: **the SWE was right**. The spec sketched `{"is_active_user": True, **attributes}`, but the hardening to `{**attributes, "is_active_user": True}` directly enforces the "single source of truth" contract that decision #1 in `plan.md` calls out by name: the flag must never be drift-able from the user side. Allowing `attributes={"is_active_user": False}` to silently shadow the flag is precisely the two-source drift the design forbids. The unit test `test_properties_caller_cannot_override_is_active_user_flag` and the BP2 e2e check pin the contract. Surfacing to PM for explicit acceptance, but recommend ACCEPT — the spec wording was an oversight, not an intentional constraint.

**Acceptance criteria**
- [x] PASS — `apps/memory/src/tree/entities/users.py` exists with the `User` class. Evidence: `apps/memory/src/tree/entities/users.py:63-148`.
- [x] PASS — `User.identifier` is `Indexed(str, unique=True)`. Evidence: `users.py:75`; index introspection on real Mongo: `index identifier_1: key=[('identifier', 1)] unique=True`.
- [x] PASS — `User.attributes` is `dict[str, Any] = Field(default_factory=dict)`. Evidence: `users.py:79`; `test_users.py::test_minimal_user_constructs_with_defaults` asserts `attributes == {}`.
- [x] PASS — `User` registered in `tree.db.ALL_DOCUMENT_MODELS`. Evidence: `apps/memory/src/tree/db.py:7` (`ALL_DOCUMENT_MODELS = [Document, KnowledgeGraphEntry, User]`); unit `test_users.py::test_user_is_registered_in_db_document_models`.
- [x] PASS — Exported from `tree.entities` and `tree.entities.users`. Evidence: `apps/memory/src/tree/entities/__init__.py:8,17`; manual `python -c "from tree.entities import User; ..."` succeeds; unit `test_user_is_exported_from_entities_package`.
- [x] PASS — `User(identifier="paul@example.com")` constructs; `model_dump()` shows `attributes == {}`, tz-aware timestamps. Evidence: `test_users.py::test_minimal_user_constructs_with_defaults`, `::test_model_dump_round_trips`.
- [x] PASS — Duplicate identifier raises `DuplicateKeyError`. Evidence: integration `test_duplicate_identifier_raises_duplicate_key_error` green; e2e BP1 reproduced (code 11000).
- [x] PASS — Real-Mongo insert creates the expected `KnowledgeGraphEntry`. Evidence: integration `test_insert_creates_self_person_node` green; e2e happy path reproduced manually.
- [x] PASS — Re-firing hook does not duplicate. Evidence: integration `test_rerunning_hook_does_not_duplicate_self_person`; e2e BP3 fired the hook 5x → `count == 1`.
- [x] PASS — `canonical_name` falls back to `identifier` when `attributes` has no `name`. Evidence: unit `test_canonical_name_falls_back_to_identifier_when_no_name`; integration `test_canonical_name_falls_back_to_identifier`; e2e bonus reproduced.
- [x] PASS — No `User.self_person_id` field. Evidence: unit `test_no_self_person_id_field_exists` (asserts `"self_person_id" not in User.model_fields`); manual `from tree.entities import User; print('self_person_id' in User.model_fields)` → `False`.
- [x] PASS — Format / lint / pre-commit clean. Evidence: see commands in Test summary.
- [x] PASS — `make memory-unit-tests` green (modulo the pre-existing config-drift failure flagged on #016).
- [x] PASS — Targeted integration tests green (5/5). Full integration suite is also green (134 passed / 11 skipped).

**Evidence (selected command outputs)**
```
$ make memory-format-check && make memory-lint-check && make pre-commit
202 files already formatted
All checks passed!
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ uv --directory apps/memory run pytest tests/unit/entities -W error
================================ 53 passed in 0.18s ================================

$ uv --directory apps/memory run pytest tests/integration/entities -W error -v
... 5 passed in 0.22s

$ make memory-integration-tests
================== 134 passed, 11 skipped in 212.89s (0:03:32) =====================

$ uv --directory apps/memory run python /tmp/qa_017_e2e.py
[happy] user_id=6a086d859fae0650396dd917 node_id=6a086d859fae0650396dd917:person:self canonical_name=Paul
[bp1] PASS: DuplicateKeyError raised as expected (11000)
[bp2] PASS: is_active_user=True even when caller passed False; extra=1
[bp3] PASS: count stayed at 1 after 5 hook invocations
[bonus] PASS: canonical_name fallback to identifier (got 'fallback@e2e.test')
[bonus] PASS: two users -> two distinct ids
=== ALL ADVERSARIAL CHECKS PASSED ===

$ # User index inspection against real Mongo (qa_017_index_check DB, dropped after)
Collection name: users
  index _id_: key=[('_id', 1)] unique=None
  index identifier_1: key=[('identifier', 1)] unique=True
```

**Other issues found**
- None blocking. Note for PM: the `properties` merge-order deviation (flag-wins) is technically out of strict spec compliance, but tightens decision #1's "single source of truth" guarantee. Recommend explicit ACCEPT during PM review.
- Note for #018: the `# TODO(#018)` markers at `users.py:106-109` (stamp `user_id` on the KG entry) and around `_build_self_person_id` (swap for the canonical `build_node_id`) are correctly placed.

**VERDICT: PASS**
