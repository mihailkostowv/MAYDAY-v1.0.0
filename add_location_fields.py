import sqlite3
from pathlib import Path

database_path = Path(__file__).with_name("mayday.db")
connection = sqlite3.connect(database_path)

try:
    columns = connection.execute("PRAGMA table_info(events)").fetchall()

    column_names = [column[1] for column in columns]

    if "latitude" not in column_names:
        connection.execute("ALTER TABLE events ADD COLUMN latitude REAL")

    if "longitude" not in column_names:
        connection.execute("ALTER TABLE events ADD COLUMN longitude REAL")

    if "source_url" not in column_names:
        connection.execute("ALTER TABLE events ADD COLUMN source_url TEXT")

    connection.commit()

finally:
    connection.close()

print("Location and source columns are ready.")
