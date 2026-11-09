import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import pickle
import re
import sqlite3

SOURCE_URL = "https://data.ntsb.gov/avdata"
MAX_SUMMARY = 600
NONFATAL = {"NONE", "MINR", "SERS"}


def clean(value):
    if value is None:
        return ""
    value = re.sub(r"<[^>]*>", " ", html.unescape(str(value)))
    return " ".join(value.split())


def count(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return (
        int(number)
        if math.isfinite(number) and number >= 0 and number.is_integer()
        else None
    )


def fatality_details(event):
    onboard = count(event.get("inj_tot_f"))
    ground = count(event.get("inj_f_grnd"))
    severity = clean(event.get("ev_highest_injury"))
    positive = (onboard or 0) > 0 or (ground or 0) > 0
    status = (
        "fatal"
        if positive or severity == "FATL"
        else "nonfatal" if severity in NONFATAL else "unknown"
    )
    severity_label = {
        "FATL": "fatal",
        "SERS": "serious",
        "MINR": "minor",
        "NONE": "none",
    }.get(severity, "unreported")
    note = (
        f"Onboard deaths recorded: {onboard if onboard is not None else 'unreported'}; "
        f"ground deaths recorded: {ground if ground is not None else 'unreported'}. "
        f"Highest injury classification: {severity_label}."
    )
    if status == "unknown" or (status == "fatal" and not positive):
        return None, "Fatality count not established in this snapshot", status, note
    if status == "nonfatal":
        return 0, "No fatal injuries reported by NTSB", status, note
    if onboard is not None and ground is not None:
        return (
            onboard + ground,
            "Onboard and ground fatalities reported by NTSB",
            status,
            note,
        )
    if onboard is not None and onboard > 0:
        return (
            onboard,
            "Onboard fatalities reported; ground fatalities not recorded",
            status,
            note,
        )
    return (
        ground,
        "Ground fatalities reported; onboard fatalities not recorded",
        status,
        note,
    )


def sentences(value):

    value = clean(value)
    protected = re.sub(
        r"\b(?:U\.S\.|U\.K\.|Mr\.|Mrs\.|Dr\.|Capt\.|Inc\.|Co\.|No\.|St\.|Ft\.)",
        lambda match: match.group().replace(".", "\ufff0"),
        value,
    )
    return [
        part.replace("\ufff0", ".").strip()
        for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", protected)
        if part.strip()
    ]


def source_excerpt(value, prefix=""):
    parts = [
        part
        for part in sentences(value)
        if not re.search(
            r"(?:full report is available|accident report number is|all investigative information will be released)",
            part,
            re.I,
        )
    ]
    if not parts:
        return ""
    action = re.compile(
        r"\b(collid\w*|crash\w*|impact\w*|struck|overran|overrun|"
        r"veered|nose[d]? over|nosed over|flipped|ditched|"
        r"lost control|loss of control|loss of (?:engine )?power|"
        r"engine (?:failed|failure)|forced landing|caught fire|"
        r"fire erupted|hit the|contacted the|substantial damage)\b",
        re.I,
    )
    selected = {0}
    action_index = next(
        (i for i, part in enumerate(parts[:18]) if action.search(part)), None
    )
    if action_index is not None:
        selected.add(action_index)
        if action_index > 0:
            selected.add(action_index - 1)
        elif len(parts) > 1:
            selected.add(1)
    elif len(parts) > 1:
        selected.add(1)
    output, previous = prefix, -1
    for index in sorted(selected):
        separator = " " if previous < 0 or index == previous + 1 else " … "
        addition = (separator if output else "") + parts[index]
        if len(output + addition) <= MAX_SUMMARY:
            output += addition
            previous = index
            continue
        if previous < 0:
            available = MAX_SUMMARY - len(output) - 2
            output += " " + parts[index][:available].rsplit(" ", 1)[0] + "…"
        break
    return output.strip()


def usable_narrative(value):
    value = clean(value)
    notification_only = re.search(
        r"(?:has notified the NTSB|NTSB has appointed|accredited representative)",
        value,
        re.I,
    )
    event_detail = re.search(
        r"\b(?:collid\w*|crash\w*|impact\w*|struck|overran|veered|ditched|"
        r"lost control|loss of power|engine failed|forced landing|caught fire|"
        r"substantial damage|fatal injuries|fatally injured)\b",
        value,
        re.I,
    )
    if notification_only and not event_detail:
        return ""
    return (
        value
        if len(value) >= 70 and value.lower() not in {"import", "factual pending"}
        else ""
    )


def make_description(source_event, existing, narrative_rows, record_status):

    final_rows = [
        (row, usable_narrative(row.get("narr_accf"))) for row in narrative_rows
    ]
    final_rows = [
        (row, value) for row, value in final_rows if value and record_status != "P"
    ]
    if final_rows:
        row, value = final_rows[0]
        summary = source_excerpt(value)
        if summary:
            return (
                summary,
                "source_excerpt",
                "Excerpt from the NTSB final account; selected passages may be shortened.",
            )
    preliminary_rows = [
        (row, usable_narrative(row.get("narr_accp"))) for row in narrative_rows
    ]
    preliminary_rows = [(row, value) for row, value in preliminary_rows if value]
    if preliminary_rows:
        row, value = preliminary_rows[0]
        prefix = (
            "Preliminary NTSB account:"
            if record_status == "P"
            else "NTSB occurrence account:"
        )
        summary = source_excerpt(value, prefix)
        note = "Excerpt from the NTSB occurrence account. "
        note += (
            "Preliminary information may change; no final cause is asserted."
            if record_status == "P"
            else "No final probable-cause finding is asserted."
        )
        if summary:
            return summary, "source_excerpt", note
    aircraft = clean(existing.get("aircraft")) or "an aircraft"
    location = (
        clean(source_event.get("ev_city"))
        or clean(existing.get("country"))
        or "an unrecorded location"
    )
    when = clean(existing.get("event_date")) or "an unrecorded date"
    summary = f"NTSB records an accident involving {aircraft} on {when} at {location}. A detailed account is unavailable in this data snapshot."
    if len(summary) > MAX_SUMMARY:
        summary = summary[: MAX_SUMMARY - 1].rsplit(" ", 1)[0] + "…"
    if record_status == "P":
        summary = "Preliminary record. " + summary
        summary = summary[:MAX_SUMMARY]
    return (
        summary,
        "basic_record",
        "Basic context from structured NTSB fields; not a reconstruction or a cause finding.",
    )


def table_rows(table):
    columns = list(table)
    lengths = {len(table[column]) for column in columns}
    if len(lengths) != 1:
        raise ValueError("Source table has unequal column lengths")
    return (
        dict(zip(columns, values))
        for values in zip(*(table[column] for column in columns))
    )


def load_tables(mdb, cache_dir):
    tables, database = {}, None
    for name in ("events", "narratives", "NTSB_Admin"):
        cache_path = cache_dir / (name + ".pickle") if cache_dir else None
        if cache_path and cache_path.is_file():
            with cache_path.open("rb") as source:
                tables[name] = pickle.load(source)
        else:
            if database is None:
                from access_parser import AccessParser

                database = AccessParser(str(mdb))
            tables[name] = database.parse_table(name)
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with cache_path.open("wb") as output:
                    pickle.dump(tables[name], output)
    return tables


def build_enrichment(database_path, tables):
    source_events = {clean(row["ev_id"]): row for row in table_rows(tables["events"])}
    status = {
        clean(row["ev_id"]): clean(row.get("rec_stat"))
        for row in table_rows(tables["NTSB_Admin"])
    }
    narratives = defaultdict(list)
    for row in table_rows(tables["narratives"]):
        narratives[clean(row["ev_id"])].append(row)
    connection = sqlite3.connect(
        database_path.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        existing = connection.execute(
            """SELECT s.source_record_id, e.event_date, e.country, e.aircraft
            FROM event_sources s JOIN events e ON e.id=s.event_id WHERE s.source_name='NTSB'"""
        ).fetchall()
    finally:
        connection.close()
    output, counts = {}, Counter()
    for existing_row in existing:
        existing_row = dict(existing_row)
        key = existing_row["source_record_id"]
        event = source_events.get(key)
        if event is None:
            counts["missing_source_row"] += 1
            continue
        fatalities, scope, fatal_status, fatal_note = fatality_details(event)
        summary, kind, summary_note = make_description(
            event, existing_row, narratives.get(key, []), status.get(key, "")
        )
        output[key] = {
            "fatalities": fatalities,
            "fatalities_scope": scope,
            "fatality_status": fatal_status,
            "summary": summary,
            "summary_kind": kind,
            "enrichment_source_url": SOURCE_URL,
            "fatality_note": fatal_note,
            "summary_note": summary_note,
            "notes": f"NTSB case {clean(event.get('ntsb_no'))}. Public-domain official source data.",
        }
        counts[kind] += 1
        counts[fatal_status] += 1
        counts[
            (
                "known_fatality_count"
                if fatalities is not None
                else "unknown_fatality_count"
            )
        ] += 1
        counts["preliminary"] += status.get(key) == "P"
    counts["records"] = len(output)
    return {
        "source_name": "NTSB",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "notes": "Source injury totals and separately recorded ground fatalities are preserved with explicit scope. Zero counts with unknown severity are not asserted as nonfatal. Summaries are short source excerpts or explicitly basic records; no probable-cause field is used.",
        "counts": dict(counts),
        "records": output,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--mdb", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_enrichment(args.database, load_tables(args.mdb, args.cache_dir))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(payload["counts"], indent=2))
    print("Output:", args.output)


if __name__ == "__main__":
    main()
