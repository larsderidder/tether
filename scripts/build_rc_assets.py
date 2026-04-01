#!/usr/bin/env python3
"""Build release-candidate assets for .deb and Homebrew installs."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


WHEEL_RE = re.compile(
    r"^(?P<dist>.+)-(?P<version>[^-]+)-[^-]+-[^-]+-[^-]+\.whl$"
)


def release_tag_to_python_version(release_tag: str) -> str:
    return release_tag.lstrip("v").replace("-rc", "rc")


def validate_release_tag_matches_package_version(
    release_tag: str,
    package_version: str,
) -> None:
    expected_version = release_tag_to_python_version(release_tag)
    if expected_version != package_version:
        raise SystemExit(
            "release tag/version mismatch: "
            f"tag {release_tag!r} expects wheel version {expected_version!r}, "
            f"found {package_version!r}"
        )


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_wheel_metadata(path: Path) -> tuple[str, str]:
    match = WHEEL_RE.match(path.name)
    if not match:
        raise ValueError(f"unexpected wheel filename: {path.name}")
    return match.group("dist"), match.group("version")


def debian_version(release_tag: str, package_version: str) -> str:
    tag = release_tag.lstrip("v")
    if tag and tag != package_version:
        return tag.replace("-rc", "~rc")
    return package_version


def build_wheelhouse(
    python_bin: str,
    primary_wheel: Path,
    wheelhouse_dir: Path,
    project_name: str,
    install_extras: str,
    extra_requirements: list[str],
    extra_wheels: list[Path],
) -> None:
    if wheelhouse_dir.exists():
        shutil.rmtree(wheelhouse_dir)
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    install_target = (
        f"{project_name}[{install_extras}] @ {primary_wheel.resolve().as_uri()}"
        if install_extras
        else primary_wheel.resolve().as_uri()
    )
    with tempfile.TemporaryDirectory(prefix="wheelhouse-seed-") as seed_root:
        seed_dir = Path(seed_root)
        shutil.copy2(primary_wheel, seed_dir / primary_wheel.name)
        for wheel in extra_wheels:
            shutil.copy2(wheel, seed_dir / wheel.name)
        download_cmd = [
            python_bin,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheelhouse_dir),
            "--only-binary=:all:",
            "--find-links",
            str(seed_dir),
            install_target,
            *extra_requirements,
            *(wheel.resolve().as_uri() for wheel in extra_wheels),
        ]
        run(download_cmd)
    shutil.copy2(primary_wheel, wheelhouse_dir / primary_wheel.name)
    for wheel in extra_wheels:
        shutil.copy2(wheel, wheelhouse_dir / wheel.name)


def build_wheelhouse_archive(wheelhouse_dir: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in sorted(wheelhouse_dir.iterdir()):
            tar.add(item, arcname=item.name)


def create_deb(
    *,
    package_name: str,
    display_name: str,
    version: str,
    project_name: str,
    install_extras: str,
    primary_wheel_name: str,
    wheelhouse_dir: Path,
    command_aliases: dict[str, str],
    output_path: Path,
) -> None:
    package_dir = Path(tempfile.mkdtemp(prefix=f"{package_name}-deb-"))
    try:
        debian_dir = package_dir / "DEBIAN"
        debian_dir.mkdir(parents=True, exist_ok=True)
        share_dir = package_dir / "usr" / "share" / package_name / "wheelhouse"
        share_dir.mkdir(parents=True, exist_ok=True)
        for wheel in sorted(wheelhouse_dir.iterdir()):
            shutil.copy2(wheel, share_dir / wheel.name)

        control = "\n".join(
            [
                f"Package: {package_name}",
                f"Version: {version}",
                "Section: utils",
                "Priority: optional",
                "Architecture: all",
                "Maintainer: Codex <codex@example.invalid>",
                "Depends: bash, python3, python3-venv",
                f"Description: {display_name} release candidate package",
                f" {display_name} packaged as a self-contained RC install.",
                "",
            ]
        )
        (debian_dir / "control").write_text(control, encoding="utf-8")

        link_commands = "\n".join(
            [
                f'ln -sf "$VENV/bin/{src}" "/usr/local/bin/{dst}"'
                for src, dst in command_aliases.items()
            ]
        )
        unlink_commands = "\n".join(
            [f'rm -f "/usr/local/bin/{dst}"' for dst in command_aliases.values()]
        )
        postinst = "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                f'PREFIX="/opt/{package_name}"',
                'VENV="$PREFIX/venv"',
                f'SHARE="/usr/share/{package_name}/wheelhouse"',
                f'INSTALL_TARGET="{project_name}'
                + (f'[{install_extras}]' if install_extras else "")
                + f' @ file://$SHARE/{primary_wheel_name}"',
                'rm -rf "$VENV"',
                'mkdir -p "$PREFIX" /usr/local/bin',
                'python3 -m venv "$VENV"',
                '"$VENV/bin/pip" install --no-index --find-links="$SHARE" "$INSTALL_TARGET"',
                link_commands,
                "",
            ]
        )
        (debian_dir / "postinst").write_text(postinst, encoding="utf-8")
        os.chmod(debian_dir / "postinst", 0o755)

        postrm = "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                unlink_commands,
                'if [[ "${1:-}" == "purge" ]]; then',
                f'  rm -rf "/opt/{package_name}" "/usr/share/{package_name}"',
                "fi",
                "",
            ]
        )
        (debian_dir / "postrm").write_text(postrm, encoding="utf-8")
        os.chmod(debian_dir / "postrm", 0o755)

        run(["dpkg-deb", "--build", str(package_dir), str(output_path)])
    finally:
        shutil.rmtree(package_dir, ignore_errors=True)


def ruby_class_name(package_name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", package_name))


def create_formula(
    *,
    package_name: str,
    display_name: str,
    homepage: str,
    version: str,
    wheelhouse_asset_url: str,
    wheelhouse_sha256: str,
    project_name: str,
    install_extras: str,
    primary_wheel_name: str,
    command_aliases: dict[str, str],
    formula_path: Path,
) -> None:
    class_name = ruby_class_name(package_name)
    link_commands = "\n".join(
        [
            f'    bin.install_symlink libexec/"bin/{src}" => "{dst}"'
            for src, dst in command_aliases.items()
        ]
    )
    test_command = next(iter(command_aliases.values()))
    content = "\n".join(
        [
            "class " + class_name + " < Formula",
            '  include Language::Python::Virtualenv',
            f'  desc "{display_name} release candidate"',
            f'  homepage "{homepage}"',
            f'  url "{wheelhouse_asset_url}"',
            f'  sha256 "{wheelhouse_sha256}"',
            f'  version "{version}"',
            '  depends_on "python@3.12"',
            "",
            "  def install",
            '    venv = virtualenv_create(libexec, "python3.12")',
            '    system libexec/"bin/pip", "install", "--no-index", "--find-links=#{buildpath}", '
            + f'"{project_name}'
            + (f'[{install_extras}]' if install_extras else "")
            + f' @ file://#{{buildpath}}/{primary_wheel_name}"',
            link_commands,
            "  end",
            "",
            "  test do",
            f'    assert_match "usage:", shell_output("#{{bin}}/{test_command} --help")',
            "  end",
            "end",
            "",
        ]
    )
    formula_path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--package-name", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--homepage", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--repo-owner", required=True)
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--install-extras", default="")
    parser.add_argument(
        "--extra-requirement",
        action="append",
        default=[],
        help="Additional requirement to bundle in the wheelhouse",
    )
    parser.add_argument(
        "--extra-wheel",
        action="append",
        default=[],
        type=Path,
        help="Additional wheel file to seed into the wheelhouse",
    )
    parser.add_argument(
        "--primary-command",
        action="append",
        default=[],
        help="Command alias mapping in SRC=DST form",
    )
    parser.add_argument("--python-bin", default="python3")
    args = parser.parse_args()

    wheels = sorted(args.dist_dir.glob("*.whl"))
    if not wheels:
        raise SystemExit(f"no wheel files found in {args.dist_dir}")
    primary_wheel = wheels[0]
    _, package_version = parse_wheel_metadata(primary_wheel)
    validate_release_tag_matches_package_version(args.release_tag, package_version)
    command_aliases: dict[str, str] = {}
    for item in args.primary_command:
        src, sep, dst = item.partition("=")
        if not sep or not src or not dst:
            raise SystemExit(f"invalid --primary-command value: {item}")
        command_aliases[src] = dst
    if not command_aliases:
        raise SystemExit("at least one --primary-command mapping is required")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    wheelhouse_dir = args.output_dir / "wheelhouse"
    build_wheelhouse(
        args.python_bin,
        primary_wheel,
        wheelhouse_dir,
        args.project_name,
        args.install_extras,
        args.extra_requirement,
        args.extra_wheel,
    )

    wheelhouse_asset_name = f"{args.package_name}-{args.release_tag}-wheelhouse.tar.gz"
    wheelhouse_archive = args.output_dir / wheelhouse_asset_name
    build_wheelhouse_archive(wheelhouse_dir, wheelhouse_archive)

    wheelhouse_asset_url = (
        f"https://github.com/{args.repo_owner}/{args.repo_name}/releases/download/"
        f"{args.release_tag}/{wheelhouse_asset_name}"
    )
    wheelhouse_digest = sha256(wheelhouse_archive)

    formula_version = args.release_tag.lstrip("v")
    formula_path = args.output_dir / f"{args.package_name}.rb"
    create_formula(
        package_name=args.package_name,
        display_name=args.display_name,
        homepage=args.homepage,
        version=formula_version,
        wheelhouse_asset_url=wheelhouse_asset_url,
        wheelhouse_sha256=wheelhouse_digest,
        project_name=args.project_name,
        install_extras=args.install_extras,
        primary_wheel_name=primary_wheel.name,
        command_aliases=command_aliases,
        formula_path=formula_path,
    )

    deb_path = args.output_dir / (
        f"{args.package_name}_{debian_version(args.release_tag, package_version)}_all.deb"
    )
    create_deb(
        package_name=args.package_name,
        display_name=args.display_name,
        version=debian_version(args.release_tag, package_version),
        project_name=args.project_name,
        install_extras=args.install_extras,
        primary_wheel_name=primary_wheel.name,
        wheelhouse_dir=wheelhouse_dir,
        command_aliases=command_aliases,
        output_path=deb_path,
    )

    print(f"Built wheelhouse: {wheelhouse_archive}")
    print(f"Built formula:    {formula_path}")
    print(f"Built deb:        {deb_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
