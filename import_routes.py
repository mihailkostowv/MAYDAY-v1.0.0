import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import urlparse


def valid_point(point):
    return isinstance(point, dict) and all(
        type(point.get(key)) in (int, float)
        and math.isfinite(point[key])
        and abs(point[key]) <= limit
        for key, limit in [("latitude", 90), ("longitude", 180)]
    )


def source_url(value):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlparse(value)
        parsed.port
        return (
            parsed.scheme in {"https", "http"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def read_snapshot(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    records = payload["records"]
    if not isinstance(records, list):
        raise ValueError("Snapshot records must be a list")
    seen = set()
    for record in records:
        if record.get("route_kind") != "reconstructed":
            raise ValueError("This importer accepts only labeled approximate routes")
        departure = record.get("departure")
        if (
            not valid_point(departure)
            or not isinstance(departure.get("name"), str)
            or not departure["name"].strip()
        ):
            raise ValueError("Invalid departure coordinates/name")
        if not source_url(record.get("source_url")) or not source_url(
            departure.get("source_url")
        ):
            raise ValueError("Each route and origin require HTTP(S) source links")
        if record.get("source_name") not in {"NTSB", "wikidata", "curated"}:
            raise ValueError("Unknown route source")
        if (
            not isinstance(record.get("source_record_id"), str)
            or not record["source_record_id"].strip()
            or not isinstance(record.get("note"), str)
            or not record["note"].strip()
        ):
            raise ValueError("Every route requires a stable source ID and scope note")
        record.setdefault("route_key", "departure")
        if not isinstance(record["route_key"], str) or not record["route_key"].strip():
            raise ValueError("Every route requires a nonempty route key")
        key = (
            record["source_name"],
            str(record["source_record_id"]),
            record["route_key"],
        )
        if key in seen:
            raise ValueError(f"Duplicate route key: {key}")
        seen.add(key)
    return records


def import_routes(database, snapshots):

    records = [record for path in snapshots for record in read_snapshot(path)]
    connection = sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=rw", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        sources = {
            (row["source_name"], str(row["source_record_id"])): row["event_id"]
            for row in connection.execute(
                "SELECT source_name,source_record_id,event_id FROM event_sources"
            )
        }
        events = {
            row["id"]: dict(row)
            for row in connection.execute("SELECT id,latitude,longitude FROM events")
        }
        stamp = datetime.now(timezone.utc).isoformat()
        imported = unmapped = unmatched = 0
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS event_routes (
                event_id INTEGER NOT NULL REFERENCES events(id),
                source_name TEXT NOT NULL, source_record_id TEXT NOT NULL, route_key TEXT NOT NULL,
                route_json TEXT NOT NULL, imported_at TEXT NOT NULL,
                PRIMARY KEY(source_name,source_record_id,route_key))""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS route_event ON event_routes(event_id)"
            )
            for record in records:
                lookup = (
                    "wikidata"
                    if record["source_name"] == "curated"
                    else record["source_name"]
                )
                event_id = sources.get((lookup, str(record["source_record_id"])))
                if event_id not in events:
                    unmatched += 1
                    continue
                if not valid_point(events[event_id]):
                    unmapped += 1
                    continue
                if (
                    record.get("event_id") is not None
                    and record["event_id"] != event_id
                ):
                    raise ValueError(
                        "Source-to-event identity changed; review the snapshot"
                    )
                route = dict(record, event_id=event_id)

                route.pop("occurrence", None)
                connection.execute(
                    """INSERT INTO event_routes VALUES (?,?,?,?,?,?)
                    ON CONFLICT(source_name,source_record_id,route_key) DO UPDATE SET
                    event_id=excluded.event_id,route_json=excluded.route_json,imported_at=excluded.imported_at""",
                    (
                        event_id,
                        route["source_name"],
                        str(route["source_record_id"]),
                        route["route_key"],
                        json.dumps(route, ensure_ascii=False),
                        stamp,
                    ),
                )
                imported += 1
        result = {
            "imported_routes": imported,
            "unmapped_events_skipped": unmapped,
            "unmatched_source_ids": unmatched,
            "events_with_routes": connection.execute(
                "SELECT count(DISTINCT event_id) FROM event_routes"
            ).fetchone()[0],
        }
        print(json.dumps(result, indent=2))
        return result
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, action="append", required=True)
    args = parser.parse_args()
    import_routes(args.database, args.snapshot)
