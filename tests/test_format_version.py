"""Every entry point that reads `lod_columns` metadata refuses another format version."""

import gzip
import json
import struct

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from tiled_geoparquet import drop_rate, read_overview, tile_sizes, write
from tiled_geoparquet._layout import BOOTSTRAP_BYTES, finalize_layout
from tiled_geoparquet.encoding import LOD_VERSION, is_compatible_version
from tiled_geoparquet.writer import _write_unindexed

OPTIONS = dict(
    zooms=range(10, 13),
    complete_from=12,
    thinning=drop_rate(2.5),
    workers=1,
    log=lambda *_: None,
)


@pytest.fixture(scope='module')
def source(tmp_path_factory):
    geometry = [
        shapely.box(-105 + i * 0.001, 39.7, -104.9995 + i * 0.001, 39.7005) for i in range(60)
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    path = tmp_path_factory.mktemp('version') / 'source.parquet'
    table = pa.table({'geometry': shapely.to_wkb(geometry)})
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), path)
    return path


def _restated(path, out, version):
    """A copy of the unindexed file `path` whose `lod_columns` states `version`."""
    table = pq.read_table(path)
    geo = json.loads(table.schema.metadata[b'geo'])
    geo['lod_columns']['version'] = version
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), out)
    return out


def test_only_this_minor_is_compatible_while_the_major_is_zero():
    assert is_compatible_version(LOD_VERSION)
    for other in ('0.1.0', '0.3.0', '1.2.0', '', None, 2):
        assert not is_compatible_version(other)


def test_read_overview_and_the_layout_pass_refuse_another_version(source, tmp_path):
    staged = _write_unindexed(source, tmp_path / 'staged.parquet', **OPTIONS)
    assert read_overview(staged, 12).num_rows == 60
    old = _restated(staged, tmp_path / 'old.parquet', '0.1.0')
    with pytest.raises(ValueError, match='version 0.1.0 is not compatible'):
        read_overview(old, 12)
    with pytest.raises(ValueError, match='version 0.1.0 is not compatible'):
        finalize_layout(old, tmp_path / 'laid.parquet', workers=1, log=lambda *_: None)
    assert not (tmp_path / 'laid.parquet').exists()


def test_tile_sizes_refuses_a_root_of_another_version(source, tmp_path):
    out = write(source, tmp_path / 'out.parquet', **OPTIONS)
    assert tile_sizes(out)
    raw = bytearray(out.read_bytes())
    (length,) = struct.unpack('<I', raw[12:16])
    root = json.loads(gzip.decompress(bytes(raw[16 : 16 + length])))
    root['lod']['version'] = '0.1.0'
    packed = gzip.compress(json.dumps(root).encode(), mtime=0)
    assert 16 + len(packed) <= BOOTSTRAP_BYTES
    raw[12:16] = struct.pack('<I', len(packed))
    raw[16 : 16 + len(packed)] = packed
    old = tmp_path / 'old.parquet'
    old.write_bytes(raw)
    with pytest.raises(ValueError, match='version 0.1.0 is not compatible'):
        tile_sizes(old)
