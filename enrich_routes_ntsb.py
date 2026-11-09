import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import pickle
import re
import sqlite3

NTSB_URL = "https://data.ntsb.gov/avdata"
AIRPORT_DATA_URL = "https://ourairports.com/data/"
PLACEHOLDERS = {
    "",
    "PVT",
    "PRIVATE",
    "NONE",
    "N/A",
    "NA",
    "UNK",
    "UNKN",
    "UNKNOWN",
    "ZZZZ",
    "NIL",
}
IDENTIFIER_COLUMNS = ("ident", "icao_code", "gps_code", "iata_code", "local_code")


def clean(value):
    return str(value).strip() if value is not None else ""


def code(value):
    return clean(value).upper()


def coordinates(latitude, longitude):
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    return (
        (lat, lon)
        if math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        else None
    )


class AirportIndex:

    def __init__(self, airports):
        self.by_code = defaultdict(dict)
        self.countries = set()
        self.count = 0
        for airport in airports:
            ident = code(airport.get("ident"))
            point = coordinates(
                airport.get("latitude_deg"), airport.get("longitude_deg")
            )
            if not ident or point is None:
                continue
            airport = dict(airport, latitude=point[0], longitude=point[1])
            self.countries.add(code(airport.get("iso_country")))
            self.count += 1
            for column in IDENTIFIER_COLUMNS:
                identifier = code(airport.get(column))
                if identifier and identifier not in PLACEHOLDERS:
                    self.by_code[identifier][ident] = airport

    def match(self, identifier, country=None, state=None):
        identifier, country, state = code(identifier), code(country), code(state)
        if identifier in PLACEHOLDERS or not re.fullmatch(
            r"[A-Z0-9][A-Z0-9-]{1,11}", identifier
        ):
            return None, "departure_identifier_missing_or_placeholder"
        candidates = list(self.by_code.get(identifier, {}).values())
        if not candidates:
            return None, "departure_identifier_not_found"
        country = "US" if country == "USA" else country
        if country and country not in self.countries:
            return None, "departure_country_code_not_supported"
        if country:
            candidates = [
                a for a in candidates if code(a.get("iso_country")) == country
            ]
            if not candidates:
                return None, "departure_country_conflict"
        if state and state not in PLACEHOLDERS:
            region = (
                state if "-" in state else f"{country}-{state}" if country else None
            )
            if region:
                candidates = [
                    a for a in candidates if code(a.get("iso_region")) == region
                ]
            else:
                candidates = [
                    a
                    for a in candidates
                    if code(a.get("iso_region")).partition("-")[2] == state
                ]
            if not candidates:
                return None, "departure_state_conflict"

        if len(candidates) != 1:
            return None, "departure_identifier_ambiguous"
        return candidates[0], "exact_identifier_with_recorded_region_checks"


def table_rows(table):
    columns = list(table)
    if len({len(table[column]) for column in columns}) != 1:
        raise ValueError("Source table has unequal column lengths")
    return (
        dict(zip(columns, values))
        for values in zip(*(table[column] for column in columns))
    )


def load_tables(mdb, cache_dir):
    tables, database = {}, None
    for name in ("aircraft", "events"):
        cache = cache_dir / (name + ".pickle") if cache_dir else None
        if cache and cache.is_file():
            with cache.open("rb") as source:
                tables[name] = pickle.load(source)
        else:
            if database is None:
                from access_parser import AccessParser

                database = AccessParser(str(mdb))
            tables[name] = database.parse_table(name)
    return tables


def distance_km(first, second):
    lat1, lon1, lat2, lon2 = map(math.radians, (*first, *second))
    value = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 12742 * math.asin(min(1, math.sqrt(value)))


def endpoint_conflict(point, source_event, airport_index):
    lat_text, lon_text = code(source_event.get("latitude")), code(
        source_event.get("longitude")
    )
    for value, original, positive, negative in (
        (point[0], lat_text, "N", "S"),
        (point[1], lon_text, "E", "W"),
    ):
        if re.fullmatch(r"[0-9.\s+-]+[" + positive + negative + r"]", original):
            if (original.endswith(positive) and value < 0) or (
                original.endswith(negative) and value > 0
            ):
                return "event_coordinate_hemisphere_conflict"
    near_airport, _ = airport_index.match(
        source_event.get("ev_nr_apt_id"),
        source_event.get("ev_country"),
        source_event.get("ev_state"),
    )
    if (
        near_airport
        and distance_km(point, (near_airport["latitude"], near_airport["longitude"]))
        > 1000
    ):
        return "event_point_conflicts_with_reported_airport"
    return None


def airport_name(airport):
    return f"{clean(airport.get('name')) or clean(airport['ident'])} ({clean(airport['ident'])})"


