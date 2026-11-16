import json
from .db import encode, token, Invalid, NotFound


class Jobs:
    def __init__(self, database, registry, batches):
        self.db = database
        self.registry = registry
        self.batches = batches

    def create(self, batch, mapping, request_id, *, name=None, revision=None):
        token(request_id, 'request_id')
        if (name is None) == (revision is None):
            raise Invalid('supply exactly one plan selector')
        if type(batch) is not int or (revision is not None and type(revision) is not int):
            raise Invalid('batch/revision must be integers')
        if name is not None:
            token(name, 'name')
        if not isinstance(mapping, dict):
            raise Invalid('mapping must be a field-to-group dictionary')
        for field, group in mapping.items():
            token(field, 'field')
            token(group, 'group id')
        # Detach the caller's mutable dictionary.
        mapping = dict(mapping)
        payload = {'batch': batch, 'name': name, 'revision': revision, 'mapping': mapping}
        with self.db.transaction() as con:
            job = self.db.replay('job', request_id, payload)
            if job is not None:
                return self.get(job)
            self.batches.get(batch)
            plan = self.registry.get(name, revision=revision)
            groups = {g['id'] for g in plan['plan']['groups']}
            if any(group not in groups for group in mapping.values()):
                raise Invalid('mapping references an unknown capture')
            job = con.execute(
                "INSERT INTO jobs(batch,revision,mapping,state) VALUES(?,?,?,'pending')",
                (batch, plan['revision'], encode(mapping))).lastrowid
            self.db.remember('job', request_id, payload, job)
            return self.get(job)

    def get(self, job):
        if type(job) is not int:
            raise Invalid('job must be an integer')
        row = self.db.connection.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise NotFound('unknown job')
        return {'job': row['id'], 'batch': row['batch'], 'revision': row['revision'],
                'mapping': json.loads(row['mapping']), 'state': row['state'],
                'error': row['error']}
