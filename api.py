from pathlib import Path
import gzip
import hashlib
import json
import sqlite3
from threading import Lock
from orientation import arrival_bearing, preferred_routes, event_bearing

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

PROJECT_DIR = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_DIR / "mayday.db"
STATIC_DIR = PROJECT_DIR / "static"
INDEX_COLUMNS = [
    "id",
    "name",
    "event_date",
    "country",
    "category",
    "aircraft",
    "latitude",
    "longitude",
    "fatalities",
    "fatality_status",
    "route_bearing",
]
_index_lock = Lock()
_index_cache = None

app = FastAPI(title="MAYDAY", version="1.0.0")

app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


def read_rows(sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
    connection = None
    try:

        connection = sqlite3.connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not read mayday.db. Put your existing database beside "
                "api.py and run your project's database setup/migrations. The events "
                "table must contain id, name, event_date, country, category, aircraft, "
                "latitude, longitude, and source_url."
            ),
        ) from error
    finally:
        if connection is not None:
            connection.close()


@app.get("/")
def home():

    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=503, detail="Place index.html inside the static folder."
        )
    return FileResponse(index_path)


def _database_version():

    try:
        path = DATABASE_PATH.resolve()
        stat = path.stat()
        try:
            wal = Path(str(path) + "-wal").stat()
            wal_version = (wal.st_mtime_ns, wal.st_size)
        except FileNotFoundError:
            wal_version = None
        return str(path), stat.st_mtime_ns, stat.st_size, wal_version
    except OSError as error:
        raise HTTPException(
            status_code=503,
            detail="Could not read mayday.db. Check that the database is beside api.py.",
        ) from error


def _build_event_index():
    connection = None
    try:
        connection = sqlite3.connect(
            DATABASE_PATH.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.execute("BEGIN")
        events = []
        stats = dict(
            total=0,
            mapped=0,
            fatalities_known=0,
            fatal_events=0,
            nonfatal_events=0,
            unknown_outcome_events=0,
            source_descriptions=0,
        )
        route_rows = {}
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_routes'"
        ).fetchone():
            for event_id, source, payload in connection.execute(
                """SELECT event_id, source_name, route_json FROM event_routes ORDER BY event_id,
                CASE source_name WHEN 'curated' THEN 0 WHEN 'NTSB' THEN 1 ELSE 2 END, route_key"""
            ):
                route_rows.setdefault(event_id, []).append((source, payload))
        columns = ", ".join(INDEX_COLUMNS[:-1])
        for row in connection.execute(
            f"SELECT {columns}, summary_kind FROM events ORDER BY id"
        ):
            direction = event_bearing(
                {"latitude": row[6], "longitude": row[7]},
                preferred_routes(route_rows.get(row[0], [])),
            )
            events.append(list(row[:10]) + [direction])
            latitude, longitude = row[6], row[7]
            stats["mapped"] += (
                isinstance(latitude, (int, float))
                and isinstance(longitude, (int, float))
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            )
            stats["fatalities_known"] += row[8] is not None
            stats["fatal_events"] += row[9] == "fatal"
            stats["nonfatal_events"] += row[9] == "nonfatal"
            stats["unknown_outcome_events"] += row[9] == "unknown"
            stats["source_descriptions"] += (
                row[10] is not None and row[10] != "basic_record"
            )
        stats["total"] = len(events)
        if not events:

            for key in (
                "fatalities_known",
                "fatal_events",
                "nonfatal_events",
                "unknown_outcome_events",
                "source_descriptions",
            ):
                stats[key] = None
        stats["sources"] = []
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_sources'"
        ).fetchone():
            stats["sources"] = [
                {"source_name": name, "records": count}
                for name, count in connection.execute(
                    "SELECT source_name, COUNT(DISTINCT event_id) FROM event_sources GROUP BY source_name"
                )
            ]
        payload = {
            "version": 1,
            "columns": INDEX_COLUMNS,
            "events": events,
            "stats": stats,
        }
        plain = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        compressed = gzip.compress(plain, compresslevel=5, mtime=0)
        return {
            "plain": plain,
            "gzip": compressed,
            "plain_etag": '"' + hashlib.sha256(plain).hexdigest() + '"',
            "gzip_etag": '"' + hashlib.sha256(compressed).hexdigest() + '"',
        }
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise HTTPException(
            status_code=503,
            detail="Could not build the event index. Check the database and run the project's data migrations.",
        ) from error
    finally:
        if connection is not None:
            connection.close()


def _cached_event_index():
    global _index_cache
    with _index_lock:
        version = _database_version()
        if _index_cache is not None and _index_cache["database_version"] == version:
            return _index_cache

        for _ in range(2):
            version = _database_version()
            result = _build_event_index()
            if version == _database_version():
                result["database_version"] = version
                _index_cache = result
                return result
        return result


