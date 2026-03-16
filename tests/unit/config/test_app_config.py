import textwrap

from twin.config.app_config import AppConfig, load_app_config


class TestLoadAppConfig:
    def test_loads_default_yaml(self):
        config = load_app_config()

        assert config.models.llm.provider == "gemini"
        assert config.models.llm.model == "gemini-2.5-flash-lite"
        assert config.models.embedding.dimensions == 512
        assert config.extraction.chunk_size == 512
        assert config.extraction.llm_concurrency == 5

    def test_loads_custom_yaml(self, tmp_path):
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            textwrap.dedent("""\
                models:
                  llm:
                    model: gemini-2.0-flash
                extraction:
                  chunk_size: 256
                  similarity_threshold: 0.9
            """)
        )

        config = load_app_config(custom)

        assert config.models.llm.model == "gemini-2.0-flash"
        assert config.models.llm.provider == "gemini"
        assert config.extraction.chunk_size == 256
        assert config.extraction.similarity_threshold == 0.9
        # Unset values keep defaults.
        assert config.extraction.chunk_overlap == 64
        assert config.models.embedding.dimensions == 768

    def test_missing_file_returns_defaults(self, tmp_path):
        config = load_app_config(tmp_path / "nonexistent.yaml")

        assert config == AppConfig()

    def test_empty_yaml_returns_defaults(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("")

        config = load_app_config(empty)

        assert config == AppConfig()

    def test_env_var_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "env.yaml"
        custom.write_text("extraction:\n  chunk_size: 1024\n")
        monkeypatch.setenv("APP_CONFIG_PATH", str(custom))

        config = load_app_config()

        assert config.extraction.chunk_size == 1024
