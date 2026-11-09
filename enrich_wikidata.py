import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def fatalities_from_entity(entity):
    statements = [
        statement
        for statement in entity.get("claims", {}).get("P1120", [])
        if statement.get("rank") != "deprecated"
    ]
    if not statements:
        return None, "unknown", "No nondeprecated P1120 death count is available."
    preferred = [
        statement for statement in statements if statement.get("rank") == "preferred"
    ]
    selected = preferred or statements
    totals = [statement for statement in selected if not statement.get("qualifiers")]
    if not totals:
        return (
            None,
            "unknown",
            "Death statements have scope, uncertainty, time, or other qualifiers; no unqualified event total was used.",
        )
    counts = set()
    for statement in totals:
        snak = statement.get("mainsnak", {})
        if snak.get("snaktype") != "value":
            return (
                None,
                "unknown",
                "An unqualified best-rank death statement has an unknown or unavailable value.",
            )
        value = snak.get("datavalue", {}).get("value", {})
        if not isinstance(value, dict) or value.get("unit") != "1":
            return (
                None,
                "unknown",
                "A death statement does not contain a unitless count.",
            )
        try:
            amount = Decimal(value["amount"])
            if (
                not amount.is_finite()
                or amount < 0
                or amount != amount.to_integral_value()
            ):
                return (
                    None,
                    "unknown",
                    "A death count is negative, nonfinite, or noninteger.",
                )
            for bound in ("lowerBound", "upperBound"):
                if bound in value and Decimal(value[bound]) != amount:
                    return (
                        None,
                        "unknown",
                        "The source expresses a death-count interval rather than an exact count.",
                    )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return None, "unknown", "A death-count quantity could not be read reliably."
        counts.add(int(amount))
    if len(counts) != 1:
        return (
            None,
            "unknown",
            "Best-rank unqualified death counts disagree; no total selected.",
        )
    return (
        counts.pop(),
        "event_total_source_reported",
        (
            "Unambiguous, unqualified, exact Wikidata P1120 at best available rank. "
            "Source-reported event deaths; not independently verified. Qualified passenger, crew, "
            "aircraft-specific, approximate, and other restricted counts were not added together."
        ),
    )


def read_records(database):
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("""
            SELECT s.source_record_id, s.source_url, s.raw_json,
                   e.name, e.event_date, e.country, e.aircraft
            FROM event_sources s JOIN events e ON e.id=s.event_id
            WHERE s.source_name='wikidata'
            ORDER BY s.source_record_id
        """)]
    finally:
        connection.close()


def load_description_cache(cache_dir):
    descriptions = {}
    for path in cache_dir.glob("*.json") if cache_dir.exists() else []:
        data = json.loads(path.read_text(encoding="utf-8"))
        for qid, entity in data.get("entities", {}).items():
            description = entity.get("descriptions", {}).get("en", {}).get("value")
            if description:
                descriptions[qid] = description
    return descriptions


def retry_delay(error):
    header = (
        error.headers.get("Retry-After", "")
        if isinstance(error, HTTPError)
        else getattr(error, "retry_after", "")
    ).strip()
    if header.isdigit():
        return float(header)
    if header:
        try:
            retry_time = parsedate_to_datetime(header)
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_time - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def download_descriptions(ids, cache_dir, request_interval=6.0):
    cache_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    last_request = None
    for start in range(0, len(ids), 50):
        batch = ids[start : start + 50]
        key = hashlib.sha256("|".join(batch).encode()).hexdigest()[:20]
        path = cache_dir / (key + ".json")
        if path.exists():
            continue
        parameters = {
            "action": "wbgetentities",
            "format": "json",
            "ids": "|".join(batch),
            "props": "descriptions",
            "languages": "en",
            "maxlag": 5,
        }
        url = "https://www.wikidata.org/w/api.php?" + urlencode(parameters)
        for attempt in range(2):
            try:
                if last_request is not None:
                    time.sleep(
                        max(0.0, request_interval - (time.monotonic() - last_request))
                    )
                request = Request(
                    url,
                    headers={
                        "User-Agent": "MAYDAYAviationAtlas/0.1 (educational data enrichment)",
                        "Accept": "application/json",
                    },
                )
                last_request = time.monotonic()
                with urlopen(request, timeout=35) as response:
                    payload = json.load(response)
                if "error" in payload:
                    error = RuntimeError(str(payload["error"]))
                    error.retry_after = response.headers.get("Retry-After", "")
                    raise error
                write_json(path, payload)
                print(
                    f"Descriptions: {min(start + 50, len(ids))}/{len(ids)} checked",
                    flush=True,
                )
                break
            except (HTTPError, URLError, TimeoutError, RuntimeError) as error:
                delay = retry_delay(error)
                if delay is not None and delay > 60:
                    retry_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat(timespec="seconds")
                    failures.append(
                        f"{error}; stopped to honor Retry-After. Retry no earlier than {retry_at}."
                    )
                    return failures
                if attempt == 1:
                    note = str(error)
                    if delay is not None:
                        retry_at = (
                            datetime.now(timezone.utc) + timedelta(seconds=delay)
                        ).isoformat(timespec="seconds")
                        note += f"; retry no earlier than {retry_at}."
                    failures.append(note)

                    return failures

                wait = (
                    delay
                    if delay is not None
                    else (
                        60
                        if isinstance(error, HTTPError) and error.code == 429
                        else request_interval
                    )
                )
                print(f"Request delayed {wait:.0f}s after {error}", flush=True)
                time.sleep(wait)
    return failures


