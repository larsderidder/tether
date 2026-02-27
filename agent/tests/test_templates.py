"""Tests for session template loading and resolution."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from tether.templates import (
    TemplateError,
    find_template,
    list_templates,
    load_template,
    resolve_template,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_template(directory: Path, name: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / f"{name}.yaml"
    f.write_text(textwrap.dedent(content))
    return f


# ---------------------------------------------------------------------------
# load_template
# ---------------------------------------------------------------------------


class TestLoadTemplate:
    def test_loads_valid_template(self, tmp_path):
        f = write_template(
            tmp_path,
            "myproj",
            """
            clone_url: git@github.com:user/repo.git
            branch: main
            adapter: claude_auto
            """,
        )
        data = load_template(f)
        assert data["clone_url"] == "git@github.com:user/repo.git"
        assert data["branch"] == "main"
        assert data["adapter"] == "claude_auto"

    def test_loads_empty_template(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        data = load_template(f)
        assert data == {}

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(TemplateError, match="Cannot read"):
            load_template(tmp_path / "nonexistent.yaml")

    def test_raises_on_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text(":\n: [\n")
        with pytest.raises(TemplateError, match="Invalid YAML"):
            load_template(f)

    def test_raises_on_non_mapping(self, tmp_path):
        f = tmp_path / "list.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(TemplateError, match="must be a YAML mapping"):
            load_template(f)

    def test_raises_on_unknown_keys(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("unknown_key: value\n")
        with pytest.raises(TemplateError, match="Unknown key"):
            load_template(f)

    def test_all_known_keys_accepted(self, tmp_path):
        f = write_template(
            tmp_path,
            "full",
            """
            name: My Project
            clone_url: https://github.com/u/r.git
            branch: develop
            adapter: claude_auto
            approval_mode: 2
            platform: telegram
            auto_branch: true
            auto_checkpoint: true
            shallow: false
            directory: /tmp/mydir
            """,
        )
        data = load_template(f)
        assert data["name"] == "My Project"
        assert data["approval_mode"] == 2
        assert data["auto_checkpoint"] is True

    def test_loads_yml_extension(self, tmp_path):
        f = tmp_path / "myproj.yml"
        f.write_text("adapter: opencode\n")
        data = load_template(f)
        assert data["adapter"] == "opencode"


# ---------------------------------------------------------------------------
# find_template
# ---------------------------------------------------------------------------


class TestFindTemplate:
    def test_finds_by_name_in_user_config(self, tmp_path):
        tpl_dir = tmp_path / "templates"
        write_template(tpl_dir, "myproj", "adapter: claude_auto\n")

        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            found = find_template("myproj")
        assert found is not None
        assert found.name == "myproj.yaml"

    def test_finds_yml_extension(self, tmp_path):
        tpl_dir = tmp_path / "templates"
        tpl_dir.mkdir()
        f = tpl_dir / "myproj.yml"
        f.write_text("adapter: opencode\n")

        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            found = find_template("myproj")
        assert found is not None
        assert found.name == "myproj.yml"

    def test_returns_none_when_not_found(self, tmp_path):
        with patch("tether.templates._search_dirs", return_value=[tmp_path]):
            result = find_template("nonexistent")
        assert result is None

    def test_finds_explicit_path(self, tmp_path):
        f = write_template(tmp_path, "tpl", "adapter: pi\n")
        found = find_template(str(f))
        assert found == f

    def test_returns_none_for_missing_explicit_path(self, tmp_path):
        result = find_template(str(tmp_path / "no.yaml"))
        assert result is None

    def test_explicit_path_detected_by_slash(self, tmp_path):
        f = write_template(tmp_path, "tpl", "adapter: pi\n")
        # Path with slash triggers explicit-path mode
        found = find_template(str(f))
        assert found == f

    def test_first_match_wins_across_dirs(self, tmp_path):
        dir1 = tmp_path / "d1"
        dir2 = tmp_path / "d2"
        write_template(dir1, "proj", "adapter: claude_auto\n")
        write_template(dir2, "proj", "adapter: opencode\n")

        with patch("tether.templates._search_dirs", return_value=[dir1, dir2]):
            found = find_template("proj")
        data = load_template(found)
        assert data["adapter"] == "claude_auto"


# ---------------------------------------------------------------------------
# list_templates
# ---------------------------------------------------------------------------


class TestListTemplates:
    def test_returns_empty_when_no_dirs(self, tmp_path):
        with patch("tether.templates._search_dirs", return_value=[tmp_path / "empty"]):
            result = list_templates()
        assert result == []

    def test_lists_templates_from_dir(self, tmp_path):
        tpl_dir = tmp_path / "templates"
        write_template(tpl_dir, "alpha", "adapter: claude_auto\n")
        write_template(tpl_dir, "beta", "adapter: opencode\n")

        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = list_templates()

        names = [t["name"] for t in result]
        assert "alpha" in names
        assert "beta" in names

    def test_deduplicates_across_dirs(self, tmp_path):
        dir1 = tmp_path / "d1"
        dir2 = tmp_path / "d2"
        write_template(dir1, "proj", "adapter: claude_auto\n")
        write_template(dir2, "proj", "adapter: opencode\n")

        with patch("tether.templates._search_dirs", return_value=[dir1, dir2]):
            result = list_templates()

        names = [t["name"] for t in result]
        assert names.count("proj") == 1

    def test_result_contains_name_path_source(self, tmp_path):
        tpl_dir = tmp_path / "templates"
        f = write_template(tpl_dir, "myproj", "adapter: pi\n")

        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = list_templates()

        assert len(result) == 1
        assert result[0]["name"] == "myproj"
        assert result[0]["path"] == str(f)
        assert result[0]["source"] == str(tpl_dir)


# ---------------------------------------------------------------------------
# resolve_template
# ---------------------------------------------------------------------------


class TestResolveTemplate:
    def _make_template(self, tmp_path, content: str) -> str:
        tpl_dir = tmp_path / "templates"
        write_template(tpl_dir, "proj", content)
        return "proj"

    def test_resolves_basic_fields(self, tmp_path):
        name = self._make_template(
            tmp_path,
            """
            clone_url: git@github.com:u/r.git
            branch: main
            adapter: claude_auto
            """,
        )
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name)

        assert result["clone_url"] == "git@github.com:u/r.git"
        assert result["clone_branch"] == "main"
        assert result["adapter"] == "claude_auto"

    def test_normalises_branch_to_clone_branch(self, tmp_path):
        name = self._make_template(tmp_path, "branch: develop\n")
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name)
        assert result["clone_branch"] == "develop"
        assert "branch" not in result

    def test_overrides_win_over_template(self, tmp_path):
        name = self._make_template(
            tmp_path,
            "adapter: claude_auto\nplatform: telegram\n",
        )
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name, overrides={"adapter": "opencode"})
        assert result["adapter"] == "opencode"
        assert result["platform"] == "telegram"

    def test_none_overrides_do_not_replace_template_values(self, tmp_path):
        name = self._make_template(tmp_path, "adapter: claude_auto\n")
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name, overrides={"adapter": None})
        assert result["adapter"] == "claude_auto"

    def test_raises_on_missing_template(self, tmp_path):
        with patch("tether.templates._search_dirs", return_value=[tmp_path]):
            with pytest.raises(TemplateError, match="not found"):
                resolve_template("nonexistent")

    def test_auto_branch_defaults_false(self, tmp_path):
        name = self._make_template(tmp_path, "adapter: pi\n")
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name)
        assert result["auto_branch"] is False

    def test_auto_branch_from_template(self, tmp_path):
        name = self._make_template(tmp_path, "auto_branch: true\n")
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name)
        assert result["auto_branch"] is True

    def test_approval_mode_included(self, tmp_path):
        name = self._make_template(tmp_path, "approval_mode: 2\n")
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name)
        assert result["approval_mode"] == 2

    def test_template_name_field_in_result(self, tmp_path):
        name = self._make_template(tmp_path, "name: My Project\nadapter: pi\n")
        tpl_dir = tmp_path / "templates"
        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            result = resolve_template(name)
        assert result["template_name"] == "My Project"


# ---------------------------------------------------------------------------
# CLI: cmd_templates_list and cmd_templates_show
# ---------------------------------------------------------------------------


class TestCmdTemplatesList:
    def test_prints_templates(self, tmp_path, capsys):
        tpl_dir = tmp_path / "templates"
        write_template(tpl_dir, "alpha", "adapter: claude_auto\n")

        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            from tether.cli_client import cmd_templates_list
            cmd_templates_list()

        out = capsys.readouterr().out
        assert "alpha" in out

    def test_prints_no_templates_message(self, tmp_path, capsys):
        with patch("tether.templates._search_dirs", return_value=[tmp_path / "empty"]):
            from tether.cli_client import cmd_templates_list
            cmd_templates_list()

        out = capsys.readouterr().out
        assert "No templates found" in out


class TestCmdTemplatesShow:
    def test_shows_template_contents(self, tmp_path, capsys):
        tpl_dir = tmp_path / "templates"
        write_template(tpl_dir, "myproj", "adapter: opencode\n")

        with patch("tether.templates._search_dirs", return_value=[tpl_dir]):
            from tether.cli_client import cmd_templates_show
            cmd_templates_show("myproj")

        out = capsys.readouterr().out
        assert "opencode" in out
        assert "myproj" in out

    def test_exits_on_missing_template(self, tmp_path):
        with patch("tether.templates._search_dirs", return_value=[tmp_path]):
            from tether.cli_client import cmd_templates_show
            with pytest.raises(SystemExit):
                cmd_templates_show("nonexistent")


# ---------------------------------------------------------------------------
# CLI arg parsing: --template flag on `tether new`
# ---------------------------------------------------------------------------


class TestCliNewTemplateFlag:
    def test_template_flag_parsed(self):
        import argparse
        from tether.cli import main
        import sys

        captured = {}

        def fake_run_templates(args):
            pass

        def fake_run_client(args):
            captured["template"] = getattr(args, "template", None)

        with patch("tether.cli._run_client", fake_run_client), \
             patch("tether.cli._apply_connection_args", lambda args: None):
            main(["new", "--template", "myproj"])

        assert captured["template"] == "myproj"

    def test_no_template_flag_is_none(self):
        captured = {}

        def fake_run_client(args):
            captured["template"] = getattr(args, "template", None)

        with patch("tether.cli._run_client", fake_run_client), \
             patch("tether.cli._apply_connection_args", lambda args: None):
            from tether.cli import main
            main(["new", "."])

        assert captured["template"] is None
