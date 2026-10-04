"""Enables `python -m winbrow <command>` (see winbrow/cli.py)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
