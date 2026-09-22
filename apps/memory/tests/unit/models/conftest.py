"""Fixtures shared by the Modal test modules in this package (PR #44, Nit 3).

The doubles themselves live in :mod:`tests.unit.models.modal_fixtures`, which
``tests/unit/scripts/test_modal_model_script.py`` imports directly — a conftest
serves its own directory tree only, and that module sits in a sibling one. This
file is the re-export that turns the ones written as fixtures into fixtures
every module HERE can request by name.

Nothing is redefined: the suite-wide rails (``_modal_dry_run``,
``_no_live_modal_sdk``), ``modal_seam`` and ``run`` stay in
``tests/unit/conftest.py``, where the whole unit suite sees them.
"""

from tests.unit.models.modal_fixtures import hub

__all__ = ["hub"]
