from __future__ import annotations

import sqlite3
from pathlib import Path


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_val, exc_tb):
        result = super().__exit__(exc_type, exc_val, exc_tb)
        self.close()
        return result


class SQLiteDatabase:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, factory=ManagedConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
