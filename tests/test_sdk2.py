import copy
import unittest
from vendor.sdk2 import Graph, GraphError


def native_graph():
    return {'format': 'symbol-graph/2', 'entry': 'start', 'registers': ['s', 'e'],
            'nodes': [
                {'id': 'start', 'accept': False, 'edges': [
                    {'to': 'loop', 'symbol': None, 'writes': [['s', 'before']]}]},
                {'id': 'loop', 'accept': True, 'edges': [
                    {'to': 'loop', 'symbol': '甲', 'writes': [['e', 'after']]},
                    {'to': 'loop', 'symbol': None, 'writes': []}]}]}


class SDK2Ports(unittest.TestCase):
    def test_language_and_epsilon_cycle(self):
        graph = Graph(native_graph())
        self.assertTrue(graph.accepts(''))
        self.assertTrue(graph.accepts('甲甲'))
        self.assertFalse(graph.accepts('甲乙'))
        self.assertFalse(graph.accepts('乙甲'))

    def test_transition_writes_and_copy(self):
        payload = native_graph()
        graph = Graph(payload)
        payload['nodes'].clear()
        step = list(graph.transitions('start', '甲', 0))
        self.assertEqual(step, [(0, 'loop', 0, {'s': 0, 'e': None})])
        registers = step[0][3]
        next_steps = list(graph.transitions('loop', '甲', 0, registers))
        self.assertEqual(next_steps[0], (0, 'loop', 1, {'s': 0, 'e': 1}))
        self.assertEqual(registers, {'s': 0, 'e': None})
        saved = graph.dump()
        self.assertEqual(Graph(saved).dump(), saved)
        saved['nodes'].clear()
        self.assertTrue(graph.accepts('甲'))

    def test_bad_graphs_and_arguments(self):
        mutations = [
            lambda p: p.update(entry='missing'),
            lambda p: p['nodes'][0]['edges'][0].update(to='missing'),
            lambda p: p['nodes'][1]['edges'][0].update(symbol='ab'),
            lambda p: p['nodes'][0]['edges'][0].update(writes=[['unknown', 'before']]),
            lambda p: p['nodes'].append(copy.deepcopy(p['nodes'][0])),
            lambda p: p.update(registers=['s', 's']),
            lambda p: p['nodes'][0].update(accept=1),
        ]
        for mutate in mutations:
            payload = native_graph()
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(GraphError):
                Graph(payload)
        graph = Graph(native_graph())
        with self.assertRaises(GraphError):
            list(graph.transitions('start', '甲', 2))
        with self.assertRaises(GraphError):
            list(graph.transitions('start', '甲', 0, {'s': 0}))

    def test_clear_and_edge_order(self):
        payload = native_graph()
        payload['nodes'][1]['edges'][0]['writes'].append(['s', 'clear'])
        graph = Graph(payload)
        steps = list(graph.transitions('loop', '甲', 0, {'s': 0, 'e': None}))
        self.assertEqual([item[0] for item in steps], [0, 1])
        self.assertEqual(steps[0][3], {'s': None, 'e': 1})


if __name__ == '__main__':
    unittest.main()
