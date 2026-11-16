import json
from vendor import sdk1, sdk2
from . import translation
from .db import attach, encode, token, Invalid, NotFound


def _graph_copy(graph):
    """Validate a symbol-graph/2 payload and return an isolated JSON copy."""
    return sdk2.Graph(graph).dump()


def _group_directory(groups, graph):
    if not isinstance(groups, list):
        raise Invalid('groups must be an ordered list')
    seen = set()
    directory = []
    for entry in groups:
        if not isinstance(entry, dict) or set(entry) != {'id', 'start', 'end'}:
            raise Invalid('group entry must have exactly id, start and end')
        identifier = entry['id']
        if not isinstance(identifier, str) or not identifier:
            raise Invalid('group id must be nonempty text')
        if identifier in seen:
            raise Invalid('duplicate group id')
        seen.add(identifier)
        for key in ('start', 'end'):
            register = entry[key]
            if not isinstance(register, str) or register not in graph.registers:
                raise Invalid('group ' + identifier + ' references an unknown register')
        directory.append({'id': identifier, 'start': entry['start'], 'end': entry['end']})
    return directory


class Registry:
    def __init__(self, path_or_database):
        self.db, self._owns = attach(path_or_database)

    def put(self, name, source, request_id):
        token(name, 'name')
        token(request_id, 'request_id')
        if self.db.is_v2():
            graph, groups = translation.compile_source(source)
            public = translation.plan_for_source(source)
            payload = {'name': name, 'source': source}
            with self.db.transaction() as con:
                revision = self.db.replay('plan', request_id, payload)
                if revision is None:
                    revision = con.execute(
                        'INSERT INTO target_revisions(name,plan,execution) VALUES(?,?,?)',
                        (name, encode(public),
                         encode({'format': 'graph-capture/2', 'graph': graph,
                                 'groups': groups}))).lastrowid
                    con.execute(
                        'INSERT INTO target_heads VALUES(?,?) ON CONFLICT(name) '
                        'DO UPDATE SET revision=excluded.revision', (name, revision))
                    self.db.remember('plan', request_id, payload, revision)
                return self.get(revision=revision)
        plan = sdk1.compile_plan(source)
        payload = {'name': name, 'source': source}
        with self.db.transaction() as con:
            revision = self.db.replay('plan', request_id, payload)
            if revision is None:
                revision = con.execute('INSERT INTO revisions(name,plan) VALUES(?,?)',
                                       (name, json.dumps(plan, ensure_ascii=True))).lastrowid
                con.execute('INSERT INTO heads VALUES(?,?) ON CONFLICT(name) '
                            'DO UPDATE SET revision=excluded.revision', (name, revision))
                self.db.remember('plan', request_id, payload, revision)
            return self.get(revision=revision)

    def put_graph(self, name, graph, groups, request_id):
        """Register a native symbol-graph/2 revision (version 2 archives only)."""
        if not self.db.is_v2():
            raise Invalid('native graph plans require storage version 2')
        token(name, 'name')
        token(request_id, 'request_id')
        validated = sdk2.Graph(graph)  # GraphError before any business object
        directory = _group_directory(groups, validated)
        if translation.epsilon_cycle(validated.dump()):
            raise Invalid('native graph has a cyclic epsilon subgraph')
        stored_graph = validated.dump()
        payload = {'name': name, 'graph': graph, 'groups': groups}
        plan = {'format': 'native-capture/2', 'graph': stored_graph, 'groups': directory}
        with self.db.transaction() as con:
            revision = self.db.replay('plan', request_id, payload)
            if revision is None:
                revision = con.execute(
                    'INSERT INTO target_revisions(name,plan,execution) VALUES(?,?,?)',
                    (name, encode(plan),
                     encode({'format': 'graph-capture/2', 'graph': stored_graph,
                             'groups': directory}))).lastrowid
                con.execute(
                    'INSERT INTO target_heads VALUES(?,?) ON CONFLICT(name) '
                    'DO UPDATE SET revision=excluded.revision', (name, revision))
                self.db.remember('plan', request_id, payload, revision)
            return self.get(revision=revision)

    def get(self, name=None, *, revision=None):
        if (name is None) == (revision is None):
            raise Invalid('supply exactly one of name and revision')
        if revision is None:
            token(name, 'name')
        elif type(revision) is not int:
            raise Invalid('revision must be an integer')
        if self.db.is_v2():
            if revision is None:
                row = self.db.connection.execute(
                    'SELECT r.* FROM target_heads h JOIN target_revisions r '
                    'ON r.id=h.revision WHERE h.name=?', (name,)).fetchone()
            else:
                row = self.db.connection.execute(
                    'SELECT * FROM target_revisions WHERE id=?', (revision,)).fetchone()
        else:
            if revision is None:
                row = self.db.connection.execute(
                    'SELECT r.* FROM heads h JOIN revisions r ON r.id=h.revision '
                    'WHERE h.name=?', (name,)).fetchone()
            else:
                row = self.db.connection.execute(
                    'SELECT * FROM revisions WHERE id=?', (revision,)).fetchone()
        if row is None:
            raise NotFound('unknown plan revision')
        return {'revision': row['id'], 'name': row['name'], 'plan': json.loads(row['plan'])}

    def execution(self, revision):
        """Private version 2 execution payload for a fixed revision."""
        row = self.db.connection.execute(
            'SELECT execution FROM target_revisions WHERE id=?', (revision,)).fetchone()
        if row is None:
            raise NotFound('unknown plan revision')
        return json.loads(row['execution'])

    def history(self, name):
        token(name, 'name')
        table = 'target_revisions' if self.db.is_v2() else 'revisions'
        return [self.get(revision=row[0]) for row in self.db.connection.execute(
            'SELECT id FROM ' + table + ' WHERE name=? ORDER BY id', (name,))]

    def close(self):
        if self._owns:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
