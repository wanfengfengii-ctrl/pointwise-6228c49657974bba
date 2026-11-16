from .db import attach
from .registry import Registry
from .batches import Batches
from .jobs import Jobs
from .processing import Processing


class Warehouse:
    def __init__(self, path_or_database):
        self.db, self._owns = attach(path_or_database)
        self.registry = Registry(self.db)
        self.batches = Batches(self.db)
        self.jobs = Jobs(self.db, self.registry, self.batches)
        self.processing = Processing(self.db, self.registry, self.batches, self.jobs)

    def seal(self, lines, request_id):
        return self.batches.seal(lines, request_id)

    def batch(self, batch):
        return {**self.batches.get(batch), 'lines': self.batches.lines(batch)}

    def create_job(self, batch, mapping, request_id, *, name=None, revision=None):
        return self.jobs.create(batch, mapping, request_id, name=name, revision=revision)

    def job(self, job):
        return self.jobs.get(job)

    def run(self, job, *, checkpoint=None):
        return self.processing.run(job, checkpoint)

    def read(self, job):
        return self.processing.read(job)

    def close(self):
        if self._owns:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
