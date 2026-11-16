"""Delivery-level checks for the version 2 representation and cutover.

These extend, never replace, the original regression in this directory.  They
assert the representation invariants that the stored graphs must satisfy and
the boundaries of the upgrade/native write paths that the original suite does
not exercise.
"""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_samples import (BRANCH_GRAPH, BRANCH_GROUPS, LOOP_GRAPH,
                               LOOP_GROUPS)
from vendor import sdk1, sdk2
from warehouse import Invalid, NotFound, Conflict, Registry, Warehouse
from warehouse import translation
from warehouse.migration import upgrade
from warehouse.samples import LINES, MAPPING, SOURCE, build_legacy

OLD_TABLES = ('revisions', 'heads', 'batches', 'raw_lines', 'jobs',
              'results', 'result_rows', 'requests')


def stored_execution(path):
    with sqlite3.connect(str(path)) as con:
        return [json.loads(row[0]) for row in con.execute(
            'SELECT execution FROM target_revisions ORDER BY id')]


class Representation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='capture-repr-', dir='.')
        self.path = Path(self.directory.name) / 'archive.sqlite'

    def tearDown(self):
        self.directory.cleanup()

    def test_converted_graphs_are_independent_sdk2_payloads(self):
        sources = [SOURCE, '(乙)=(1)', '(a?)*', '(()?)*', '((a|)*)', '']
        with Warehouse(self.path) as warehouse:
            for index, source in enumerate(sources):
                warehouse.registry.put('p' + str(index), source, 'p' + str(index))
        upgrade(self.path)
        with Registry(self.path) as registry:
            for index, source in enumerate(sources):
                revision = registry.history('p' + str(index))[0]
                execution = registry.execution(revision['revision'])
                graph = sdk2.Graph(execution['graph'])       # validates the format
                self.assertEqual(graph.dump(), execution['graph'])
                self.assertFalse(translation.epsilon_cycle(execution['graph']))
                self.assertEqual(revision['plan'], sdk1.compile_plan(source))
                ids = [entry['id'] for entry in execution['groups']]
                self.assertEqual(ids, [g['id'] for g in revision['plan']['groups']])
                registers = {entry['start'] for entry in execution['groups']}
                registers |= {entry['end'] for entry in execution['groups']}
                self.assertTrue(registers <= set(graph.registers))
                # Every group has exactly one start and one end register and the
                # pair is only written as a pair.
                for entry in execution['groups']:
                    self.assertNotEqual(entry['start'], entry['end'])

    def test_execution_payload_has_no_source_or_syntax_tree(self):
        with Warehouse(self.path) as warehouse:
            warehouse.registry.put('meters', SOURCE, 'p')
        upgrade(self.path)
        (execution,) = stored_execution(self.path)
        self.assertEqual(set(execution), {'format', 'graph', 'groups'})
        self.assertEqual(execution['format'], 'graph-capture/2')
        serialized = str(execution)
        self.assertNotIn('source', serialized)
        self.assertNotIn('ordered-capture/1', serialized)

    def test_old_tables_are_audit_only_after_cutover(self):
        ids = build_legacy(self.path)
        frozen = self._audit()
        upgrade(self.path)
        with Warehouse(self.path) as warehouse:
            receipt = warehouse.run(ids['pending'])
            self.assertEqual(receipt['state'], 'succeeded')
            # Mutating the audit tables must not affect business reads.
            with sqlite3.connect(str(self.path)) as con:
                con.execute("UPDATE jobs SET mapping='{\"device\":\"g3\"}' WHERE id=?",
                            (ids['published'],))
                con.execute("DELETE FROM result_rows WHERE job=?", (ids['published'],))
            self.assertEqual(warehouse.read(ids['published'])['rows'], frozen_rows(ids))
        self.assertEqual(self._audit()['revisions'], frozen['revisions'])

    def _audit(self):
        with sqlite3.connect(str(self.path)) as con:
            return {table: con.execute('SELECT * FROM ' + table + ' ORDER BY rowid')
                    .fetchall() for table in OLD_TABLES}


