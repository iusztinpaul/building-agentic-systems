"""
YAML-based application configuration.

Loads app-level tuning parameters (model names, chunk sizes, concurrency, etc.)
from a YAML file.  Infrastructure secrets stay in settings.py / .env.

Resolution order:
    1. Path in APP_CONFIG_PATH env var
    2. configs/default.yaml (memory app root: ``apps/memory/``)
"""

import os
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "configs" / "default.yaml"


# --- Pydantic models for typed access ---


class LLMConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-2.5-flash-lite"


class EmbeddingConfig(BaseModel):
    provider: str = "gemini"
    model: str = "text-embedding-004"
    dimensions: int = 768


class ModelsConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()


class ExtractionConfig(BaseModel):
    chunk_size: int = 512
    chunk_overlap: int = 64
    llm_concurrency: int = 5
    similarity_threshold: float = 0.85


class QueryConfig(BaseModel):
    top_k: int = 10
    max_hops: int = 1
    rrf_k: int = 60
    embedding_batch_size: int = 64


# --- Source variants (discriminated union) ---


class SubstackRssSource(BaseModel):
    """A Substack RSS feed URL."""

    type: Literal["substack_rss"] = "substack_rss"
    uri: str = Field(min_length=1)


class SubstackArticleSource(BaseModel):
    """A Substack article URL (may live on a custom domain)."""

    type: Literal["substack_article"] = "substack_article"
    uri: str = Field(min_length=1)


class HuggingFaceArxivSource(BaseModel):
    """A HuggingFace arxiv-metadata dataset id (NOT a URL)."""

    type: Literal["huggingface_arxiv"] = "huggingface_arxiv"
    uri: str = Field(min_length=1)
    max_samples: int = 10
    fetch_content: bool = False
    batch_size: int = 50
    concurrency: int = 10


class WebSource(BaseModel):
    """A generic web URL ingested via the URL dispatcher."""

    type: Literal["web"] = "web"
    uri: str = Field(min_length=1)


SourceEntry = Annotated[
    Union[
        SubstackRssSource,
        SubstackArticleSource,
        HuggingFaceArxivSource,
        WebSource,
    ],
    Field(discriminator="type"),
]


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
        - URL on ``*.substack.com`` (or ``substack.com``) → ``substack_article``.
        - URL whose host matches another typed Substack source's host → ``substack_article``.
        - Anything else (HTTP/HTTPS URL or otherwise) → ``web``.
    """

    uri = entry.get("uri")
    if not isinstance(uri, str):
        # Let Pydantic raise the proper validation error downstream.
        return entry

    host = _host_of(uri)
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
        entries into ``{"sources": <list>}`` so the YAML can write the flat
        shape directly under ``AppConfig.sources``. See module-level helpers
        for the inference rules.
        """

        # Accept the flat YAML shape (``sources: [...]`` at the AppConfig
        # level) by wrapping a bare list as ``{"sources": <list>}``.
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


class MCPConfig(BaseModel):
    max_retries: int = 1
    max_results: int = 10


class AppConfig(BaseModel):
    sources: SourcesConfig = SourcesConfig()
    models: ModelsConfig = ModelsConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    query: QueryConfig = QueryConfig()
    mcp: MCPConfig = MCPConfig()


def load_app_config(path: str | Path | None = None) -> AppConfig:
    """Load application config from a YAML file.

    Args:
        path: Explicit path to a YAML file. Falls back to APP_CONFIG_PATH
              env var, then configs/default.yaml.
    """

    config_path = Path(path or os.environ.get("APP_CONFIG_PATH", _DEFAULT_CONFIG_PATH))

    if not config_path.exists():
        return AppConfig()

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    return AppConfig.model_validate(raw)


app_config = load_app_config()
