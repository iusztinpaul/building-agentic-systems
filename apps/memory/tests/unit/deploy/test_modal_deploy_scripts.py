"""Static guards on the two App deploy scripts (ADR-009 §2/§3/§9).

``deploy/modal_vllm_embedding.py`` (embeddings) and
``deploy/modal_sglang_llm.py`` (LLMs) are two boring copies of the ``serve.py``
Modal generates for ITS endpoints of that kind, parameterised by the **Modal
catalog**. Nothing here imports them: Modal re-imports a deploy script INSIDE
its container (where ``tree`` is not installed and the engine is), and a unit
suite must never reach Modal. Every assertion is made on the SOURCE —
``ast.parse`` for structure, plain text for the handful of literals the
``@app.server`` decorator must carry.

What these tests defend, in ADR-009's words: the scripts stay GLUE (no business
logic, no ``print``), the container re-import cannot break on a ``tree`` import,
**Proxy tokens** stay the only auth (no engine key, no named Modal Secret), and
the optional Hugging Face token travels as an ephemeral Secret — never baked
into a cached, inspectable image layer, never logged as a value.

Seven guards are written AGAINST A MUTANT, not only against the shipped file,
and ``TestTheGuardsCatchTheirMutants`` proves each one on a mutated COPY in
``tmp_path`` — the shipped scripts are never edited and never executed. Two
came from #142's QA (``unauthenticated=not True`` survived a literal-substring
check; ``sys.stdout.write(SPEC)`` survived a ``print(``-only check); four more
from #146's, where these mutants all passed every guard of the day:

* ``logger.info("%s", os.environ)`` — the serious one: it puts the real
  ``HF_TOKEN`` into ``modal app logs``;
* ``os.write(1, b"…")`` and an ALIASED ``print`` (``p = print; p(SPEC)``) —
  console writes the dotted-name check did not name;
* a SECOND ``.env({"HF_TOKEN": …})`` on another Image object — the helper only
  inspected the first ``.env({...})`` it found.

#147's QA added the last two, both leaks of the same token past every guard of
ITS day: ``subprocess.run(["env"])`` / ``os.system("env")``, whose CHILD
inherits the environment and the container's stdout (``_process_spawns``), and
a second logger spelled anything but ``logger.`` — ``logging.info(...)``,
``logging.getLogger().info(...)``, a ``log = logging.getLogger(…)`` alias —
which walked straight past the message allow-list (``_log_offenders``).

#153 added the seventh, and the FIRST one a live deploy — not a mutation —
found: the spec's env value must be the base64 of its JSON, baked as the one
name both sides of ``modal.is_local()`` share. Its mutant is the transport as
it shipped, ``DEPLOY_SPEC_ENV: json.dumps(SPEC)``, which the image build
corrupted for any value holding a quote. That guard's token-safety half moved
to the spec MODEL (``test_deploy_spec_has_no_credential_field`` in
``tests/unit/models/test_modal_catalog.py``): an opaque blob defeats reading
the ``.env({...})`` literal for a credential, so what is pinned instead is
that no field of a spec can hold one and that a sentinel token never appears
in the DECODED value.

#152's QA rewrote both of those in ONE token and got past them again:
``__import__("os").system("env")`` (the dotted name of a chain rooted at a CALL
is not ``os.something``) and ``getattr(logger, "info")("…")`` (the ``func`` of
such a call is a Call, not an Attribute). Both helpers now also ban what HIDES
a leak — dynamic imports, ``eval``/``exec``/``compile``, and calling the result
of a call — and each carries a KNOWN BOUNDS block naming what a static walk
still cannot see.
"""

from __future__ import annotations

import ast
import base64
import json
import pathlib
import re
from typing import Any, Callable

import pytest

from tree.models.modal_catalog import (
    EMBEDDING_DEPLOY_SPEC_ENV,
    MODAL_SERVER_NAME,
    LLM_DEPLOY_SPEC_ENV,
    MODAL_ROUTING_REGION,
    DeploySpec,
    app_script,
    build_deploy_spec,
    build_llm_deploy_spec,
    encode_deploy_spec,
    get_catalog_entry,
    modal_cli_command,
)

_APP_ROOT = pathlib.Path(__file__).resolve().parents[3]

_SCRIPTS: dict[str, pathlib.Path] = {
    "vllm": _APP_ROOT / "deploy" / "modal_vllm_embedding.py",
    "sglang": _APP_ROOT / "deploy" / "modal_sglang_llm.py",
}

_ENGINES = pytest.mark.parametrize("engine", sorted(_SCRIPTS))

# The env var each script's spec crosses into the container in — one per KIND,
# spelled out in the script because `tree` is not importable there.
_SPEC_ENV: dict[str, str] = {
    "vllm": EMBEDDING_DEPLOY_SPEC_ENV,
    "sglang": LLM_DEPLOY_SPEC_ENV,
}

# The catalog builder each script resolves ITS OWN kind of entry with.
_SPEC_BUILDER: dict[str, str] = {
    "vllm": "build_deploy_spec",
    "sglang": "build_llm_deploy_spec",
}

# The seed each script's kind is proven on, as the (builder, model) pair the
# OPERATOR's side would resolve — so the transport tests below cross the same
# bytes a real deploy would.
_SEED: dict[str, tuple[Callable[[str], DeploySpec], str]] = {
    "vllm": (build_deploy_spec, "voyageai/voyage-4-nano"),
    "sglang": (build_llm_deploy_spec, "LiquidAI/LFM2.5-350M"),
}

# The ONE name the spec's env value is bound to on BOTH sides of
# `modal.is_local()` (ADR-009 §3): the encoder's output locally, the env var's
# own raw string in the container. One name keeps the `.env({...})` literal —
# and so the image definition — byte-identical where Modal re-imports the file.
_SPEC_VALUE_NAME = "SPEC_ENV_VALUE"

# The module-level constant each script raises with when the value does not
# decode, and the statements of the container branch that are NOT the decode
# (they configure logging or build the Secret, neither of which a unit test
# may run: `basicConfig(force=True)` would reconfigure pytest's own logging).
_SPEC_ERROR_CONSTANT = "SPEC_DECODE_ERROR"
_NOT_THE_DECODE = ("logging.", "HF_SECRET", _SPEC_VALUE_NAME)

# The engine object and the exact keyword set each script constructs it with
# (verified in the `autoinference-utils` 0.2.6 source).
_ENDPOINT_CALLS: dict[str, tuple[str, str, set[str]]] = {
    "vllm": (
        "VLLMEndpoint",
        "model",
        {
            "model",
            "worker_port",
            "extra_server_args",
            "health_timeout",
            "health_poll_interval",
        },
    ),
    "sglang": (
        "SGLangEndpoint",
        "model_path",
        {
            "model_path",
            "worker_port",
            "tp",
            "extra_server_args",
            "health_timeout",
            "health_poll_interval",
        },
    ),
}

