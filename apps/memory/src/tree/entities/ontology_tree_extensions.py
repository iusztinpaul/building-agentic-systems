"""Tree-specific ontology extensions.

This module is the future home of Tree's downstream
``register_node_subtype(...)`` / ``register_edge_type(...)`` /
``register_node_type(...)`` calls that layer Tree's domain vocabulary
on top of the POLE+O canonical types defined in
:mod:`tree.entities.ontology`.

Phase-3 part 1 (task #027) only creates the import path; importing
this module is a no-op on :data:`tree.entities.ontology.NODE_REGISTRY`
and :data:`tree.entities.ontology.EDGE_REGISTRY`. The actual
``register_node_subtype("object", "task", ...)`` etc. calls land in
task #028.
"""
