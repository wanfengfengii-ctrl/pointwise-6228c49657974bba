import json
from vendor import sdk1
from .db import encode


class Processing:
    def __init__(self, database, registry, batches, jobs):
        self.db = database
        self.registry = registry
        self.batches = batches
        self.jobs = jobs

    @staticmethod
    def extract(plan, line, position, mapping):
        match = sdk1.search(plan, line)
        captures = ({g['id']: None for g in plan['groups']}
                    if match is None else match['captures'])
        fields = {field: (None if captures[group] is None
                          else line[captures[group][0]:captures[group][1]])
                  for field, group in mapping.items()}
        return {'position': position, 'matched': match is not None,
                'span': None if match is None else match['span'],
                'captures': captures, 'fields': fields}

    def run(self, job, checkpoint=None):
        self.jobs.get(job)
        try:
            with self.db.transaction() as con:
                bound = self.jobs.get(job)
                if bound['state'] == 'succeeded':
                    return self._receipt(bound)
                plan = self.registry.get(revision=bound['revision'])['plan']
                lines = self.batches.lines(bound['batch'])
                rows = []
                for position, line in enumerate(lines):
                    rows.append(self.extract(plan, line, position, bound['mapping']))
                    if checkpoint is not None:
                        checkpoint('processing.row', position)
                con.execute('INSERT INTO results VALUES(?,?)', (job, len(rows)))
                con.executemany('INSERT INTO result_rows VALUES(?,?,?)',
                                ((job, row['position'], encode(row)) for row in rows))
                con.execute("UPDATE jobs SET state='succeeded',error=NULL WHERE id=?", (job,))
                if checkpoint is not None:
                    checkpoint('processing.before_commit', job)
                return self._receipt(self.jobs.get(job))
        except Exception as exc:
            # The publication transaction has already rolled back.
            with self.db.transaction() as con:
                con.execute("UPDATE jobs SET state='failed',error=? WHERE id=? "
                            "AND state!='succeeded'",
                            (type(exc).__name__ + ': ' + str(exc), job))
            raise

    def _receipt(self, bound):
        row = self.db.connection.execute('SELECT row_count FROM results WHERE job=?',
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
                    'SELECT payload FROM result_rows WHERE job=? ORDER BY position', (job,))]
            return {**bound, 'result': result, 'rows': rows}