# The one log line that proves the Secret arrived, and the only other place the
# variable's name may appear (as the key that line looks up).
_TOKEN_LOG_MESSAGE = "HF_TOKEN set in container: %s"
_TOKEN_VAR = "HF_TOKEN"

# The ONE expression allowed to read the token: the boolean, never the value.
# Spelled as `ast.unparse` renders it (single quotes).
_TOKEN_BOOLEAN = "bool(os.environ.get('HF_TOKEN'))"

# EVERY message either script may log, per engine. An allow-list, not a shape
# check: `modal app logs` is the one place a container's bytes end up, so what
# may appear there is enumerated here and nowhere else.
_LOG_MESSAGES: dict[str, set[str]] = {
    "vllm": {_TOKEN_LOG_MESSAGE, "vLLM serving %s (revision %s) on port %d"},
    "sglang": {_TOKEN_LOG_MESSAGE, "SGLang serving %s (revision %s) on port %d"},
}

# What a logger ARGUMENT may never mention: the environment (the token lives
# there) or a Secret. `_TOKEN_BOOLEAN` is the one exception.
_FORBIDDEN_IN_LOG_ARGS = ("environ", "getenv", "Secret", "HF_")

# The names that put bytes on the container's stdout/stderr directly, instead
# of through the logger: `print` (bare or via `builtins`), `pprint`, any
# `...stdout.write*` / `...stderr.write*` attribute chain, and the raw file
# descriptor writers.
_PRINTERS = frozenset({"print", "pprint"})
_CONSOLE_STREAMS = frozenset({"stdout", "stderr", "__stdout__", "__stderr__"})
_RAW_WRITERS = frozenset({"os.write", "os.writev"})

# The modules whose whole point is starting a child process. `commands` is
# Python 2's — never importable here, but the guard reads SOURCE, and naming it
# costs one word.
_SPAWN_MODULES = frozenset({"subprocess", "pty", "commands", "multiprocessing"})

# The two ways to name a module WITHOUT an import statement, and the three that
# run a string the AST cannot read. Both families are banned OUTRIGHT in these
# scripts (#152 QA): they use neither, so any occurrence is either a leak being
# hidden (`__import__("os").system("env")`) or the machinery to hide one.
_DYNAMIC_IMPORTS = frozenset(
    {"__import__", "importlib.import_module", "importlib.__import__"}
)
_DYNAMIC_EXECUTION = frozenset({"eval", "exec", "compile"})

# The methods that EMIT a log record, on any receiver: `logger.info(...)`,
# `logging.info(...)`, `logging.getLogger(__name__).warning(...)` and a
# `log = logging.getLogger("x")` alias all reach the same `modal app logs`
# (#147 QA). `basicConfig` / `getLogger` are not emit methods and stay legal —
# both scripts configure logging that way.
_LOG_EMIT_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)

# Reading the environment WHOLE (or under another name) is how the token
# escapes without the string "HF_TOKEN" appearing anywhere.
_ENVIRON = "os.environ"
_FORBIDDEN_ENV_READERS = frozenset({"os.getenv", "os.environ.copy", "os.environb"})

# The anchors the mutants are grafted onto — one per guard. Each must occur
# EXACTLY once in every script (`_mutate` asserts it), so a mutation cannot
# silently land in a second place or nowhere at all.
_AUTH_KEYWORD = "unauthenticated=False,"
_STOP_CALL = "        self.endpoint.stop()"
_APP_LINE = 'app = modal.App(SPEC["app_name"])'
_SPEC_ENV_LAYER = f"DEPLOY_SPEC_ENV: {_SPEC_VALUE_NAME}}}"

# The words each script may no longer contain (ADR-009 §2): one App per kind,
# neither of them a rung on a ladder. `(?<!v)llm` keeps `vLLM` /
# `VLLMEndpoint`, which are what the vLLM file IS; the SGLang pattern forbids
# every trace of the embedding mode this file used to have.
_RETIRED_VOCABULARY: dict[str, re.Pattern[str]] = {
    "vllm": re.compile(
        r"fallback|eject|ladder|serving path|SERVING=|(?<!v)llm|chat", re.IGNORECASE
    ),
    "sglang": re.compile(
        r"fallback|eject|ladder|serving path|SERVING=|is.embedding|embeddings",
        re.IGNORECASE,
    ),
}

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


def _image_env_dicts(module: ast.Module) -> list[ast.Dict]:
    """EVERY dict literal baked into an image with ``.env({...})``.

    All of them, not the first one found: a second Image object with its own
    ``.env({"HF_TOKEN": …})`` is a cached, inspectable layer carrying the
    credential, and a guard that stops at the first call never sees it.
    """

    return [
        node.args[0]
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "env"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Dict)
    ]


def _image_env_keys(module: ast.Module) -> list[str]:
    """Every constant key of every ``.env({...})`` layer."""

    return [
        key.value
        for env in _image_env_dicts(module)
        for key in env.keys
        if isinstance(key, ast.Constant)
    ]


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


def _module_dict(module: ast.Module, name: str) -> dict[str, object]:
    """A module-level ``NAME = {...}`` literal, evaluated as DATA.

    ``SPEC["repo_id"]`` — the one non-literal inside the warm-up payload — is
    replaced by a marker, so ``strict: True`` and ``additionalProperties:
    False`` can be asserted as the BOOLEANS a server reads instead of as
    source text (a quoted ``"false"`` would pass a substring check and silently
    disable the constraint).
    """

    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            rendered = ast.unparse(statement.value).replace(
                "SPEC['repo_id']", "'<repo_id>'"
            )
            value = ast.literal_eval(ast.parse(rendered, mode="eval").body)
            assert isinstance(value, dict), name
            return value
    raise AssertionError(f"the script has no module-level `{name} = {{...}}`")


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


def _console_aliases(module: ast.Module) -> set[str]:
    """Names bound to a console writer — ``p = print``, ``from sys import stdout``.

    Simple bindings only, which is what a debugging session leaves behind; the
    point is that the guard no longer trusts the SPELLING of the call.
    """

    aliases: set[str] = set()
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name | ast.Attribute)
        ):
            dotted = _dotted(node.value)
            parts = dotted.split(".")
            if parts[-1] in _PRINTERS | _CONSOLE_STREAMS or dotted in _RAW_WRITERS:
                aliases.add(node.targets[0].id)
        if isinstance(node, ast.ImportFrom) and node.module in {
            "sys",
            "builtins",
            "os",
        }:
            for alias in node.names:
                if alias.name in _PRINTERS | _CONSOLE_STREAMS | {"write", "writev"}:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _console_writes(module: ast.Module) -> list[str]:
    """Every call in ``module`` that writes to the console itself.

    Dotted names, as written, so the failure message can NAME the call: a
    ``print``/``pprint`` under any prefix (``builtins.print``,
    ``pprint.pprint``), any attribute chain whose last two parts are
    ``stdout``/``stderr`` (or their ``__`` originals) + ``write``/``writelines``,
    the raw ``os.write`` family, and any ALIAS of those (#146 QA: ``p = print;
    p(SPEC)`` passed every guard of the day).
    """

    aliases = _console_aliases(module)
    offenders: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        parts = dotted.split(".")
        if parts[-1] in _PRINTERS or dotted in _RAW_WRITERS:
            offenders.append(dotted)
        elif (
            len(parts) > 1
            and parts[-1].startswith("write")
            and parts[-2] in _CONSOLE_STREAMS
        ):
            offenders.append(dotted)
        elif dotted in aliases or (
            len(parts) > 1 and parts[0] in aliases and parts[-1].startswith("write")
        ):
            offenders.append(dotted)
    return offenders


