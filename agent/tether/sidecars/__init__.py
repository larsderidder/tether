"""Bundled TypeScript sidecar entry points.

Each .mjs file is a self-contained esbuild bundle that can be run with
``node <path>``.  The ``bundle_path`` helper resolves the absolute path
so callers don't need to know where the package is installed.
"""

from pathlib import Path


_DIR = Path(__file__).parent


def bundle_path(name: str) -> Path:
    """Return the absolute path to a bundled sidecar.

    ``name`` is one of ``"codex-sidecar"`` or ``"opencode-sidecar"``.
    Raises ``FileNotFoundError`` if the bundle has not been built.
    """
    p = _DIR / f"{name}.mjs"
    if not p.exists():
        raise FileNotFoundError(
            f"Sidecar bundle not found: {p}. "
            "Run scripts/build-sidecars.sh to build the bundles."
        )
    return p
