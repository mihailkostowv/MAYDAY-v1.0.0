# MAYDAY

Explore aviation history on an interactive 3D globe.

MAYDAY is a Python-based Windows application for exploring documented aviation accidents and incidents. Its scope covers 1939-2026, with a growing archive of over 32,000 records.

## Features

- Interactive globe with satellite imagery and individual aircraft markers
- Search by event name, country or aircraft
- Filters for time span, event type and fatal/nonfatal outcomes
- Event descriptions, reported death tolls and source links
- Approximate route connections where departure information is available

## Download and run

Download the Windows app ZIP from this repository's **Releases** section.

1. Extract the entire ZIP.
2. Open the extracted MAYDAY folder.
3. Double-click **MAYDAY.exe**.

Keep the `_internal` folder alongside the executable. Python does not need to be installed. The app starts its local API automatically and stops it when you close the window.

Requires Windows 10/11 x64 and [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/). Internet is needed for the globe library, satellite imagery and external articles. The current release is unsigned.

## Built with

Python, FastAPI, SQLite, CesiumJS, pywebview and PyInstaller.

## Run from source

Use Python 3.15 on Windows. The repository contains the code, not the archive database. Copy `mayday.db` from the downloaded app's `_internal` folder into the project root, beside `api.py`.

```powershell
py -3.15 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop-lock.txt
.\.venv\Scripts\python.exe -m uvicorn api:app --reload
```

Open http://127.0.0.1:8000 in your browser. See [DESKTOP.md](DESKTOP.md) to build a Windows download.

## Data and limitations

The archive uses sources including the NTSB and Wikidata. Coverage is incomplete, and overlapping sources may describe the same event. See [DATA-SOURCES.md](DATA-SOURCES.md) for details.

Locations vary in precision. Unknown death tolls are shown as unknown. Route lines are approximate connections from a documented departure to the recorded event location, not recorded flight tracks.

Icons follow the arrival direction of a sourced approximate route when one unambiguous route is available. This route-derived bearing is an estimate, not a recorded heading before impact. Reviewed heading evidence takes priority, with the observation time explained in the details. Missing, ambiguous, very short and antipodal routes retain a neutral orientation.

The app includes a database snapshot; records do not update automatically.

## Project status

Run the route-direction checks from the project folder with `python -m unittest discover -s tests`.

MAYDAY 1.0.0 is the first public release and an ongoing learning project. Bug reports and suggestions are welcome through GitHub Issues.

## Author

Mihail Kostov
