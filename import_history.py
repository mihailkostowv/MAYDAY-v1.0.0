import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SOURCE = "wikidata"
LICENSE = "CC0-1.0"
ROOT_TYPES = (
    "Q744913",
    "Q3002150",
    "Q3149875",
    "Q898712",
    "Q108284447",
    "Q21152775",
    "Q6539177",
)
FIELDS = (
    "name",
    "event_date",
    "country",
    "category",
    "aircraft",
    "latitude",
    "longitude",
    "source_url",
)
USER_AGENT = "MAYDAYResearch/0.1 (educational aviation atlas; Python urllib)"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def fetch_json(url):
    for attempt in range(3):
        try:
            request = Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError) as error:
            if attempt == 2:
                raise RuntimeError(f"Download failed after retries: {error}") from error
            retry = (
                error.headers.get("Retry-After", "")
                if isinstance(error, HTTPError)
                else ""
            )
            time.sleep(min(60, int(retry)) if retry.isdigit() else 3 * (attempt + 1))


def candidate_query(through):
    exclusive_end = (date.fromisoformat(through) + timedelta(days=1)).isoformat()
    roots = " ".join("wd:" + value for value in ROOT_TYPES)
    return f"""PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?item ?date ?type WHERE {{
 VALUES ?type {{ {roots} }}
 ?item wdt:P31 ?type ; wdt:P585 ?date .
 FILTER(?date >= "1939-01-01T00:00:00Z"^^xsd:dateTime && ?date < "{exclusive_end}T00:00:00Z"^^xsd:dateTime)
}}"""


def fetch_entities(ids, cache_dir, props="labels|claims"):
    entities = {}
    for index in range(0, len(ids), 50):
        batch = ids[index : index + 50]
        key = hashlib.sha256((props + "|" + "|".join(batch)).encode()).hexdigest()[:20]
        cache = cache_dir / (key + ".json")
        if cache.is_file():
            response = json.loads(cache.read_text(encoding="utf-8"))
        else:
            query = urlencode(
                {
                    "action": "wbgetentities",
                    "format": "json",
                    "ids": "|".join(batch),
                    "props": props,
                    "languages": "en",
                    "languagefallback": 1,
                }
            )
            response = fetch_json("https://www.wikidata.org/w/api.php?" + query)
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            write_json(cache, response)
            time.sleep(0.3)
        entities.update(response.get("entities", {}))
        print(
            f"Retrieved {min(index + 50, len(ids))}/{len(ids)} entity records",
            flush=True,
        )
    return entities


def values(entity, property_id):
    statements = [
        s
        for s in entity.get("claims", {}).get(property_id, [])
        if s.get("rank") != "deprecated"
        and s.get("mainsnak", {}).get("snaktype") == "value"
    ]
    preferred = [s for s in statements if s.get("rank") == "preferred"]
    return [s["mainsnak"]["datavalue"]["value"] for s in (preferred or statements)]


def entity_ids(entity, property_id):
    return sorted(
        {
            value["id"]
            for value in values(entity, property_id)
            if isinstance(value, dict) and "id" in value
        }
    )


def label(entity, fallback):
    labels = entity.get("labels", {})
    return labels.get("en", next(iter(labels.values()), {"value": fallback}))["value"]


