import json
import tempfile
from pathlib import Path
from . import Warehouse
from .samples import build_legacy


def main():
    with tempfile.TemporaryDirectory(prefix='warehouse-demo-', dir='.') as directory:
        path = Path(directory) / 'archive.sqlite'
        ids = build_legacy(path)
        with Warehouse(path) as warehouse:
            before = warehouse.read(ids['published'])
            warehouse.run(ids['pending'])
            warehouse.run(ids['failed'])
            assert warehouse.read(ids['pending'])['rows'] == before['rows']
            assert warehouse.read(ids['failed'])['rows'] == before['rows']
            assert warehouse.read(ids['published']) == before
            print(json.dumps({'archive': ids,
                              'old_job': before,
                              'current_job': warehouse.read(ids['current_job']),
                              'retried_job': warehouse.read(ids['failed'])},
                             ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
