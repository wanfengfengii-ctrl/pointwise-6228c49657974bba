"""Explicit symbol graph, version 2. No ordered-capture matching policy."""
import json


class GraphError(ValueError):
    pass


class Graph:
    def __init__(self, payload):
        try:
            data = json.loads(json.dumps(payload, ensure_ascii=True, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise GraphError('graph must be JSON data') from exc
        if not isinstance(data, dict) or set(data) != {'format', 'entry', 'registers', 'nodes'}:
            raise GraphError('invalid graph header')
        if data['format'] != 'symbol-graph/2':
            raise GraphError('wrong graph version')
        registers = data['registers']
        nodes = data['nodes']
        if (not isinstance(registers, list)
                or any(not isinstance(r, str) or not r for r in registers)
                or len(set(registers)) != len(registers)):
            raise GraphError('invalid registers')
        if not isinstance(nodes, list) or not nodes:
            raise GraphError('nodes must be a nonempty list')
        by_id = {}
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {'id', 'accept', 'edges'}:
                raise GraphError('invalid node')
            key = node['id']
            if not isinstance(key, str) or not key or key in by_id:
                raise GraphError('invalid or duplicate node id')
            if type(node['accept']) is not bool or not isinstance(node['edges'], list):
                raise GraphError('invalid accept flag or edges')
            by_id[key] = node
        if not isinstance(data['entry'], str) or data['entry'] not in by_id:
            raise GraphError('missing entry')
        for node in nodes:
            for edge in node['edges']:
                if not isinstance(edge, dict) or set(edge) != {'to', 'symbol', 'writes'}:
                    raise GraphError('invalid edge')
                if not isinstance(edge['to'], str) or edge['to'] not in by_id:
                    raise GraphError('dangling edge')
                symbol = edge['symbol']
                if symbol is not None and (not isinstance(symbol, str) or len(symbol) != 1):
                    raise GraphError('symbol must be null or one code point')
                if not isinstance(edge['writes'], list):
                    raise GraphError('invalid writes')
                for write in edge['writes']:
                    if (not isinstance(write, list) or len(write) != 2
                            or not isinstance(write[0], str) or write[0] not in registers
                            or write[1] not in ('before', 'after', 'clear')):
                        raise GraphError('invalid register write')
        self._data = data
        self._nodes = by_id
        self.entry = data['entry']
        self.registers = tuple(registers)

    def dump(self):
        return json.loads(json.dumps(self._data))

    def is_accepting(self, state):
        return self._nodes[state]['accept']

    def transitions(self, state, text, position, registers=None):
        """One edge only. Returned edge index is identity, not a priority decision."""
        if not isinstance(text, str) or type(position) is not int or not 0 <= position <= len(text):
            raise GraphError('invalid input position')
        values = dict.fromkeys(self.registers)
        if registers is not None:
            if set(registers) != set(self.registers):
                raise GraphError('register set differs')
            for value in registers.values():
                if value is not None and (type(value) is not int or not 0 <= value <= len(text)):
                    raise GraphError('invalid register position')
            values.update(registers)
        for index, edge in enumerate(self._nodes[state]['edges']):
            symbol = edge['symbol']
            if symbol is None:
                end = position
            elif position < len(text) and text[position] == symbol:
                end = position + 1
            else:
                continue
            updated = values.copy()
            for register, action in edge['writes']:
                updated[register] = {'before': position, 'after': end, 'clear': None}[action]
            yield index, edge['to'], end, updated

    def _closure(self, states):
        reached = set(states)
        pending = list(states)
        while pending:
            for edge in self._nodes[pending.pop()]['edges']:
                if edge['symbol'] is None and edge['to'] not in reached:
                    reached.add(edge['to'])
                    pending.append(edge['to'])
        return reached

    def accepts(self, text):
        """Whole-string language membership. Register writes do not affect membership."""
        if not isinstance(text, str):
            raise GraphError('text required')
        states = self._closure({self.entry})
        for char in text:
            states = self._closure({edge['to'] for state in states
                                    for edge in self._nodes[state]['edges']
                                    if edge['symbol'] == char})
        return any(self.is_accepting(state) for state in states)
