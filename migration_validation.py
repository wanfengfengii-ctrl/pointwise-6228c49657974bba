"""Executable public migration scenarios; contains no target implementation."""
import copy
import itertools
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch

from warehouse import Warehouse, Registry, Invalid, Conflict
from warehouse.samples import build_legacy, SOURCE, MAPPING
from vendor import sdk1, sdk2
from migration_samples import (
    CASES, EXTRA_SOURCES, BRANCH_GRAPH, BRANCH_GROUPS, BRANCH_ROW,
    LOOP_GRAPH, LOOP_GROUPS, LOOP_ROW,
)

OLD_TABLES = ("revisions", "heads", "batches", "raw_lines", "jobs",
              "results", "result_rows", "requests")


def upgrade(path, checkpoint=None):
    from warehouse.migration import upgrade as perform
    return perform(path, checkpoint=checkpoint)


def logical_dump(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as con:
        return tuple(con.iterdump())


def audit(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as con:
        return {table: con.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in OLD_TABLES}


def version(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as con:
        return con.execute(
            "SELECT value FROM metadata WHERE key='storage_version'").fetchone()[0]


def oracle(source, lines, mapping):
    plan = sdk1.compile_plan(source)
    rows = []
    for position, text in enumerate(lines):
        match = sdk1.search(plan, text)
        captures = (dict.fromkeys(g["id"] for g in plan["groups"])
                    if match is None else match["captures"])
        rows.append({
            "position": position, "matched": match is not None,
            "span": None if match is None else match["span"],
            "captures": captures,
            "fields": {field: None if captures[group] is None else
                       text[captures[group][0]:captures[group][1]]
                       for field, group in mapping.items()},
        })
    return rows


@contextmanager
def target_only(*objects):
    connections = list({id(obj.db.connection): obj.db.connection for obj in objects}.values())

    def forbidden(*args, **kwargs):
        raise AssertionError("SDK1 used after cutover")

    def authorize(action, table, column, database, caller):
        if table in OLD_TABLES and action in (
                sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    original = sdk2.Graph.transitions
    observed = {"transitions": 0}

    def transitions(self, *args, **kwargs):
        observed["transitions"] += 1
        return original(self, *args, **kwargs)

    with ExitStack() as stack:
        for name in ("compile_plan", "load", "search"):
            stack.enter_context(patch.object(sdk1, name, forbidden))
        stack.enter_context(patch.object(sdk2.Graph, "transitions", transitions))
        for connection in connections:
            connection.set_authorizer(authorize)
        try:
            yield observed
        finally:
            for connection in connections:
                connection.set_authorizer(None)


class MigrationCases(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="capture-migration-", dir=".")
        self.path = Path(self.directory.name) / "archive.sqlite"

    def tearDown(self):
        self.directory.cleanup()

    def test_joint_fixed_jobs_native_and_reopen(self):
        ids = build_legacy(self.path)
        frozen_audit = audit(self.path)
        with Warehouse(self.path) as w, Registry(self.path) as registry:
            frozen = w.read(ids["published"])
            old = registry.get(revision=ids["old_revision"])
            self.assertIsNone(upgrade(self.path))
            self.assertEqual(version(self.path), "2")
            with target_only(w, registry) as observed:
                self.assertEqual(registry.get(revision=ids["old_revision"]), old)
                self.assertEqual(w.read(ids["published"]), frozen)
                self.assertEqual(w.read(ids["failed"])["state"], "failed")
                held = w.create_job(ids["batch"], MAPPING, "held",
                                    revision=ids["old_revision"])
                native = registry.put_graph("meters", copy.deepcopy(BRANCH_GRAPH),
                                            copy.deepcopy(BRANCH_GROUPS), "native-1")
                self.assertEqual(native["plan"], {
                    "format": "native-capture/2", "graph": BRANCH_GRAPH,
                    "groups": BRANCH_GROUPS})
                self.assertEqual(w.registry.get("meters"), native)
                batch = w.seal(["aa"], "native-batch")
                job = w.create_job(batch["batch"], {"device": "g2"}, "native-job",
                                    name="meters")
                w.run(job["job"])
                self.assertEqual(w.read(job["job"])["rows"], [BRANCH_ROW])
                for key in ("pending", "failed"):
                    w.run(ids[key])
                    self.assertEqual(w.read(ids[key])["rows"], frozen["rows"])
                w.run(held["job"])
                self.assertEqual(w.read(held["job"])["rows"], frozen["rows"])
                self.assertEqual(registry.put("meters", SOURCE, "plan-1"), old)
                self.assertEqual(registry.get("meters"), native)
                current = registry.put_graph("meters", LOOP_GRAPH, LOOP_GROUPS, "native-2")
                self.assertEqual(registry.put_graph(
                    "meters", BRANCH_GRAPH, BRANCH_GROUPS, "native-1"), native)
                self.assertEqual(registry.get("meters"), current)
                with self.assertRaises(Conflict):
                    registry.put("meters", "(a)", "native-1")
                batch2 = w.seal(["噪甲甲尾"], "loop-batch")
                job2 = w.create_job(batch2["batch"], {"value": "sample"}, "loop-job",
                                     name="meters")
                w.run(job2["job"])
                self.assertEqual(w.read(job2["job"])["rows"], [LOOP_ROW])
                before = registry.history("meters")
                cyclic = copy.deepcopy(LOOP_GRAPH)
                cyclic["nodes"][0]["edges"].append(
                    {"to": "loop", "symbol": None, "writes": []})
                with self.assertRaises(Invalid):
                    registry.put_graph("meters", cyclic, LOOP_GROUPS, "bad-native")
                with self.assertRaises(Invalid):
                    registry.put_graph("meters", LOOP_GRAPH,
                                       [{"id": "x", "start": "absent", "end": "right"}],
                                       "bad-native")
                self.assertEqual(registry.history("meters"), before)
                registry.put_graph("other", LOOP_GRAPH, LOOP_GROUPS, "bad-native")
                self.assertGreater(observed["transitions"], 0)
            self.assertEqual(audit(self.path), frozen_audit)
        with Warehouse(self.path) as reopened:
            with target_only(reopened):
                self.assertEqual(reopened.read(ids["published"]), frozen)
                self.assertEqual(reopened.read(job["job"])["rows"], [BRANCH_ROW])
                self.assertEqual(reopened.read(job2["job"])["rows"], [LOOP_ROW])
                self.assertEqual(reopened.registry.get("meters"), current)
        before = logical_dump(self.path)
        self.assertIsNone(upgrade(self.path))
        self.assertEqual(logical_dump(self.path), before)

    def test_finite_independent_capture_differential(self):
        sources = list(dict.fromkeys([case[0] for case in CASES] + EXTRA_SOURCES +
                                     ["", "x", "a\\*b", "()", "(a)?(b*)"]))
        texts = ["".join(chars) for size in range(4)
                 for chars in itertools.product("ab", repeat=size)]
        texts += ["噪乙🙂", "a*b", "甲", "🙂", "\x00"]
        checks = []
        with Warehouse(self.path) as w:
            batch = w.seal(texts, "corpus")
            for index, source in enumerate(sources):
                plan = w.registry.put("p" + str(index), source, "p" + str(index))
                mapping = {"field:" + g["id"]: g["id"] for g in plan["plan"]["groups"]}
                job = w.create_job(batch["batch"], mapping, "j" + str(index),
                                   revision=plan["revision"])
                checks.append((source, mapping, job["job"], oracle(source, texts, mapping)))
        upgrade(self.path)
        with Warehouse(self.path) as w:
            with target_only(w) as observed:
                for index, (source, mapping, job, expected) in enumerate(checks):
                    with self.subTest(source=source):
                        w.run(job)
                        self.assertEqual(w.read(job)["rows"], expected)
                        new = w.registry.put("new" + str(index), source, "new" + str(index))
                        next_job = w.create_job(batch["batch"], mapping, "next" + str(index),
                                                revision=new["revision"])
                        w.run(next_job["job"])
                        self.assertEqual(w.read(next_job["job"])["rows"], expected)
                self.assertGreater(observed["transitions"], 0)

    def test_upgrade_rollback_and_retry(self):
        for phase in ("migration.converted", "migration.before_commit"):
            with self.subTest(phase=phase):
                path = Path(self.directory.name) / (phase + ".sqlite")
                ids = build_legacy(path)
                before = logical_dump(path)
                events = []

                def fail(here, detail):
                    events.append((here, detail))
                    if here == phase:
                        raise RuntimeError("controlled conversion interruption")

                with self.assertRaises(RuntimeError):
                    upgrade(path, checkpoint=fail)
                self.assertIn(phase, [event[0] for event in events])
                self.assertEqual(logical_dump(path), before)
                self.assertEqual(version(path), "1")
                with Warehouse(path) as old:
                    self.assertEqual(old.read(ids["published"])["state"], "succeeded")
                    self.assertEqual(old.read(ids["pending"])["state"], "pending")
                events.clear()
                self.assertIsNone(upgrade(path, checkpoint=lambda *event: events.append(event)))
                self.assertEqual(events[-1], ("migration.before_commit", None))
                self.assertEqual({event[1] for event in events if event[0] == "migration.converted"},
                                 {ids["old_revision"], ids["current_revision"]})
                after = logical_dump(path)
                self.assertIsNone(upgrade(path, checkpoint=fail))
                self.assertEqual(logical_dump(path), after)
                with Warehouse(path) as w:
                    with target_only(w):
                        w.run(ids["pending"])
                        self.assertEqual(w.read(ids["pending"])["rows"],
                                         w.read(ids["published"])["rows"])

    def test_target_publication_failure_reopen(self):
        ids = build_legacy(self.path)
        upgrade(self.path)

        def fail(phase, detail):
            if phase == "processing.before_commit":
                raise RuntimeError("target publication interruption")

        with Warehouse(self.path) as w:
            with target_only(w):
                frozen = w.read(ids["published"])
                with self.assertRaises(RuntimeError):
                    w.run(ids["pending"], checkpoint=fail)
                state = w.read(ids["pending"])
                self.assertEqual(state["state"], "failed")
                self.assertIsNone(state["rows"])
                self.assertIsNone(state["result"])
        with Warehouse(self.path) as w:
            with target_only(w):
                receipt = w.run(ids["pending"])
                self.assertEqual(w.run(ids["pending"]), receipt)
                self.assertEqual(w.read(ids["pending"])["rows"], frozen["rows"])
                self.assertEqual(w.read(ids["published"]), frozen)


def demo():
    with tempfile.TemporaryDirectory(prefix="capture-upgrade-demo-", dir=".") as directory:
        path = Path(directory) / "archive.sqlite"
        ids = build_legacy(path)
        with Warehouse(path) as w:
            before = w.read(ids["published"])
            upgrade(path)
            with target_only(w):
                w.run(ids["pending"])
                w.run(ids["failed"])
                assert w.read(ids["pending"])["rows"] == before["rows"]
                assert w.read(ids["failed"])["rows"] == before["rows"]
                native = w.registry.put_graph("meters", BRANCH_GRAPH, BRANCH_GROUPS, "native")
                batch = w.seal(["aa"], "native-batch")
                job = w.create_job(batch["batch"], {"device": "g2"}, "native-job",
                                   name="meters")
                w.run(job["job"])
                result = w.read(job["job"])
                assert result["rows"] == [BRANCH_ROW]
                print(json.dumps({"version": "2", "migrated_fixed_task": w.read(ids["pending"]),
                                  "native_revision": native["revision"], "native_task": result},
                                 ensure_ascii=True, indent=2))


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else None
    if mode == "verify":
        suite = unittest.defaultTestLoader.discover("tests")
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(MigrationCases))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
    if mode == "demo":
        demo()
        return
    raise SystemExit("usage: python3.11 -B migration_validation.py verify|demo")


if __name__ == "__main__":
    main()
