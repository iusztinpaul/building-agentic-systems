"""The one Modal double this package shares with the models tests (PR #44, Nit 3).

``test_modal_model_script.py`` drives the same auto-router as
``tests/unit/models/test_modal_router.py``, so it needs the same faked Hugging
Face transport. A conftest serves its own directory tree only — the models one
is invisible here — so the fixture is re-exported from the module both sides
import, :mod:`tests.unit.models.modal_fixtures`.

Re-exported HERE rather than imported into the test module itself: several
tests there take ``hub`` as an argument, which shadows a module-level import
and makes ruff's F811 fire on every one of them.
"""

from tests.unit.models.modal_fixtures import hub

__all__ = ["hub"]
