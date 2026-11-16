"""Finite archive fixture built exclusively through public SDK1 business APIs."""
from . import Warehouse, Conflict


LINES = ['前缀甲乙甲=010尾', 'unparsed', '乙=1', '=']
SOURCE = '((甲|乙)*)=((0|1)*)'
MAPPING = {'device': 'g1', 'value': 'g3', 'last_device': 'g2', 'last_digit': 'g4'}


def build_legacy(path):
    """Create a complete old archive in a fresh database and return its public IDs."""
    with Warehouse(path) as warehouse:
        if warehouse.db.connection.execute(
                'SELECT (SELECT count(*) FROM revisions) + (SELECT count(*) FROM batches)'
                ).fetchone()[0]:
            raise Conflict('sample builder requires an empty archive')
        old = warehouse.registry.put('meters', SOURCE, 'plan-1')
        batch = warehouse.seal(LINES, 'batch-1')
        published = warehouse.create_job(batch['batch'], MAPPING, 'job-published', name='meters')
        warehouse.run(published['job'])
        pending = warehouse.create_job(batch['batch'], MAPPING, 'job-pending', name='meters')
        current = warehouse.registry.put('meters', '(乙)=(1)', 'plan-2')
        new_job = warehouse.create_job(batch['batch'], {'device': 'g1', 'value': 'g2'},
                                       'job-current', name='meters')
        warehouse.run(new_job['job'])
        failed = warehouse.create_job(batch['batch'], MAPPING, 'job-failed',
                                       revision=old['revision'])

        def fail(phase, detail):
            if phase == 'processing.before_commit':
                raise RuntimeError('sample publication interruption')

        try:
            warehouse.run(failed['job'], checkpoint=fail)
        except RuntimeError as exc:
            if str(exc) != 'sample publication interruption':
                raise
        return {'old_revision': old['revision'], 'current_revision': current['revision'],
                'batch': batch['batch'], 'published': published['job'],
                'pending': pending['job'], 'current_job': new_job['job'],
                'failed': failed['job']}
