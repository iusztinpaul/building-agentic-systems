"""Programmatic sweep: every Pydantic *Properties* field has a description.

Per `plan.md:380-401`, every attribute on every Pydantic model
registered against the ontology MUST carry ``Field(description="…")``.
The LLM uses these descriptions as its only context for what each
property means — a missing description silently degrades extraction
quality.

This test walks every model registered in:

* ``NODE_REGISTRY[*].properties_schema``
* ``EDGE_REGISTRY[*].properties_schema`` (when set)
* ``RELATION_SEMANTICS[*].properties_schema`` (when set)
* ``SUBTYPE_EXTRAS[*]``

and asserts every field has a non-empty ``description``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from tree.entities.ontology import (
    EDGE_REGISTRY,
    NODE_REGISTRY,
    RELATION_SEMANTICS,
    SUBTYPE_EXTRAS,
)


def _collect_property_models() -> list[tuple[str, type[BaseModel]]]:
    """Return ``[(label, model_cls), ...]`` for every registered model.

    ``label`` is human-readable and surfaced in the test ID, so a
    failing run names the offending model directly.
    """

    seen: dict[int, tuple[str, type[BaseModel]]] = {}

    def _add(label: str, model: type[BaseModel] | None) -> None:
        if model is None:
            return
        key = id(model)
        if key in seen:
            return
        seen[key] = (label, model)

    for name, spec in NODE_REGISTRY.items():
        _add(f"NODE_REGISTRY[{name!r}].properties_schema", spec.properties_schema)

    for name, spec in EDGE_REGISTRY.items():
        _add(f"EDGE_REGISTRY[{name!r}].properties_schema", spec.properties_schema)

    for name, spec in RELATION_SEMANTICS.items():
        _add(f"RELATION_SEMANTICS[{name!r}].properties_schema", spec.properties_schema)

    for (parent, subtype), model in SUBTYPE_EXTRAS.items():
        _add(f"SUBTYPE_EXTRAS[({parent!r}, {subtype!r})]", model)

    return list(seen.values())


_MODELS = _collect_property_models()


@pytest.mark.parametrize(
    "label,model",
    _MODELS,
    ids=[label for label, _ in _MODELS],
)
def test_every_property_model_field_has_description(
    label: str, model: type[BaseModel]
) -> None:
    """Every field on every registered properties model carries a description.

    A regression here means a property was added without
    ``Field(description="…")`` — the LLM prompt would surface that
    field with no context, silently degrading extraction quality.
    """

    fields = model.model_fields
    if not fields:
        # A schema with zero fields is degenerate but not buggy — skip
        # rather than fail (today there are no zero-field property
        # models in the registry, but be forward-friendly).
        pytest.skip(f"{label} has no fields")
    schema = model.model_json_schema()
    properties: dict[str, Any] = schema.get("properties", {})
    for field_name, _info in fields.items():
        # Pydantic surfaces the description via the JSON schema; if a
        # field is declared with ``Field(description="…")`` the JSON
        # schema carries the value, otherwise the key is absent.
        prop = properties.get(field_name, {})
        description = prop.get("description")
        assert description, (
            f"{label}.{field_name} is missing Field(description=...); "
            "every property model field must carry a non-empty description."
        )
        assert isinstance(description, str)
        assert description.strip(), (
            f"{label}.{field_name} has empty description after stripping"
        )
