"""Data-source definitions + the one way to materialise ``SourceEntry``s.

Owns the ``SourceEntry`` discriminated union (the typed source variants and the
untyped-entry ``type`` inference) and turns committed source files
(``sources/backfill.yaml`` / ``sources/listen.yaml``) and ad-hoc CLI ``--uri``
tokens into those entries.

Per ADR-003, source definitions are operator DATA living under the repo-root
``sources/`` directory, split by cadence (backfill = one-shot, listen = polled
RSS). This module is the single place that defines and reads them so the offline
coordinator, the online URL router, and the arxiv defaults cannot drift.
"""

import functools
import typing
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator

# --- Source variants (discriminated union) ---


class SubstackRssSource(BaseModel):
    """A Substack RSS feed URL."""

    type: Literal["substack_rss"] = "substack_rss"
    uri: str = Field(min_length=1)


class SubstackArticleSource(BaseModel):
    """A Substack article URL (may live on a custom domain)."""

    type: Literal["substack_article"] = "substack_article"
    uri: str = Field(min_length=1)


class HuggingFaceDatasetSource(BaseModel):
    """A HuggingFace dataset id (NOT a URL).

    The ``uri`` is the dataset id (``namespace/name``) and is used to
    dispatch to a per-dataset ETL pipeline registered in
    ``tree.data.offline_pipeline``. Unknown dataset ids raise at dispatch time.

    Two of these fields draw an authored-vs-runtime split (#070, the config
    foundation for HF offset-window fan-out):

    * ``num_workers`` — operator-AUTHORED YAML (like ``batch_size``). The
      offset-window fan-out width: #072 dispatches ``num_workers``
      ``data-etl-worker`` runs, each ingesting one disjoint offset-window of the
      dataset. Default ``1`` ⇒ a single window covering the whole
      ``max_samples`` ⇒ today's behavior. Must be ``>= 1``.
    * ``offset`` — a dispatch-time RUNTIME coordinate, NOT authored in YAML and
      never present in ``default.yaml``. The coordinator sets it ONLY at
      dispatch via ``entry.model_copy(update={"offset": ...})`` (#072), and #071
      makes the ingest skip the first ``offset`` rows. Default ``None`` ⇒ no
      skip ⇒ today's behavior.

    The discriminated-union round-trip MUST preserve both fields: the
    coordinator serializes shards through ``run_deployment`` flow-run params
    (``model_dump()`` → JSON → ``TypeAdapter(list[SourceEntry])``), so a set
    ``offset`` round-trips as the int and ``offset=None`` round-trips as ``None``.
    """

    type: Literal["huggingface_dataset"] = "huggingface_dataset"
    uri: str = Field(min_length=1)
    max_samples: int = 10
    fetch_content: bool = False
    batch_size: int = 50
    concurrency: int = 10
    # YAML-authored offset-window fan-out width (#070). See class docstring.
    num_workers: int = Field(default=1, ge=1)
    # Dispatch-time runtime coordinate (#070), never authored in YAML. See
    # class docstring.
    offset: int | None = None


class YouTubeVideoSource(BaseModel):
    """A YouTube video URL (or 11-char video id)."""

    type: Literal["youtube_video"] = "youtube_video"
    uri: str = Field(min_length=1)


class YouTubeRssSource(BaseModel):
    """A YouTube channel feed: ``youtube.com/feeds/videos.xml?channel_id=…``."""

    type: Literal["youtube_rss"] = "youtube_rss"
    uri: str = Field(min_length=1)


class WebSource(BaseModel):
    """A generic web URL ingested via the URL dispatcher."""

    type: Literal["web"] = "web"
    uri: str = Field(min_length=1)


SourceEntry = Annotated[
    Union[
        SubstackRssSource,
        SubstackArticleSource,
        HuggingFaceDatasetSource,
        YouTubeVideoSource,
        YouTubeRssSource,
        WebSource,
    ],
    Field(discriminator="type"),
]


_YOUTUBE_HOSTS: frozenset[str] = frozenset({"youtube.com", "m.youtube.com", "youtu.be"})


def _is_youtube_host(host: str) -> bool:
    """True iff ``host`` is a recognized YouTube host (``www.`` already stripped)."""

    return host in _YOUTUBE_HOSTS


def _is_substack_subdomain(host: str) -> bool:
    """True iff ``host`` is ``substack.com`` or any ``*.substack.com`` subdomain.

    Strips a leading ``www.`` for tolerance.
    """

    if not host:
        return False
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "substack.com" or host.endswith(".substack.com")


