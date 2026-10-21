"""Run the existing Django application at http://127.0.0.1:8000/."""

import argparse
import logging
import os
import signal
import socket
import sqlite3
import stat
import sys
import threading
import time
from contextlib import closing

from app.local_gateway import LocalGateway
from app.server_config import (
    ServerError,
    configure_environment,
    load_configuration,
    private_path,
)

PORT = 8000
FAILURE = "Production startup failed. Check the private configuration, database, lock and local port."


class InstanceLock:
    """Kernel-owned lock for the configured DB; never unlink a lock file."""

    def __init__(self, database):
        self.path = database.with_name(f".{database.name}.server.lock")
        self.stream = None

    def __enter__(self):
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            private_path(str(self.path))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not os.path.samestat(info, self.path.stat()):
                raise ServerError("Invalid instance lock.")
            if os.name == "nt":
                import msvcrt

                if not info.st_size:
                    self.stream.write(b"0")
                self.stream.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.stream.close()
            raise
        return self

    def __exit__(self, *arguments):
        self.stream.close()


def validate_database(configuration):
    from django.apps import apps
    from django.db.migrations.loader import MigrationLoader

    private_path(str(configuration.database))
    with closing(sqlite3.connect(configuration.database.as_uri() + "?mode=ro", uri=True)) as database:
        database.execute("PRAGMA query_only=ON")
        applied = list(database.execute("SELECT app, name FROM django_migrations"))
        if (len(applied) != len(set(applied))
                or set(applied) != set(MigrationLoader(None).disk_migrations)):
            raise ServerError("Database migrations do not match this source revision.")
        for model in apps.get_models():
            table = model._meta.db_table.replace('"', '""')
            info = list(database.execute(f'PRAGMA table_info("{table}")'))
            if ({row[1] for row in info} != {field.column for field in model._meta.local_fields}
                    or {row[1] for row in info if row[5]} != {model._meta.pk.column}):
                raise ServerError("Database schema does not match this source revision.")
            expected = {
                (field.remote_field.model._meta.db_table, field.column, field.target_field.column)
                for field in model._meta.local_fields if field.is_relation and field.db_constraint
            }
            if {tuple(row[2:5]) for row in database.execute(f'PRAGMA foreign_key_list("{table}")')} != expected:
                raise ServerError("Database foreign key schema does not match.")
        if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ServerError("Database integrity check failed.")
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise ServerError("Database foreign key check failed.")


def validate_settings(configuration):
    from django.conf import settings

    if (settings.SETTINGS_MODULE != "app.production_settings" or settings.DEBUG
            or getattr(settings, "MARJAN_DESKTOP_TEST", False)
            or settings.DATABASES["default"]["NAME"] != configuration.database
            or settings.MEDIA_ROOT != configuration.media
            or settings.RENDERING_ROOT != configuration.rendered
            or settings.SECRET_KEY != configuration.secret
            or any(getattr(settings, name) != value for name, value in configuration.smtp.items())):
        raise ServerError("Configuration changed while production settings were loading.")


def create_application(configuration):
    from django.core.wsgi import get_wsgi_application
    from whitenoise import WhiteNoise

    validate_settings(configuration)
    application = WhiteNoise(get_wsgi_application(), autorefresh=False, allow_all_origins=False)
    validate_database(configuration)
    from django.contrib.staticfiles.finders import get_finders

    locations = set()
    for finder in get_finders():
        for _, storage in finder.list([]):
            if storage.location not in locations:
                locations.add(storage.location)
                application.add_files(storage.location, prefix="static/")
    if "/static/ui/app.css" not in application.files:
        raise ServerError("Required production CSS is missing.")
    return application


class LocalServer:
    """Pinned Waitress lifecycle: stop accepting, drain active work, join, release."""

    def __init__(self, application, *, port=PORT):
        from waitress import create_server
        from waitress.task import ThreadedTaskDispatcher

        self.channels = {}
        self.thread = None
        self.failed = threading.Event()
        self.dispatcher = ThreadedTaskDispatcher()
        gateway = LocalGateway(application, port)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            self.server = create_server(
                gateway, sockets=[listener], threads=2,
                map=self.channels, _dispatcher=self.dispatcher,
                asyncore_loop_timeout=0.05, clear_untrusted_proxy_headers=True,
                expose_tracebacks=False,
            )
            self.dispatcher.set_thread_count(2)
        except BaseException:
            listener.close()
            self.dispatcher.shutdown()
            for channel in list(self.channels.values()):
                channel.close()
            raise
        gateway.authority = f"127.0.0.1:{self.server.effective_port}"

    def start(self):
        def serve():
            try:
                self.server.run()
            except Exception:  # noqa: BLE001 -- thread errors must not expose private exception text
                logging.getLogger(__name__).warning("Local server failed.")
            finally:
                self.failed.set()

        self.thread = threading.Thread(target=serve, name="marjan-production-wsgi", daemon=True)
        self.thread.start()

    def close(self):
        self.server.accepting = False
        self.dispatcher.shutdown(timeout=0.25)
        while self.dispatcher.threads:
            self.dispatcher.shutdown(timeout=0.25)
        deadline = time.monotonic() + 5
        while (any(getattr(channel, "total_outbufs_len", 0) for channel in list(self.channels.values()))
               and time.monotonic() < deadline):
            time.sleep(0.01)
        self.server.close()
        for channel in list(self.channels.values()):
            channel.close()
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise ServerError("Server shutdown did not finish.")


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return "MarjanApp production: server diagnostic; private details suppressed."


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.root.handlers[:] = [handler]
    logging.root.setLevel(logging.WARNING)
    for logger in logging.root.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            logger.handlers.clear()
            logger.propagate = True


def run(path, *, check=False):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ServerError("Production must not run as root.")
    configuration = load_configuration(path)
    configure_environment(configuration)
    configure_logging()
    import django

    django.setup()
    validate_settings(configuration)
    if check:
        validate_database(configuration)
        print("Production configuration and database verified.", flush=True)
        return
    with InstanceLock(configuration.database):
        application = create_application(configuration)
        stop = threading.Event()
        handlers = {}
        backend = LocalServer(application)
        try:
            for name in (signal.SIGINT, signal.SIGTERM):
                handlers[name] = signal.signal(name, lambda *_: stop.set())
            backend.start()
            print("MarjanApp listening at http://127.0.0.1:8000/", flush=True)
            while not stop.wait(0.25):
                if backend.failed.is_set():
                    raise ServerError("Local server stopped unexpectedly.")
        finally:
            backend.close()
            for name, handler in handlers.items():
                signal.signal(name, handler)
        print("MarjanApp stopped.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Absolute private production JSON path.")
    parser.add_argument("--check", action="store_true", help="Read-only configuration/schema check; no listener.")
    arguments = parser.parse_args(argv)
    try:
        run(arguments.config, check=arguments.check)
    except Exception:  # noqa: BLE001 -- fixed, credential-free CLI boundary
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