def _spawns_from_os(name: str) -> bool:
    """``system`` / ``popen`` / ``fork*`` / the ``exec*`` and ``spawn*`` families."""

    return name in {"system", "popen"} or name.startswith(
        ("exec", "spawn", "posix_spawn", "fork")
    )


def _dynamically_imported(node: ast.expr) -> str | None:
    """The module ``__import__("os")`` / ``import_module("os")`` names, or ``None``.

    Only the LITERAL first argument is resolved: a computed module name is
    already an offender on its own (any dynamic import is), so there is nothing
    to resolve for it.
    """

    if (
        isinstance(node, ast.Call)
        and _dotted(node.func) in _DYNAMIC_IMPORTS
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _process_spawns(module: ast.Module) -> list[str]:
    """Every way ``module`` could start a CHILD PROCESS, or hide one, as written.

    A child inherits the container's stdout AND its environment, so
    ``subprocess.run(["env"])`` or ``os.system("env")`` inside ``@modal.enter``
    puts the real ``HF_TOKEN`` into ``modal app logs`` — while naming no
    variable, writing to no stream and reading no ``os.environ``, which is why
    ``_console_writes``, ``_environ_reads`` and ``_log_offenders`` are all
    silent on it (#147 QA).

    Four shapes, all blunt on purpose:

    * the IMPORT of a process-spawning module (``subprocess``, ``pty``,
      ``commands``, ``multiprocessing`` — however aliased or nested);
    * the ``os`` spawners by dotted name (``os.system``, ``os.popen``,
      ``os.fork*``, ``os.exec*``, ``os.spawn*``, ``os.posix_spawn*``) or as a
      ``from os import …``, and — receiver-agnostic, like
      :data:`_LOG_EMIT_METHODS` — any ``…create_subprocess*`` (asyncio's pair);
    * any DYNAMIC IMPORT or dynamic execution — ``__import__``,
      ``importlib.import_module``, ``eval``, ``exec``, ``compile`` — CALLED or
      merely BOUND TO A NAME. ``__import__("os").system("env")`` hid a spawn
      from the dotted-name check above (#152 QA: the chain is rooted at a Call,
      so ``.system`` renders as bare ``"system"``), and ``imp = __import__;
      imp("os").system("env")`` hid it once more behind an alias — so the
      BINDING is the offence too, which needs no data-flow analysis. A spawn
      written as a STRING is unreadable to an AST walk, so banning the
      primitive that would run it is the only sound answer;
    * CALLING THE RESULT OF A CALL — ``getattr(os, "system")("env")``,
      ``g = getattr; g(os, "system")("env")``. The ``func`` of such a call is a
      Call, not an Attribute, so no dotted-name rule can see what it dispatches
      to; these scripts call the result of a call exactly nowhere, so the blunt
      rule costs nothing. :func:`_log_offenders` bans the same shape, for the
      same reason — a spawn hidden this way must fail as a SPAWN, not as an
      unreviewed log line.

    Reporting an import (or a dynamic-execution name) alone is enough: these
    scripts are glue that starts ONE server through the engine object, so the
    occurrence is already the offence.

    KNOWN BOUNDS — what a static walk of THIS file cannot see, named rather
    than implied:

    * exfiltration that is not a spawn (``socket``, ``urllib``, ``http.client``
      POSTing ``os.environ`` somewhere) — a different attack class, caught by
      neither this guard nor ``_environ_reads`` if it never names ``os.environ``
      (it must, so ``_environ_reads`` holds that line);
    * a dynamic import BOUND through an attribute rather than by its own name
      (``f = importlib.import_module; f("subprocess")``, ``b = builtins;
      b.__import__("os")``): the binding rule reads a bare ``ast.Name``, and
      resolving what an attribute chain was bound to is the data-flow analysis
      this file deliberately does not do (the same bound
      :func:`_log_offenders` names for a bound emit method);
    * anything a THIRD-PARTY import does on our behalf — the engine object
      itself starts the server process, which is the whole point of the script.
    """

    offenders: list[str] = []
    # The nodes that ARE the callee of a call, so the binding rule below can
    # report `imp = __import__` without also reporting the `__import__` of a
    # direct `__import__("os")` a second time (the Call rule has that one).
    called = {id(node.func) for node in ast.walk(module) if isinstance(node, ast.Call)}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            offenders += [
                ast.unparse(node)
                for alias in node.names
                if alias.name.split(".")[0] in _SPAWN_MODULES
            ]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _SPAWN_MODULES or (
                root == "os" and any(_spawns_from_os(a.name) for a in node.names)
            ):
                offenders.append(ast.unparse(node))
        elif isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            imported = _dynamically_imported(node.value)
            if dotted.startswith("os.") and _spawns_from_os(dotted.removeprefix("os.")):
                offenders.append(dotted)
            elif node.attr.startswith("create_subprocess"):
                offenders.append(ast.unparse(node))
            elif imported == "os" and _spawns_from_os(node.attr):
                offenders.append(ast.unparse(node))
        # Only the BARE-NAME primitives can match here (`__import__`, `eval`,
        # `exec`, `compile`); the dotted spellings in `_DYNAMIC_IMPORTS` are a
        # `node.id` no one can have, which is the attribute-binding bound named
        # in the docstring above, not an oversight.
        elif (
            isinstance(node, ast.Name)
            and node.id in _DYNAMIC_IMPORTS | _DYNAMIC_EXECUTION
            and id(node) not in called
        ):
            offenders.append(node.id)
        elif isinstance(node, ast.Call) and (
            _dotted(node.func) in (_DYNAMIC_IMPORTS | _DYNAMIC_EXECUTION)
            or isinstance(node.func, ast.Call)
        ):
            offenders.append(ast.unparse(node))
    return offenders


def _approved_environ_nodes(module: ast.Module, engine: str) -> set[int]:
    """The ``os.environ`` nodes an App script is ALLOWED to have.

    Exactly three shapes: ``os.environ["MODAL_MODEL"]`` (local branch),
    ``os.environ["<the spec env var>"]`` (container branch) and the token
    BOOLEAN — never the token's value, never the environment as a whole.
    """

    allowed_keys = {"MODAL_MODEL", _SPEC_ENV[engine]}
    approved: set[int] = set()
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Subscript)
            and _dotted(node.value) == _ENVIRON
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in allowed_keys
        ):
            approved.add(id(node.value))
        if (
            isinstance(node, ast.Call)
            and _dotted(node.func) == "bool"
            and ast.unparse(node) == _TOKEN_BOOLEAN
        ):
            inner = node.args[0]
            assert isinstance(inner, ast.Call)
            assert isinstance(inner.func, ast.Attribute)
            approved.add(id(inner.func.value))
    return approved


