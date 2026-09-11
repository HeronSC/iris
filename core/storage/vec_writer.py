# File: core/storage/vec_writer.py

from __future__ import annotations

import json
import sqlite3
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        sys.stderr.write("usage: vec_writer <db_path> <table> <key_column>\n")
        return 2
    db_path, table, key_column = argv[1], argv[2], argv[3]
    if not table.isidentifier() or not key_column.isidentifier():
        sys.stderr.write("table and key column must be plain identifiers\n")
        return 2
    #! @allow-local-import
    import sqlite_vec

    conn = sqlite3.connect(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA busy_timeout=5000")
        rows: list[tuple[int, bytes]] = []
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            key, vector = json.loads(line)
            rows.append((int(key), sqlite_vec.serialize_float32([float(value) for value in vector])))
        if rows:
            conn.executemany(f"INSERT OR REPLACE INTO {table}({key_column}, embedding) VALUES (?, ?)", rows)
            conn.commit()
        sys.stdout.write(f"{len(rows)}\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
