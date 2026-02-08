"""Unit tests for config module."""

import os

import pytest

from tether.config import config_dir, data_dir_default, load_config, parse_env_file


class TestParseEnvFile:
    """Test .env file parsing."""

    def test_simple_key_value(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("FOO=bar\nBAZ=qux\n")
        result = parse_env_file(f)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_double_quoted_value(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text('KEY="hello world"\n')
        assert parse_env_file(f) == {"KEY": "hello world"}

    def test_single_quoted_value(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY='hello world'\n")
        assert parse_env_file(f) == {"KEY": "hello world"}

    def test_export_prefix(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("export MY_VAR=123\n")
        assert parse_env_file(f) == {"MY_VAR": "123"}

    def test_comments_and_blank_lines(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# comment\n\nKEY=val\n  # indented comment\n")
        assert parse_env_file(f) == {"KEY": "val"}

    def test_inline_comment(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=value # this is a comment\n")
        assert parse_env_file(f) == {"KEY": "value"}

    def test_no_inline_comment_in_quotes(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text('KEY="value # not a comment"\n')
        assert parse_env_file(f) == {"KEY": "value # not a comment"}

    def test_empty_value(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=\n")
        assert parse_env_file(f) == {"KEY": ""}

    def test_missing_file(self, tmp_path):
        result = parse_env_file(tmp_path / "nonexistent")
        assert result == {}

    def test_no_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("INVALID_LINE\n")
        assert parse_env_file(f) == {}

    def test_spaces_around_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY = value\n")
        assert parse_env_file(f) == {"KEY": "value"}


class TestLoadConfig:
    """Test load_config precedence."""

    def test_loads_from_user_config(self, tmp_path, monkeypatch):
        config = tmp_path / "config"
        config.mkdir()
        (config / "tether").mkdir()
        (config / "tether" / "config.env").write_text("TEST_LOAD_CFG=from_user\n")

        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TEST_LOAD_CFG", raising=False)

        load_config()
        assert os.environ["TEST_LOAD_CFG"] == "from_user"

        # Cleanup
        monkeypatch.delenv("TEST_LOAD_CFG", raising=False)

    def test_local_env_overrides_user_config(self, tmp_path, monkeypatch):
        config = tmp_path / "config"
        config.mkdir()
        (config / "tether").mkdir()
        (config / "tether" / "config.env").write_text("TEST_OVERRIDE=user\n")

        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / ".env").write_text("TEST_OVERRIDE=local\n")

        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        monkeypatch.chdir(workdir)
        monkeypatch.delenv("TEST_OVERRIDE", raising=False)

        load_config()
        assert os.environ["TEST_OVERRIDE"] == "local"

        monkeypatch.delenv("TEST_OVERRIDE", raising=False)

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / ".env").write_text("TEST_PREC=from_file\n")

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        monkeypatch.chdir(workdir)
        monkeypatch.setenv("TEST_PREC", "from_env")

        load_config()
        assert os.environ["TEST_PREC"] == "from_env"

    def test_missing_files_no_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
        monkeypatch.chdir(tmp_path)
        # Should not raise
        load_config()


class TestConfigDir:
    """Test config_dir helper."""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        from pathlib import Path

        expected = Path.home() / ".config" / "tether"
        assert config_dir() == expected

    def test_xdg_override(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
        from pathlib import Path

        assert config_dir() == Path("/custom/config/tether")


class TestDataDirDefault:
    """Test data_dir_default helper."""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        from pathlib import Path

        expected = Path.home() / ".local" / "share" / "tether"
        assert data_dir_default() == expected

    def test_xdg_override(self, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        from pathlib import Path

        assert data_dir_default() == Path("/custom/data/tether")