def _index_encoding(accept_encoding):
    qualities = {}
    for item in (accept_encoding or "").split(","):
        parts = [part.strip() for part in item.split(";")]
        name, quality = parts[0].lower(), 1.0
        if not name:
            continue
        for parameter in parts[1:]:
            key, separator, value = parameter.partition("=")
            if separator and key.strip().lower() == "q":
                try:
                    quality = float(value)
                    if not 0 <= quality <= 1:
                        quality = 0.0
                except ValueError:
                    quality = 0.0
        qualities[name] = quality
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))
    identity_quality = qualities.get(
        "identity", 0.0 if qualities.get("*") == 0 else 1.0
    )
    if gzip_quality > 0 and gzip_quality >= identity_quality:
        return "gzip"
    if identity_quality > 0:
        return "plain"
    if gzip_quality > 0:
        return "gzip"
    raise HTTPException(
        status_code=406, detail="Neither gzip nor identity encoding is acceptable."
    )


@app.get("/event-index")
def get_event_index(request: Request):
    encoding = _index_encoding(request.headers.get("accept-encoding"))
    cached = _cached_event_index()
    etag = cached[encoding + "_etag"]
    headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    candidates = [
        tag.strip() for tag in request.headers.get("if-none-match", "").split(",")
    ]
    if any(
        tag == "*" or (tag[2:] if tag.startswith("W/") else tag) == etag
        for tag in candidates
    ):
        return Response(status_code=304, headers=headers)
    if encoding == "gzip":
        headers["Content-Encoding"] = "gzip"
    return Response(cached[encoding], media_type="application/json", headers=headers)


@app.get("/events")
def get_events(
    limit: int = Query(default=500, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):

    rows = read_rows(
        """
        SELECT id, name, event_date, country, category, aircraft,
               latitude, longitude, source_url, fatalities, fatality_status
        FROM events
        ORDER BY id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    return [dict(row) for row in rows]


@app.get("/events/{event_id}")
def get_event(event_id: int):
    rows = read_rows(
        """SELECT id, name, event_date, country, category, aircraft,
                  latitude, longitude, source_url, fatalities, fatality_status,
                  fatalities_scope, fatality_note, fatalities_source_url,
                  summary, summary_kind, summary_note, summary_source_url
           FROM events WHERE id = ?""",
        (event_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Event not found.")
    event = dict(rows[0])
    event["routes"] = []
    if read_rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='event_routes'"
    ):
        routes = read_rows(
            """SELECT source_name,route_json FROM event_routes WHERE event_id=? ORDER BY
               CASE source_name WHEN 'curated' THEN 0 WHEN 'NTSB' THEN 1 ELSE 2 END,route_key""",
            (event_id,),
        )

        event["routes"] = preferred_routes(
            [(row["source_name"], row["route_json"]) for row in routes]
        )
    event["route_bearing"] = event_bearing(event, event["routes"])
    for route in event["routes"]:
        route["arrival_bearing"] = arrival_bearing(event, route)
    return event


@app.get("/stats")
def get_stats():
    rows = read_rows("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE
                   WHEN latitude IS NOT NULL AND longitude IS NOT NULL
                    AND typeof(latitude) IN ('integer', 'real')
                    AND typeof(longitude) IN ('integer', 'real')
                    AND latitude BETWEEN -90 AND 90
                    AND longitude BETWEEN -180 AND 180
                   THEN 1 ELSE 0
               END), 0) AS mapped
        FROM events
        """)
    stats = dict(rows[0])
    stats.update(dict(read_rows("""SELECT
        SUM(CASE WHEN fatalities IS NOT NULL THEN 1 ELSE 0 END) AS fatalities_known,
        SUM(CASE WHEN fatality_status = 'fatal' THEN 1 ELSE 0 END) AS fatal_events,
        SUM(CASE WHEN fatality_status = 'nonfatal' THEN 1 ELSE 0 END) AS nonfatal_events,
        SUM(CASE WHEN fatality_status = 'unknown' THEN 1 ELSE 0 END) AS unknown_outcome_events,
        SUM(CASE WHEN summary_kind != 'basic_record' THEN 1 ELSE 0 END) AS source_descriptions
        FROM events""")[0]))
    stats["sources"] = []
    if read_rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='event_sources'"
    ):
        stats["sources"] = [
            dict(row)
            for row in read_rows(
                "SELECT source_name, COUNT(DISTINCT event_id) AS records FROM event_sources GROUP BY source_name"
            )
        ]
    return stats

