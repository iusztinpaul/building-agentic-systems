"""Static guards on the two FALLBACK Modal deploy scripts (ADR-009 §2/§3/§9).

``deploy/modal_vllm_embedding.py`` and ``deploy/modal_sglang_embedding.py`` are
two boring copies of Modal's generated ``serve.py``, parameterised by the
**Embedding catalog**. Nothing here imports them: Modal re-imports a deploy
script INSIDE its container (where ``tree`` is not installed and the engine
is), and a unit suite must never reach Modal. Every assertion is made on the
SOURCE — ``ast.parse`` for structure, plain text for the handful of literals
the ``@app.server`` decorator must carry.

What these tests defend, in ADR-009's words: the scripts stay GLUE (no business
logic, no ``print``), the container re-import cannot break on a ``tree`` import,
**Proxy tokens** stay the only auth (no engine key, no named Modal Secret), and
the optional Hugging Face token travels as an ephemeral Secret — never baked
into a cached, inspectable image layer, never logged as a value.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tree.models.modal_catalog import (
    DEPLOY_SPEC_ENV,
    EMBEDDING_SERVER_NAME,
    MODAL_ROUTING_REGION,
    get_catalog_entry,
    modal_cli_command,
)

_APP_ROOT = pathlib.Path(__file__).resolve().parents[3]

_SCRIPTS: dict[str, pathlib.Path] = {
    "vllm": _APP_ROOT / "deploy" / "modal_vllm_embedding.py",
    "sglang": _APP_ROOT / "deploy" / "modal_sglang_embedding.py",
}

_ENGINES = pytest.mark.parametrize("engine", sorted(_SCRIPTS))

# The one log line that proves the Secret arrived, and the only other place the
# variable's name may appear (as the key it looks up).
_TOKEN_LOG_MESSAGE = "HF_TOKEN set in container: %s"
_TOKEN_VAR = "HF_TOKEN"


def _source(engine: str) -> str:
    return _SCRIPTS[engine].read_text(encoding="utf-8")


def _module(engine: str) -> ast.Module:
    return ast.parse(_source(engine))


def _dotted(node: ast.expr) -> str:
    """``modal.Secret.from_dict`` for the ``func`` of a call, as written."""

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """Every call to ``name`` (dotted, as written) inside ``node``."""

    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _dotted(child.func) == name
    ]


def _is_local_branch(module: ast.Module) -> ast.If:
    """The module-level ``if modal.is_local():`` statement.

    It is the ONLY place a fallback script may touch ``tree``: the same file is
    re-imported in a container that has no ``tree`` installed.
    """

    for statement in module.body:
        if isinstance(statement, ast.If) and _calls(statement.test, "modal.is_local"):
            return statement
    raise AssertionError("the script has no module-level `if modal.is_local():`")


def _nodes_in(statements: list[ast.stmt]) -> set[int]:
    """Identity of every node under ``statements`` — for containment checks."""

    return {id(node) for stmt in statements for node in ast.walk(stmt)}


def _server_decorator(module: ast.Module) -> ast.Call:
    """The ``@app.server(...)`` call decorating the server class."""

    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and _dotted(decorator.func) == "app.server"
                ):
                    return decorator
    raise AssertionError("no class is decorated with `@app.server(...)`")


def _image_env_dict(module: ast.Module) -> ast.Dict:
    """The dict literal baked into the image with ``.env({...})``."""

    for node in ast.walk(module):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "env"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Dict)
        ):
            return node.args[0]
    raise AssertionError("the image has no `.env({...})` layer")


@_ENGINES
class TestGlueContract:
    """The scripts are entry points: glue over ``tree.models.modal_catalog``."""

    def test_nothing_is_printed(self, engine: str) -> None:
        """Logging is the native logger everywhere (AGENTS.md)."""

        assert not _calls(_module(engine), "print")

    def test_the_logger_is_configured_locally(self, engine: str) -> None:
        """``init_logger()`` runs on the deploying machine only — the container
        re-import has no ``tree`` to configure it from."""

        module = _module(engine)
        local_branch = _nodes_in(_is_local_branch(module).body)
        init_calls = _calls(module, "init_logger")

        assert len(init_calls) == 1
        assert id(init_calls[0]) in local_branch

    def test_tree_is_imported_only_under_is_local(self, engine: str) -> None:
        """The container-side re-import must not touch ``tree`` (ADR-009 §3)."""

        module = _module(engine)
        local_branch = _nodes_in(_is_local_branch(module).body)

        tree_imports = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "tree"
        ] + [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Import)
            and any(alias.name.split(".")[0] == "tree" for alias in node.names)
        ]

        assert tree_imports, "the script resolves nothing from the catalog"
        for node in tree_imports:
            assert id(node) in local_branch

    def test_the_server_class_carries_the_catalog_name(self, engine: str) -> None:
        """``Server`` in app ``ep-<endpoint_name>`` (e.g.
        ``ep-tree-voyage-4-nano``): the SHAPE Modal's own generated
        ``serve.py`` uses, so ONE lookup resolves every path."""

        module = _module(engine)
        decorated = [
            node.name
            for node in ast.walk(module)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(decorator, ast.Call)
                and _dotted(decorator.func) == "app.server"
                for decorator in node.decorator_list
            )
        ]

        assert decorated == [EMBEDDING_SERVER_NAME]

    def test_the_decorator_pins_the_serving_contract(self, engine: str) -> None:
        """The knobs an operator must not silently lose: proxy auth, the EU
        routing region, scale-to-zero and the concurrency target."""

        source = _source(engine)

        for literal in (
            "unauthenticated=False",
            f'routing_region="{MODAL_ROUTING_REGION}"',
            "scaledown_window=5 * MINUTES",
            "target_concurrency=16",
            "min_containers=0",
            "exit_grace_period=25",
        ):
            assert literal in source

    def test_the_retired_auth_never_comes_back(self, engine: str) -> None:
        """ADR-009 §4 retired the engine-level key and every named Modal
        Secret; §2 keeps the smoke test in ONE shared place."""

        source = _source(engine)

        for forbidden in (
            "local_entrypoint",
            "MODAL_EMBEDDING_API_KEY",
            "--api-key",
            "Secret.from_name",
        ):
            assert forbidden not in source

    def test_the_script_resolves_its_own_engine(self, engine: str) -> None:
        """The engine is the SCRIPT's — a catalog entry names none. The model
        crosses from the driver as ``MODAL_MODEL``, the one env var both
        kinds' scripts read."""

        source = _source(engine)

        assert f'build_deploy_spec(os.environ["MODAL_MODEL"], "{engine}")' in source

    def test_the_endpoint_is_constructed_the_way_its_engine_takes_it(
        self, engine: str
    ) -> None:
        """``VLLMEndpoint(model=…)`` vs ``SGLangEndpoint(model_path=…)`` —
        verified in the ``autoinference-utils`` 0.2.6 source."""

        constructor, model_keyword = {
            "vllm": ("VLLMEndpoint", "model"),
            "sglang": ("SGLangEndpoint", "model_path"),
        }[engine]

        calls = _calls(_module(engine), constructor)
        assert len(calls) == 1
        assert {keyword.arg for keyword in calls[0].keywords} == {
            model_keyword,
            "worker_port",
            "extra_server_args",
            "health_timeout",
            "health_poll_interval",
        }
        assert f"{constructor}({model_keyword}=" in "".join(_source(engine).split())

    def test_the_token_boolean_is_the_first_thing_enter_does(self, engine: str) -> None:
        """Logged BEFORE the engine import: a broken image would otherwise
        raise an ``ImportError`` with no token line at all, and #141 could not
        tell "the Secret never arrived" from "the image is broken"."""

        enter = [
            node
            for node in ast.walk(_module(engine))
            if isinstance(node, ast.FunctionDef)
            and _calls(
                ast.Module(body=node.decorator_list, type_ignores=[]), "modal.enter"
            )
        ]
        assert len(enter) == 1

        body = [
            statement
            for statement in enter[0].body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
            )
        ]
        first = body[0]

        assert isinstance(first, ast.Expr)
        assert isinstance(first.value, ast.Call)
        assert _dotted(first.value.func) == "logger.info"
        assert first.value.args[0].value == _TOKEN_LOG_MESSAGE

    def test_every_def_is_typed(self, engine: str) -> None:
        """Every function/method has a return annotation, ``-> None`` included
        (AGENTS.md)."""

        for node in ast.walk(_module(engine)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert node.returns is not None, node.name

    def test_the_spec_env_var_matches_the_catalog_constant(self, engine: str) -> None:
        """The scripts spell the name out (no ``tree`` import in the container),
        so a rename on either side must fail HERE, not in a cold container."""

        assert f'DEPLOY_SPEC_ENV = "{DEPLOY_SPEC_ENV}"' in _source(engine)
        assert f'os.environ["{DEPLOY_SPEC_ENV}"]' in _source(engine)


@_ENGINES
def test_only_the_hf_token_secret(engine: str) -> None:
    """The ONE Secret is the Hugging Face token's, and it is never in the image.

    ADR-009 §9: built from the local settings under ``modal.is_local()``, an
    EMPTY dict on the in-container re-import (same list length on both sides),
    attached as ``secrets=[HF_SECRET]``, and absent from the image env, whose
    layers are cached and inspectable. The value is never logged — the only
    thing the container prints is the boolean.
    """

    module = _module(engine)
    source = _source(engine)
    branch = _is_local_branch(module)
    local_nodes = _nodes_in(branch.body)
    container_nodes = _nodes_in(branch.orelse)

    from_dict = _calls(module, "modal.Secret.from_dict")
    assert len(from_dict) == 2

    local_call = next(call for call in from_dict if id(call) in local_nodes)
    container_call = next(call for call in from_dict if id(call) in container_nodes)

    assert len(local_call.args) == 1
    assert isinstance(local_call.args[0], ast.Call)
    assert _dotted(local_call.args[0].func) == "hf_token_env"

    assert len(container_call.args) == 1
    assert isinstance(container_call.args[0], ast.Dict)
    assert container_call.args[0].keys == []

    secrets = [
        keyword.value
        for keyword in _server_decorator(module).keywords
        if keyword.arg == "secrets"
    ]
    assert len(secrets) == 1
    assert isinstance(secrets[0], ast.List)
    assert [element.id for element in secrets[0].elts] == ["HF_SECRET"]

    image_keys = [
        key.value
        for key in _image_env_dict(module).keys
        if isinstance(key, ast.Constant)
    ]
    assert _TOKEN_VAR not in image_keys

    # The variable's name appears exactly twice: in the boolean log line, and
    # as the key that line looks up. Never as a value, never in the image.
    token_strings = [
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _TOKEN_VAR in node.value
    ]
    assert sorted(token_strings) == sorted([_TOKEN_LOG_MESSAGE, _TOKEN_VAR])
    assert source.count(_TOKEN_VAR) == 2

    lookups = [
        call
        for call in _calls(module, "bool")
        if len(call.args) == 1
        and isinstance(call.args[0], ast.Call)
        and _dotted(call.args[0].func) == "os.environ.get"
        and [arg.value for arg in call.args[0].args] == [_TOKEN_VAR]
    ]
    assert len(lookups) == 1


def test_script_paths_match_cli_command() -> None:
    """The driver deploys a FILE: what ``modal_cli_command`` names must exist.

    A path typo would surface as a ``modal`` error after the operator has
    already been told the deploy started (#139 exits 2 on a missing file).
    """

    for model, serving in (
        ("voyageai/voyage-4-nano", "vllm"),
        ("Qwen/Qwen3-Embedding-0.6B", "sglang"),
    ):
        script = modal_cli_command("deploy", get_catalog_entry(model), serving)[-1]

        assert (_APP_ROOT / script).is_file(), script
