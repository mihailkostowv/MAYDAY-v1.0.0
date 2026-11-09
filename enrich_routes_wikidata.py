import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def best_statements(entity, prop):
    values = [
        s
        for s in entity.get("claims", {}).get(prop, [])
        if s.get("rank") != "deprecated"
    ]
    return [s for s in values if s.get("rank") == "preferred"] or values


def unique_place(entity, prop):
    statements = best_statements(entity, prop)
    if not statements:
        return None, "missing_origin" if prop == "P1427" else "missing_destination"

    if any(set(s.get("qualifiers", {})) - {"P1932", "P7452"} for s in statements):
        return None, "qualified_origin"
    ids = set()
    for statement in statements:
        snak = statement.get("mainsnak", {})
        if snak.get("snaktype") != "value":
            return None, "unknown_origin"
        value = snak.get("datavalue", {}).get("value", {})
        if not isinstance(value, dict) or not value.get("id"):
            return None, "invalid_origin"
        ids.add(value["id"])
    return (next(iter(ids)), None) if len(ids) == 1 else (None, "multiple_origins")


def coordinate(entity):
    coordinates = set()
    for statement in best_statements(entity, "P625"):
        if statement.get("qualifiers"):
            return None
        snak = statement.get("mainsnak", {})
        if snak.get("snaktype") != "value":
            return None
        value = snak.get("datavalue", {}).get("value", {})
        if not isinstance(value, dict) or not value.get("globe", "").endswith("/Q2"):
            continue
        lat, lon = value.get("latitude"), value.get("longitude")
        if type(lat) not in (int, float) or type(lon) not in (int, float):
            return None
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            return None
        coordinates.add((lat, lon))
    return next(iter(coordinates)) if len(coordinates) == 1 else None


def read_candidates(database):
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    skipped = Counter()
    records = []
    try:
        rows = connection.execute("""
            SELECT s.source_record_id,s.raw_json,s.source_url,e.id AS event_id,
                   e.name,e.latitude,e.longitude
            FROM event_sources s JOIN events e ON s.event_id=e.id
            WHERE s.source_name='wikidata'
            ORDER BY s.source_record_id
        """)
        for row in rows:
            skipped["source_records"] += 1
            entity = json.loads(row["raw_json"])
            origin, reason = unique_place(entity, "P1427")
            if reason:
                skipped[reason] += 1
                continue
            if best_statements(entity, "P2825"):

                skipped["stopovers_require_review"] += 1
                continue
            lat, lon = row["latitude"], row["longitude"]
            if (
                type(lat) not in (int, float)
                or type(lon) not in (int, float)
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
            ):
                skipped["missing_occurrence_coordinates"] += 1
                continue
            destination, _ = unique_place(entity, "P1444")
            records.append(
                {
                    "source_record_id": row["source_record_id"],
                    "event_id": row["event_id"],
                    "name": row["name"],
                    "source_url": row["source_url"],
                    "origin_id": origin,
                    "destination_id": destination,
                }
            )
    finally:
        connection.close()
    return records, skipped


def load_entities(cache_dirs, needed):
    entities = {}
    for directory in cache_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            for qid, entity in payload.get("entities", {}).items():
                if qid not in needed:
                    continue
                existing = entities.setdefault(qid, {})

                for key in ("labels", "claims", "id", "type"):
                    if key in entity:
                        existing[key] = entity[key]
    return entities


def retry_after(error):
    header = (
        error.headers.get("Retry-After", "")
        if isinstance(error, HTTPError)
        else getattr(error, "retry_after", "")
    ).strip()
    if header.isdigit():
        return float(header)
    if header:
        try:
            moment = parsedate_to_datetime(header)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def fetch_places(ids, directory, interval=6.0):
    directory.mkdir(parents=True, exist_ok=True)
    last_request = None
    for offset in range(0, len(ids), 50):
        batch = ids[offset : offset + 50]
        key = hashlib.sha256("|".join(batch).encode()).hexdigest()[:20]
        path = directory / ("places-" + key + ".json")
        if path.exists():
            continue
        url = "https://www.wikidata.org/w/api.php?" + urlencode(
            {
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(batch),
                "props": "labels|claims",
                "languages": "en",
                "maxlag": 5,
            }
        )
        for attempt in range(2):
            try:
                if last_request is not None:
                    time.sleep(max(0.0, interval - (time.monotonic() - last_request)))
                request = Request(
                    url,
                    headers={
                        "User-Agent": "MAYDAYAviationAtlas/0.1 (educational data enrichment)",
                        "Accept": "application/json",
                    },
                )
                last_request = time.monotonic()
                with urlopen(request, timeout=35) as response:
                    data = json.load(response)
                if "error" in data:
                    error = RuntimeError(str(data["error"]))
                    error.retry_after = response.headers.get("Retry-After", "")
                    raise error
                write_json(path, data)
                print(
                    f"Origin places fetched: {min(offset + 50, len(ids))}/{len(ids)}",
                    flush=True,
                )
                break
            except (HTTPError, URLError, TimeoutError, RuntimeError) as error:
                delay = retry_after(error)
                if attempt == 1 or (delay is not None and delay > 60):
                    message = str(error)
                    if delay is not None:
                        message += "; retry no earlier than " + (
                            datetime.now(timezone.utc) + timedelta(seconds=delay)
                        ).isoformat(timespec="seconds")
                    return [message]
                time.sleep(
                    delay
                    if delay is not None
                    else (
                        60
                        if isinstance(error, HTTPError) and error.code == 429
                        else interval
                    )
                )
    return []


