import os

from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

_env_file = os.environ.get("ENV_FILE_PATH", ".env")


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_env_file, extra="ignore")

    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_initdb_root_username: str = "tree"
    mongo_initdb_root_password: SecretStr = SecretStr("tree")
    mongo_initdb_database: str = "tree"
    mongot_port: int = 27028

    @computed_field
    @property
    def mongo_uri(self) -> SecretStr:
        return SecretStr(
            f"mongodb://{self.mongo_initdb_root_username}:"
            f"{self.mongo_initdb_root_password.get_secret_value()}"
            f"@{self.mongo_host}:{self.mongo_port}"
            f"/?directConnection=true&authSource=admin"
        )


class DedupConfig(BaseSettings):
    """Three-tier dedup thresholds (#032) + same-type guard.

    Per ``plan.md:524-530`` the resolver / dedup pipeline reads its
    thresholds from a single config object so operators can tune the
    auto-merge / human-review trade-off via env without editing YAML.
    All thresholds apply uniformly across POLE+O nodes, preferences,
    and facts.

    Field semantics:
        * ``auto_merge_threshold`` (default 0.95): combined score >=
          this AND the contradiction-judge says NO -> auto-merge
          (``same_as`` with ``status=confirmed`` equivalent).
        * ``flag_threshold`` (default 0.85): combined score in
          ``[flag_threshold, auto_merge_threshold)`` AND the
          contradiction-judge says NO -> human-review ``same_as`` with
          ``status=pending``. Also drives the contradiction-judge
          candidate window: cosine >= ``flag_threshold`` triggers the
          judge call.
        * ``fuzzy_threshold`` (default 90): rapidfuzz ratio on a 0-100
          scale. The runtime :class:`DeduplicationConfig` consumes it
          as a 0-1 float; the conversion lives in
          :func:`tree.config.app_config.load_app_config`.
        * ``match_same_type_only`` (default True): pins the
          vector-search ``filter`` to one POLE+O type at a time so a
          ``person:alice`` never merges with an ``organization:alice``.
        * ``supersession_candidate_cap`` (default 8): per-partition cap
          on the bound-candidate-set the supersession resolver feeds
          to the contradiction judge. Per the QA-revised supersession
          design (#032 fix-1), the resolver does NOT pre-filter on
          embedding cosine — instead it pulls the K most-recent active
          preferences in the ``(user_id, category)`` partition and
          calls the judge on each in turn (first contradiction wins).
          K caps LLM cost; the "most-recent active" ordering preserves
          the bi-temporal semantics. Model-agnostic, so the resolver
          works the same way under MiniLM-L6-v2 (dev) and voyage-3
          (prod).

    Env vars use the ``DEDUP_`` prefix (e.g.
    ``DEDUP_AUTO_MERGE_THRESHOLD=0.97``).
    """

    model_config = SettingsConfigDict(
        env_prefix="DEDUP_", env_file=_env_file, extra="ignore"
    )

    auto_merge_threshold: float = 0.95
    flag_threshold: float = 0.85
    fuzzy_threshold: int = 90
    match_same_type_only: bool = True
    supersession_candidate_cap: int = 8


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_env_file, extra="ignore")

    mongo: MongoSettings = MongoSettings()
    dedup: DedupConfig = DedupConfig()
    google_api_key: SecretStr = SecretStr("")
    voyage_api_key: SecretStr = SecretStr("")
    modal_embedding_api_key: SecretStr = SecretStr("")
    brightdata_api_key: SecretStr = SecretStr("")
    brightdata_unlocker_zone: str = ""
    brightdata_serp_zone: str = ""

    # --- Pinned embedding configuration (Phase 1 multi-tenancy) ---
    #
    # The embedding model identifier and the vector dimension are pinned
    # here — not in YAML / app_config — because they are dimension-coupled
    # to the Atlas Vector Search index defined under ``docker/mongot/``.
    # A mismatch between ``embedding_dim`` and the live ``vector_index``
    # ``numDimensions`` corrupts writes silently; the indexing pipeline
    # calls :func:`tree.memory.indexing.core.assert_settings_match_live_vector_index`
    # on boot to make that mismatch a hard, startup-time error rather
    # than a runtime data-loss bug. See
    # ``tracker/016-pin-embedding-model-and-dim-in-settings.groomed.md``.
    #
    # YAML (``app_config.models.embedding.*``) may still override the
    # provider/model/dimensions for local dev, but ``app_config`` logs a
    # WARNING when the YAML dimension disagrees with this pin.
    embedding_provider: str = "voyage"
    embedding_model: str = "voyage-3"
    embedding_dim: int = 1024


settings = Settings()
