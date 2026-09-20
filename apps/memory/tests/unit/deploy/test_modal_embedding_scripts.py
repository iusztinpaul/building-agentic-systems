"""Static guards on the two App deploy scripts (ADR-009 §2/§3/§9).

``deploy/modal_vllm_embedding.py`` and ``deploy/modal_sglang_embedding.py`` are
two boring copies of Modal's generated ``serve.py``, parameterised by the
**Modal catalog**. Nothing here imports them: Modal re-imports a deploy
script INSIDE its container (where ``tree`` is not installed and the engine
is), and a unit suite must never reach Modal. Every assertion is made on the
SOURCE — ``ast.parse`` for structure, plain text for the handful of literals
the ``@app.server`` decorator must carry.

What these tests defend, in ADR-009's words: the scripts stay GLUE (no business
logic, no ``print``), the container re-import cannot break on a ``tree`` import,
**Proxy tokens** stay the only auth (no engine key, no named Modal Secret), and
the optional Hugging Face token travels as an ephemeral Secret — never baked
into a cached, inspectable image layer, never logged as a value.

Two guards are written AGAINST A MUTANT, not only against the shipped file:
mutation testing during #142's QA showed that ``unauthenticated=not True``
survived a literal-substring check (the docstring carries the literal too) and
that ``sys.stdout.write(SPEC)`` survived a ``print(``-only check. Both now read
the AST, and ``TestTheGuardsCatchTheirMutants`` proves each one on a mutated
COPY in ``tmp_path`` — the shipped scripts are never edited and never executed.
"""

from __future__ import annotations

import ast
import pathlib
import re

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

# The names that put bytes on the container's stdout/stderr directly, instead
# of through the logger: `print` (bare or via `builtins`), `pprint`, and any
# `...stdout.write*` / `...stderr.write*` attribute chain.
_PRINTERS = frozenset({"print", "pprint"})
_CONSOLE_STREAMS = frozenset({"stdout", "stderr"})

# The two anchors the mutants are grafted onto — one per guard. Each must
# occur EXACTLY once in every script (`_mutate` asserts it), so a mutation
# cannot silently land in a second place or nowhere at all.
_AUTH_KEYWORD = "unauthenticated=False,"
_STOP_CALL = "        self.endpoint.stop()"

# The words the vLLM script may no longer contain (ADR-009 §2): it is not a
# fallback on a ladder, and it serves no LLM. `(?<!v)llm` keeps `vLLM` /
# `VLLMEndpoint`, which are what the file IS.
_RETIRED_VOCABULARY = re.compile(
    r"fallback|eject|ladder|serving path|SERVING=|(?<!v)llm|chat", re.IGNORECASE
)

_CONFIGS = _APP_ROOT / "configs" / "default.yaml"


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

    It is the ONLY place an App script may touch ``tree``: the same file is
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


def _keyword(call: ast.Call, name: str) -> ast.expr:
    """The value of ``name=`` on ``call``, as written."""

    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"the call passes no `{name}=`")


def _one_call(module: ast.Module, name: str) -> ast.Call:
    """The ONE call to ``name`` — a second one would make the assertions
    ambiguous about which call they pinned."""

    calls = _calls(module, name)
    assert len(calls) == 1, f"expected one `{name}(...)`, found {len(calls)}"
    return calls[0]


def _module_constant(module: ast.Module, name: str) -> object:
    """The value of a module-level ``NAME = <literal>`` assignment."""

    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            assert isinstance(statement.value, ast.Constant), name
            return statement.value.value
    raise AssertionError(f"the script has no module-level `{name} = ...`")


def _mutate(
    engine: str, tmp_path: pathlib.Path, anchor: str, replacement: str
) -> ast.Module:
    """A COPY of the script in ``tmp_path``, with ``anchor`` replaced.

    The shipped file is only ever READ: a mutation test that edited it in
    place would leave a public server behind on any failure.
    """

    source = _source(engine)
    assert source.count(anchor) == 1, f"{anchor!r} is not a unique anchor in {engine}"

    mutant = tmp_path / _SCRIPTS[engine].name
    mutant.write_text(source.replace(anchor, replacement), encoding="utf-8")
    return ast.parse(mutant.read_text(encoding="utf-8"))


def _console_writes(module: ast.Module) -> list[str]:
    """Every call in ``module`` that writes to the console itself.

    Dotted names, as written, so the failure message can NAME the call: a
    ``print``/``pprint`` under any prefix (``builtins.print``,
    ``pprint.pprint``) and any attribute chain whose last two parts are
    ``stdout``/``stderr`` + ``write``/``writelines``.
    """

    offenders: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        parts = dotted.split(".")
        if parts[-1] in _PRINTERS:
            offenders.append(dotted)
        elif (
            len(parts) > 1
            and parts[-1].startswith("write")
            and parts[-2] in _CONSOLE_STREAMS
        ):
            offenders.append(dotted)
    return offenders


