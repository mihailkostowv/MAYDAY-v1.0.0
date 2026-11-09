import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

COLUMNS = {
    "fatalities": "INTEGER",
    "fatality_status": "TEXT NOT NULL DEFAULT 'unknown'",
    "fatalities_scope": "TEXT",
    "fatality_note": "TEXT",
    "fatalities_source_url": "TEXT",
    "summary": "TEXT",
    "summary_kind": "TEXT NOT NULL DEFAULT 'basic_record'",
    "summary_note": "TEXT",
    "summary_source_url": "TEXT",
}


def load_records(path, source):
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    if isinstance(records, dict):
        records = [
            dict(value, source_record_id=str(key)) for key, value in records.items()
        ]
    output = []
    for record in records:
        count = record.get("fatalities")
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError(
                f'Invalid fatality count: {source}/{record.get("source_record_id")}'
            )
        status = record.get("fatality_status", "unknown")
        if status not in {"fatal", "nonfatal", "unknown"}:
            raise ValueError("Invalid fatality status")
        if count is not None and count > 0 and status != "fatal":
            raise ValueError("A positive death count must be classified fatal")
        if count == 0 and status != "nonfatal":
            raise ValueError("An explicit zero must be classified nonfatal")
        if not isinstance(record.get("summary"), str) or not record["summary"].strip():
            raise ValueError(
                "Every enrichment needs a description or explicit basic context"
            )
        output.append(dict(record, source_name=source))
    return output


def fallback(event):
    details = [event["name"] or "Unnamed aviation event"]
    if event["event_date"]:
        details.append("Recorded date: " + event["event_date"])
    if event["country"]:
        details.append("Recorded location: " + event["country"])
    return (
        ". ".join(details)
        + ". A detailed account is not available in the imported source."
    )


