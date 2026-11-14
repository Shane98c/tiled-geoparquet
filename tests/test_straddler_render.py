"""Straddler copies are rendered by jobs spread over the worker pool; the file must not show it."""

import json
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from tiled_geoparquet import drop_rate, validate, write
from tiled_geoparquet import writer as writer_module


@pytest.fixture
def blocks(tmp_path):
    """Boxes tens of metres across, so many cross tile edges at several zooms."""
    rng = np.random.default_rng(7)
    n = 4000
    x = -105 + rng.random(n) * 0.4
    y = 39.6 + rng.random(n) * 0.3
    size = np.exp(rng.normal(-6.5, 0.5, n))
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {
            'name': pa.array([f'b{i}' for i in range(n)]),
            'geometry': pa.array(
                shapely.to_wkb(shapely.box(x, y, x + size, y + size * 0.7)), pa.binary()
            ),
        }
    )
    path = tmp_path / 'blocks.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), path)
    return path


def test_straddler_copies_are_the_same_however_their_rendering_is_split(
    blocks, tmp_path, monkeypatch
):
    options = dict(
        zooms=range(9, 15),
        complete_from=12,
        thinning=drop_rate(1.5),
        attributes=['name'],
        log=lambda *_: None,
    )
    # the fixture fits one scan batch and one render job per LOD
    whole = write(blocks, tmp_path / 'whole.parquet', workers=1, **options)
    validate(whole)
    expected = pq.read_table(whole)

    # rows per render job, by the LOD's grid scale
    render_jobs: dict[float, list[int]] = {}

    class Recording(ProcessPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            if fn is writer_module._render:
                wkb, _, _, scale, *_ = args[0]
                render_jobs.setdefault(scale, []).append(len(wkb))
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(writer_module, 'ProcessPoolExecutor', Recording)
    monkeypatch.setattr(writer_module, 'COPY_SCAN_ROWS', 500)
    monkeypatch.setattr(writer_module, 'COPY_JOB_ROWS', 16)
    for workers in (1, 4):
        render_jobs.clear()
        out = write(blocks, tmp_path / f'split-{workers}.parquet', workers=workers, **options)
        assert len(render_jobs) > 1, 'the fixture has straddlers at several LODs'
        assert max(max(rows) for rows in render_jobs.values()) <= 16
        assert sum(len(rows) for rows in render_jobs.values()) > 4 * workers, (
            'the fixture makes more jobs than are in flight at once'
        )
        assert pq.read_table(out).equals(expected, check_metadata=True)
