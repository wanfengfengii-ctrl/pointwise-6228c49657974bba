import itertools
import unittest
from vendor import sdk1
from warehouse import Database, Warehouse


# Hand-calculated code-point offsets, including captures surviving a later iteration.
CASES = [
    ('(a|aa)(a?)', 'aa', [0, 2], [[0, 1], [1, 2]]),
    ('(a*)a', 'aaa', [0, 3], [[0, 2]]),
    ('(a|b)*', 'aba', [0, 3], [[2, 3]]),
    ('(a(b)?)*', 'aba', [0, 3], [[2, 3], [1, 2]]),
    ('(a)?(b*)', '', [0, 0], [None, [0, 0]]),
    ('(a?)*', 'aa', [0, 2], [[2, 2]]),
    ('((a?)*)', 'a', [0, 1], [[0, 1], [1, 1]]),
    ('(a|)*', 'a', [0, 1], [[1, 1]]),
    ('(()?)*', '', [0, 0], [[0, 0], [0, 0]]),
    ('(a*)*b', 'aab', [0, 3], [[2, 2]]),
    ('(a|aa)*b', 'aab', [0, 3], [[1, 2]]),
    ('(甲|乙)(🙂?)', '噪乙🙂', [1, 3], [[1, 2], [2, 3]]),
    ('()|a', 'a', [0, 0], [[0, 0]]),
    ('(a)(b)', 'zzab', [2, 4], [[2, 3], [3, 4]]),
]


class CaptureSemantics(unittest.TestCase):
    def test_hand_calculated_captures(self):
        for source, text, span, captures in CASES:
            with self.subTest(source=source, text=text):
                actual = sdk1.search(sdk1.compile_plan(source), text)
                self.assertEqual(actual, {
                    'span': span, 'captures': {'g' + str(i + 1): item
                                               for i, item in enumerate(captures)}})

    def test_null_and_empty_in_business_fields(self):
        with Database(':memory:') as db:
            w = Warehouse(db)
            w.registry.put('optional', '(a)?(b*)', 'p')
            batch = w.seal(['', 'a', 'bb'], 'b')
            job = w.create_job(batch['batch'], {'optional': 'g1', 'empty': 'g2'},
                               'j', name='optional')
            w.run(job['job'])
            rows = w.read(job['job'])['rows']
            self.assertEqual(rows[0]['fields'], {'optional': None, 'empty': ''})
            self.assertEqual(rows[0]['captures'], {'g1': None, 'g2': [0, 0]})
            self.assertEqual(rows[1]['fields'], {'optional': 'a', 'empty': ''})
            self.assertEqual(rows[2]['fields'], {'optional': None, 'empty': 'bb'})

    def test_search_leftmost_and_termination(self):
        # Exhaustive small corpus for nullable bodies; every call must finish.
        sources = ['(a?)*', '((a|)*)', '(()?)*', '((a*)?)*b', '(a|aa)(a?)']
        for source in sources:
            plan = sdk1.compile_plan(source)
            for size in range(5):
                for chars in itertools.product('ab', repeat=size):
                    text = ''.join(chars)
                    match = sdk1.search(plan, text)
                    if match is not None:
                        start, end = match['span']
                        self.assertTrue(0 <= start <= end <= len(text))
                        for capture in match['captures'].values():
                            if capture is not None:
                                self.assertTrue(0 <= capture[0] <= capture[1] <= len(text))

    def test_literals_and_syntax_errors(self):
        for source, text in [('a\\*b', 'a*b'), ('\\.', '.'), ('\\1', '1'),
                             ('\\\\', '\\'), ('\n', '\n'), ('', '')]:
            with self.subTest(source=source):
                self.assertIsNotNone(sdk1.search(sdk1.compile_plan(source), text))
        for source in ['(', ')', 'a**', 'a??', '*a', '[a]', 'a+', '^a', 'a{2}', '.',
                       '(?=a)', '(?:a)', 'a\\']:
            with self.subTest(source=source), self.assertRaises(sdk1.SyntaxError):
                sdk1.compile_plan(source)
        bad = sdk1.compile_plan('(x)')
        bad['groups'][0]['id'] = 'g9'
        with self.assertRaises(sdk1.SyntaxError):
            sdk1.load(bad)
        self.assertIsNone(sdk1.search(sdk1.compile_plan('(x)'), 'y'))


if __name__ == '__main__':
    unittest.main()
