"""Public native SDK2 inputs and independently stated business results."""
from tests.test_capture_semantics import CASES

EXTRA_SOURCES = [
    "((a|)*)", "((a*)?)*b", "((a?)*b?)*a", "((a|b)*)*",
    "(a?|b)*b", "(|a)*b", "((a|)b?)*b", "((a?)*)*b",
]

BRANCH_GRAPH = {
    "format": "symbol-graph/2", "entry": "entry", "registers": ["E", "B"],
    "nodes": [
        {"id": "done", "accept": True, "edges": []},
        {"id": "long-end", "accept": False, "edges": [
            {"to": "done", "symbol": "a", "writes": [["E", "after"]]}]},
        {"id": "long", "accept": False, "edges": [
            {"to": "long-end", "symbol": "a", "writes": []}]},
        {"id": "short", "accept": False, "edges": [
            {"to": "done", "symbol": "a", "writes": [["E", "after"]]}]},
        {"id": "entry", "accept": False, "edges": [
            {"to": "short", "symbol": None, "writes": [["B", "before"]]},
            {"to": "long", "symbol": None, "writes": [["B", "before"]]}]},
    ],
}
BRANCH_GROUPS = [{"id": "g2", "start": "B", "end": "E"}]
BRANCH_ROW = {
    "position": 0, "matched": True, "span": [0, 1],
    "captures": {"g2": [0, 1]}, "fields": {"device": "a"},
}

LOOP_GRAPH = {
    "format": "symbol-graph/2", "entry": "entry", "registers": ["right", "left"],
    "nodes": [
        {"id": "loop", "accept": True, "edges": [
            {"to": "loop", "symbol": "甲", "writes": [["right", "after"]]}]},
        {"id": "entry", "accept": False, "edges": [
            {"to": "loop", "symbol": "甲",
             "writes": [["left", "before"], ["right", "after"]]}]},
    ],
}
LOOP_GROUPS = [{"id": "sample", "start": "left", "end": "right"}]
LOOP_ROW = {
    "position": 0, "matched": True, "span": [1, 3],
    "captures": {"sample": [1, 3]}, "fields": {"value": "甲甲"},
}