def _environ_reads(module: ast.Module, engine: str) -> list[str]:
    """Every environment read that is NOT one of the three allowed shapes.

    The mutant this exists for is ``logger.info("%s", os.environ)``: it names
    no variable, so a guard that counts occurrences of ``"HF_TOKEN"`` is blind
    to it — and it dumps the real token into ``modal app logs``.
    """

    approved = _approved_environ_nodes(module, engine)
    offenders: list[str] = []
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Attribute)
            and _dotted(node) == _ENVIRON
            and id(node) not in approved
        ):
            offenders.append(ast.unparse(node))
        if isinstance(node, ast.Call) and _dotted(node.func) in _FORBIDDEN_ENV_READERS:
            offenders.append(_dotted(node.func))
    return offenders


def _log_offenders(module: ast.Module, engine: str) -> list[str]:
    """Every logging call that is not an allow-listed message with safe args.

    Two rules, because either alone has a hole: the MESSAGE must be one of the
    literals pinned above (so a new line cannot appear unreviewed), and no
    ARGUMENT may reference the environment or a Secret (so an allow-listed
    ``%s`` cannot be fed the token).

    RECEIVER-AGNOSTIC: any call to an emit method counts, whatever it is called
    on. The old rule matched the dotted name ``logger.*``, so
    ``logging.info(...)``, ``logging.getLogger().info(...)`` and a
    ``log = logging.getLogger(__name__)`` alias all walked past the allow-list
    into ``modal app logs`` (#147 QA) — and resolving those aliases by
    data-flow is a far bigger walk than simply not trusting the spelling.
    ``.log(level, msg, …)`` puts its message SECOND; every other method first.

    A SUPERSET of the old rule, never a swap: anything on the module's own
    ``logger`` still counts whatever the method is called, so
    ``logger.handle(record)`` — which pushes a ``LogRecord`` past the message
    allow-list straight into the handlers — cannot slip through the narrower
    emit-method list.

    KNOWN BOUNDS — what this walk cannot see, named rather than implied:

    * a message built by ``eval`` / ``exec`` of a string: unreadable here, which
      is why :func:`_process_spawns` bans those primitives outright;
    * a HANDLER redirected on a logger this file does not name
      (``logging.getLogger().addHandler(…)``): ``addHandler`` is not an emit
      method, and only the module's own ``logger.`` prefix is guarded for
      non-emit methods;
    * an emit method BOUND to a name and called through it
      (``emit = logger.info; emit("…")``): the call's ``func`` is then a plain
      ``ast.Name``, and this walk reads the CALL, never the binding — closing it
      means the data-flow analysis the receiver-agnostic rule exists to avoid.
      An accepted bound, deliberately, not an oversight;
    * bytes that never become a log record at all — a ``socket`` / ``urllib``
      POST of the environment (a different attack class, out of reach of a
      logging guard by construction).
    """

    offenders: list[str] = []
    # DYNAMIC DISPATCH, not a logging rule: `getattr(logger, "info")(msg)` has
    # a Call — not an Attribute — for its `func`, so it walked straight past
    # the receiver-agnostic match below, and so does `g = getattr; g(logger,
    # "info")(msg)` (#152 QA). Both shipped scripts call the RESULT of a call
    # exactly nowhere, so the sound rule is the blunt one: calling a call is
    # the offence, whatever it resolves to.
    offenders += [
        ast.unparse(node)
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Call)
    ]
    calls = [
        (node, node.func.attr)
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and (
            node.func.attr in _LOG_EMIT_METHODS
            or _dotted(node.func).startswith("logger.")
        )
    ]
    for call, method in calls:
        rendered = ast.unparse(call)
        index = 1 if method == "log" else 0
        first = call.args[index] if len(call.args) > index else None
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            offenders.append(rendered)
            continue
        if first.value not in _LOG_MESSAGES[engine]:
            offenders.append(rendered)
        for argument in call.args[index + 1 :]:
            unparsed = ast.unparse(argument)
            if unparsed != _TOKEN_BOOLEAN and any(
                needle in unparsed for needle in _FORBIDDEN_IN_LOG_ARGS
            ):
                offenders.append(rendered)
    return offenders


def _env_layer_value(module: ast.Module, key_name: str) -> list[str]:
    """What every image ``.env({...})`` stores under the NAME ``key_name``."""

    return [
        ast.unparse(value)
        for env in _image_env_dicts(module)
        for key, value in zip(env.keys, env.values, strict=True)
        if isinstance(key, ast.Name) and key.id == key_name
    ]


def _assignments_to(module: ast.Module, name: str) -> list[ast.Assign]:
    """Every module-level-or-nested ``name = ...`` assignment, as written."""

    return [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
    ]


def _assert_the_spec_crosses_as_base64(module: ast.Module, engine: str) -> None:
    """The spec's transport, pinned on both sides of ``modal.is_local()``.

    ADR-009 §3, after the live crash of ``tasks/141`` round 1: the image layer
    bakes the BARE NAME the two branches share, the local branch fills it from
    ``encode_deploy_spec`` (the catalog's one encoder, imported HERE so a
    rename cannot drift), the container branch echoes the env var and decodes
    it with the stdlib — and nothing in the file serialises JSON any more,
    because a raw JSON value is what the image build corrupted.
    """

    stored = _env_layer_value(module, "DEPLOY_SPEC_ENV")
    assert stored == [_SPEC_VALUE_NAME], (
        f"the image layer must bake the bare `{_SPEC_VALUE_NAME}` "
        f"(base64, ADR-009 §3), not {stored}"
    )

    branch = _is_local_branch(module)
    local_nodes = _nodes_in(branch.body)
    container_nodes = _nodes_in(branch.orelse)
    assignments = _assignments_to(module, _SPEC_VALUE_NAME)
    assert len(assignments) == 2, (
        f"`{_SPEC_VALUE_NAME}` must be assigned exactly once per branch, "
        f"found {len(assignments)}"
    )

    local = next(node for node in assignments if id(node) in local_nodes)
    assert isinstance(local.value, ast.Call)
    assert _dotted(local.value.func) == encode_deploy_spec.__name__

    container = next(node for node in assignments if id(node) in container_nodes)
    assert ast.unparse(container.value) == f"os.environ[{_SPEC_ENV[engine]!r}]"

    decode = ast.Module(body=branch.orelse, type_ignores=[])
    assert _calls(decode, "base64.b64decode"), "the container decodes no base64"
    assert not _calls(module, "json.dumps"), "a raw JSON value is what broke"


