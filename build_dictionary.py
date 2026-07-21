from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "resources" / "ecdict.csv"
TARGET = ROOT / "resources" / "ecdict.db"


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"Dictionary source not found: {SOURCE}")

    if TARGET.exists():
        TARGET.unlink()

    csv.field_size_limit(16 * 1024 * 1024)
    db = sqlite3.connect(TARGET)
    db.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE entries (
            word TEXT PRIMARY KEY COLLATE NOCASE,
            phonetic TEXT NOT NULL DEFAULT '',
            definition TEXT NOT NULL DEFAULT '',
            translation TEXT NOT NULL DEFAULT '',
            pos TEXT NOT NULL DEFAULT '',
            exchange TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        """
    )

    count = 0
    batch: list[tuple[str, str, str, str, str, str]] = []
    with SOURCE.open("r", encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            word = (row.get("word") or "").strip()
            if not word:
                continue
            batch.append(
                (
                    word,
                    (row.get("phonetic") or "").strip(),
                    (row.get("definition") or "").strip(),
                    (row.get("translation") or "").strip(),
                    (row.get("pos") or "").strip(),
                    (row.get("exchange") or "").strip(),
                )
            )
            if len(batch) >= 5000:
                db.executemany(
                    "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)", batch
                )
                count += len(batch)
                batch.clear()

    if batch:
        db.executemany("INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?)", batch)
        count += len(batch)

    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"Built {TARGET} with {count:,} entries ({TARGET.stat().st_size / 1024 / 1024:.1f} MiB)")


if __name__ == "__main__":
    main()
