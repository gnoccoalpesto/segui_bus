"""Pytest bootstrap.

Loads .env before any test module is imported. Flags read at import time (such
as SEGUI_BUS_LIVE) can therefore live in .env instead of the shell, so "Run
Test" from the VS Code Test Explorer behaves like the command line.
"""

from pathlib import Path

from dotenv import load_dotenv

# override=False: a real environment variable still wins over .env.
load_dotenv(Path(__file__).with_name(".env"), override=False)