def entity_name(entity, qid):
    return entity.get("labels", {}).get("en", {}).get("value", qid)


def build_routes(candidates, entities):
    routes = []
    unavailable = Counter()
    for record in candidates:
        origin = record["origin_id"]
        entity = entities.get(origin, {})
        coords = coordinate(entity)
        if not coords:
            unavailable["origin_coordinates_unavailable_or_ambiguous"] += 1
            continue
        route = {
            "source_name": "wikidata",
            "source_record_id": record["source_record_id"],
            "event_id": record["event_id"],
            "route_key": "journey",
            "route_kind": "reconstructed",
            "departure": {
                "name": entity_name(entity, origin),
                "latitude": coords[0],
                "longitude": coords[1],
                "source_url": "https://www.wikidata.org/wiki/" + origin,
            },
            "departure_label": "Journey origin",
            "source_url": record["source_url"],
            "note": (
                "Illustrative connection from the source-listed journey origin to the recorded occurrence location. "
                "This is not an actual flight track, a verified final departure leg, or a crash trajectory. "
                "Stopovers, intermediate waypoints and altitude are not reconstructed. "
                "The origin place and occurrence coordinates are source-reported and may be approximate."
            ),
        }
        destination = record.get("destination_id")
        if destination and entities.get(destination, {}).get("labels", {}).get(
            "en", {}
        ).get("value"):
            route["destination_name"] = entity_name(entities[destination], destination)
        routes.append(route)
    return routes, unavailable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entity-cache", type=Path, action="append", default=[])
    parser.add_argument(
        "--place-cache",
        type=Path,
        default=Path(__file__).parent / "data" / "wikidata-route-places",
    )
    parser.add_argument(
        "--fetch-missing",
        action="store_true",
        help="Explicitly fetch missing origin entities; no network by default",
    )
    parser.add_argument("--request-interval", type=float, default=6.0)
    args = parser.parse_args()
    if args.request_interval < 1:
        parser.error("--request-interval must be at least one second")
    candidates, counts = read_candidates(args.database)
    origins = {record["origin_id"] for record in candidates}
    needed = origins | {
        record["destination_id"] for record in candidates if record["destination_id"]
    }
    cache_dirs = args.entity_cache + [args.place_cache]
    entities = load_entities(cache_dirs, needed)
    missing = sorted(qid for qid in origins if "claims" not in entities.get(qid, {}))
    write_json(
        args.output.with_suffix(".candidates.json"),
        {"candidates": candidates, "missing_origin_ids": missing},
    )
    failures = (
        fetch_places(missing, args.place_cache, args.request_interval)
        if args.fetch_missing
        else []
    )
    if args.fetch_missing:
        entities = load_entities(cache_dirs, needed)
    routes, unavailable = build_routes(candidates, entities)
    report = {
        **counts,
        "eligible_single_origin_events": len(candidates),
        "unique_origin_entities": len(origins),
        "origin_entities_needing_download": len(missing),
        "routes_available": len(routes),
        **unavailable,
        "network_failures": failures,
    }
    write_json(
        args.output,
        {
            "source_name": "wikidata",
            "license": "CC0-1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "records": routes,
            "coverage": report,
            "property_sources": [
                "https://www.wikidata.org/wiki/Property:P1427",
                "https://www.wikidata.org/wiki/Property:P1444",
                "https://www.wikidata.org/wiki/Property:P625",
            ],
        },
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