def enrich(database, ntsb, wikidata, curated=None):
    records = load_records(ntsb, "NTSB") + load_records(wikidata, "wikidata")
    if curated:
        records += load_records(curated, "curated")
    connection = sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=rw", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    try:
        events = {
            row["id"]: dict(row) for row in connection.execute("SELECT * FROM events")
        }
        source_map = {
            (row["source_name"], row["source_record_id"]): row["event_id"]
            for row in connection.execute(
                "SELECT event_id,source_name,source_record_id FROM event_sources"
            )
        }
        grouped = defaultdict(list)
        unmatched = 0
        for record in records:
            lookup_source = (
                "wikidata"
                if record["source_name"] == "curated"
                else record["source_name"]
            )
            event_id = source_map.get((lookup_source, str(record["source_record_id"])))
            if event_id not in events:
                unmatched += 1
                continue
            grouped[event_id].append(record)

        for event in events.values():
            if (
                event["source_url"]
                == "https://www.atsb.gov.au/investigations/ao-2012-059"
            ):
                grouped[event["id"]].insert(
                    0,
                    {
                        "source_name": "ATSB",
                        "source_record_id": "AO-2012-059",
                        "fatalities": 1,
                        "fatality_status": "fatal",
                        "fatalities_scope": "Pilot fatality reported in the final ATSB investigation",
                        "fatality_note": "Pilot death reported in the final investigation.",
                        "summary": "While mustering cattle near Bourke, the Cessna stalled during low-level manoeuvring and struck the ground. The pilot died. The ATSB could not determine which control inputs led to the stall.",
                        "summary_kind": "source_summary",
                        "summary_note": "Paraphrase of the final ATSB investigation.",
                        "enrichment_source_url": event["source_url"],
                    },
                )
        priority = {"curated": 0, "ATSB": 0, "NTSB": 1, "wikidata": 2}
        stamp = datetime.now(timezone.utc).isoformat()
        with connection:
            present = {
                row[1] for row in connection.execute("PRAGMA table_info(events)")
            }
            for name, sql_type in COLUMNS.items():
                if name not in present:
                    connection.execute(
                        f"ALTER TABLE events ADD COLUMN {name} {sql_type}"
                    )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS event_enrichment (
                source_name TEXT NOT NULL, source_record_id TEXT NOT NULL,
                event_id INTEGER NOT NULL REFERENCES events(id), imported_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(source_name,source_record_id))"""
            )
            for event_id, event in events.items():
                candidates = sorted(
                    grouped[event_id], key=lambda r: priority[r["source_name"]]
                )

                strongest = next(
                    (r for r in candidates if r.get("fatality_status") != "unknown"),
                    candidates[0] if candidates else {},
                )
                strongest_status = strongest.get("fatality_status", "unknown")
                known = [
                    r
                    for r in candidates
                    if r.get("fatalities") is not None
                    and (
                        strongest_status == "unknown"
                        or r.get("fatality_status") == strongest_status
                    )
                ]
                count_record = known[0] if known else strongest
                count = count_record.get("fatalities")
                outcome = count_record.get("fatality_status", "unknown")
                note = count_record.get("fatality_note", "")
                scope = count_record.get(
                    "fatalities_scope", "Unknown; not reported in the imported source"
                )
                if strongest_status != "unknown" and any(
                    r.get("fatality_status") not in {"unknown", strongest_status}
                    for r in candidates
                ):
                    note = (
                        note
                        + " Higher-priority source outcome retained; another source disagrees."
                    ).strip()
                same_rank = (
                    [
                        r
                        for r in known
                        if priority[r["source_name"]]
                        == priority[count_record["source_name"]]
                    ]
                    if known
                    else []
                )
                if len({r["fatalities"] for r in same_rank}) > 1:
                    count = None
                    agreed_outcomes = {r["fatality_status"] for r in same_rank}
                    outcome = (
                        next(iter(agreed_outcomes))
                        if len(agreed_outcomes) == 1
                        else "unknown"
                    )
                    note = (
                        "Conflicting source counts; no single total has been selected."
                    )
                    scope = "Unknown; source counts conflict"
                if candidates:
                    strongest_outcomes = {
                        r["fatality_status"]
                        for r in candidates
                        if priority[r["source_name"]]
                        == priority[strongest["source_name"]]
                        and r["fatality_status"] != "unknown"
                    }
                    if len(strongest_outcomes) > 1:
                        count, outcome = None, "unknown"
                        note = "Equal-priority sources disagree on whether this event was fatal."
                        scope = "Unknown; source outcomes conflict"
                summary_record = next(
                    (r for r in candidates if r.get("summary_kind") != "basic_record"),
                    candidates[0] if candidates else {},
                )
                values = {
                    "fatalities": count,
                    "fatality_status": outcome,
                    "fatalities_scope": scope,
                    "fatality_note": note,
                    "fatalities_source_url": count_record.get(
                        "enrichment_source_url", event["source_url"]
                    ),
                    "summary": summary_record.get("summary") or fallback(event),
                    "summary_kind": summary_record.get("summary_kind", "basic_record"),
                    "summary_note": summary_record.get("summary_note")
                    or (
                        "Basic record context; no detailed narrative is available."
                        if not summary_record
                        or summary_record.get("summary_kind") == "basic_record"
                        else "Short source excerpt; consult the full record for context."
                    ),
                    "summary_source_url": summary_record.get(
                        "enrichment_source_url", event["source_url"]
                    ),
                }
                assignments = ",".join(name + "=?" for name in COLUMNS)
                connection.execute(
                    f"UPDATE events SET {assignments} WHERE id=?",
                    (*values.values(), event_id),
                )
                for record in candidates:
                    connection.execute(
                        """INSERT INTO event_enrichment VALUES (?,?,?,?,?)
                        ON CONFLICT(source_name,source_record_id) DO UPDATE SET
                        event_id=excluded.event_id,imported_at=excluded.imported_at,payload_json=excluded.payload_json""",
                        (
                            record["source_name"],
                            str(record["source_record_id"]),
                            event_id,
                            stamp,
                            json.dumps(record, ensure_ascii=False),
                        ),
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_outcome ON events(fatality_status)"
            )
        totals = dict(
            connection.execute(
                """SELECT COUNT(*) AS records,
            SUM(fatalities IS NOT NULL) AS known_death_tolls,
            SUM(summary_kind!='basic_record') AS source_accounts,
            SUM(summary_kind='basic_record') AS basic_contexts FROM events"""
            ).fetchone()
        )
        totals["outcomes"] = dict(
            connection.execute(
                "SELECT fatality_status,COUNT(*) FROM events GROUP BY fatality_status"
            ).fetchall()
        )
        totals["unmatched_source_records"] = unmatched
        print(json.dumps(totals, indent=2))
        return totals
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--ntsb", required=True, type=Path)
    parser.add_argument("--wikidata", required=True, type=Path)
    parser.add_argument("--curated", type=Path)
    args = parser.parse_args()
    enrich(args.database, args.ntsb, args.wikidata, args.curated)
