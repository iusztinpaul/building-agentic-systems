"""Shared source loader — the one way to materialise ``SourceEntry``s.

Turns committed source files (``sources/backfill.yaml`` / ``sources/listen.yaml``)
and ad-hoc CLI ``--uri`` tokens into the typed ``SourceEntry`` discriminated union
defined in :mod:`tree.config.app_config`. This module imports those models
one-way (no cycle); it does NOT relocate them.

Per ADR-003, source definitions are operator DATA living under the repo-root
``sources/`` directory, split by cadence (backfill = one-shot, listen = polled
RSS). This loader is the single place that reads them so the offline
orchestrator, the online URL router, and the arxiv defaults cannot drift.
"""

import functools
import typing
from pathlib import Path

import yaml

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourceEntry,
    SourcesConfig,
)

# The repo root is six path components up from this module
# (config -> tree -> src -> memory -> apps -> repo root); the committed
# ``sources/`` data dir lives there, NOT under the memory app root.
_REPO_ROOT = Path(__file__).resolve().parents[5]

SOURCES_DIR = _REPO_ROOT / "sources"
BACKFILL_PATH = SOURCES_DIR / "backfill.yaml"
LISTEN_PATH = SOURCES_DIR / "listen.yaml"


def _source_type_literals() -> frozenset[str]:
    """The full set of ``SourceEntry`` ``type`` discriminator literals.

    Derived from the discriminated union itself so a new variant added to
    ``app_config.SourceEntry`` is picked up here automatically — including
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

    Cached because the offline orchestrator, the online URL router, and the
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
