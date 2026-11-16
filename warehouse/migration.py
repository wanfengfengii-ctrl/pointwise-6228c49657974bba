"""One-shot, single-worker cutover from storage version 1 to version 2.

All historical revisions, the current heads, fixed tasks, published results and
request receipts are copied into the ``target_*`` tables inside one immediate
transaction that also flips ``metadata.storage_version``.  Any exception (a
source row, a translation failure or a checkpoint callback) rolls the whole
transaction back, so before the commit the old archive is still complete and
usable.  Re-running the upgrade after a failure simply starts over; running it
on an archive that is already version 2 returns without writing anything or
invoking callbacks.

The old tables are never dropped and never read again by the business code
path: they remain as the audit trail required by MIGRATION.md.
"""
import json
import os

from vendor import sdk1
from . import translation
from .db import Database, Invalid, create_target_schema, encode


def upgrade(path, *, checkpoint=None):
    if isinstance(path, Database) or not isinstance(path, (str, os.PathLike)):
        raise Invalid('upgrade requires a SQLite file path')
    database = Database(path)
    try:
        if database.is_v2():
            return None
        with database.transaction() as con:
            create_target_schema(con)
            for row in con.execute(
                    'SELECT id,name,plan FROM revisions ORDER BY id').fetchall():
                plan = _legacy_plan(row['plan'])
                graph, groups = translation.compile_source(plan['source'])
                con.execute(
                    'INSERT INTO target_revisions(id,name,plan,execution) VALUES(?,?,?,?)',
                    (row['id'], row['name'], encode(plan),
                     encode({'format': 'graph-capture/2', 'graph': graph,
                             'groups': groups})))
                if checkpoint is not None:
                    checkpoint('migration.converted', row['id'])
            _copy(con, 'heads', 'target_heads', ('name', 'revision'))
            _copy(con, 'batches', 'target_batches', ('id', 'digest', 'row_count'))
            _copy(con, 'raw_lines', 'target_raw_lines',
                  ('batch', 'position', 'text'))
            _copy(con, 'jobs', 'target_jobs',
                  ('id', 'batch', 'revision', 'mapping', 'state', 'error'))
            _copy(con, 'results', 'target_results', ('job', 'row_count'))
            _copy(con, 'result_rows', 'target_result_rows',
                  ('job', 'position', 'payload'))
            _copy(con, 'requests', 'target_requests',
                  ('scope', 'request', 'payload', 'result'))
            con.execute("UPDATE metadata SET value='2' WHERE key='storage_version'")
            if checkpoint is not None:
                checkpoint('migration.before_commit', None)
        return None
    finally:
        database.close()


def _legacy_plan(raw):
    try:
        plan = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise sdk1.SyntaxError('legacy revision plan is not JSON') from exc
    if not isinstance(plan, dict) or plan.get('format') != 'ordered-capture/1':
        raise sdk1.SyntaxError('not an SDK1 plan')
    expected = translation.plan_for_source(plan.get('source'))
    if plan != expected:
        raise sdk1.SyntaxError('capture directory differs from source')
    return plan


def _copy(con, source, target, columns):
    names = ', '.join(columns)
    rows = con.execute('SELECT ' + names + ' FROM ' + source
                       + ' ORDER BY rowid').fetchall()
    con.executemany('INSERT INTO ' + target + '(' + names + ') VALUES('
                    + ', '.join('?' for _ in columns) + ')',
                    (tuple(row) for row in rows))
