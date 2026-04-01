"""
YAML-based application configuration.

Loads app-level tuning parameters (model names, chunk sizes, concurrency, etc.)
from a YAML file.  Infrastructure secrets stay in settings.py / .env.

Resolution order:
    1. Path in APP_CONFIG_PATH env var
    2. configs/default.yaml (project root)
"""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel

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


class HuggingFaceArxivDatasetConfig(BaseModel):
    max_samples: int = 10
    fetch_content: bool = False
    batch_size: int = 50
    concurrency: int = 10


class SourcesConfig(BaseModel):
    substack: list[str] = []
    substack_articles: list[str] = []
    huggingface_arxiv_dataset: HuggingFaceArxivDatasetConfig = (
        HuggingFaceArxivDatasetConfig()
    )


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