def frozen_rows(ids):
    from warehouse.samples import build_legacy as build
    with tempfile.TemporaryDirectory(prefix='capture-frozen-', dir='.') as directory:
        path = Path(directory) / 'frozen.sqlite'
        build(path)
        with Warehouse(path) as warehouse:
            return warehouse.read(ids['published'])['rows']


class CutoverBoundaries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='capture-cut-', dir='.')
        self.path = Path(self.directory.name) / 'archive.sqlite'

    def tearDown(self):
        self.directory.cleanup()

    def test_put_graph_requires_version_two(self):
        with Warehouse(self.path) as warehouse:
            with self.assertRaises(Invalid):
                warehouse.registry.put_graph('native', BRANCH_GRAPH, BRANCH_GROUPS, 'r')
            self.assertEqual(warehouse.registry.history('native'), [])
            with self.assertRaises(Invalid):
                warehouse.registry.put_graph('native', BRANCH_GRAPH, BRANCH_GROUPS, '')

    def test_put_graph_validation_leaves_nothing_behind(self):
        upgrade(self.path)
        with Warehouse(self.path) as warehouse:
            bad_graph = copy.deepcopy(BRANCH_GRAPH)
            bad_graph['entry'] = 'absent'
            with self.assertRaises(sdk2.GraphError):
                warehouse.registry.put_graph('native', bad_graph, BRANCH_GROUPS, 'r')
            with self.assertRaises(Invalid):
                warehouse.registry.put_graph(
                    'native', BRANCH_GRAPH,
                    [{'id': 'g2', 'start': 'E', 'end': 'E', 'extra': True}], 'r')
            with self.assertRaises(Invalid):
                warehouse.registry.put_graph(
                    'native', BRANCH_GRAPH,
                    [{'id': 'g2', 'start': 'E', 'end': 'missing'}], 'r')
            with self.assertRaises(Invalid):
                warehouse.registry.put_graph(
                    'native', BRANCH_GRAPH,
                    [{'id': 'g2', 'start': 'E', 'end': 'E'},
                     {'id': 'g2', 'start': 'B', 'end': 'E'}], 'r')
            cyclic = copy.deepcopy(LOOP_GRAPH)
            cyclic['nodes'][0]['edges'].append(
                {'to': 'loop', 'symbol': None, 'writes': []})
            with self.assertRaises(Invalid):
                warehouse.registry.put_graph('native', cyclic, LOOP_GROUPS, 'r')
            self.assertEqual(warehouse.registry.history('native'), [])

    def test_put_graph_request_namespace_and_head(self):
        upgrade(self.path)
        with Warehouse(self.path) as warehouse:
            registry = warehouse.registry
            first = registry.put_graph('meters', BRANCH_GRAPH, BRANCH_GROUPS, 'shared')
            self.assertEqual(registry.get('meters'), first)
            second = registry.put_graph('meters', LOOP_GRAPH, LOOP_GROUPS, 'later')
            self.assertEqual(registry.get('meters'), second)
            self.assertEqual(
                registry.put_graph('meters', BRANCH_GRAPH, BRANCH_GROUPS, 'shared'),
                first)
            self.assertEqual(registry.get('meters'), second)
            with self.assertRaises(Conflict):
                registry.put('meters', '(a)', 'shared')
            with self.assertRaises(Conflict):
                registry.put_graph('meters', LOOP_GRAPH, LOOP_GROUPS, 'shared')

    def test_native_plan_is_an_isolated_copy(self):
        upgrade(self.path)
        graph = copy.deepcopy(BRANCH_GRAPH)
        groups = copy.deepcopy(BRANCH_GROUPS)
        with Warehouse(self.path) as warehouse:
            revision = warehouse.registry.put_graph('native', graph, groups, 'r')
            graph['nodes'].clear()
            groups.clear()
            saved = warehouse.registry.get(revision=revision['revision'])['plan']
            self.assertEqual(saved, {'format': 'native-capture/2',
                                     'graph': BRANCH_GRAPH, 'groups': BRANCH_GROUPS})
            returned = warehouse.registry.get(revision=revision['revision'])['plan']
            returned['graph']['nodes'].clear()
            self.assertEqual(
                warehouse.registry.get(revision=revision['revision'])['plan']['graph'],
                BRANCH_GRAPH)

    def test_native_invalid_register_pair_fails_the_batch(self):
        upgrade(self.path)
        graph = {'format': 'symbol-graph/2', 'entry': 'e', 'registers': ['s', 't'],
                 'nodes': [
                     {'id': 'e', 'accept': False, 'edges': [
                         {'to': 'done', 'symbol': None, 'writes': [['s', 'before']]}]},
                     {'id': 'done', 'accept': True, 'edges': []}]}
        groups = [{'id': 'g', 'start': 's', 'end': 't'}]
        with Warehouse(self.path) as warehouse:
            revision = warehouse.registry.put_graph('native', graph, groups, 'r')
            batch = warehouse.seal(['x'], 'b')['batch']
            job = warehouse.create_job(batch, {'field': 'g'}, 'j',
                                       revision=revision['revision'])
            with self.assertRaises(Invalid):
                warehouse.run(job['job'])
            failed = warehouse.read(job['job'])
            self.assertEqual(failed['state'], 'failed')
            self.assertIsNone(failed['rows'])
            self.assertIsNone(failed['result'])

    def test_native_group_not_participating_stays_null(self):
        upgrade(self.path)
        graph = {'format': 'symbol-graph/2', 'entry': 'e', 'registers': ['s', 't'],
                 'nodes': [
                     {'id': 'e', 'accept': False, 'edges': [
                         {'to': 'done', 'symbol': None, 'writes': []}]},
                     {'id': 'done', 'accept': True, 'edges': []}]}
        groups = [{'id': 'g', 'start': 's', 'end': 't'}]
        with Warehouse(self.path) as warehouse:
            revision = warehouse.registry.put_graph('native', graph, groups, 'r')
            batch = warehouse.seal(['', 'x'], 'b')['batch']
            job = warehouse.create_job(batch, {'field': 'g'}, 'j',
                                       revision=revision['revision'])
            warehouse.run(job['job'])
            rows = warehouse.read(job['job'])['rows']
            self.assertEqual(rows[0]['captures'], {'g': None})
            self.assertEqual(rows[0]['fields'], {'field': None})
            self.assertEqual(rows[0]['span'], [0, 0])

    def test_same_name_replacement_does_not_rebind_tasks(self):
        upgrade(self.path)
        with Warehouse(self.path) as warehouse:
            old = warehouse.registry.put('meters', '(a)(b)', 'p1')
            batch = warehouse.seal(['ab'], 'b')['batch']
            job = warehouse.create_job(batch, {'device': 'g1'}, 'j', name='meters')
            new = warehouse.registry.put('meters', '(b)(a)', 'p2')
            self.assertEqual(warehouse.registry.get('meters')['revision'],
                             new['revision'])
            warehouse.run(job['job'])
            self.assertEqual(warehouse.read(job['job'])['rows'][0]['fields'],
                             {'device': 'a'})
            self.assertEqual(warehouse.job(job['job'])['revision'], old['revision'])
            self.assertEqual(warehouse.registry.get('meters')['revision'],
                             new['revision'])

    def test_batch_and_request_replay_survive_cutover(self):
        ids = build_legacy(self.path)
        upgrade(self.path)
        with Warehouse(self.path) as warehouse:
            replayed = warehouse.seal(LINES, 'batch-1')
            self.assertEqual(replayed['batch'], ids['batch'])
            self.assertEqual(warehouse.batch(ids['batch'])['lines'], LINES)
            self.assertEqual(
                warehouse.registry.put('meters', SOURCE, 'plan-1')['revision'],
                ids['old_revision'])
            self.assertEqual(warehouse.registry.get('meters')['revision'],
                             ids['current_revision'])
            with self.assertRaises(Conflict):
                warehouse.seal(['other'], 'batch-1')
            with self.assertRaises(NotFound):
                warehouse.job(999)

    def test_repeated_upgrade_is_a_no_op(self):
        build_legacy(self.path)
        upgrade(self.path)
        with sqlite3.connect(str(self.path)) as con:
            before = tuple(con.iterdump())
        calls = []
        upgrade(self.path, checkpoint=lambda *event: calls.append(event))
        self.assertEqual(calls, [])
        with sqlite3.connect(str(self.path)) as con:
            self.assertEqual(tuple(con.iterdump()), before)

    def test_empty_archive_converts_in_place(self):
        with Warehouse(self.path):
            pass
        upgrade(self.path)
        with Warehouse(self.path) as warehouse:
            self.assertEqual(warehouse.db.storage_version(), '2')
            revision = warehouse.registry.put('meters', SOURCE, 'p')
            batch = warehouse.seal(LINES, 'b')['batch']
            job = warehouse.create_job(batch, MAPPING, 'j', name='meters')
            warehouse.run(job['job'])
            self.assertEqual(len(warehouse.read(job['job'])['rows']), len(LINES))
            self.assertEqual(revision['revision'], 1)

    def test_live_objects_follow_the_cutover(self):
        with Warehouse(self.path) as warehouse:
            warehouse.registry.put('meters', SOURCE, 'p')
            upgrade(self.path)
            # The already-open object must use the target tables from now on.
            batch = warehouse.seal(LINES, 'b')['batch']
            job = warehouse.create_job(batch, MAPPING, 'j', name='meters')
            warehouse.run(job['job'])
            self.assertEqual(warehouse.read(job['job'])['rows'][0]['fields']['device'],
                             '甲乙甲')
            native = warehouse.registry.put_graph(
                'meters', BRANCH_GRAPH, BRANCH_GROUPS, 'native')
            self.assertEqual(native['plan']['format'], 'native-capture/2')


