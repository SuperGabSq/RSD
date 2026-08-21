"""Shared test support: paths, in-memory doubles, and the simulator harness.

Nothing in here contains assertions. Keeping it out of the phase directories means a
reader opening ``tests/phase2_domain/`` sees only Phase 2's claims, and a helper that
grows a second caller does not have to move house.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
SIMULATOR_PATH = BACKEND_DIR / "mock" / "mock_uc_server.py"

__all__ = ["BACKEND_DIR", "REPO_ROOT", "SIMULATOR_PATH"]
