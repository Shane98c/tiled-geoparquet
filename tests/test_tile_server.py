"""The JS tile server and map source helper, against a file with LODs and the
plain GeoParquet it was built from."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from tiled_geoparquet import write

ROOT = Path(__file__).resolve().parents[1]
# The coarsest LOD is z5, so the zooms below it draw from that LOD.
ZOOMS = list(range(5, 13))


@pytest.fixture(scope='module')
def files(tmp_path_factory):
    """Buildings near Denver as plain GeoParquet, and written with LODs and a
    number and a string as attributes."""
    tmp = tmp_path_factory.mktemp('tile_server')
    rng = np.random.default_rng(11)
    n = 3000
    cx = -105 + rng.random(n) * 0.5
    cy = 39.6 + rng.random(n) * 0.3
    size = np.exp(rng.normal(-9.5, 0.6, n))
    polys = [shapely.box(x, y, x + s, y + s * 0.7) for x, y, s in zip(cx, cy, size, strict=True)]
    table = pa.table(
        {
            'risk': pa.array(rng.random(n).astype(np.float32)),
            'zone': pa.array([f'zone{i % 4}' for i in range(n)]),
            'name': pa.array([f'b{i}' for i in range(n)]),
            'geometry': pa.array([shapely.to_wkb(p) for p in polys], pa.binary()),
        }
    )
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    plain = tmp / 'buildings.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), plain)
    lod = write(
        plain,
        tmp / 'buildings.lod.parquet',
        zooms=ZOOMS,
        attributes=['risk', 'zone'],
        workers=1,
        log=lambda *_: None,
    )
    assert json.loads(pq.read_schema(lod).metadata[b'geo'])['lod_columns']['zooms'] == ZOOMS
    return lod, plain


def node(script: str, *paths: Path) -> str:
    result = subprocess.run(
        ['node', str(ROOT / 'js' / 'test' / script), *map(str, paths)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_handler_and_command_serve_the_tiles_render_tile_gives(files):
    """Tiles over HTTP equal `renderTile`'s at every zoom, from below the
    coarsest LOD to past the finest, and the command serves them with TileJSON."""
    out = node('server.test.mjs', *files)
    assert 'tile server: handler, Node adapter and command passed' in out
    compared = int(out.rsplit(',', 1)[1].split()[0])
    assert compared > 0


def test_vector_source_takes_its_limits_from_each_file(files):
    """The MapLibre source ends at the finest LOD zoom, or 22 without LODs."""
    out = node('vector-source.test.mjs', *files)
    assert 'vector source: zoom limits and bounds from built files' in out
