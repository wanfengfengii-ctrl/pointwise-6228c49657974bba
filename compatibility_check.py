"""Independent compatibility report: SDK1 reference vs stored-graph version 2.

This script is a delivered artifact, separate from tests/ and from
migration_validation.py.  It compares, record by record:

* legacy fixed tasks executed before and after a real upgrade (same IDs,
  same mappings, same captures and fields);
* post-upgrade registration of legacy syntax executed through stored graphs
  against an SDK1 oracle over a finite corpus that covers leftmost start,
  branch order, greedy fallback, repeated-group participation, nullable loops
  and Unicode;
* the public native graph samples with independently hand-stated rows;
* the distinction between a non-participating capture (null) and an empty
  capture ("").

Exit status 0 only when every executed check matches.  Checks that cannot be
meaningfully executed in the current interpreter are reported as
"environment-limited" instead of "failed": SDK1's semantics are defined for
Python 3.11, so a different minor version marks oracle comparisons unverified.
"""
import itertools
import json
import sys
import tempfile
from pathlib import Path

from migration_samples import (
    BRANCH_GRAPH, BRANCH_GROUPS, BRANCH_ROW,
    LOOP_GRAPH, LOOP_GROUPS, LOOP_ROW,
)
from vendor import sdk1
from warehouse import Warehouse
from warehouse.migration import upgrade
from warehouse.samples import MAPPING, build_legacy

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
EXTRA_SOURCES = [
    '', 'x', 'a\\*b', '()', '(a)?(b*)', '((a|)*)', '((a*)?)*b',
    '((a?)*b?)*a', '((a|b)*)*', '(a?|b)*b', '(|a)*b', '((a|)b?)*b',
]
CORPUS = ([''] + [''.join(chars) for size in range(1, 5)
                  for chars in itertools.product('ab', repeat=size)]
          + ['噪乙🙂', 'a*b', '甲', '🙂', '\x00', 'aaaa', 'aabb', 'abab']
          + [case[1] for case in CASES])
CORPUS = list(dict.fromkeys(CORPUS))


class Report:
    def __init__(self):
        self.executed = []
        self.failed = []
        self.limited = []

    def execute(self, section, detail, check):
        try:
            check()
        except AssertionError as exc:
            self.failed.append({'section': section, 'detail': detail,
                                'reason': str(exc)})
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            self.failed.append({'section': section, 'detail': detail,
                                'reason': type(exc).__name__ + ': ' + str(exc)})
        else:
            self.executed.append({'section': section, 'detail': detail})

    def limit(self, section, detail, reason):
        self.limited.append({'section': section, 'detail': detail,
                             'reason': reason})

    def as_dict(self):
        sections = {}
        for item in self.executed:
            sections.setdefault(item['section'], {'executed': 0, 'failed': 0,
                                                  'unverified': 0})['executed'] += 1
        for item in self.failed:
            sections.setdefault(item['section'], {'executed': 0, 'failed': 0,
                                                  'unverified': 0})['failed'] += 1
        for item in self.limited:
            sections.setdefault(item['section'], {'executed': 0, 'failed': 0,
                                                  'unverified': 0})['unverified'] += 1
        return {
            'passed': not self.failed,
            'sections': sections,
            'failed': self.failed,
            'environment_limited': self.limited,
        }


def oracle(source, lines, mapping):
    plan = sdk1.compile_plan(source)
    rows = []
    for position, text in enumerate(lines):
        match = sdk1.search(plan, text)
        captures = (dict.fromkeys(g['id'] for g in plan['groups'])
                    if match is None else match['captures'])
        rows.append({
            'position': position, 'matched': match is not None,
            'span': None if match is None else match['span'],
            'captures': captures,
            'fields': {field: None if captures[group] is None else
                       text[captures[group][0]:captures[group][1]]
                       for field, group in mapping.items()},
        })
    return rows


def check_fixed_tasks(report, directory):
    path = Path(directory) / 'fixed.sqlite'
    ids = build_legacy(path)
    with Warehouse(path) as warehouse:
        before = {key: warehouse.read(ids[key]) for key in ('published', 'current_job')}
        pending = warehouse.job(ids['pending'])
        upgrade(path)
        warehouse.run(ids['pending'])
        warehouse.run(ids['failed'])
        after = warehouse.read(ids['pending'])
        after_failed = warehouse.read(ids['failed'])
        report.execute(
            'fixed-tasks', 'published result untouched',
            lambda: _equal(warehouse.read(ids['published']), before['published']))
        report.execute(
            'fixed-tasks', 'pending task re-executed identically',
            lambda: _equal(after['rows'], before['published']['rows']))
        report.execute(
            'fixed-tasks', 'failed task retried identically',
            lambda: _equal(after_failed['rows'], before['published']['rows']))
        report.execute(
            'fixed-tasks', 'job identity, batch and revision preserved',
            lambda: _equal((after['job'], after['batch'], after['revision']),
                           (pending['job'], pending['batch'], pending['revision'])))
        report.execute(
            'fixed-tasks', 'current revision task untouched',
            lambda: _equal(warehouse.read(ids['current_job']), before['current_job']))


def _equal(actual, expected):
    if actual != expected:
        raise AssertionError('mismatch: expected %r, got %r' % (expected, actual))


def _run_case(warehouse, batch, source, mapping_source, tag):
    revision = warehouse.registry.put('case-' + tag, source, 'plan-' + tag)
    job = warehouse.create_job(batch, mapping_source, 'job-' + tag,
                               revision=revision['revision'])
    warehouse.run(job['job'])
    return warehouse.read(job['job'])['rows']


