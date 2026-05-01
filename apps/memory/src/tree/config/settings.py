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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=_env_file, extra="ignore")

    mongo: MongoSettings = MongoSettings()
    google_api_key: SecretStr = SecretStr("")
    voyage_api_key: SecretStr = SecretStr("")
    modal_embedding_api_key: SecretStr = SecretStr("")
    brightdata_api_key: SecretStr = SecretStr("")
    brightdata_unlocker_zone: str = ""


settings = Settings()