def _container_decode(engine: str, value: str) -> object:
    """Run the container branch's DECODE over ``value``, nothing else.

    The script is never imported (it would need ``modal``, and Modal re-imports
    it in a container that has neither ``tree`` nor a test runner): the decode
    statements are lifted out of the ``else`` branch by AST and executed with
    the stdlib names they use. The statements that are NOT the decode —
    ``logging.basicConfig(force=True)``, the Secret, the env read this
    function replaces — are dropped by name.

    That makes this the drift guard between the script's decoder and
    ``encode_deploy_spec``: if either side changes alone, the round trip below
    stops closing.
    """

    statements = [
        ast.unparse(statement)
        for statement in _is_local_branch(_module(engine)).orelse
        if not ast.unparse(statement).startswith(_NOT_THE_DECODE)
    ]
    source = "\n".join(statements)
    # Not `assert statements`: that only proves SOMETHING survived the filter
    # above, so a renamed variable could leave this helper executing the
    # neighbours and passing vacuously.
    assert "b64decode" in source, f"no decode survived the lift:\n{source}"

    namespace: dict[str, Any] = {
        "json": json,
        "base64": base64,
        _SPEC_VALUE_NAME: value,
        _SPEC_ERROR_CONSTANT: _module_constant(_module(engine), _SPEC_ERROR_CONSTANT),
    }
    # `exec` of THIS project's own source, lifted line by line from a file the
    # tests above have already walked — the alternative is importing a module
    # that needs `modal` and reconfigures logging.
    exec(compile(source, "<container>", "exec"), namespace)
    return namespace["SPEC"]


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
        ``sys.stdout.write(str(SPEC))``, an ``os.write(1, …)`` or a ``p =
        print`` left behind after a debugging session puts the whole deploy
        spec there just as well, which is why the guard walks the calls — and
        their aliases — instead of grepping for ``print(``.
        """

        writes = _console_writes(_module(engine))

        assert not writes, (
            "the script writes to the console instead of the logger: "
            f"{', '.join(writes)}"
        )

    def test_no_process_is_spawned(self, engine: str) -> None:
        """A child process inherits the container's stdout AND its environment.

        ``subprocess.run(["env"])`` or ``os.system("env")`` inside
        ``@modal.enter`` puts the real ``HF_TOKEN`` into ``modal app logs``
        without the script naming a variable, writing to a stream or reading
        ``os.environ`` — invisible to all three guards above (#147 QA). These
        scripts are glue: they start ONE server, through the engine object —
        so the primitives that would HIDE a spawn from this walk (a dynamic
        import, an ``eval``) are banned with it (#152 QA).
        """

        spawns = _process_spawns(_module(engine))

        assert not spawns, (
            f"the script spawns a process, or hides one behind dynamic "
            f"execution: {', '.join(spawns)} — a child inherits the "
            "environment the HF_TOKEN lives in"
        )

    def test_only_allow_listed_lines_are_logged(self, engine: str) -> None:
        """``modal app logs`` is where a container's bytes end up, so every
        message is enumerated here, no argument may read the environment, and
        no call is dispatched dynamically past the allow-list."""

        offenders = _log_offenders(_module(engine), engine)

        assert not offenders, (
            f"unreviewed or dynamically dispatched log call(s): {'; '.join(offenders)}"
        )

    def test_the_environment_is_read_only_where_it_must_be(self, engine: str) -> None:
        """Three reads, no more: the model id, the spec, and the token as a
        BOOLEAN. Anything else — ``os.getenv``, ``os.environ.copy()``, the
        whole mapping handed to something — can carry the token out."""

        reads = _environ_reads(_module(engine), engine)

        assert not reads, f"the script reads the environment at: {', '.join(reads)}"

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

        assert decorated == [MODAL_SERVER_NAME]

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

    def test_the_script_resolves_its_own_kind(self, engine: str) -> None:
        """The kind is the SCRIPT's — a catalog entry names no engine, and the
        model crosses from the driver as ``MODAL_MODEL``, the one env var both
        scripts read."""

        builder = _SPEC_BUILDER[engine]

        assert f'{builder}(os.environ["MODAL_MODEL"])' in _source(engine)

    def test_the_endpoint_is_constructed_the_way_its_engine_takes_it(
        self, engine: str
    ) -> None:
        """``VLLMEndpoint(model=…)`` vs ``SGLangEndpoint(model_path=…, tp=…)``
        — verified in the ``autoinference-utils`` 0.2.6 source."""

        constructor, model_keyword, keywords = _ENDPOINT_CALLS[engine]

        calls = _calls(_module(engine), constructor)
        assert len(calls) == 1
        assert {keyword.arg for keyword in calls[0].keywords} == keywords
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

        spec_env = _SPEC_ENV[engine]

        assert f'DEPLOY_SPEC_ENV = "{spec_env}"' in _source(engine)
        assert f'os.environ["{spec_env}"]' in _source(engine)

    def test_the_spec_crosses_as_base64(self, engine: str) -> None:
        """ADR-009 §3: the value is the base64 of the spec's JSON.

        The raw JSON died live — Modal renders an image env var as a
        Dockerfile ``ENV k=<shlex.quote(v)>`` and the build unescaped every
        backslash, so voyage-4-nano's JSON-in-JSON server args reached the
        container broken and the App crash-looped at import.
        """

        _assert_the_spec_crosses_as_base64(_module(engine), engine)

        assert "json.dumps" not in _source(engine)

    def test_the_container_decode_inverts_the_catalog_encoder(
        self, engine: str
    ) -> None:
        """The two halves of the transport are written in two files; this is
        the one test that closes the loop between them."""

        builder, model = _SEED[engine]
        spec = builder(model)

        decoded = _container_decode(engine, encode_deploy_spec(spec))

        assert decoded == spec.model_dump()

    @pytest.mark.parametrize(
        "value,reason",
        [
            ("", "empty"),
            ("not base64!", "outside the alphabet"),
            (base64.b64encode(b"{not json").decode(), "base64 of broken JSON"),
            (base64.b64encode(b"[1, 2]").decode(), "base64 of a JSON non-object"),
            (base64.b64encode(b"null").decode(), "base64 of JSON null"),
            # The most plausible corruption left once the quoting layers can
            # no longer touch the value: a clipped env var. Cut ON a 4-char
            # group boundary, so the base64 itself is still well formed and
            # only the JSON it hides is truncated.
            (base64.b64encode(b'{"repo_id": "x"}').decode()[:8], "clipped value"),
        ],
    )
    def test_a_corrupt_spec_fails_at_import_naming_the_env_var(
        self, engine: str, value: str, reason: str
    ) -> None:
        """A value that does not decode kills the import LOUDLY.

        The failure mode this whole task exists for was diagnosed from the
        import traceback, so the replacement must not degrade into a silent
        default: whatever the corruption, the container raises and the message
        names the variable an operator has to look at.
        """

        with pytest.raises(RuntimeError) as excinfo:
            _container_decode(engine, value)

        assert _SPEC_ENV[engine] in str(excinfo.value), reason

    def test_the_script_talks_about_its_own_kind_only(self, engine: str) -> None:
        """ADR-009 §2: one App per kind — the vLLM script serves embedding
        models, the SGLang script LLMs, and neither is a rung on a ladder."""

        hits = sorted(
            {
                match.group(0)
                for match in _RETIRED_VOCABULARY[engine].finditer(_source(engine))
            }
        )

        assert not hits, f"the {engine} script still talks about: {', '.join(hits)}"


@_ENGINES
def test_only_the_hf_token_secret(engine: str) -> None:
    """The ONE Secret is the Hugging Face token's, and it is never in an image.

    ADR-009 §9: built from the local settings under ``modal.is_local()``, an
    EMPTY dict on the in-container re-import (same list length on both sides),
    attached as ``secrets=[HF_SECRET]``, and absent from EVERY image env, whose
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

    # EVERY `.env({...})` layer, not only the first one found.
    assert _image_env_dicts(module), "the image has no `.env({...})` layer"
    assert _TOKEN_VAR not in _image_env_keys(module)

    # The variable's name appears exactly twice: in the boolean log line, and
    # as the key that line looks up. Never as a value, never in an image.
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
    """Every guard, proven on a MUTATED COPY in ``tmp_path``.

    Each blind spot below was found by mutation during #142's and #146's QA: a
    guard that only greps for ``unauthenticated=False`` / ``print(`` passes a
    script that is public, or that dumps the deploy spec — or the real token —
    into ``modal app logs``. The shipped scripts are read-only here: the mutant
    is a copy, and it is never imported or executed.
    """

    def test_the_shipped_script_passes_every_guard(self, engine: str) -> None:
        """The control row: without a mutation, all seven guards are silent."""

        module = _module(engine)

        _assert_proxy_auth_is_pinned(module)
        _assert_the_spec_crosses_as_base64(module, engine)
        assert not _console_writes(module)
        assert not _environ_reads(module, engine)
        assert not _log_offenders(module, engine)
        assert not _process_spawns(module)
        assert _TOKEN_VAR not in _image_env_keys(module)

    @pytest.mark.parametrize(
        "mutation",
        [
            # The transport as it shipped before #153 — the one that killed
            # the DEFAULT embedding model's container at import.
            "DEPLOY_SPEC_ENV: json.dumps(SPEC)}",
            # ... and re-encoding the spec in the layer instead of baking the
            # value both branches share: the container would then read an env
            # var nothing wrote the same way.
            "DEPLOY_SPEC_ENV: encode_deploy_spec(RESOLVED_SPEC)}",
        ],
        ids=["raw-json", "recomputed"],
    )
    def test_a_spec_not_baked_as_the_shared_base64_value_fails(
        self, engine: str, mutation: str, tmp_path: pathlib.Path
    ) -> None:
        mutant = _mutate(engine, tmp_path, _SPEC_ENV_LAYER, mutation)

        with pytest.raises(AssertionError, match=_SPEC_VALUE_NAME):
            _assert_the_spec_crosses_as_base64(mutant, engine)

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
            # #146 QA: these two passed every guard of the day.
            ('os.write(1, b"leak")', "os.write"),
            ('sys.__stdout__.write("x")', "sys.__stdout__.write"),
        ],
    )
    def test_a_console_write_fails_and_is_named(
        self, engine: str, statement: str, call: str, tmp_path: pathlib.Path
    ) -> None:
        mutant = _mutate(
            engine, tmp_path, _STOP_CALL, f"        {statement}\n{_STOP_CALL}"
        )

        assert _console_writes(mutant) == [call]

    def test_an_aliased_print_fails(self, engine: str, tmp_path: pathlib.Path) -> None:
        """#146 QA: ``p = print`` then ``p(SPEC)`` — the dotted name is ``p``,
        which no list of printer names contains."""

        mutant = _mutate(
            engine,
            tmp_path,
            _STOP_CALL,
            f"        p = print\n        p(SPEC)\n{_STOP_CALL}",
        )

        assert _console_writes(mutant) == ["p"]

    @pytest.mark.parametrize(
        "statement",
        [
            # THE serious one: it names no variable, so an "HF_TOKEN appears
            # twice" check is blind to it — and `modal app logs` gets the real
            # token.
            'logger.info("%s", os.environ)',
            'logger.info("HF_TOKEN set in container: %s", os.environ["HF_TOKEN"])',
            'logger.info("%s", os.getenv("HF_TOKEN"))',
        ],
        ids=["whole-environ", "the-value", "getenv"],
    )
    def test_a_logged_environment_fails(
        self, engine: str, statement: str, tmp_path: pathlib.Path
    ) -> None:
        mutant = _mutate(
            engine, tmp_path, _STOP_CALL, f"        {statement}\n{_STOP_CALL}"
        )

        assert _environ_reads(mutant, engine)
        assert _log_offenders(mutant, engine)

    def test_an_unreviewed_log_message_fails(
        self, engine: str, tmp_path: pathlib.Path
    ) -> None:
        """A line nobody reviewed is a line that may carry anything."""

        mutant = _mutate(
            engine,
            tmp_path,
            _STOP_CALL,
            f'        logger.info("spec is %s", SPEC)\n{_STOP_CALL}',
        )

        assert _log_offenders(mutant, engine) == ["logger.info('spec is %s', SPEC)"]

    @pytest.mark.parametrize(
        "statement",
        [
            'logging.getLogger().info("spec is %s", SPEC)',
            'logging.info("spec is %s", SPEC)',
            'logging.getLogger(__name__).warning("spec is %s", SPEC)',
            'log = logging.getLogger("x"); log.error("spec is %s", SPEC)',
            'logger.log(logging.INFO, "spec is %s", SPEC)',
        ],
        ids=["root-getLogger", "module-level", "named-getLogger", "alias", "log-level"],
    )
    def test_any_spelling_of_a_logging_call_fails(
        self, engine: str, statement: str, tmp_path: pathlib.Path
    ) -> None:
        """#147 QA: the allow-list only saw calls spelled ``logger.``.

        A second logger — however it is spelled, and whatever name it is bound
        to — reaches the same ``modal app logs``, so the RULE is the allow-list,
        not the receiver.
        """

        mutant = _mutate(
            engine, tmp_path, _STOP_CALL, f"        {statement}\n{_STOP_CALL}"
        )

        offenders = _log_offenders(mutant, engine)

        assert any("spec is %s" in offender for offender in offenders), offenders

    def test_a_record_pushed_past_the_allow_list_fails(
        self, engine: str, tmp_path: pathlib.Path
    ) -> None:
        """The widened rule must stay a SUPERSET of the old ``logger.*`` one.

        ``logger.handle(record)`` emits — it hands a ``LogRecord`` to the
        handlers — while naming none of the nine emit methods.
        """

        record = 'logging.LogRecord("x", 20, "f", 1, "spec is %s", (SPEC,), None)'
        mutant = _mutate(
            engine,
            tmp_path,
            _STOP_CALL,
            f"        logger.handle({record})\n{_STOP_CALL}",
        )

        assert _log_offenders(mutant, engine)

    def test_an_allow_listed_message_passes_whatever_the_spelling(
        self, engine: str, tmp_path: pathlib.Path
    ) -> None:
        """The other half of the same rule: a reviewed message with safe
        arguments is legal through ANY logger — the guard is not a style check
        on how the logger was obtained."""

        mutant = _mutate(
            engine,
            tmp_path,
            _STOP_CALL,
            f'        logging.getLogger().info("{_TOKEN_LOG_MESSAGE}", '
            f"{_TOKEN_BOOLEAN})\n{_STOP_CALL}",
        )

        assert _log_offenders(mutant, engine) == []

    @pytest.mark.parametrize(
        "statement,offender",
        [
            ('import subprocess; subprocess.run(["env"])', "subprocess"),
            ('import subprocess as sp; sp.check_output("env")', "subprocess"),
            ('from subprocess import run; run(["env"])', "subprocess"),
            ('os.system("env")', "os.system"),
            ('os.popen("env").read()', "os.popen"),
            ('os.execvp("env", ["env"])', "os.execvp"),
            ('os.spawnlp(os.P_WAIT, "env", "env")', "os.spawnlp"),
            ("os.fork()", "os.fork"),
            ('from os import system; system("env")', "system"),
            ('import pty; pty.spawn("env")', "pty"),
            ("import commands", "commands"),
            # #152 QA: each of these is a ONE-TOKEN rewrite of the rows above,
            # and each passed the guard as first written. `__import__("os")` is
            # a Call, so the dotted name of `.system` rendered as bare
            # `"system"` and never matched the `"os."` prefix.
            ('__import__("os").system("env")', "__import__('os').system"),
            (
                'importlib.import_module("subprocess").run(["env"])',
                "importlib.import_module",
            ),
            ('asyncio.create_subprocess_exec("env")', "create_subprocess_exec"),
            ('asyncio.create_subprocess_shell("env")', "create_subprocess_shell"),
            (
                "import multiprocessing; multiprocessing.Process().start()",
                "multiprocessing",
            ),
            # A spawn inside a string is unreadable to an AST walk, so the
            # primitives that would run it are banned outright instead.
            ("eval(\"__import__('os').system('env')\")", "eval"),
            ('exec("import subprocess")', "exec"),
            ('compile("import subprocess", "<x>", "exec")', "compile"),
            # ... and one rewrite further: bind the primitive first, so the
            # call names neither `__import__` nor `os` (#152 QA's own example).
            # BINDING one is the offence, which needs no data-flow analysis.
            ('imp = __import__; imp("os").system("env")', "__import__"),
            ("runner = eval; runner(\"__import__('os').system('env')\")", "eval"),
            # Dynamic DISPATCH: the `func` of the outer call is a Call, so no
            # dotted-name rule can see `os.system` behind it. Caught here as a
            # SPAWN, not only by `_log_offenders`, so the failure names what it
            # is.
            ('getattr(os, "system")("env")', "getattr(os, 'system')('env')"),
            (
                'g = getattr; g(os, "system")("env")',
                "g(os, 'system')('env')",
            ),
        ],
    )
    def test_a_spawned_process_fails(
        self, engine: str, statement: str, offender: str, tmp_path: pathlib.Path
    ) -> None:
        """#147 QA: ``subprocess.run(["env"])`` inside ``@modal.enter`` dumps the
        container environment — the real ``HF_TOKEN`` — to the inherited stdout,
        i.e. into ``modal app logs``, and passed every guard of the day."""

        mutant = _mutate(
            engine, tmp_path, _STOP_CALL, f"        {statement}\n{_STOP_CALL}"
        )

        spawns = _process_spawns(mutant)

        assert any(offender in spawn for spawn in spawns), spawns

    @pytest.mark.parametrize(
        "statement",
        [
            'getattr(logger, "info")("spec is %s", SPEC)',
            'g = getattr; g(logger, "info")("spec is %s", SPEC)',
            'getattr(logging, METHOD)("spec is %s", SPEC)',
        ],
        ids=["getattr", "aliased-getattr", "non-literal-method"],
    )
    def test_a_dynamically_dispatched_logging_call_fails(
        self, engine: str, statement: str, tmp_path: pathlib.Path
    ) -> None:
        """#152 QA: ``getattr(logger, "info")(…)`` walked past the allow-list.

        The receiver-agnostic rule above still requires ``node.func`` to be an
        ``ast.Attribute``; a ``getattr`` call is an ``ast.Call``, so a
        one-token rewrite routed an unreviewed message — or the token — into
        ``modal app logs`` again.
        """

        mutant = _mutate(
            engine, tmp_path, _STOP_CALL, f"        {statement}\n{_STOP_CALL}"
        )

        offenders = _log_offenders(mutant, engine)

        assert any("spec is %s" in offender for offender in offenders), offenders

    def test_an_imported_module_name_alone_fails(
        self, engine: str, tmp_path: pathlib.Path
    ) -> None:
        """``__import__`` hides the name from an import-statement walk."""

        mutant = _mutate(
            engine,
            tmp_path,
            _STOP_CALL,
            f'        __import__("subprocess").run(["env"])\n{_STOP_CALL}',
        )

        assert _process_spawns(mutant)

    def test_a_second_image_env_carrying_the_token_fails(
        self, engine: str, tmp_path: pathlib.Path
    ) -> None:
        """#146 QA: the helper stopped at the first ``.env({...})`` it found,
        so a SECOND Image object could bake the credential into a cached layer.
        """

        leak = (
            "leak = modal.Image.debian_slim()"
            '.env({"HF_TOKEN": os.environ["HF_TOKEN"]})\n'
        )
        mutant = _mutate(engine, tmp_path, _APP_LINE, leak + _APP_LINE)

        # Asserted on the WIDENED helper specifically: the "HF_TOKEN occurs
        # twice" check would also fail here, for a different reason.
        assert len(_image_env_dicts(mutant)) == 2
        assert _TOKEN_VAR in _image_env_keys(mutant)


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
    assert "HF_XET_HIGH_PERFORMANCE" in _image_env_keys(module)

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
    # TARGET_INPUTS is 16; scale-to-zero after 5 minutes). An embedding entry
    # has no `n_gpus`, so the GPU string is the catalog's as-is.
    decorator = _server_decorator(module)
    assert ast.unparse(_keyword(decorator, "gpu")) == "SPEC['gpu']"
    assert _keyword(decorator, "port").id == "PORT"
    assert _keyword(decorator, "min_containers").value == 0
    assert _keyword(decorator, "exit_grace_period").value == 25
    assert _keyword(decorator, "routing_region").value == MODAL_ROUTING_REGION
    assert ast.unparse(_keyword(decorator, "scaledown_window")) == "5 * MINUTES"


def test_the_sglang_script_follows_modals_llm_template() -> None:
    """Value by value, the ``serve.py`` Modal generates for ITS LLM endpoints
    (``openai/gpt-oss-120b`` and ``Qwen/Qwen3.6-35B-A3B-FP8``, read
    2026-09-20) — minus the per-model tuning those two carry.

    The OFFICIAL image is the headline: an LLM script that pip-installed the
    engine onto a CUDA base (what this file did while it served embeddings)
    would build from source what Modal's own recipe gets prebuilt.
    """

    module = _module("sglang")

    # Image: the official SGLang image, tagged from the catalog — so NO
    # `add_python` (it ships Python 3.12), NO `.entrypoint([])` and no engine
    # wheel.
    registry = _one_call(module, "modal.Image.from_registry")
    assert len(registry.args) == 1
    assert isinstance(registry.args[0], ast.JoinedStr), "the tag is not the YAML's"
    tag = ast.unparse(registry.args[0])
    assert tag.removeprefix("f").lstrip("\"'").startswith("lmsysorg/sglang:")
    assert "engine_version" in tag
    assert not registry.keywords, "the official image needs no add_python"
    assert _calls(module, "entrypoint") == []

    wheels = _one_call(module, "uv_pip_install")
    assert len(wheels.args) == 1
    assert "autoinference_utils_version" in ast.unparse(wheels.args[0])
    assert "sglang==" not in _source("sglang"), "the engine comes from the IMAGE"
    assert "HF_XET_HIGH_PERFORMANCE" in _image_env_keys(module)

    # The engine: tensor parallelism from the catalog, and NO speculative
    # draft model — that is per-model tuning (and the parameter is optional in
    # autoinference-utils 0.2.6).
    endpoint = _one_call(module, "SGLangEndpoint")
    keywords = {keyword.arg for keyword in endpoint.keywords}
    assert "speculative_model_path" not in keywords
    assert ast.unparse(_keyword(endpoint, "model_path")) == "SPEC['repo_id']"
    assert ast.unparse(_keyword(endpoint, "tp")) == "SPEC['n_gpus']"
    assert _keyword(endpoint, "worker_port").id == "PORT"
    assert _keyword(endpoint, "health_poll_interval").value == 5.0

    # The warm-up: two constrained completions, Modal's own payload.
    warmup = _one_call(module, "warmup_chat_completions")
    assert _keyword(warmup, "port").id == "PORT"
    assert _keyword(warmup, "successful_requests").value == 2
    assert _keyword(warmup, "request_timeout").value == 60.0
    assert _keyword(warmup, "payload").id == "WARMUP_PAYLOAD"
    assert _module_constant(module, "PORT") == 8000

    # `<gpu>:<n_gpus>`, Modal's own `f"{GPU_TYPE}:{N_GPUS}"`.
    decorator = _server_decorator(module)
    gpu = ast.unparse(_keyword(decorator, "gpu"))
    assert "SPEC['gpu']" in gpu
    assert "SPEC['n_gpus']" in gpu
    assert _keyword(decorator, "port").id == "PORT"
    assert _keyword(decorator, "min_containers").value == 0
    assert _keyword(decorator, "exit_grace_period").value == 25
    assert _keyword(decorator, "routing_region").value == MODAL_ROUTING_REGION
    assert ast.unparse(_keyword(decorator, "scaledown_window")) == "5 * MINUTES"


def test_the_sglang_warm_up_is_modals_strict_json_payload() -> None:
    """Story 4: a model that cannot do constrained JSON must never go healthy.

    The payload is Modal's ``city_facts`` one, value for value — ``strict``
    true, ``additionalProperties`` false, both keys required. Dropping any of
    those turns the warm-up into "the model answered something", which is
    exactly what ``ModalLLM`` cannot use.
    """

    payload = _module_dict(_module("sglang"), "WARMUP_PAYLOAD")

    assert payload == {
        "model": "<repo_id>",
        "messages": [{"role": "user", "content": "Reply with JSON facts about Tokyo."}],
        "max_tokens": 64,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "city_facts",
                "schema": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "population": {"type": "integer"},
                    },
                    "required": ["city", "population"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        },
    }
    schema = payload["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False


def test_the_sglang_script_carries_no_per_model_tuning() -> None:
    """Modal's two LLM recipes tune for a 120B and a 35B-MoE model: a draft
    model for speculative decoding, mamba scheduler flags, multimodal, and
    per-model image env (``SGLANG_ENABLE_JIT_DEEPGEMM`` …). A script that
    serves whatever the catalog names must assume none of it — a model that
    wants a flag says so in its entry's ``extra_server_args``."""

    source = _source("sglang")

    for forbidden in (
        "--speculative",
        "speculative_model_path=",
        "--mamba",
        "--enable-multimodal",
        "SGLANG_ENABLE_JIT_DEEPGEMM",
        "TORCHINDUCTOR_COMPILE_THREADS",
    ):
        assert forbidden not in source


def test_no_seed_asks_the_server_for_matryoshka() -> None:
    """ADR-009 §3: the client truncates client-side on EVERY path, so no entry
    flags ``is_matryoshka`` server-side — Modal's own 8B recipe does and its
    0.6B recipe does not, which is exactly why we never rely on it.

    The script's docstring carries the reason; the catalog carries no override.
    """

    assert "is_matryoshka" in _source("vllm")
    assert "is_matryoshka" not in _CONFIGS.read_text(encoding="utf-8")


def test_script_paths_match_cli_command() -> None:
    """The driver deploys a FILE: what the KIND names must exist.

    A path typo would surface as a ``modal`` error after the operator has
    already been told the deploy started (#139 exits 2 on a missing file).
    """

    assert app_script("embedding") == "deploy/modal_vllm_embedding.py"
    assert app_script("llm") == "deploy/modal_sglang_llm.py"

    for model, expected in (
        ("voyageai/voyage-4-nano", _SCRIPTS["vllm"]),
        ("LiquidAI/LFM2.5-350M", _SCRIPTS["sglang"]),
    ):
        script = modal_cli_command("deploy", get_catalog_entry(model), "app")[-1]

        assert (_APP_ROOT / script).is_file(), script
        assert _APP_ROOT / script == expected
