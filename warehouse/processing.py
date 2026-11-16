import json
from vendor import sdk1, sdk2
from . import translation
from .db import encode


class Processing:
    def __init__(self, database, registry, batches, jobs):
        self.db = database
        self.registry = registry
        self.batches = batches
        self.jobs = jobs

    def _table(self, base):
        return 'target_' + base if self.db.is_v2() else base

    @staticmethod
    def extract(plan, line, position, mapping):
        match = sdk1.search(plan, line)
        return Processing.project(match, line, position, mapping,
                                  [g['id'] for g in plan['groups']])

    @staticmethod
    def project(match, line, position, mapping, group_ids):
        captures = (dict.fromkeys(group_ids)
                    if match is None else match['captures'])
        fields = {field: (None if captures[group] is None
                          else line[captures[group][0]:captures[group][1]])
                  for field, group in mapping.items()}
        return {'position': position, 'matched': match is not None,
                'span': None if match is None else match['span'],
                'captures': captures, 'fields': fields}

    def _extractor(self, revision):
        if not self.db.is_v2():
            plan = self.registry.get(revision=revision)['plan']
            return lambda line, position, mapping: self.extract(
                plan, line, position, mapping)
        execution = self.registry.execution(revision)
        graph = sdk2.Graph(execution['graph'])
        groups = execution['groups']

        def extract(line, position, mapping):
            match = translation.search(graph, groups, line)
            return self.project(match, line, position, mapping,
                                [group['id'] for group in groups])

        return extract

    def run(self, job, checkpoint=None):
        self.jobs.get(job)
        try:
            with self.db.transaction() as con:
                bound = self.jobs.get(job)
                if bound['state'] == 'succeeded':
                    return self._receipt(bound)
                extract = self._extractor(bound['revision'])
                lines = self.batches.lines(bound['batch'])
                rows = []
                for position, line in enumerate(lines):
                    rows.append(extract(line, position, bound['mapping']))
                    if checkpoint is not None:
                        checkpoint('processing.row', position)
                con.execute('INSERT INTO ' + self._table('results') + ' VALUES(?,?)',
                            (job, len(rows)))
                con.executemany(
                    'INSERT INTO ' + self._table('result_rows') + ' VALUES(?,?,?)',
                    ((job, row['position'], encode(row)) for row in rows))
                con.execute('UPDATE ' + self._table('jobs')
                            + " SET state='succeeded',error=NULL WHERE id=?", (job,))
                if checkpoint is not None:
                    checkpoint('processing.before_commit', job)
                return self._receipt(self.jobs.get(job))
        except Exception as exc:
            # The publication transaction has already rolled back.
            with self.db.transaction() as con:
                con.execute('UPDATE ' + self._table('jobs')
                            + " SET state='failed',error=? WHERE id=? AND state!='succeeded'",
                            (type(exc).__name__ + ': ' + str(exc), job))
            raise

    def _receipt(self, bound):
        row = self.db.connection.execute(
            'SELECT row_count FROM ' + self._table('results') + ' WHERE job=?',
            (bound['job'],)).fetchone()
        return {'job': bound['job'], 'result': bound['job'], 'state': 'succeeded',
                'row_count': row['row_count']}

    def read(self, job):
        with self.db.transaction(write=False):
            bound = self.jobs.get(job)
            result = None
            rows = None
            if bound['state'] == 'succeeded':
                result = self._receipt(bound)
                rows = [json.loads(row[0]) for row in self.db.connection.execute(
                    'SELECT payload FROM ' + self._table('result_rows')
                    + ' WHERE job=? ORDER BY position', (job,))]
            return {**bound, 'result': result, 'rows': rows}
