import sqlite3
from pathlib import Path

database_path = Path(__file__).with_name("mayday.db")

connection = sqlite3.connect(database_path)

connection.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        event_date TEXT,
        country TEXT,
        category TEXT NOT NULL,
        aircraft TEXT
    )
""")

connection.commit()
connection.close()

print("MAYDAY database is ready.")