def clean_text(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def make_summary(record, entity, descriptions):
    qid = record["source_record_id"]
    description = descriptions.get(qid) or entity.get("descriptions", {}).get(
        "en", {}
    ).get("value")
    name = clean_text(record["name"]).rstrip(".")
    date = clean_text(record["event_date"])
    location = clean_text(record["country"] or "")
    pieces = [
        f"{name}: {clean_text(description).rstrip('.')}." if description else f"{name}."
    ]
    pieces.append(f"Recorded date: {date}.")
    if location and location.lower() not in {"not stated", "unknown"}:
        pieces.append(f"Country/location recorded: {location}.")

    if not description:
        pieces.append("A detailed account is not available in this imported record.")
    summary = " ".join(pieces)
    if len(summary) > 600:
        summary = summary[:597].rsplit(" ", 1)[0] + "..."
    return summary, "source_description" if description else "basic_record"


def build_enrichment(records, descriptions):
    result = []
    for record in records:
        entity = json.loads(record["raw_json"])
        fatalities, scope, note = fatalities_from_entity(entity)
        summary, kind = make_summary(record, entity, descriptions)
        result.append(
            {
                "source_record_id": record["source_record_id"],
                "fatalities": fatalities,
                "fatality_status": (
                    "unknown"
                    if fatalities is None
                    else "fatal" if fatalities > 0 else "nonfatal"
                ),
                "fatalities_scope": (
                    "Source-reported event deaths; inclusion of ground fatalities is not established"
                    if fatalities is not None
                    else "Unknown; no usable unambiguous event-wide total"
                ),
                "summary": summary,
                "summary_kind": kind,
                "summary_note": (
                    "English Wikidata description and record context; not an investigation narrative or verified cause."
                    if kind == "source_description"
                    else "Basic record context only; no detailed what-happened narrative is available."
                ),
                "enrichment_source_url": record["source_url"],
                "fatality_note": note,
                "notes": note
                + (
                    " Summary uses the English Wikidata description and record facts."
                    if kind == "source_description"
                    else " Summary is factual record context, not a narrative or inferred cause."
                ),
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).parent / "data" / "wikidata-descriptions",
    )
    parser.add_argument("--fetch-descriptions", action="store_true")
    parser.add_argument(
        "--request-interval",
        type=float,
        default=6.0,
        help="Minimum seconds between sequential API requests (default: 6)",
    )
    args = parser.parse_args()
    if args.request_interval < 1:
        parser.error("--request-interval must be at least 1 second")
    records = read_records(args.database)

    descriptions = load_description_cache(args.cache_dir)
    write_json(args.output, build_enrichment(records, descriptions))
    failures = (
        download_descriptions(
            [record["source_record_id"] for record in records],
            args.cache_dir,
            args.request_interval,
        )
        if args.fetch_descriptions
        else []
    )
    descriptions = load_description_cache(args.cache_dir)
    result = build_enrichment(records, descriptions)
    write_json(args.output, result)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Wikidata",
        "license": "CC0-1.0",
        "records": len(result),
        "fatalities_known": sum(row["fatalities"] is not None for row in result),
        "explicit_zero": sum(row["fatalities"] == 0 for row in result),
        "fatalities_unknown": sum(row["fatalities"] is None for row in result),
        "summary_kinds": dict(Counter(row["summary_kind"] for row in result)),
        "network_failures": failures,
        "selection_policy": "Use only exact, unqualified, nondeprecated, best-rank P1120 with one unambiguous value. Never sum restricted counts or infer fatalities from classification.",
        "source_documentation": [
            "https://www.wikidata.org/wiki/Property:P1120",
            "https://www.wikidata.org/wiki/Property:P518",
            "https://www.wikidata.org/wiki/Help:Data_access",
        ],
    }
    write_json(args.output.with_suffix(".report.json"), report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
