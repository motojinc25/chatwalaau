"""CLI package for ChatWalaʻau (CTR-0033 v4, PRP-0052).

Re-exports ``main`` to preserve the entry point defined in
pyproject.toml [project.scripts].
"""

from app.cli.main import main

__all__ = ["main"]
