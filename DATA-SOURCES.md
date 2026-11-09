# Data sources and coverage

Saved on 5 November 2026: 32,796 records, 28,992 with coordinates. Original Australian record preserved.

- NTSB: 29,075 accident records, 2008-2026. Official bulk dataset: https://data.ntsb.gov/avdata . Coverage is US civil aviation plus selected foreign investigations, including aircraft types beyond airplanes. Other country codes are retained from the source. License: NTSB-authored US government public-domain data. Report identifiers are in event names and provenance. The source button currently opens the official dataset.
- Wikidata: 3,723 normalized source records, 3,720 new database rows. Worldwide selection, 1939-2026, from direct aviation occurrence classes. License: CC0. https://www.wikidata.org/wiki/Wikidata:Licensing . Includes 11 source-classified terrorist attack records. Categories and coordinates are community-maintained and have not all been independently verified. Known conflicting coordinates are withheld. Multiple source items can describe one occurrence.

This is NOT every documented aviation event. Pre-2008, military, non-US and smaller-aircraft coverage is incomplete. Cross-source duplicate review remains necessary. No locations are filled using guessed airport coordinates.

The normalised snapshots are saved in data/ (private, not served by the API). Database tables event_sources and import_runs preserve source identifiers, licensing, retrieval date, classification basis and import checksums. Reimporting the same source IDs does not insert duplicates or overwrite curated fields.

To reimport a saved snapshot in VS Code's PowerShell terminal:

    .\.venv\Scripts\python.exe import_history.py --snapshot data/wikidata.json --database mayday.db
    .\.venv\Scripts\python.exe import_history.py --snapshot data/ntsb.json --database mayday.db

To refresh the worldwide selection:

    .\.venv\Scripts\python.exe import_history.py --download-wikidata --snapshot data/wikidata-new.json --through 2026-11-05

Review a new snapshot before importing it. The download step does not change the database.

NTSB normalization: import_ntsb.py reads the official Access MDB using access-parser==0.0.6. The raw Access download is not included in the repository; obtain it from the official NTSB bulk dataset linked above. Earlier NTSB archives are separate downloads, not included yet.

Map: Esri World Imagery via the public ArcGIS MapServer, with automatic provider credits. Satellite detail varies by location and needs internet. 16px red plane markers, no clustering; coincident records may overlap exactly. The Time span default shows all imported years from 1939 through 2026.

## Outcomes and accounts, 5 November 2026

The installed database has 31,204 reported death counts: 8,688 fatal records and 22,516 explicitly nonfatal records. Another 1,592 outcomes are unknown. There are 28,861 source accounts/descriptions and 3,935 basic context notes. These are record counts, not counts of unique historical accidents. Every detail card shows a death toll or unknown, the count's scope, and a short account or clearly labeled basic context. The original event fields and source provenance were preserved.

NTSB enrichment uses the official Access dataset's event, aircraft, narrative and administrative records. Onboard fatalities and ground fatalities are separate fields. Positive onboard counts are labeled onboard-only when a ground count is unavailable. Missing/default zero values do not establish that an event was nonfatal. Known fatal injury severity can establish a fatal outcome even if a numeric total is unavailable. Final report excerpts and preliminary factual accounts are labeled separately; preliminary accounts are not treated as final findings. Boilerplate notifications without an account fall back to basic context. The parser does not use the probable-cause field to invent a narrative.

Three primary-source reviews supplement conservative Wikidata extraction: Pan Am 103 (FBI, 270 including 11 on the ground), MH17 (Dutch Safety Board, 298 occupants), and United 1175 (NTSB, no fatalities). Their exact sources and review notes are in data/curated-enrichment.json and the event_enrichment audit table. The original Bourke Cessna accident also has a final ATSB account and one reported pilot fatality.

To reproduce the enrichment from the saved local snapshots, after backing up mayday.db:

    .\.venv\Scripts\python.exe enrich_archive.py --database mayday.db --ntsb data/ntsb-enrichment.json --wikidata data/wikidata-enrichment.json --curated data/curated-enrichment.json

enrich_archive.py adds the outcome/description columns if needed and updates only those fields. event_enrichment retains each source payload. enrich_ntsb.py prepares an NTSB snapshot from the Access database; enrich_wikidata.py prepares a Wikidata snapshot and can fetch English descriptions in cached, rate-limited batches. Run either script with --help for its inputs. Downloads prepare files; the command above applies them to SQLite. After importing new events, rerun enrichment so the new records have the same fields. Raw datasets, enrichment snapshots and the live database are private and excluded from Git and the code-only ZIP.

The API sends counts/outcomes with the paginated event list, and loads the longer account and source notes only when an event is selected through /events/{id}. Counts and descriptions can come from different sources, so each has its own source link.

Next: expand pre-2008/regional/military sources, review duplicates and coordinates, verify terrorism classifications and add report/article links. Server-side SQL filtering would reduce loading time as the database grows.

## Wikidata death counts and short descriptions

`enrich_wikidata.py` reads the database without changing it and produces an enrichment JSON file. A usable death count comes from [number of deaths (P1120)](https://www.wikidata.org/wiki/Property:P1120): a nondeprecated, exact, unqualified, unambiguous integer at the best available rank. Passenger-only, crew-only, aircraft-specific, approximate and other qualified values are not added together. Conflicting or unavailable counts remain unknown. An explicit zero is retained as zero. The source does not always establish whether ground fatalities are included.

Fatal/nonfatal/unknown describes the accepted death count independently of accident, incident, combat-loss or terrorist-attack classification. The dataset includes umbrella events and their component flights, so adding every record's fatalities would double-count some people; counts are presented per record.

English descriptions come from the official Wikidata entity API and are attributed to each source item under CC0. `source_description` means a short Wikidata description with record context, not an investigation narrative or independently verified cause. `basic_record` means only the available name, date and location context; no what-happened narrative was available.

Known aircraft-field limitation: the original Wikidata import reads [item operated (P121)](https://www.wikidata.org/wiki/Property:P121), which can include other equipment. For example, Iran Air Flight 655's source lists USS Vincennes as well as the aircraft. Enrichment summaries omit this field instead of presenting all P121 values as aircraft. The existing aircraft field still requires a later source-aware review.


Latest update: 19,941 events now have sourced approximate route connections; plane markers are 16px and reused during filtering. Route details in the app identify each source and its limitations. Actual flight tracks are not imported.

Performance: Routes now use solid turquoise lines. The compact cached index, early loading, shared icon texture and geographically partitioned marker collections reduce startup work. Full globe quality settings remain unchanged. Missing routes display only No route available.


## Icon direction in version 1.0.0

Reviewed true-heading evidence takes priority. Otherwise, an event with exactly one preferred sourced route uses the final great-circle bearing of the departure-to-event connection. The compact map index includes this estimate so icons are oriented at initial load. The event panel labels it as a route estimate and links its source. It is not a measured aircraft heading, an actual flight track, or evidence of the direction at impact.

Curated routes take precedence over NTSB routes, then other sources, matching the route display. Events with several preferred routes retain neutral icons until a particular route is selected. Closing the event restores the archive orientation. Missing or unsafe sources, invalid coordinates, routes shorter than 1 km, antipodal endpoints and an endpoint at a geographic pole do not produce a bearing. Unknown directions are not filled with random headings.
