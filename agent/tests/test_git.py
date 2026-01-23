"""Unit tests for git utilities."""

import os

from tether.git import normalize_directory_path, has_git_repository


class TestNormalizeDirectoryPath:
    """Test directory path normalization."""

    def test_normalizes_relative_path(self, tmp_path) -> None:
        """Relative paths are converted to absolute."""
        os.chdir(tmp_path)
        result = normalize_directory_path("subdir")
        assert str(tmp_path / "subdir") == result

    def test_expands_home_directory(self) -> None:
        """Tilde is expanded to home directory."""
        result = normalize_directory_path("~/somedir")
        assert result.startswith("/")
        assert "~" not in result

    def test_handles_nonexistent_path(self) -> None:
        """Nonexistent paths are still normalized."""
        result = normalize_directory_path("/nonexistent/path/here")
        assert result == "/nonexistent/path/here"


class TestHasGitRepository:
    """Test git repository detection."""

    def test_returns_true_for_git_repo(self, tmp_path) -> None:
        """Returns True when .git directory exists."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        assert has_git_repository(str(tmp_path)) is True

    def test_returns_false_for_non_repo(self, tmp_path) -> None:
        """Returns False when no .git directory."""
        assert has_git_repository(str(tmp_path)) is False

    def test_returns_false_for_nonexistent_path(self) -> None:
        """Returns False for nonexistent paths."""
        assert has_git_repository("/nonexistent/path") is False

    def test_returns_false_when_git_is_file(self, tmp_path) -> None:
        """Returns False when .git is a file, not directory."""
        git_file = tmp_path / ".git"
        git_file.write_text("gitdir: /some/path")

        assert has_git_repository(str(tmp_path)) is False
