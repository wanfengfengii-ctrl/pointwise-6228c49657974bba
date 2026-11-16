import tempfile
import unittest
from pathlib import Path
from vendor import sdk1
from warehouse import Warehouse, Invalid, NotFound
from warehouse.samples import build_legacy, LINES, MAPPING, SOURCE


class FailureBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='warehouse-test-', dir='.')
        self.path = Path(self.temp.name) / 'archive.sqlite'
        self.w = Warehouse(self.path)

    def tearDown(self):
        self.w.close()
        self.temp.cleanup()

    def counts(self):
        tables = ('revisions', 'heads', 'batches', 'raw_lines', 'jobs',
                  'requests', 'results', 'result_rows')
        return {table: self.w.db.connection.execute(
            'SELECT count(*) FROM ' + table).fetchone()[0] for table in tables}

    def reopen(self):
        self.w.close()
        self.w = Warehouse(self.path)

    def prepare(self):
        plan = self.w.registry.put('meters', SOURCE, 'p')
        batch = self.w.seal(LINES, 'b')
        job = self.w.create_job(batch['batch'], MAPPING, 'j', name='meters')
        return plan, batch, job

    def test_bad_plan(self):
        old = self.w.registry.put('meters', '(a)', 'p')
        before = self.counts()
        with self.assertRaises(sdk1.SyntaxError):
            self.w.registry.put('meters', '(broken', 'bad')
        self.reopen()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.w.registry.get('meters'), old)
        self.w.registry.put('meters', '(b)', 'bad')
        self.assertEqual(len(self.w.registry.history('meters')), 2)

    def test_source_failure(self):
        def broken():
            yield 'first'
            yield 'second'
            raise RuntimeError('source disconnected')
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'source disconnected'):
            self.w.seal(broken(), 'batch')
        self.reopen()
        self.assertEqual(self.counts(), before)
        result = self.w.seal(['first', 'second', 'third'], 'batch')
        self.assertEqual(self.w.batch(result['batch'])['lines'], ['first', 'second', 'third'])
        self.assertEqual(self.w.seal(iter(['first', 'second', 'third']), 'batch'), result)

    def test_bad_binding(self):
        self.w.registry.put('meters', '(a)', 'p')
        batch = self.w.seal(['a'], 'b')
        before = self.counts()
        with self.assertRaises(Invalid):
            self.w.create_job(batch['batch'], {'device': 'g2'}, 'bad', name='meters')
        with self.assertRaises(NotFound):
            self.w.create_job(999, {'device': 'g1'}, 'bad', name='meters')
        self.reopen()
        self.assertEqual(self.counts(), before)
        job = self.w.create_job(batch['batch'], {'device': 'g1'}, 'bad', name='meters')
        self.assertEqual(job['state'], 'pending')

    def test_publication_failure_and_reopen(self):
        old, batch, job = self.prepare()
        history_job = self.w.create_job(batch['batch'], MAPPING, 'historical',
                                        revision=old['revision'])
        self.w.run(history_job['job'])
        historical = self.w.read(history_job['job'])
        self.w.registry.put('meters', '(乙)=(1)', 'p2')

        def fail(phase, detail):
            if phase == 'processing.before_commit':
                raise RuntimeError('publication refused')

        with self.assertRaisesRegex(RuntimeError, 'publication refused'):
            self.w.run(job['job'], checkpoint=fail)
        self.reopen()
        failure = self.w.read(job['job'])
        self.assertEqual(failure['state'], 'failed')
        self.assertIn('publication refused', failure['error'])
        self.assertIsNone(failure['rows'])
        self.assertIsNone(failure['result'])
        self.assertEqual(self.w.db.connection.execute(
            'SELECT count(*) FROM result_rows WHERE job=?', (job['job'],)).fetchone()[0], 0)
        self.assertEqual(self.w.read(history_job['job']), historical)
        receipt = self.w.run(job['job'])
        self.assertEqual(self.w.read(job['job'])['rows'], historical['rows'])
        self.assertEqual(self.w.read(job['job'])['revision'], old['revision'])
        self.assertEqual(self.w.run(job['job']), receipt)
        self.assertEqual(self.w.db.connection.execute(
            'SELECT count(*) FROM results WHERE job=?', (job['job'],)).fetchone()[0], 1)

    def test_mid_execution_failure(self):
        _, _, job = self.prepare()

        def fail(phase, detail):
            if phase == 'processing.row' and detail == 1:
                raise ArithmeticError('line evaluation interrupted')

        with self.assertRaises(ArithmeticError):
            self.w.run(job['job'], checkpoint=fail)
        self.reopen()
        self.assertEqual(self.w.read(job['job'])['state'], 'failed')
        self.assertEqual(self.counts()['result_rows'], 0)
        self.w.run(job['job'])
        self.assertEqual(len(self.w.read(job['job'])['rows']), len(LINES))

    def test_complete_legacy_fixture(self):
        self.w.close()
        ids = build_legacy(self.path)
        self.w = Warehouse(self.path)
        self.assertEqual(len(self.w.registry.history('meters')), 2)
        self.assertEqual(self.w.read(ids['published'])['state'], 'succeeded')
        self.assertEqual(self.w.read(ids['pending'])['state'], 'pending')
        self.assertEqual(self.w.read(ids['failed'])['state'], 'failed')
        frozen = self.w.read(ids['published'])
        requests = [tuple(row) for row in self.w.db.connection.execute(
            'SELECT * FROM requests ORDER BY scope,request')]
        self.reopen()
        self.w.run(ids['pending'])
        self.w.run(ids['failed'])
        self.assertEqual(self.w.read(ids['pending'])['rows'], frozen['rows'])
        self.assertEqual(self.w.read(ids['failed'])['rows'], frozen['rows'])
        self.assertEqual(self.w.read(ids['published']), frozen)
        self.assertEqual([tuple(row) for row in self.w.db.connection.execute(
            'SELECT * FROM requests ORDER BY scope,request')], requests)
        current = self.w.read(ids['current_job'])
        self.assertEqual(current['revision'], ids['current_revision'])
        self.assertNotEqual(current['rows'], frozen['rows'])


if __name__ == '__main__':
    unittest.main()
