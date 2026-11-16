import hashlib
import json
from .db import encode, token, Invalid, NotFound


class Batches:
    def __init__(self, database):
        self.db = database

    def seal(self, lines, request_id):
        token(request_id, 'request_id')
        # Consume before publication: iterator failure cannot expose a prefix.
        materialized = []
        for line in lines:
            if not isinstance(line, str):
                raise Invalid('each input line must be text')
            materialized.append(line)
        payload = {'lines': materialized}
        digest = hashlib.sha256(encode(materialized).encode('ascii')).hexdigest()
        with self.db.transaction() as con:
            batch = self.db.replay('batch', request_id, payload)
            if batch is None:
                batch = con.execute('INSERT INTO batches(digest,row_count) VALUES(?,?)',
                                    (digest, len(materialized))).lastrowid
                con.executemany('INSERT INTO raw_lines VALUES(?,?,?)',
                                ((batch, pos, encode(line)) for pos, line in enumerate(materialized)))
                self.db.remember('batch', request_id, payload, batch)
            return self.get(batch)

    def get(self, batch):
        if type(batch) is not int:
            raise Invalid('batch must be an integer')
        row = self.db.connection.execute('SELECT * FROM batches WHERE id=?', (batch,)).fetchone()
        if row is None:
            raise NotFound('unknown batch')
        return {'batch': row['id'], 'digest': row['digest'], 'row_count': row['row_count']}

    def lines(self, batch):
        self.get(batch)
        return [json.loads(row[0]) for row in self.db.connection.execute(
            'SELECT text FROM raw_lines WHERE batch=? ORDER BY position', (batch,))]