def build_routes(database_path, aircraft_table, airport_index, event_table=None):
    by_event = defaultdict(list)
    for row in table_rows(aircraft_table):
        by_event[clean(row["ev_id"])].append(row)
    source_events_by_id = (
        {clean(row["ev_id"]): row for row in table_rows(event_table)}
        if event_table
        else {}
    )
    connection = sqlite3.connect(
        database_path.resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        source_events = connection.execute(
            """SELECT s.source_record_id, e.latitude, e.longitude
            FROM event_sources s JOIN events e ON e.id=s.event_id
            WHERE s.source_name='NTSB' ORDER BY s.source_record_id"""
        ).fetchall()
    finally:
        connection.close()
    records, counts = [], Counter()
    for event_key, event_latitude, event_longitude in source_events:
        aircraft = by_event.get(event_key, [])
        if not aircraft:
            counts["aircraft_record_missing"] += 1
            continue
        if len(aircraft) != 1:
            counts["multiple_aircraft_event_skipped"] += 1
            continue
        if (
            not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in (event_latitude, event_longitude)
            )
            or coordinates(event_latitude, event_longitude) is None
        ):
            counts["event_coordinates_unavailable"] += 1
            continue
        aircraft = aircraft[0]
        departure, reason = airport_index.match(
            aircraft.get("dprt_apt_id"),
            aircraft.get("dprt_country"),
            aircraft.get("dprt_state"),
        )
        if departure is None:
            counts[reason] += 1
            continue
        conflict = endpoint_conflict(
            (event_latitude, event_longitude),
            source_events_by_id.get(event_key, {}),
            airport_index,
        )
        if conflict:
            counts[conflict] += 1
            continue
        destination, _ = airport_index.match(
            aircraft.get("dest_apt_id"),
            aircraft.get("dest_country"),
            aircraft.get("dest_state"),
        )
        destination_name = airport_name(destination) if destination else None
        aircraft_name = " ".join(
            filter(
                None,
                [clean(aircraft.get("acft_make")), clean(aircraft.get("acft_model"))],
            )
        )
        registration = clean(aircraft.get("regis_no"))
        if registration:
            aircraft_name = f"{aircraft_name} ({registration})".strip()
        note = (
            "Reconstructed connection from the NTSB-reported last departure airport to the recorded event location. "
            "The line is schematic and does not describe the aircraft's actual flown track. "
            "The start uses an airport reference point, not a runway takeoff position. "
            "Airport reference coordinates reflect the current OurAirports snapshot; historical relocations are not resolved."
        )
        records.append(
            {
                "source_name": "NTSB",
                "source_record_id": event_key,
                "route_key": "aircraft:"
                + (clean(aircraft.get("Aircraft_Key")) or "single"),
                "departure": {
                    "name": airport_name(departure),
                    "latitude": departure["latitude"],
                    "longitude": departure["longitude"],
                    "source_url": "https://ourairports.com/airports/"
                    + clean(departure["ident"])
                    + "/",
                },
                "destination_name": destination_name,
                "source_url": NTSB_URL,
                "route_kind": "reconstructed",
                "note": note,
                "aircraft": aircraft_name or None,
                "matching": {
                    "departure_identifier": clean(aircraft.get("dprt_apt_id")),
                    "departure_country": clean(aircraft.get("dprt_country")),
                    "departure_state": clean(aircraft.get("dprt_state")),
                    "airport_ident": clean(departure["ident"]),
                    "basis": reason,
                },
            }
        )
        counts["routes"] += 1
        counts["matched_intended_destination"] += destination_name is not None
    counts["source_events_checked"] = len(source_events)
    return {
        "source_name": "NTSB",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "airport_reference_source": AIRPORT_DATA_URL,
        "airport_reference_license": "Public domain",
        "counts": dict(counts),
        "records": records,
        "notes": "Only unambiguous exact airport-code matches with matching recorded country/state are used. Multi-aircraft events, placeholders and missing accident coordinates are skipped. Conflicting source hemispheres and event points more than 1,000 km from their reported event airport are also omitted, without altering the event coordinates. Destination is informational and is never used as the route start or crash endpoint.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--mdb", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--airports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.airports.open(encoding="utf-8-sig", newline="") as source:
        airport_index = AirportIndex(csv.DictReader(source))
    tables = load_tables(args.mdb, args.cache_dir)
    payload = build_routes(
        args.database, tables["aircraft"], airport_index, tables["events"]
    )
    with args.airports.open("rb") as source:
        payload["airport_reference_sha256"] = hashlib.file_digest(
            source, "sha256"
        ).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(payload["counts"], indent=2))
    print("Output:", args.output)


if __name__ == "__main__":
    main()
