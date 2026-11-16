"""Translate ordered-capture/1 sources into symbol-graph/2 and execute graphs.

The parser and graph builder run only when a revision is registered or a legacy
revision is converted.  Record processing never sees this module: it interprets
stored graphs exclusively through ``sdk2.Graph.transitions``/``is_accepting``.

Translation invariants (see MIGRATION_DESIGN.md):

* every capture group owns two registers, ``gN.start`` and ``gN.end``;
* a group entry edge writes the pre-edge position, the matching exit edge writes
  the post-edge position, so the pair is either both null (not participating)
  or a code-point interval;
* alternation branches keep source order as edge order, so depth-first edge
  enumeration is branch order;
* a greedy star tries its body before its exit, which gives greedy fallback
  (no longest-match rule);
* a star body that cannot match the empty string only needs one loop copy; a
  nullable body gets two copies (first/no-consumption and consumed) so the
  epsilon subgraph stays acyclic while still allowing exactly one final empty
  iteration, matching Python 3.11's capture rule for nullable repeats.
"""
import sys
from contextlib import contextmanager

from vendor import sdk1
from vendor import sdk2
from .db import Invalid


class _Parser:
    def __init__(self, source):
        if not isinstance(source, str):
            raise sdk1.SyntaxError('source must be text')
        self.source = source
        self.pos = 0
        self.groups = 0

    def peek(self):
        return self.source[self.pos:self.pos + 1]

    def expression(self):
        branches = [self.sequence()]
        while self.peek() == '|':
            self.pos += 1
            branches.append(self.sequence())
        return ('alt', branches)

    def sequence(self):
        items = []
        while self.peek() and self.peek() not in ')|':
            c = self.peek()
            self.pos += 1
            if c == '(':
                self.groups += 1
                number = self.groups
                body = self.expression()
                if self.peek() != ')':
                    raise sdk1.SyntaxError('unclosed capture')
                self.pos += 1
                node = ('group', number, body)
            elif c == '\\':
                if not self.peek():
                    raise sdk1.SyntaxError('trailing escape')
                node = ('lit', self.peek())
                self.pos += 1
            elif c in '*?+.[]{}^$':
                raise sdk1.SyntaxError('unexpected operator: ' + c)
            else:
                node = ('lit', c)
            if self.peek() and self.peek() in '*?':
                quantifier = self.peek()
                self.pos += 1
                if self.peek() and self.peek() in '*?':
                    raise sdk1.SyntaxError('stacked quantifiers')
                node = ('star' if quantifier == '*' else 'opt', node)
            items.append(node)
        return ('seq', items)

    def parse(self):
        node = self.expression()
        if self.pos != len(self.source):
            raise sdk1.SyntaxError('unmatched closing capture')
        return node


def parse(source):
    """Validate old syntax and return its ordered syntax tree."""
    try:
        parser = _Parser(source)
        node = parser.parse()
    except RecursionError as exc:
        raise sdk1.SyntaxError('capture nesting exceeds interpreter capacity') from exc
    return node, parser.groups


@contextmanager
def _nesting_room(source):
    """Give deeply nested legacy sources roughly the room SDK1's parser had.

    The syntax tree walk here uses more frames per nesting level than SDK1's
    parser, so the interpreter limit is raised in proportion to the source's
    parenthesis depth and restored exactly afterwards.
    """
    previous = sys.getrecursionlimit()
    depth = 0
    if isinstance(source, str):
        level = 0
        escaped = False
        for char in source:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '(':
                level += 1
                depth = max(depth, level)
            elif char == ')':
                level = max(0, level - 1)
    wanted = min(max(previous, 8 * depth + 2000), 100000)
    if wanted == previous:
        yield
        return
    sys.setrecursionlimit(wanted)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


def _minimum(node):
    kind = node[0]
    if kind == 'lit':
        return 1
    if kind == 'seq':
        return sum(_minimum(item) for item in node[1])
    if kind == 'alt':
        return min(_minimum(branch) for branch in node[1])
    if kind in ('group', 'opt'):
        return 0 if kind == 'opt' else _minimum(node[2])
    if kind == 'star':
        return 0
    raise AssertionError(node)


