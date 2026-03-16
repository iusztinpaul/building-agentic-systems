from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_initdb_root_username: str = "twin"
    mongo_initdb_root_password: SecretStr = SecretStr("twin")
    mongo_initdb_database: str = "twin"
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
    model_config = SettingsConfigDict(env_prefix="")

    mongo: MongoSettings = MongoSettings()
    google_api_key: SecretStr = SecretStr("")
    embedding_api_key: SecretStr = SecretStr("EMPTY")
    embedding_base_url: str = "http://localhost:8000/v1"


settings = Settings()
