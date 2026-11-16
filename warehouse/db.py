import json
import sqlite3
from contextlib import contextmanager


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def token(value, label):
    if not isinstance(value, str) or not value:
        raise Invalid(label + ' must be nonempty text')
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO metadata VALUES('storage_version','1');
CREATE TABLE IF NOT EXISTS revisions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, plan TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS heads(
 name TEXT PRIMARY KEY, revision INTEGER NOT NULL REFERENCES revisions(id));
CREATE TABLE IF NOT EXISTS batches(
 id INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL, row_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS raw_lines(
 batch INTEGER NOT NULL REFERENCES batches(id), position INTEGER NOT NULL,
 text TEXT NOT NULL, PRIMARY KEY(batch,position));
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch INTEGER NOT NULL REFERENCES batches(id),
 revision INTEGER NOT NULL REFERENCES revisions(id),
 mapping TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','failed','succeeded')),
 error TEXT);
CREATE TABLE IF NOT EXISTS results(
 job INTEGER PRIMARY KEY REFERENCES jobs(id), row_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS result_rows(
 job INTEGER NOT NULL REFERENCES results(job), position INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(job,position));
CREATE TABLE IF NOT EXISTS requests(
 scope TEXT NOT NULL, request TEXT NOT NULL, payload TEXT NOT NULL, result INTEGER NOT NULL,
 PRIMARY KEY(scope,request));
"""


class Database:
    """One worker per file; transactions also serve maintenance operations."""
    def __init__(self, path):
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        try:
            self.connection.executescript(SCHEMA)
            if self.connection.execute(
                    "SELECT value FROM metadata WHERE key='storage_version'").fetchone()[0] != '1':
                raise Invalid('unsupported storage version')
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self, write=True):
        self.connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def replay(self, scope, request, payload):
        token(request, 'request_id')
        row = self.connection.execute(
            'SELECT payload,result FROM requests WHERE scope=? AND request=?',
            (scope, request)).fetchone()
        if row is None:
            return None
        if row['payload'] != encode(payload):
            raise Conflict('request_id reused with different input')
        return row['result']

    def remember(self, scope, request, payload, result):
        self.connection.execute('INSERT INTO requests VALUES(?,?,?,?)',
                                (scope, request, encode(payload), result))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def attach(path_or_database):
    if isinstance(path_or_database, Database):
        return path_or_database, False
    return Database(path_or_database), True