class ExecutionBoundaries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='capture-exec-', dir='.')
        self.path = Path(self.directory.name) / 'archive.sqlite'
        upgrade(self.path)
        self.warehouse = Warehouse(self.path)

    def tearDown(self):
        self.warehouse.close()
        self.directory.cleanup()

    def run_rows(self, source, lines, mapping):
        revision = self.warehouse.registry.put('meters', source, 'p' + str(id(lines)))
        batch = self.warehouse.seal(lines, 'b' + str(id(lines)))['batch']
        job = self.warehouse.create_job(batch, mapping, 'j' + str(id(lines)),
                                        revision=revision['revision'])
        self.warehouse.run(job['job'])
        return self.warehouse.read(job['job'])['rows']

    def test_nullable_loops_terminate_on_long_inputs(self):
        cases = [('((a?)*)*b', [0, 65]), ('(a*)*b', [0, 65]),
                 ('((a|b)*)*c', [0, 65]), ('((a|)*)*b', [0, 65]),
                 ('(()?)*b', [64, 65])]
        for source, span in cases:
            with self.subTest(source=source):
                tail = source[-1]
                rows = self.run_rows(source, ['a' * 64 + tail], {'f': 'g1'})
                self.assertTrue(rows[0]['matched'])
                self.assertEqual(rows[0]['span'], span)

    def test_leftmost_start_and_greedy_fallback_order(self):
        rows = self.run_rows('(a|aa)(a?)', ['zaa'], {'f': 'g1'})
        self.assertEqual(rows[0]['span'], [1, 3])
        self.assertEqual(rows[0]['captures']['g1'], [1, 2])

    def test_capture_survives_later_iteration(self):
        rows = self.run_rows('(a(b)?)*', ['aba'], {'whole': 'g1', 'inner': 'g2'})
        self.assertEqual(rows[0]['captures'], {'g1': [2, 3], 'g2': [1, 2]})
        self.assertEqual(rows[0]['fields'], {'whole': 'a', 'inner': 'b'})


if __name__ == '__main__':
    unittest.main()