def _assert_proxy_auth_is_pinned(module: ast.Module) -> None:
    """``unauthenticated`` is the LITERAL ``False`` — never an expression.

    The keyword's AST node must be ``ast.Constant(value=False)``: ``not True``
    evaluates to the same thing but a one-character edit (``not False``) makes
    the server PUBLIC, and a name or a call moves the decision out of the file
    this test reads.
    """

    keywords = [
        keyword
        for keyword in _server_decorator(module).keywords
        if keyword.arg == "unauthenticated"
    ]
    assert len(keywords) == 1, "`@app.server` must pass `unauthenticated` exactly once"

    value = keywords[0].value
    assert isinstance(value, ast.Constant) and value.value is False, (
        "`unauthenticated` must be the literal `False` (Proxy tokens, "
        f"ADR-009 §4), not `{ast.unparse(value)}`"
    )


@_ENGINES
class TestGlueContract:
    """The scripts are entry points: glue over ``tree.models.modal_catalog``."""

    def test_nothing_is_printed(self, engine: str) -> None:
        """Logging is the native logger everywhere (AGENTS.md).

        ``print`` is not the only way to reach ``modal app logs``: a
        ``sys.stdout.write(str(SPEC))`` left behind after a debugging session
        puts the whole deploy spec there just as well, which is why the guard
        walks the calls instead of grepping for ``print(``.
        """

        writes = _console_writes(_module(engine))

        assert not writes, (
            "the script writes to the console instead of the logger: "
            f"{', '.join(writes)}"
        )

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
        routing region, scale-to-zero and the concurrency target.

        ``unauthenticated`` is checked on the AST, not as a substring: the
        docstring quotes ``unauthenticated=False`` too, so a decorator mutated
        to ``not True`` used to pass on the docstring's copy alone.
        """

        source = _source(engine)

        for literal in (
            f'routing_region="{MODAL_ROUTING_REGION}"',
            "scaledown_window=5 * MINUTES",
            "target_concurrency=16",
            "min_containers=0",
            "exit_grace_period=25",
        ):
            assert literal in source

        _assert_proxy_auth_is_pinned(_module(engine))

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


@_ENGINES
class TestTheGuardsCatchTheirMutants:
    """The two guards, proven on a MUTATED COPY in ``tmp_path``.

    Both blind spots were found by mutation during #142's QA: a guard that
    only greps for ``unauthenticated=False`` / ``print(`` passes a script that
    is public, or that dumps the deploy spec into ``modal app logs``. The
    shipped scripts are read-only here — the mutant is a copy, and it is never
    imported or executed.
    """

    def test_the_shipped_script_passes_both_guards(self, engine: str) -> None:
        """The control row: without a mutation, both guards are silent."""

        module = _module(engine)

        _assert_proxy_auth_is_pinned(module)
        assert not _console_writes(module)

    @pytest.mark.parametrize(
        "mutation",
        [
            # Evaluates to False, so the server stays private — but one
            # deleted character (`not False`) makes it PUBLIC.
            "unauthenticated=not True,",
            "unauthenticated=not False,",
            # A name moves the decision out of this file entirely.
            "unauthenticated=AUTH,",
        ],
    )
    def test_a_non_literal_unauthenticated_fails(
        self, engine: str, mutation: str, tmp_path: pathlib.Path
    ) -> None:
        mutant = _mutate(engine, tmp_path, _AUTH_KEYWORD, mutation)

        with pytest.raises(AssertionError, match="literal `False`"):
            _assert_proxy_auth_is_pinned(mutant)

    @pytest.mark.parametrize(
        "statement,call",
        [
            # The case the OLD `print(`-only guard caught: the widened guard
            # must still be a superset of it.
            ("print(SPEC)", "print"),
            ('sys.stdout.write("x")', "sys.stdout.write"),
            ('sys.stderr.write("x")', "sys.stderr.write"),
            ("sys.stdout.writelines([SPEC])", "sys.stdout.writelines"),
            ("pprint(SPEC)", "pprint"),
            ("builtins.print(SPEC)", "builtins.print"),
        ],
    )
    def test_a_console_write_fails_and_is_named(
        self, engine: str, statement: str, call: str, tmp_path: pathlib.Path
    ) -> None:
        mutant = _mutate(
            engine, tmp_path, _STOP_CALL, f"        {statement}\n{_STOP_CALL}"
        )

        assert _console_writes(mutant) == [call]


def test_the_vllm_script_follows_modals_embedding_template() -> None:
    """Value by value, the ``serve.py`` Modal generates for ITS embedding
    endpoints (``Qwen/Qwen3-Embedding-0.6B`` and ``-8B``, read 2026-09-20).

    One behaviour — "this file is Modal's embedding recipe, parameterised by
    the catalog" — so the template's values are asserted together: the CUDA
    base and its four wheels, the engine object, the embedding probe, and the
    ``@app.server`` knobs Modal's own recipe sets.
    """

    module = _module("vllm")

    # Image: Modal's CUDA base, its own entrypoint dropped (the vLLM process
    # is the entrypoint), the engine pinned from the catalog.
    registry = _one_call(module, "modal.Image.from_registry")
    assert [argument.value for argument in registry.args] == [
        "nvidia/cuda:13.0.2-devel-ubuntu22.04"
    ]
    assert _keyword(registry, "add_python").value == "3.12"

    entrypoint = _one_call(module, "entrypoint")
    assert len(entrypoint.args) == 1
    assert isinstance(entrypoint.args[0], ast.List)
    assert entrypoint.args[0].elts == []

    wheels = _one_call(module, "uv_pip_install")
    assert isinstance(wheels.args[0], ast.JoinedStr), "the vLLM pin is not the YAML's"
    assert "engine_version" in ast.unparse(wheels.args[0])
    assert {
        argument.value for argument in wheels.args if isinstance(argument, ast.Constant)
    } >= {"httpx", "huggingface-hub"}
    assert "HF_XET_HIGH_PERFORMANCE" in [
        key.value
        for key in _image_env_dict(module).keys
        if isinstance(key, ast.Constant)
    ]

    # The engine and the ONE request that proves it embeds before the
    # container serves traffic.
    endpoint = _one_call(module, "VLLMEndpoint")
    assert _keyword(endpoint, "worker_port").id == "PORT"
    assert _keyword(endpoint, "health_poll_interval").value == 5.0

    probe = _one_call(module, "validate_embeddings_endpoint")
    assert _keyword(probe, "port").id == "PORT"
    assert _keyword(probe, "request_timeout").value == 60.0
    payload = _keyword(probe, "payload")
    assert isinstance(payload, ast.Dict)
    payload_items = {
        key.value: value
        for key, value in zip(payload.keys, payload.values, strict=True)
        if isinstance(key, ast.Constant)
    }
    assert payload_items["encoding_format"].value == "float"
    assert payload_items["input"].id == "PROBES"
    assert _module_constant(module, "PORT") == 8000

    # The decorator's own knobs, Modal's values (its 0.6B recipe's
    # TARGET_INPUTS is 16; scale-to-zero after 5 minutes).
    decorator = _server_decorator(module)
    assert _keyword(decorator, "port").id == "PORT"
    assert _keyword(decorator, "min_containers").value == 0
    assert _keyword(decorator, "exit_grace_period").value == 25
    assert _keyword(decorator, "routing_region").value == MODAL_ROUTING_REGION
    assert ast.unparse(_keyword(decorator, "scaledown_window")) == "5 * MINUTES"


def test_the_vllm_script_is_about_embedding_models_only() -> None:
    """ADR-009 §2: the vLLM App serves embedding models from Hugging Face,
    nothing else — no ladder, no fallback, no LLM, no chat warm-up.

    ``llm`` is matched only where it is NOT preceded by a ``v``: the file is
    the vLLM script, so ``vLLM`` and ``VLLMEndpoint`` are the subject, not a
    leftover. (The task's acceptance criterion greps a bare ``LLM``, which
    every mention of vLLM would hit — and the pre-existing
    ``VLLMEndpoint(model=`` guard requires those mentions.)
    """

    hits = sorted(
        {match.group(0) for match in _RETIRED_VOCABULARY.finditer(_source("vllm"))}
    )

    assert not hits, f"the vLLM script still talks about: {', '.join(hits)}"


def test_no_seed_asks_the_server_for_matryoshka() -> None:
    """ADR-009 §3: the client truncates client-side on EVERY path, so no entry
    flags ``is_matryoshka`` server-side — Modal's own 8B recipe does and its
    0.6B recipe does not, which is exactly why we never rely on it.

    The script's docstring carries the reason; the catalog carries no override.
    """

    assert "is_matryoshka" in _source("vllm")
    assert "is_matryoshka" not in _CONFIGS.read_text(encoding="utf-8")


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
