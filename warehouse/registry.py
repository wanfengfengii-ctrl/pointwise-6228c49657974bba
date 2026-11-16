import json
from vendor import sdk1
from .db import attach, token, Invalid, NotFound


class Registry:
    def __init__(self, path_or_database):
        self.db, self._owns = attach(path_or_database)

    def put(self, name, source, request_id):
        token(name, 'name')
        token(request_id, 'request_id')
        plan = sdk1.compile_plan(source)
        payload = {'name': name, 'source': source}
        with self.db.transaction() as con:
            revision = self.db.replay('plan', request_id, payload)
            if revision is None:
                revision = con.execute('INSERT INTO revisions(name,plan) VALUES(?,?)',
                                       (name, json.dumps(plan, ensure_ascii=True))).lastrowid
                con.execute('INSERT INTO heads VALUES(?,?) ON CONFLICT(name) '
                            'DO UPDATE SET revision=excluded.revision', (name, revision))
                self.db.remember('plan', request_id, payload, revision)
            return self.get(revision=revision)

    def get(self, name=None, *, revision=None):
        if (name is None) == (revision is None):
            raise Invalid('supply exactly one of name and revision')
        if revision is None:
            token(name, 'name')
            row = self.db.connection.execute(
                'SELECT r.* FROM heads h JOIN revisions r ON r.id=h.revision WHERE h.name=?',
                (name,)).fetchone()
        else:
            if type(revision) is not int:
                raise Invalid('revision must be an integer')
            row = self.db.connection.execute('SELECT * FROM revisions WHERE id=?',
                                             (revision,)).fetchone()
        if row is None:
            raise NotFound('unknown plan revision')
        return {'revision': row['id'], 'name': row['name'], 'plan': json.loads(row['plan'])}

    def history(self, name):
        token(name, 'name')
        return [self.get(revision=row[0]) for row in self.db.connection.execute(
            'SELECT id FROM revisions WHERE name=? ORDER BY id', (name,))]

    def close(self):
        if self._owns:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