def normalize_wikidata(entity, labels, through):
    qid = entity["id"]
    dates = set()
    for value in values(entity, "P585"):
        raw_date = value.get("time", "").lstrip("+").split("T")[0]
        precision = value.get("precision", 0)
        if precision >= 9 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            if "1939-01-01" <= raw_date <= through:
                dates.add(
                    raw_date[:4]
                    if precision == 9
                    else raw_date[:7] if precision == 10 else raw_date
                )
    if len(dates) != 1:
        return None

    types = set(entity_ids(entity, "P31"))
    if "Q2223653" in types:
        category, basis = (
            "terrorist_attack",
            "Explicit Wikidata instance of terrorist attack (Q2223653).",
        )
    elif "Q6539177" in types:
        category, basis = (
            "combat_loss",
            "Explicit Wikidata aircraft shootdown (Q6539177); this category also includes civilian shootdowns.",
        )
    elif types.intersection({"Q744913", "Q3002150"}):
        category, basis = (
            "accident",
            "Explicit Wikidata aviation accident or aircraft crash classification.",
        )
    elif types.intersection({"Q3149875", "Q898712"}):
        category, basis = (
            "incident",
            "Explicit Wikidata aviation incident or aircraft hijacking classification; hijacking alone is not labeled terrorism.",
        )
    else:
        category, basis = (
            "unclassified",
            "Wikidata aviation occurrence; more specific classification not established.",
        )

    coordinates = set()
    for coordinate in values(entity, "P625"):
        if coordinate.get("globe", "").endswith("/Q2"):
            lat, lon = coordinate.get("latitude"), coordinate.get("longitude")
            if (
                isinstance(lat, (int, float))
                and isinstance(lon, (int, float))
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                coordinates.add((lat, lon))
    latitude, longitude = (
        next(iter(coordinates)) if len(coordinates) == 1 else (None, None)
    )
    quality = "source_reported_unverified" if latitude is not None else "unavailable"
    if qid == "Q501208":

        latitude, longitude, quality = None, None, "withheld_source_location_conflict"
    country_ids = entity_ids(entity, "P17")
    aircraft_ids = entity_ids(entity, "P121")
    return {
        "source_record_id": qid,
        "name": label(entity, "Wikidata event " + qid),
        "event_date": dates.pop(),
        "country": "; ".join(labels.get(value, value) for value in country_ids)
        or "Not stated",
        "category": category,
        "aircraft": "; ".join(labels.get(value, value) for value in aircraft_ids)
        or "Not stated",
        "latitude": latitude,
        "longitude": longitude,
        "source_url": "https://www.wikidata.org/wiki/" + qid,
        "coordinate_quality": quality,
        "classification_basis": basis,
        "raw": entity,
    }


def download_wikidata(snapshot_path, candidates_path=None, through=None, max_records=0):
    through = through or date.today().isoformat()
    cache_dir = snapshot_path.parent / "wikidata-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if candidates_path:
        candidates = json.loads(Path(candidates_path).read_text(encoding="utf-8-sig"))
    else:
        query = candidate_query(through)
        write_json(
            cache_dir / "query.json", {"query": query, "retrieved_at": utc_now()}
        )
        candidates = fetch_json(
            "https://query.wikidata.org/sparql?"
            + urlencode({"query": query, "format": "json"})
        )
        write_json(cache_dir / "candidates.json", candidates)
    all_ids = sorted(
        {
            row["item"]["value"].rsplit("/", 1)[-1]
            for row in candidates["results"]["bindings"]
        },
        key=lambda item: int(item[1:]),
    )
    ids = all_ids[:max_records] if max_records else all_ids
    print(f"Wikidata candidates: {len(all_ids)}; processing {len(ids)}", flush=True)
    entities = fetch_entities(ids, cache_dir)
    related = sorted(
        {
            qid
            for entity in entities.values()
            for prop in ("P17", "P121")
            for qid in entity_ids(entity, prop)
        }
    )
    related_entities = fetch_entities(related, cache_dir, "labels") if related else {}
    labels = {qid: label(entity, qid) for qid, entity in related_entities.items()}
    records = [
        record
        for entity in entities.values()
        if (record := normalize_wikidata(entity, labels, through))
    ]
    snapshot = {
        "source_name": SOURCE,
        "license": LICENSE,
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "retrieved_at": utc_now(),
        "scope_start": "1939-01-01",
        "scope_end": through,
        "source_url": "https://www.wikidata.org/wiki/Help:Data_access",
        "candidate_count": len(all_ids),
        "processed_count": len(ids),
        "truncated": len(ids) != len(all_ids),
        "skipped_ambiguous_or_missing_date": len(entities) - len(records),
        "notes": (
            "Incomplete community-maintained worldwide selection, using direct instance-of aviation classes only. "
            "Missing/subclass-only records and undated records are not covered. Coordinates are source-reported and "
            "not independently verified; missing/conflicting coordinates stay null. No airport geocoding. "
            "Category terrorist_attack requires explicit terrorist-attack classification; aircraft shootdowns are "
            "shown under combat_loss, including civilian shootdowns. Dates preserve source precision."
        ),
        "records": records,
    }
    write_json(snapshot_path, snapshot)
    print(
        json.dumps(
            {
                "records": len(records),
                "mapped": sum(r["latitude"] is not None for r in records),
                "categories": dict(Counter(r["category"] for r in records)),
                "snapshot": str(snapshot_path),
            },
            indent=2,
        )
    )
    return snapshot


def normalized_name(value):
    return re.sub(r"\W+", "", str(value).casefold())


def import_snapshot(snapshot_path, database_path):
    payload = Path(snapshot_path).read_bytes()
    snapshot = json.loads(payload.decode("utf-8-sig"))
    source = snapshot["source_name"]
    connection = sqlite3.connect(
        Path(database_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    counts = Counter(inserted=0, linked_existing=0, already_present=0, skipped=0)
    try:
        available = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
        if not set(FIELDS).union({"id"}).issubset(available):
            raise ValueError(
                "Existing events table does not have the required MAYDAY columns."
            )
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS event_sources (
                event_id INTEGER NOT NULL REFERENCES events(id), source_name TEXT NOT NULL,
                source_record_id TEXT NOT NULL, source_url TEXT NOT NULL, license TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, coordinate_quality TEXT NOT NULL,
                classification_basis TEXT NOT NULL, raw_json TEXT NOT NULL,
                PRIMARY KEY (source_name, source_record_id)
            );
            CREATE TABLE IF NOT EXISTS import_runs (
                run_id INTEGER PRIMARY KEY, source_name TEXT NOT NULL, imported_at TEXT NOT NULL,
                scope_start TEXT, scope_end TEXT, snapshot_sha256 TEXT NOT NULL,
                inserted INTEGER NOT NULL, linked_existing INTEGER NOT NULL,
                already_present INTEGER NOT NULL, skipped INTEGER NOT NULL, notes TEXT
            );
            CREATE INDEX IF NOT EXISTS event_sources_event_id ON event_sources(event_id);
        """)
        existing = defaultdict(list)
        for row in connection.execute("SELECT id, name, event_date FROM events"):
            existing[(normalized_name(row["name"]), row["event_date"])].append(
                row["id"]
            )
        with connection:
            for record in snapshot["records"]:
                source_id = str(record["source_record_id"])
                if connection.execute(
                    "SELECT 1 FROM event_sources WHERE source_name=? AND source_record_id=?",
                    (source, source_id),
                ).fetchone():
                    counts["already_present"] += 1
                    continue
                event_date = str(record.get("event_date", ""))
                if (
                    not re.fullmatch(r"\d{4}(?:-\d{2}){0,2}", event_date)
                    or not 1939 <= int(event_date[:4]) <= 2026
                ):
                    counts["skipped"] += 1
                    continue
                key = (normalized_name(record["name"]), event_date)
                matches = existing.get(key, [])
                if len(matches) == 1:
                    event_id = matches[0]
                    counts["linked_existing"] += 1
                else:
                    values_to_insert = tuple(record.get(field) for field in FIELDS)
                    cursor = connection.execute(
                        "INSERT INTO events (name,event_date,country,category,aircraft,latitude,longitude,source_url) VALUES (?,?,?,?,?,?,?,?)",
                        values_to_insert,
                    )
                    event_id = cursor.lastrowid
                    existing[key].append(event_id)
                    counts["inserted"] += 1
                connection.execute(
                    "INSERT INTO event_sources (event_id,source_name,source_record_id,source_url,license,retrieved_at,coordinate_quality,classification_basis,raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        event_id,
                        source,
                        source_id,
                        record["source_url"],
                        snapshot["license"],
                        snapshot["retrieved_at"],
                        record.get("coordinate_quality", "unavailable"),
                        record.get("classification_basis", "Source classification."),
                        json.dumps(record.get("raw", record), ensure_ascii=False),
                    ),
                )
            connection.execute(
                "INSERT INTO import_runs (source_name,imported_at,scope_start,scope_end,snapshot_sha256,inserted,linked_existing,already_present,skipped,notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    source,
                    utc_now(),
                    snapshot.get("scope_start"),
                    snapshot.get("scope_end"),
                    hashlib.sha256(payload).hexdigest(),
                    counts["inserted"],
                    counts["linked_existing"],
                    counts["already_present"],
                    counts["skipped"],
                    snapshot.get("notes", ""),
                ),
            )
    finally:
        connection.close()
    print(json.dumps({"source": source, **counts}, indent=2))
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="Normalized JSON snapshot to write or import",
    )
    parser.add_argument("--download-wikidata", action="store_true")
    parser.add_argument(
        "--candidates", type=Path, help="Optional previously saved SPARQL JSON result"
    )
    parser.add_argument("--through", default=date.today().isoformat())
    parser.add_argument(
        "--max-records",
        type=int,
        default=0,
        help="Optional explicit download cap; 0 processes all candidates",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Existing database to append to; BACK IT UP BEFORE IMPORTING",
    )
    args = parser.parse_args()
    if args.download_wikidata:
        download_wikidata(
            args.snapshot, args.candidates, args.through, args.max_records
        )
    if args.database:
        import_snapshot(args.snapshot, args.database)
    if not args.download_wikidata and not args.database:
        parser.error("Choose --download-wikidata, --database, or both.")


if __name__ == "__main__":
    main()
