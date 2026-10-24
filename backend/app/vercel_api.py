"""Entrypoint of the Vercel service ``api`` (see vercel.json and pyproject.toml)."""

from .remote_app import create_remote_app

app = create_remote_app()
