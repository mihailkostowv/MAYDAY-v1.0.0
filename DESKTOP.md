# MAYDAY Windows release

This is a portable Windows x64 release, version 1.0.0. There is no installer, account, hosting bill or separate Python installation for visitors.

## Use the download

1. Download `MAYDAY-1.0.0-windows-x64.zip` from the project's GitHub Release.
2. Right-click the ZIP and choose **Extract All**.
3. Open the extracted MAYDAY folder and double-click **MAYDAY.exe**.

Keep the entire folder, including `_internal`. The app starts a private, read-only archive API on a free localhost port. Closing the app stops that API. It does not require the developer's computer or server to be running.

Windows 10/11 x64 and Microsoft Edge WebView2 Runtime are required. If WebView2 is missing, install Microsoft's Evergreen Runtime from https://developer.microsoft.com/microsoft-edge/webview2/ . No Python is needed. This release is unsigned; Windows may display an unknown-publisher warning. A signed installer can be added for a later release.

The archive snapshot is included. The globe library, satellite imagery and external articles require internet. This is not a fully offline app. Satellite quality and the existing filters/routes are unchanged. The snapshot does not update automatically; download a new release for updated records. Historical coverage is incomplete; see DATA-SOURCES.md.

Cache and diagnostic logs are stored in `%LOCALAPPDATA%\MAYDAY`. The archive inside `_internal` is opened read-only. Closing the app and deleting the extracted folder removes the program; its cache can be deleted separately.

## Build a new release in VS Code

Use **64-bit Python 3.15 on Windows**. Build dependencies are separate from the normal development server.

```powershell
py -3.15 -m venv .desktop-venv
.\.desktop-venv\Scripts\python.exe -m pip install -r requirements-desktop-lock.txt
.\.desktop-venv\Scripts\python.exe build_desktop.py
```

The script snapshots `mayday.db` safely using SQLite backup, packages Python and the web assets, copies dependency license notices and creates the ZIP under `dist`. It never modifies the live database. To use a different archive or output folder, run `build_desktop.py --help`. The lock file captures all versions used for this release; requirements-desktop.txt lists the direct build/runtime dependencies.

Test the packaged API without opening a window:

```powershell
.\dist\MAYDAY\MAYDAY.exe --self-test desktop-test.json
```

For a public download, create a GitHub Release tagged `v1.0.0` and attach the generated ZIP and SHA256 file. Commit the code, build instructions and requirements, not `.venv`, build output or the 138 MB database. The database travels inside the release ZIP. Publish the release when its download has been tested.

macOS and Linux need their own native builds; this Windows executable does not run on those platforms.
