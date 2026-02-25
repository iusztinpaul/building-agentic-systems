from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    mongo_port: int = 27017
    mongo_initdb_root_username: str = "twin"
    mongo_initdb_root_password: str = "twin"
    mongo_initdb_database: str = "twin"
    mongot_port: int = 27028

    @computed_field
    @property
    def mongo_uri(self) -> str:
        return (
            f"mongodb://{self.mongo_initdb_root_username}:"
            f"{self.mongo_initdb_root_password}"
            f"@localhost:{self.mongo_port}"
            f"/?directConnection=true&authSource=admin"
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="")

    mongo: MongoSettings = MongoSettings()


settings = Settings()
