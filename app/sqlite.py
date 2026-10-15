"""Open an explicitly configured live database without creating a replacement."""

import sqlite3


def existing_database_connection(database, *, database_path, **kwargs):
    if str(database) == str(database_path):
        # SQLite enforces existence on every connection, including reconnects.
        database = database_path.as_uri() + "?mode=rw"
        kwargs["uri"] = True
    # Django can still create a separate test database using the same options.
    return sqlite3.Connection(database, **kwargs)
