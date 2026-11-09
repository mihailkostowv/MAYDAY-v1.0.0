import sqlite3
from pathlib import Path

database_path = Path(__file__).with_name("mayday.db")


event = {
    "id": 2,
    "name": "VH-UWR accident near Bourke",
    "event_date": "2012-04-29",
    "country": "Australia",
    "category": "accident",
    "aircraft": "Cessna 150M",
    "latitude": -29.7283,
    "longitude": 146.3867,
    "source_url": "https://www.atsb.gov.au/investigations/ao-2012-059",
}

connection = sqlite3.connect(database_path)

try:
    connection.execute(
        """
        INSERT INTO events (
            id, name, event_date, country, category, aircraft,
            latitude, longitude, source_url
        )
        VALUES (
            :id, :name, :event_date, :country, :category, :aircraft,
            :latitude, :longitude, :source_url
        )
        ON CONFLICT(id) DO NOTHING
    """,
        event,
    )

    connection.execute("""
        DELETE FROM events
        WHERE id = 1
          AND name = 'DEMO - Fictional event'
          AND event_date = '1940-01-01'
    """)

    connection.commit()

finally:
    connection.close()

print("Real-event seed script finished.")
