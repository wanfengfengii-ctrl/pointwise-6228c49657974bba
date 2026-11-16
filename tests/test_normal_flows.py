import unittest
from warehouse import Database, Warehouse, Conflict, Invalid, NotFound
from warehouse.samples import SOURCE, MAPPING, LINES, build_legacy


class NormalFlows(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.w = Warehouse(self.db)

    def tearDown(self):
        self.db.close()

    def prepare(self, source=SOURCE, lines=LINES, mapping=MAPPING):
        plan = self.w.registry.put('meters', source, 'p')
        batch = self.w.seal(iter(lines), 'b')
        job = self.w.create_job(batch['batch'], mapping, 'j', name='meters')
        return plan, batch, job

    def test_register_and_bind(self):
        plan, batch, job = self.prepare()
        self.assertEqual(plan['plan']['groups'],
                         [{'id': 'g' + str(n), 'number': n} for n in range(1, 5)])
        self.assertEqual(job['revision'], plan['revision'])
        self.assertEqual(job['batch'], batch['batch'])
        self.assertEqual(job['mapping'], MAPPING)
        self.assertEqual(self.w.read(job['job'])['rows'], None)
        self.assertEqual(self.w.read(job['job'])['state'], 'pending')
        self.assertEqual(self.w.batch(batch['batch'])['lines'], LINES)
        changed = dict(MAPPING)
        another = self.w.create_job(batch['batch'], changed, 'j2', name='meters')
        changed['device'] = 'g4'
        self.assertEqual(self.w.job(another['job'])['mapping'], MAPPING)

    def test_order_unicode_and_no_match(self):
        _, batch, job = self.prepare()
        receipt = self.w.run(job['job'])
        self.assertEqual(receipt, {'job': job['job'], 'result': job['job'],
                                   'state': 'succeeded', 'row_count': 4})
        rows = self.w.read(job['job'])['rows']
        self.assertEqual([r['position'] for r in rows], [0, 1, 2, 3])
        self.assertEqual(rows[0], {
            'position': 0, 'matched': True, 'span': [2, 9],
            'captures': {'g1': [2, 5], 'g2': [4, 5], 'g3': [6, 9], 'g4': [8, 9]},
            'fields': {'device': '甲乙甲', 'value': '010', 'last_device': '甲', 'last_digit': '0'}})
        self.assertEqual(rows[1], {
            'position': 1, 'matched': False, 'span': None,
            'captures': dict.fromkeys(['g1', 'g2', 'g3', 'g4']),
            'fields': dict.fromkeys(MAPPING)})
        self.assertEqual(rows[2]['fields']['device'], '乙')
        self.assertEqual(rows[3]['fields'], {
            'device': '', 'value': '', 'last_device': None, 'last_digit': None})
        self.assertEqual(self.w.run(job['job']), receipt)
        self.assertEqual(self.w.batch(batch['batch'])['row_count'], 4)

    def test_fixed_revisions(self):
        old, batch, job = self.prepare('(a)(b)', ['ab', 'ba'], {'device': 'g1', 'value': 'g2'})
        new = self.w.registry.put('meters', '(b)(a)', 'p2')
        new_job = self.w.create_job(batch['batch'], {'device': 'g1', 'value': 'g2'},
                                    'j2', name='meters')
        # Replays bind the original revision even before its first execution.
        self.assertEqual(self.w.create_job(batch['batch'], {'value': 'g2', 'device': 'g1'},
                                          'j', name='meters'), job)
        self.assertEqual(self.w.registry.put('meters', '(a)(b)', 'p'), old)
        self.assertEqual(self.w.registry.get('meters')['revision'], new['revision'])
        self.w.run(job['job'])
        self.w.run(new_job['job'])
        old_result = self.w.read(job['job'])
        new_result = self.w.read(new_job['job'])
        self.assertEqual(old_result['revision'], old['revision'])
        self.assertEqual(new_result['revision'], new['revision'])
        self.assertEqual([r['matched'] for r in old_result['rows']], [True, False])
        self.assertEqual([r['matched'] for r in new_result['rows']], [False, True])
        self.w.registry.put('meters', '()', 'p3')
        self.assertEqual(self.w.read(job['job']), old_result)
        self.assertEqual(len(self.w.registry.history('meters')), 3)

    def test_request_conflicts_and_namespaces(self):
        plan, batch, job = self.prepare()
        self.assertEqual(self.w.seal(LINES, 'b'), batch)
        self.assertEqual(len(batch['digest']), 64)
        with self.assertRaises(Conflict):
            self.w.seal(list(reversed(LINES)), 'b')
        with self.assertRaises(Conflict):
            self.w.registry.put('different', SOURCE, 'p')
        with self.assertRaises(Conflict):
            self.w.create_job(batch['batch'], {'device': 'g2'}, 'j', name='meters')
        # The same token is allowed in different operation namespaces.
        self.w.seal([], 'p')
        self.assertEqual(self.w.job(job['job'])['revision'], plan['revision'])

    def test_empty_batch_and_capture_free_plan(self):
        _, _, job = self.prepare('x', [], {})
        self.assertEqual(self.w.run(job['job'])['row_count'], 0)
        self.assertEqual(self.w.read(job['job'])['rows'], [])
        batch = self.w.seal(['xx', ''], 'b2')
        second = self.w.create_job(batch['batch'], {}, 'j2', name='meters')
        self.w.run(second['job'])
        self.assertEqual([r['fields'] for r in self.w.read(second['job'])['rows']], [{}, {}])

    def test_complete_archive_in_memory(self):
        ids = build_legacy(self.db)
        old = self.w.read(ids['published'])
        self.assertEqual(self.w.read(ids['pending'])['state'], 'pending')
        failed = self.w.read(ids['failed'])
        self.assertEqual(failed['state'], 'failed')
        self.assertIsNone(failed['rows'])
        self.assertIsNone(failed['result'])
        self.w.run(ids['pending'])
        self.w.run(ids['failed'])
        self.assertEqual(self.w.read(ids['pending'])['rows'], old['rows'])
        self.assertEqual(self.w.read(ids['failed'])['rows'], old['rows'])
        self.assertEqual(self.w.read(ids['published']), old)
        with self.assertRaises(Conflict):
            build_legacy(self.db)

    def test_text_roundtrip_and_batch_identity(self):
        lines = ['🙂', 'é', 'é', '\x00', '\ud800', 'a\nb']
        batch = self.w.seal(lines, 'unicode')
        self.assertEqual(self.w.batch(batch['batch'])['lines'], lines)
        self.assertNotEqual(self.w.seal(['é'], 'one')['digest'],
                            self.w.seal(['é'], 'two')['digest'])
        self.assertEqual(self.w.seal(lines, 'unicode'), batch)

    def test_defensive_and_unknown_inputs(self):
        for method in (self.w.job, self.w.read, self.w.run, self.w.batch):
            with self.subTest(method=method.__name__), self.assertRaises(NotFound):
                method(999)
        with self.assertRaises(NotFound):
            self.w.registry.get('absent')
        with self.assertRaises(Invalid):
            self.w.registry.get()
        with self.assertRaises(Invalid):
            self.w.seal(['ok', 3], 'bad')
        with self.assertRaises(Invalid):
            self.w.seal([], '')
        plan = self.w.registry.put('literal', '(字)', 'literal')
        plan['plan']['groups'][0]['id'] = 'corrupted'
        self.assertEqual(self.w.registry.get('literal')['plan']['groups'][0]['id'], 'g1')


if __name__ == '__main__':
    unittest.main()