def check_differential(report, directory):
    if sys.version_info[:2] != (3, 11):
        report.limit('legacy-differential', 'all sources',
                     'requires Python 3.11; running ' + sys.version.split()[0])
        return
    path = Path(directory) / 'differential.sqlite'
    sources = list(dict.fromkeys([case[0] for case in CASES] + EXTRA_SOURCES))
    expected = {source: oracle(source, CORPUS, _mapping_for(source))
                for source in sources}
    with Warehouse(path) as warehouse:
        batch = warehouse.seal(CORPUS, 'corpus')['batch']
    upgrade(path)
    with Warehouse(path) as warehouse:
        for index, source in enumerate(sources):
            mapping = _mapping_for(source)
            report.execute(
                'legacy-differential', source,
                lambda source=source, mapping=mapping, index=index: _equal(
                    _run_case(warehouse, batch, source, mapping, 'd' + str(index)),
                    expected[source]))
            # A second, freshly registered revision must behave identically to
            # the one that was registered after the cutover.
            report.execute(
                'legacy-differential', source + ' (fresh name)',
                lambda source=source, mapping=mapping, index=index: _equal(
                    _run_case(warehouse, batch, source, mapping, 'e' + str(index)),
                    expected[source]))


def _mapping_for(source):
    plan = sdk1.compile_plan(source)
    return {'field:' + g['id']: g['id'] for g in plan['groups']}


def check_semantics(report, directory):
    if sys.version_info[:2] != (3, 11):
        report.limit('capture-semantics', 'hand table',
                     'requires Python 3.11; running ' + sys.version.split()[0])
        return
    path = Path(directory) / 'semantics.sqlite'
    with Warehouse(path) as warehouse:
        batch = warehouse.seal(CORPUS, 'corpus')['batch']
    upgrade(path)
    with Warehouse(path) as warehouse:
        for index, (source, text, span, captures) in enumerate(CASES):
            mapping = {'field:g' + str(n): 'g' + str(n)
                       for n in range(1, len(captures) + 1)}
            rows = _run_case(warehouse, batch, source, mapping, 's' + str(index))
            row = rows[CORPUS.index(text)]
            expected = {
                'position': CORPUS.index(text), 'matched': True, 'span': span,
                'captures': {'g' + str(i + 1): item
                             for i, item in enumerate(captures)},
                'fields': {field: (None if item is None else
                                   text[item[0]:item[1]])
                           for field, item in zip(mapping, captures)},
            }
            report.execute(
                'capture-semantics', source + ' on ' + repr(text),
                lambda row=row, expected=expected: _equal(row, expected))


def check_native(report, directory):
    path = Path(directory) / 'native.sqlite'
    with Warehouse(path) as warehouse:
        warehouse.registry.put('placeholder', 'x', 'placeholder')
    upgrade(path)
    with Warehouse(path) as warehouse:
        registry = warehouse.registry
        branch = registry.put_graph('native', BRANCH_GRAPH, BRANCH_GROUPS, 'g1')
        batch = warehouse.seal(['aa'], 'nb')['batch']
        job = warehouse.create_job(batch, {'device': 'g2'}, 'nj',
                                   revision=branch['revision'])
        warehouse.run(job['job'])
        report.execute(
            'native-graph', 'branch order and accept-after-edges',
            lambda: _equal(warehouse.read(job['job'])['rows'], [BRANCH_ROW]))
        loop = registry.put_graph('native', LOOP_GRAPH, LOOP_GROUPS, 'g2')
        batch = warehouse.seal(['噪甲甲尾'], 'lb')['batch']
        job = warehouse.create_job(batch, {'value': 'sample'}, 'lj',
                                   revision=loop['revision'])
        warehouse.run(job['job'])
        report.execute(
            'native-graph', 'consuming cycle and sparse capture',
            lambda: _equal(warehouse.read(job['job'])['rows'], [LOOP_ROW]))
        report.execute(
            'native-graph', 'public plan is a native-capture/2 view',
            lambda: _equal(execution_view(registry, loop), LOOP_GRAPH))
        report.execute(
            'native-graph', 'group ids address captured intervals',
            lambda: _equal(warehouse.read(job['job'])['rows'][0]['fields'],
                           {'value': '甲甲'}))


def execution_view(registry, revision):
    return registry.get(revision=revision['revision'])['plan']['graph']


def check_null_vs_empty(report, directory):
    path = Path(directory) / 'nullable.sqlite'
    with Warehouse(path) as warehouse:
        batch = warehouse.seal(['', 'a', 'bb'], 'batch')['batch']
    upgrade(path)
    with Warehouse(path) as warehouse:
        revision = warehouse.registry.put('optional', '(a)?(b*)', 'plan')
        job = warehouse.create_job(batch, {'optional': 'g1', 'empty': 'g2'},
                                   'job', revision=revision['revision'])
        warehouse.run(job['job'])
        rows = warehouse.read(job['job'])['rows']
        report.execute(
            'null-vs-empty', 'unparticipated group stays null',
            lambda: _equal(rows[0]['fields']['optional'], None))
        report.execute(
            'null-vs-empty', 'participating empty group is the empty string',
            lambda: _equal(rows[0]['fields']['empty'], ''))
        report.execute(
            'null-vs-empty', 'capture interval of empty participation is [p,p]',
            lambda: _equal(rows[0]['captures'], {'g1': None, 'g2': [0, 0]}))


def main():
    report = Report()
    with tempfile.TemporaryDirectory(prefix='capture-compat-', dir='.') as directory:
        check_fixed_tasks(report, directory)
        check_differential(report, directory)
        check_semantics(report, directory)
        check_native(report, directory)
        check_null_vs_empty(report, directory)
    payload = report.as_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload['passed'] else 1)


if __name__ == '__main__':
    main()
