import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path

SOURCE_URL = "https://data.ntsb.gov/avdata"
LICENSE = "Public domain (NTSB-authored US Government data)"


def rows(table, columns):
    missing = set(columns) - set(table)
    if missing:
        raise ValueError(
            "Missing required source columns: " + ", ".join(sorted(missing))
        )
    lengths = {len(table[column]) for column in columns}
    if len(lengths) != 1:
        raise ValueError("Source column lengths differ; refusing an unsafe row join.")
    return (dict(zip(columns, values)) for values in zip(*(table[c] for c in columns)))


def text(value):
    return str(value).strip() if value is not None else ""


def coordinate_pair(latitude, longitude):

    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None, None
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(lat) or not math.isfinite(lon):
        return None, None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else (None, None)


def normalize(event_table, aircraft_table, start_date, end_date):
    aircraft_by_event = defaultdict(list)
    for row in rows(
        aircraft_table, ["ev_id", "acft_make", "acft_model", "acft_category"]
    ):
        aircraft_by_event[text(row["ev_id"])].append(row)
    records, skipped = [], Counter()
    event_columns = [
        "ev_id",
        "ntsb_no",
        "ev_type",
        "ev_date",
        "ev_city",
        "ev_state",
        "ev_country",
        "dec_latitude",
        "dec_longitude",
        "latlong_acq",
    ]
    seen = set()
    for event in rows(event_table, event_columns):

        if text(event["ev_type"]) != "ACC":
            skipped["not_classified_as_accident"] += 1
            continue
        try:
            event_date = date.fromisoformat(text(event["ev_date"])[:10])
        except ValueError:
            skipped["invalid_or_missing_date"] += 1
            continue
        if not start_date <= event_date <= end_date:
            skipped["outside_requested_dates"] += 1
            continue
        key = text(event["ev_id"])
        if not key or key in seen:
            skipped["missing_or_duplicate_source_id"] += 1
            continue
        seen.add(key)
        aircraft_rows = aircraft_by_event.get(key, [])
        models = sorted(
            {
                " ".join(filter(None, [text(a["acft_make"]), text(a["acft_model"])]))
                for a in aircraft_rows
            }
            - {""}
        )
        aircraft = " / ".join(models) or None
        country_code = text(event["ev_country"])
        country = "United States" if country_code == "USA" else country_code or None
        location = ", ".join(
            filter(None, [text(event["ev_city"]), text(event["ev_state"])])
        )
        case_number = text(event["ntsb_no"]) or key
        name = f"{aircraft or 'Aircraft'} accident - {location or country or 'Location not recorded'} ({case_number})"
        latitude, longitude = coordinate_pair(
            event["dec_latitude"], event["dec_longitude"]
        )

        acquisition = text(event["latlong_acq"]) or "not specified"
        records.append(
            {
                "source_record_id": key,
                "name": name,
                "event_date": event_date.isoformat(),
                "country": country,
                "category": "accident",
                "aircraft": aircraft,
                "latitude": latitude,
                "longitude": longitude,
                "source_url": SOURCE_URL,
                "coordinate_quality": (
                    "source_reported" if latitude is not None else "unavailable"
                ),
                "classification_basis": "NTSB ev_type=ACC (Accident); no terrorism/combat inference.",
                "raw": {
                    "ntsb_no": case_number,
                    "ev_id": key,
                    "ev_type": "ACC",
                    "ev_country": country_code,
                    "coordinate_acquisition": acquisition,
                    "aircraft_categories": sorted(
                        {text(a["acft_category"]) for a in aircraft_rows}
                    ),
                    "source_scope": "US civil aviation and selected foreign investigations",
                },
            }
        )
    records.sort(key=lambda record: (record["event_date"], record["source_record_id"]))
    return records, dict(skipped)


def make_snapshot(
    event_table, aircraft_table, start_date, end_date, source_sha256=None
):
    records, skipped = normalize(event_table, aircraft_table, start_date, end_date)
    return {
        "source_name": "NTSB",
        "license": LICENSE,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "scope_start": start_date.isoformat(),
        "scope_end": end_date.isoformat(),
        "notes": (
            "Official NTSB bulk data; accidents explicitly coded ACC only. "
            "avall covers 2008 onward; Pre2008 is a separate 1982-2007 file. "
            "Includes all aircraft categories in this source, not only airplanes. "
            "US civil aviation and selected foreign cases; not worldwide completeness. "
            "Country codes other than USA are retained verbatim. Source link opens the "
            "official dataset; NTSB case numbers are included in record names and provenance. "
            "Coordinates may be estimated by NTSB; acquisition code is retained in raw data."
        ),
        "source_url": SOURCE_URL,
        "license_url": "https://www.ntsb.gov/about/Pages/Website-Policies.aspx",
        "source_file_sha256": source_sha256,
        "skipped_by_reason": skipped,
        "records": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mdb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--start-date", type=date.fromisoformat, default=date(1939, 1, 1)
    )
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("start-date must not follow end-date")
    from access_parser import AccessParser

    print(
        "Reading the official Access database; this can take a few minutes.", flush=True
    )
    database = AccessParser(str(args.mdb))
    event_table = database.parse_table("events")
    aircraft_table = database.parse_table("aircraft")
    with args.mdb.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    snapshot = make_snapshot(
        event_table, aircraft_table, args.start_date, args.end_date, digest
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    mapped = sum(record["latitude"] is not None for record in snapshot["records"])
    print(
        json.dumps(
            {
                "records": len(snapshot["records"]),
                "mapped": mapped,
                "skipped": snapshot["skipped_by_reason"],
                "output": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
