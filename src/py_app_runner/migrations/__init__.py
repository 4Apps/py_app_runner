"""Tracked SQL migrations: apply only the files a database has not seen yet."""

from py_app_runner.migrations.discovery import MigrationError

__all__ = ["MigrationError"]