def _host_of(uri: str) -> str:
    """Lower-cased ``netloc`` of ``uri`` with any ``www.`` prefix stripped.

    Returns an empty string if ``uri`` has no parseable host (e.g. a
    HuggingFace dataset id).
    """

    host = (urlparse(uri).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _collect_typed_substack_hosts(raw_entries: list[Any]) -> set[str]:
    """Hosts of entries explicitly typed as a Substack variant.

    Used to coerce later untyped entries on the same custom domain.
    """

    hosts: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type not in ("substack_rss", "substack_article"):
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str):
            continue
        host = _host_of(uri)
        if host:
            hosts.add(host)
    return hosts


def _normalize_untyped_entry(
    entry: dict[str, Any], substack_hosts: set[str]
) -> dict[str, Any]:
    """Add a ``type`` to an entry that has none, based on its ``uri``.

    Rules:
        - URL on a YouTube host AND path is ``/feeds/videos.xml`` AND query has
          ``channel_id`` → ``youtube_rss``.
        - URL on a YouTube host that looks like a video URL (``/watch``,
          ``/shorts/...``, or ``youtu.be/<id>``) → ``youtube_video``.
        - URL on ``*.substack.com`` (or ``substack.com``) → ``substack_article``.
        - URL whose host matches another typed Substack source's host → ``substack_article``.
        - Anything else (HTTP/HTTPS URL or otherwise) → ``web``.
    """

    uri = entry.get("uri")
    if not isinstance(uri, str):
        # Let Pydantic raise the proper validation error downstream.
        return entry

    parsed = urlparse(uri)
    host = _host_of(uri)
    path = parsed.path or ""
    query = parsed.query or ""

    if _is_youtube_host(host):
        if path == "/feeds/videos.xml" and "channel_id=" in query:
            return {**entry, "type": "youtube_rss"}
        if host == "youtu.be" or path == "/watch" or path.startswith("/shorts/"):
            return {**entry, "type": "youtube_video"}

    if _is_substack_subdomain(host) or (host and host in substack_hosts):
        inferred_type = "substack_article"
    else:
        inferred_type = "web"

    return {**entry, "type": inferred_type}


class SourcesConfig(BaseModel):
    """Flat list of typed data sources for the ingestion pipelines."""

    sources: list[SourceEntry] = []

    @model_validator(mode="before")
    @classmethod
    def _normalize_untyped_sources(cls, data: Any) -> Any:
        """Pre-validation hook: infer ``type`` for entries that lack one.

        Runs before discriminated-union validation so untyped raw dicts can
        be coerced into a typed variant. Also coerces a bare list of source
        entries into ``{"sources": <list>}`` so a source file can write the
        flat top-level-list shape directly. See module-level helpers for the
        inference rules.
        """

        # Accept the flat YAML shape (a bare top-level list, as the
        # ``sources/*.yaml`` files use) by wrapping it as ``{"sources": <list>}``.
        if isinstance(data, list):
            data = {"sources": data}

        if not isinstance(data, dict):
            return data
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, list):
            return data

        substack_hosts = _collect_typed_substack_hosts(raw_sources)

        normalized: list[Any] = []
        for entry in raw_sources:
            if isinstance(entry, dict) and "type" not in entry:
                normalized.append(_normalize_untyped_entry(entry, substack_hosts))
            else:
                normalized.append(entry)

        return {**data, "sources": normalized}


# The repo root is six path components up from this module
# (config -> tree -> src -> memory -> apps -> repo root); the committed
# ``sources/`` data dir lives there, NOT under the memory app root. This holds
# in a source checkout ONLY: a pip-installed ``tree`` (every Prefect Managed
# run) sits in site-packages, where parents[5] is the install prefix
# (``/usr/local``) and no ``sources/`` exists — hence the paths below stay
# RELATIVE so :func:`_resolve_source_path` can fall back to the cwd.
_REPO_ROOT = Path(__file__).resolve().parents[5]

# Relative on purpose — resolved per call (repo root, then cwd), NEVER frozen to
# an absolute path at import time. An absolute path skips the cwd fallback, which
# is the only branch that resolves under a managed run.
BACKFILL_PATH = Path("sources/backfill.yaml")
LISTEN_PATH = Path("sources/listen.yaml")


def _source_type_literals() -> frozenset[str]:
    """The full set of ``SourceEntry`` ``type`` discriminator literals.

    Derived from the discriminated union itself so a new variant added to
    :data:`SourceEntry` is picked up here automatically — including
    ``huggingface_dataset``. Used by :func:`parse_uri_token` to decide whether a
    ``…=TYPE`` suffix is a real type or just part of a query-string URL.
    """

    union = typing.get_args(SourceEntry)[0]
    return frozenset(
        variant.model_fields["type"].default for variant in typing.get_args(union)
    )


_SOURCE_TYPE_LITERALS = _source_type_literals()


