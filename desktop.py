from pathlib import Path
import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
import threading
import time
from http.client import HTTPConnection

VERSION = "1.0.0"


class LocalAPI:

    def __enter__(self):
        import api
        import uvicorn

        if not api.DATABASE_PATH.is_file():
            raise RuntimeError(
                "The archive database is missing. Extract the entire MAYDAY ZIP, including _internal, before opening MAYDAY.exe."
            )
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                api.app,
                host="127.0.0.1",
                port=self.port,
                loop="asyncio",
                http="h11",
                ws="none",
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=3,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() >= deadline:
                self.__exit__(None, None, None)
                raise RuntimeError(
                    "MAYDAY could not start its local archive service. See desktop.log for details."
                )
            time.sleep(0.05)
        logging.info("MAYDAY %s ready at %s", VERSION, self.url)
        return self

    def __exit__(self, *_):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()
        logging.info("Local archive service stopped")


def check_bundle(service):

    def get(path):
        connection = HTTPConnection("127.0.0.1", service.port, timeout=30)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200, (path, response.status)
            return response.read()
        finally:
            connection.close()

    stats = json.loads(get("/stats"))
    index = json.loads(get("/event-index"))
    assert stats["total"] > 0 and len(index["events"]) == stats["total"]
    assert b"MAYDAY" in get("/")
    assert b"createGlobe" in get("/static/app.js")
    assert b"<svg" in get("/static/assets/plane-marker.svg")
    first_id = index["events"][0][0]
    detail = json.loads(get(f"/events/{first_id}"))
    assert detail["id"] == first_id and "routes" in detail
    return {
        "version": VERSION,
        "events": stats["total"],
        "mapped": stats["mapped"],
        "api": "passed",
        "port": service.port,
    }


def main():
    parser = argparse.ArgumentParser(description="MAYDAY desktop")
    parser.add_argument(
        "--self-test",
        type=Path,
        metavar="REPORT",
        help="Check bundled files and API, save JSON, and exit",
    )
    args = parser.parse_args()
    data_dir = Path(
        os.environ.get("MAYDAY_USER_DATA")
        or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MAYDAY"
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        data_dir / "desktop.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    logging.basicConfig(
        handlers=[handler],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        with LocalAPI() as service:
            if args.self_test:
                result = check_bundle(service)
            else:
                import webview

                webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
                webview.settings["ALLOW_FILE_URLS"] = False
                webview.settings["ALLOW_DOWNLOADS"] = False
                webview.create_window(
                    f"MAYDAY | {VERSION}",
                    service.url,
                    width=1440,
                    height=920,
                    min_size=(900, 650),
                    background_color="#0b0f16",
                    text_select=True,
                )
                webview.start(
                    gui="edgechromium",
                    debug=False,
                    private_mode=False,
                    storage_path=str(data_dir / "WebView2"),
                    icon=str(
                        Path(__file__).resolve().parent / "static/assets/mayday.ico"
                    ),
                )
        if args.self_test:
            with socket.socket() as probe:
                assert (
                    probe.connect_ex(("127.0.0.1", service.port)) != 0
                ), "API did not stop"
            result["shutdown"] = "passed"
            args.self_test.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0
    except Exception as error:
        logging.exception("MAYDAY could not run")
        if args.self_test:
            args.self_test.write_text(
                json.dumps({"error": str(error)}, indent=2), encoding="utf-8"
            )
        elif sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                None,
                f"MAYDAY could not open.\n\n{error}\n\nKeep the full extracted app folder together. This app requires Microsoft Edge WebView2 Runtime.\n\nDetails: {data_dir / 'desktop.log'}",
                "MAYDAY",
                0x10,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