class _Builder:
    def __init__(self):
        self.nodes = {}
        self.counter = 0
        self.registers = set()

    def node(self):
        key = 'n' + str(self.counter)
        self.counter += 1
        self.nodes[key] = {'id': key, 'accept': False, 'edges': []}
        return key

    def edge(self, source, target, symbol=None, writes=()):
        self.nodes[source]['edges'].append(
            {'to': target, 'symbol': symbol,
             'writes': [[register, action] for register, action in writes]})

    def attach(self, node, continuation):
        kind = node[0]
        if kind == 'lit':
            state = self.node()
            self.edge(state, continuation, node[1])
            return state
        if kind == 'seq':
            state = continuation
            for item in reversed(node[1]):
                state = self.attach(item, state)
            return state
        if kind == 'alt':
            state = self.node()
            for branch in node[1]:
                self.edge(state, self.attach(branch, continuation), None)
            return state
        if kind == 'group':
            number = node[1]
            start_register = 'g' + str(number) + '.start'
            end_register = 'g' + str(number) + '.end'
            self.registers.update((start_register, end_register))
            end = self.node()
            self.edge(end, continuation, None, [(end_register, 'after')])
            start = self.node()
            self.edge(start, self.attach(node[2], end), None, [(start_register, 'before')])
            return start
        if kind == 'opt':
            state = self.node()
            self.edge(state, self.attach(node[1], continuation), None)
            self.edge(state, continuation, None)
            return state
        if kind == 'star':
            return self._star(node[1], continuation)
        raise AssertionError(node)

    def _star(self, body, continuation):
        if _minimum(body) > 0:
            # The body always consumes a code point, so one loop copy suffices.
            head = self.node()
            entry = self.attach(body, head)
            self.edge(head, entry, None)
            self.edge(head, continuation, None)
            return head
        # A nullable body needs a no-consumption copy that can run at most once
        # before leaving the loop, plus a consumed copy that returns to the head.
        first = set(self.nodes)
        exit_state = self.node()
        entry = self.attach(body, exit_state)
        states = [key for key in self.nodes if key not in first]
        zero = {key: self.node() for key in states}
        one = {key: self.node() for key in states}
        for key in states:
            for edge in self.nodes[key]['edges']:
                target, symbol, writes = edge['to'], edge['symbol'], edge['writes']
                if symbol is None:
                    self.edge(zero[key], zero[target], None, writes)
                    self.edge(one[key], one[target], None, writes)
                else:
                    self.edge(zero[key], one[target], symbol, writes)
                    self.edge(one[key], one[target], symbol, writes)
        head = self.node()
        self.edge(head, zero[entry], None)
        self.edge(head, continuation, None)
        self.edge(zero[exit_state], continuation, None)
        self.edge(one[exit_state], head, None)
        return head


def compile_source(source):
    """Return (graph, groups) for an SDK1 source. Raises sdk1.SyntaxError.

    Syntax validation runs at the interpreter's normal nesting budget, like
    SDK1's own registration check; only the graph build below needs extra stack
    for the same nesting that the old parser just accepted.
    """
    tree, count = parse(source)
    with _nesting_room(source):
        return _compile_tree(tree, count)


def _compile_tree(tree, count):
    builder = _Builder()
    final = builder.node()
    final_state = builder.nodes[final]
    final_state['accept'] = True
    entry = builder.attach(tree, final)
    graph = {'format': 'symbol-graph/2', 'entry': entry,
             'registers': sorted(builder.registers),
             'nodes': list(builder.nodes.values())}
    sdk2.Graph(graph)
    groups = [{'id': 'g' + str(number),
               'start': 'g' + str(number) + '.start',
               'end': 'g' + str(number) + '.end'}
              for number in range(1, count + 1)]
    _require_acyclic_epsilon(graph)
    return graph, groups


def plan_for_source(source):
    """The public ordered-capture/1 description, compatible with SDK1's output."""
    _, count = parse(source)
    return {'format': 'ordered-capture/1', 'source': source,
            'groups': [{'id': 'g' + str(number), 'number': number}
                       for number in range(1, count + 1)]}


def epsilon_cycle(graph):
    """True when an epsilon-only cycle is reachable anywhere in the graph."""
    nodes = {node['id']: node for node in graph['nodes']}
    status = {}
    for root in nodes:
        if status.get(root, 0):
            continue
        status[root] = 1
        stack = [(root, 0)]
        while stack:
            key, index = stack[-1]
            edges = nodes[key]['edges']
            while index < len(edges) and edges[index]['symbol'] is not None:
                index += 1
            if index == len(edges):
                status[key] = 2
                stack.pop()
                continue
            stack[-1] = (key, index + 1)
            target = edges[index]['to']
            mark = status.get(target, 0)
            if mark == 1:
                return True
            if mark == 0:
                status[target] = 1
                stack.append((target, 0))
    return False


def _require_acyclic_epsilon(graph):
    # Translation guarantees this by construction; the check only turns a future
    # regression into the syntax-category error that registration already uses.
    if epsilon_cycle(graph):
        raise sdk1.SyntaxError('translated graph has an epsilon cycle')


def search(graph, groups, text):
    """Execute one record on a stored graph.

    Candidate starts increase by code point.  At every state the outgoing edges
    are tried in list order, depth first with backtracking; the node's accept
    flag is tried only after every outgoing edge failed.  Registers are copied
    per edge, so a failed path cannot leak writes.

    A ``(state, position)`` pair that produced no accepting continuation is
    never retried: edge selection never reads register values (writes only
    overwrite, and the register set is graph-wide), so failure depends only on
    the state and position.  Memoising failures preserves first-success order
    while keeping nested nullable loops polynomial.
    """
    if not isinstance(text, str):
        raise TypeError('input line must be text')
    failed = set()

    for start in range(len(text) + 1):
        stack = [[graph.entry, start, dict.fromkeys(graph.registers), None]]
        while stack:
            frame = stack[-1]
            if frame[3] is None:
                frame[3] = graph.transitions(frame[0], text, frame[1], frame[2])
            try:
                _, state, position, registers = next(frame[3])
            except StopIteration:
                stack.pop()
                failed.add((frame[0], frame[1]))
                if graph.is_accepting(frame[0]):
                    return {'span': [start, frame[1]],
                            'captures': _captures(groups, frame[2])}
                continue
            if (state, position) in failed:
                continue
            stack.append([state, position, registers, None])
    return None


def _captures(groups, registers):
    captures = {}
    for group in groups:
        start = registers[group['start']]
        end = registers[group['end']]
        if start is None and end is None:
            captures[group['id']] = None
        elif type(start) is int and type(end) is int and start <= end:
            captures[group['id']] = [start, end]
        else:
            raise Invalid('group ' + group['id'] + ' has an invalid register pair')
    return captures