def _resolve_source_path(path: str | Path) -> Path:
    """Resolve one source-file path to an existing file.

    Absolute paths are used verbatim. RELATIVE paths are resolved by trying the
    module-derived repo root AND the process cwd, first-existing-wins — so the
    cron's ``"sources/listen.yaml"`` resolves under local serve (cwd=
    ``apps/memory/``) and under a Prefect Cloud managed run (cwd=git-clone-root).
    A path that resolves under neither raises ``FileNotFoundError`` naming both
    attempted locations.
    """

    candidate = Path(path)
    if candidate.is_absolute():
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Source file not found: {candidate}")

    repo_root_candidate = _REPO_ROOT / candidate
    if repo_root_candidate.is_file():
        return repo_root_candidate

    cwd_candidate = Path.cwd() / candidate
    if cwd_candidate.is_file():
        return cwd_candidate

    raise FileNotFoundError(
        f"Source file {str(path)!r} not found under the module-derived repo "
        f"root ({repo_root_candidate}) or the process cwd ({cwd_candidate})."
    )


def load_sources(paths: list[str | Path]) -> list[SourceEntry]:
    """Read, validate, and concatenate source files in the given order.

    Each file is parsed as YAML (the flat top-level-list shape the source files
    use) and validated through :class:`SourcesConfig`, so untyped entries still
    infer their ``type``. Results are concatenated in ``paths`` order. Relative
    paths use the two-strategy resolution in :func:`_resolve_source_path`.
    """

    entries: list[SourceEntry] = []
    for path in paths:
        resolved = _resolve_source_path(path)
        raw = yaml.safe_load(resolved.read_text()) or []
        config = SourcesConfig.model_validate(raw)
        entries.extend(config.sources)
    return entries


@functools.cache
def default_configured_sources() -> list[SourceEntry]:
    """The default ingest set: backfill + listen, concatenated and cached.

    Cached because the offline coordinator, the online URL router, and the
    arxiv defaults all read the same set. The cache is clearable for tests via
    ``default_configured_sources.cache_clear()``.
    """

    return load_sources([BACKFILL_PATH, LISTEN_PATH])


def parse_uri_token(token: str) -> tuple[str, str | None]:
    """Parse one CLI ``--uri`` token into ``(uri, type | None)``.

    Splits on the RIGHTMOST ``=`` ONLY when the suffix is a recognized
    ``SourceEntry`` type literal (the full set, incl. ``huggingface_dataset``),
    returning ``(uri, type)``; otherwise the whole token is the uri and the type
    is ``None``. This keeps query-string URLs intact — e.g.
    ``…/feeds/videos.xml?channel_id=UC…`` → ``(that_url, None)`` — while
    ``…/feed=substack_rss`` → ``(…/feed, "substack_rss")``.
    """

    uri, separator, suffix = token.rpartition("=")
    if separator and suffix in _SOURCE_TYPE_LITERALS:
        return uri, suffix
    return token, None


def build_uri_sources(specs: list[tuple[str, str | None]]) -> list[SourceEntry]:
    """Build typed ``SourceEntry``s from already-parsed ``(uri, type | None)`` specs.

    Raw dicts (``{"uri": u}`` when the type is ``None``, else
    ``{"uri": u, "type": t}``) are normalized and validated in ONE
    :class:`SourcesConfig` pass, so omitted types are inferred via the same
    untyped-entry inference used for YAML files (youtube_rss / youtube_video /
    substack_article / web), including cross-entry substack-host inference.

    Raises ``ValueError`` if any resulting entry is a
    :class:`HuggingFaceDatasetSource`: HF ingest needs tuning fields
    (``max_samples`` / ``batch_size`` / ``num_workers`` / ``concurrency``) that a
    bare URL can't carry, so define it in a YAML file (e.g.
    ``sources/backfill.yaml``) and use ``--source-file``. Inference never yields
    HF, so this fires only on an explicit ``…=huggingface_dataset`` token. There
    is NO parallel-list / count-matching constraint — per-URI optional typing is
    intrinsic to the tuple shape.
    """

    raw = [
        {"uri": uri} if source_type is None else {"uri": uri, "type": source_type}
        for uri, source_type in specs
    ]
    config = SourcesConfig.model_validate({"sources": raw})

    hf_uris = [
        entry.uri
        for entry in config.sources
        if isinstance(entry, HuggingFaceDatasetSource)
    ]
    if hf_uris:
        raise ValueError(
            "huggingface_dataset sources cannot be built from a --uri token: "
            "HF ingest needs tuning fields (max_samples/batch_size/num_workers/"
            "concurrency). Define it in a YAML file (e.g. sources/backfill.yaml) "
            f"and use --source-file. Offending uri(s): {', '.join(hf_uris)}."
        )

    return list(config.sources)
