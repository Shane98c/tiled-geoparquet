"""Layout finalization writes one file, however many processes encode its LOD pages."""

import json
import multiprocessing

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from tiled_geoparquet import _layout, _tile_sizes, drop_rate, validate, write, writer
from tiled_geoparquet._layout import finalize_layout


def _quiet(*_):
    pass


def _refuse(job):
    raise RuntimeError('injected job failure')


@pytest.fixture(scope='module')
def staged(tmp_path_factory):
    """A staged file of six row groups, whose overviews thin to null runs."""
    work = tmp_path_factory.mktemp('staged')
    rng = np.random.default_rng(11)
    n = 3000
    x, y = -105 + rng.random(n) * 0.3, 39.6 + rng.random(n) * 0.3
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {
            'name': [f'feature-{i}' for i in range(n)],
            'geometry': shapely.to_wkb(shapely.box(x, y, x + 0.002, y + 0.002)),
        }
    )
    source = work / 'source.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), source)
    return writer._write_unindexed(
        source,
        work / 'staged.parquet',
        zooms=range(9, 15),
        complete_from=13,
        thinning=drop_rate(2.5),
        carry=['name'],
        row_group_rows=512,
        workers=1,
        log=_quiet,
    )


@pytest.fixture
def pools(monkeypatch):
    """The worker counts of the process pools the layout starts."""
    started = []

    class Pool(_layout.ProcessPoolExecutor):
        def __init__(self, max_workers, **options):
            started.append(max_workers)
            super().__init__(max_workers, **options)

    monkeypatch.setattr(_layout, 'ProcessPoolExecutor', Pool)
    return started


def test_worker_processes_write_the_same_bytes(staged, tmp_path, monkeypatch, pools):
    assert pq.ParquetFile(staged).metadata.num_row_groups == 6
    monkeypatch.setattr(_layout, 'PARALLEL_LOD_BYTES', 0)
    alone = finalize_layout(staged, tmp_path / 'alone.parquet', workers=1, log=_quiet)
    assert pools == []
    shared = finalize_layout(staged, tmp_path / 'shared.parquet', workers=3, log=_quiet)
    assert pools == [3]
    assert shared.read_bytes() == alone.read_bytes()
    validate(shared, full=True)
    assert sorted(tmp_path.iterdir()) == [alone, shared]


def test_little_lod_data_is_encoded_without_workers(staged, tmp_path, pools):
    finalize_layout(staged, tmp_path / 'out.parquet', workers=4, log=_quiet)
    assert pools == []


@pytest.mark.parametrize('failure', ['job', 'append'])
def test_a_failed_layout_leaves_no_workers_or_files(staged, tmp_path, monkeypatch, pools, failure):
    """An error in a worker's job, or in the main process while jobs are
    queued, ends the layout with its workers gone and its work files removed."""
    monkeypatch.setattr(_layout, 'PARALLEL_LOD_BYTES', 0)
    before = set(multiprocessing.active_children())
    if failure == 'job':
        monkeypatch.setattr(_layout, '_run_job', _refuse)
    else:

        def append(path, target, statistics=None):
            raise RuntimeError('injected append failure')

        monkeypatch.setattr(_layout, '_append_page_chunks', append)
    with pytest.raises(RuntimeError, match=f'injected {failure} failure'):
        finalize_layout(staged, tmp_path / 'out.parquet', workers=3, log=_quiet)
    assert pools == [3]
    assert set(multiprocessing.active_children()) <= before
    assert list(tmp_path.iterdir()) == []


def test_workers_must_be_positive(staged, tmp_path):
    with pytest.raises(ValueError, match='workers must be positive'):
        finalize_layout(staged, tmp_path / 'out.parquet', workers=-1, log=_quiet)


def test_write_passes_its_workers_to_the_layout(tmp_path, monkeypatch):
    given = {}
    monkeypatch.setattr(writer, '_write_unindexed', lambda src, out, **options: None)
    monkeypatch.setattr(
        _layout, 'finalize_layout', lambda src, out, **options: given.update(options)
    )
    monkeypatch.setattr(_tile_sizes, 'log_tile_sizes', lambda path, log: None)
    write('unused-source', tmp_path / 'out.parquet', workers=3, log=_quiet)
    assert given['workers'] == 3
