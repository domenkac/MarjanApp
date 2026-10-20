"""Same-process Waitress lifetime; the native event loop stays on the main thread."""

import logging
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from desktop.gateway import Gateway
from desktop.profile import (
    DesktopError,
    ProfileLock,
    configure_environment,
    default_profile,
)


def validate_database(profile):
    """Read-only compatibility check. Never migrate, provision or activate a copy."""
    from django.db.migrations.loader import MigrationLoader

    profile.load()
    with closing(sqlite3.connect(profile.database.as_uri() + "?mode=ro", uri=True)) as db:
        applied = set(db.execute("SELECT app, name FROM django_migrations"))
        if applied != set(MigrationLoader(None).disk_migrations):
            raise DesktopError("TEST baza ni združljiva s to različico. Samodejna nadgradnja ni dovoljena.")
        for roots in db.execute("SELECT local_root, icloud_root, dropbox_root FROM archive_archiveconfiguration"):
            if tuple(Path(p) for p in roots) != profile.archive_paths:
                raise DesktopError("TEST baza vsebuje nedovoljene arhivske poti. Zagon je ustavljen.")


def create_application(profile, prefix):
    from django.conf import settings
    from django.core.wsgi import get_wsgi_application
    from whitenoise import WhiteNoise

    settings.FORCE_SCRIPT_NAME = prefix
    settings.STATIC_URL = prefix + "/static/"
    settings.CSRF_COOKIE_PATH = prefix + "/"
    application = WhiteNoise(get_wsgi_application(), allow_all_origins=False)
    validate_database(profile)
    base = Path(__file__).resolve().parents[1]
    # Static resources remain readable within the bundle; no collectstatic at launch.
    css = base / "ui" / "static" / "ui" / "app.css"
    if not css.is_file() or not (base / "ui" / "templates" / "ui" / "base.html").is_file():
        raise DesktopError("Namestitveni paket nima potrebnih predlog ali statičnih datotek. Ponovite namestitev.")
    application.add_files(base / "ui" / "static", prefix="static/")
    return application


class Backend:
    def __init__(self, application, prefix):
        from waitress import create_server
        from waitress.task import ThreadedTaskDispatcher

        self.thread = None
        self.errors = []
        # Port 0 binds atomically; a private asyncore map avoids unrelated sockets.
        gateway = Gateway(application, prefix, "")
        channels = {}
        dispatcher = ThreadedTaskDispatcher()
        try:
            self.server = create_server(
                gateway, host="127.0.0.1", port=0, threads=2,
                asyncore_loop_timeout=0.05, map=channels, _dispatcher=dispatcher,
                clear_untrusted_proxy_headers=True,
            )
            dispatcher.set_thread_count(2)
        except BaseException:
            dispatcher.shutdown()
            for channel in list(channels.values()):
                channel.close()
            raise
        self.origin = f"http://127.0.0.1:{self.server.effective_port}"
        gateway.authority = self.origin.removeprefix("http://")
        self.url = self.origin + prefix + "/"

    def _run(self):
        try:
            self.server.run()
        except Exception:  # noqa: BLE001 -- thread boundary; never expose exceptions
            self.errors.append(True)

    def start(self, timeout=10):
        self.thread = threading.Thread(target=self._run, name="marjan-wsgi", daemon=True)
        self.thread.start()
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.errors:
            try:
                with opener.open(self.url + "__ready__", timeout=0.25) as response:
                    if response.status == 200 and response.read() == b"ready":
                        return
            except (OSError, URLError):
                time.sleep(0.05)
        raise DesktopError("Lokalnega TEST strežnika ni mogoče zagnati. Zaprite aplikacijo in poskusite znova.")

    def close(self):
        # Stop accepting work, finish active requests, then close all channels.
        self.server.accepting = False
        dispatcher = self.server.task_dispatcher
        dispatcher.shutdown(timeout=0.25)
        # Keep the installation lock until every in-flight domain operation ends.
        while dispatcher.threads:
            dispatcher.shutdown(timeout=0.25)
        self.server.close()
        for channel in list(self.server._map.values()):
            channel.close()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise DesktopError("TEST strežnik se ni pravočasno ustavil. Zaprite aplikacijo pred ponovnim zagonom.")


def show_error(message):
    # AppKit works even if importing pywebview or initializing Django fails.
    from AppKit import NSAlert, NSApplication

    NSApplication.sharedApplication()
    alert = NSAlert.alloc().init()
    alert.setMessageText_("MarjanApp TEST – zagon ustavljen")
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("Zapri")
    alert.runModal()


def run():
    if sys.platform != "darwin":
        raise DesktopError("Namizni zaganjalnik je namenjen samo macOS. Uporabite razvojni postopek na drugih sistemih.")
    profile = default_profile()
    profile.load()
    with ProfileLock(profile):
        configure_environment(profile)
        # No tracebacks or capability URLs in console, GUI, or persistent logs.
        logging.disable(logging.CRITICAL)
        import webview

        webview.settings.update({
            "OPEN_EXTERNAL_LINKS_IN_BROWSER": False,
            "ALLOW_FILE_URLS": False,
            "ALLOW_DOWNLOADS": False,
            "OPEN_DEVTOOLS_IN_DEBUG": False,
        })
        prefix = "/_desktop/" + secrets.token_urlsafe(32)
        backend = Backend(create_application(profile, prefix), prefix)
        stopped = False

        def stop_backend():
            nonlocal stopped
            if not stopped:
                backend.close()
                stopped = True

        try:
            backend.start()
            window = webview.create_window("MarjanApp TEST", backend.url, width=1200, height=800)
            # Cocoa termination may exit without returning from the GUI loop.
            # pywebview's closing event is synchronous, including native Quit.
            window.events.closing += stop_backend
            webview.start(gui="cocoa", debug=False, private_mode=True, http_server=False)
        finally:
            stop_backend()


def main():
    try:
        run()
        return 0
    except Exception as error:  # noqa: BLE001 -- native error boundary, fixed text only
        message = str(error) if isinstance(error, DesktopError) else (
            "Zagon MarjanApp TEST ni uspel. Izvajalec naj preveri TEST namestitev "
            "in knjižnice po navodilih docs/desktop-macos.md."
        )
        show_error(message)
        return 1
