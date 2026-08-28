"""Shared test helper - NOT a test module (no `test_` functions, so it adds
nothing to the test count validate_repo.py checks).

`hermetic_settings()` guarantees every agent test reads the shipped
`.example.yaml` files, never a hotel's own edited `config/agent.yaml` or
`config/hotel.yaml` - see factory/workflows/build-repo.md section 5, "tests
never read the live config". It copies both `.example.yaml` files into a
temp directory (stripped of the `.example` suffix) and points
`AGENT_CONFIG_DIR` at it before calling `load_settings(demo=True)`, which
also forces the `mock` provider, `shadow` mode and the `mock` adapter for
every system.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def hermetic_settings(tmp_path, monkeypatch, **kwargs):
    """A `Settings` built only from the committed `.example.yaml` files.

    Extra ``**kwargs`` (e.g. ``dry_run=True``) pass straight through to
    ``load_settings`` alongside ``demo=True``.
    """
    from core.config import load_settings

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "config" / "hotel.example.yaml", cfg_dir / "hotel.yaml")
    shutil.copy(REPO_ROOT / "config" / "agent.example.yaml", cfg_dir / "agent.yaml")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg_dir))
    return load_settings(demo=True, **kwargs)
