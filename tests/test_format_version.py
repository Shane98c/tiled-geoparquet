"""Every entry point that reads `lod_columns` metadata refuses another format
version, and every one that reads an indexed file another spatial index
version."""

import gzip
import importlib.util
import json
import struct
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from tiled_geoparquet import drop_rate, read_overview, tile_sizes, validate, write
from tiled_geoparquet._layout import BOOTSTRAP_BYTES, finalize_layout
from tiled_geoparquet.encoding import INDEX_VERSION, LOD_VERSION, is_compatible_version
from tiled_geoparquet.unpack import column_values
from tiled_geoparquet.validate import ValidationError
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
    table = pa.table(
        {'geometry': shapely.to_wkb(geometry), 'height': pa.array(range(60), pa.int32())}
    )
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


def _index_version(path, out, version, parts=('footer', 'root')):
    """A copy of the indexed file `path` whose footer advertisement and
    bootstrap root, those `parts` names, state spatial index `version`, a
    single digit."""
    raw = bytearray(path.read_bytes())
    if 'footer' in parts:
        size = int.from_bytes(raw[-8:-4], 'little')
        footer = bytes(raw[-8 - size : -8])
        advertised = b'{"version":%d,"offset":4' % INDEX_VERSION
        assert footer.count(advertised) == 1
        raw[-8 - size : -8] = footer.replace(advertised, b'{"version":%d,"offset":4' % version)
    if 'root' in parts:
        (length,) = struct.unpack('<I', raw[12:16])
        root = json.loads(gzip.decompress(bytes(raw[16 : 16 + length])))
        root['version'] = version
        packed = gzip.compress(json.dumps(root).encode(), mtime=0)
        assert 16 + len(packed) <= BOOTSTRAP_BYTES
        raw[12:16] = struct.pack('<I', len(packed))
        raw[16:BOOTSTRAP_BYTES] = packed + bytes(BOOTSTRAP_BYTES - 16 - len(packed))
    out.write_bytes(raw)
    return out


@pytest.fixture(scope='module')
def versions(source, tmp_path_factory):
    """An unindexed file with an attribute, the indexed file laid out from
    it, and copies of that stating spatial index versions 5 and 9."""
    work = tmp_path_factory.mktemp('index-version')
    staged = _write_unindexed(source, work / 'staged.parquet', attributes=['height'], **OPTIONS)
    indexed = finalize_layout(staged, work / 'indexed.parquet', workers=1, log=lambda *_: None)
    others = {v: _index_version(indexed, work / f'v{v}.parquet', v) for v in (5, 9)}
    return staged, indexed, others


def _build_helper():
    path = Path(__file__).resolve().parents[1] / 'benchmarks/standard-overviews/build.py'
    spec = importlib.util.spec_from_file_location('standard_overviews_build', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decode_chunk(path):
    lod = json.loads(pq.read_schema(path).metadata[b'geo'])['lod_columns']
    name, child = next(iter(lod['lods'].items()))
    return _build_helper().decode_chunk((path, 0, name, child['geometry_type'], lod, {}, 0))


def test_every_python_entry_point_refuses_another_index_version(versions, tmp_path):
    """An indexed file of another spatial index version is refused before
    any value is read; a file without the index, and one of this version,
    are read."""
    staged, indexed, others = versions
    for path in (staged, indexed):
        assert read_overview(path, 12).num_rows == 60
        validate(path)
        validate(path, full=True)
        assert _decode_chunk(path)[1].num_rows
        finalize_layout(path, tmp_path / f'again-{path.name}', workers=1, log=lambda *_: None)
    for version, path in others.items():
        refused = f'spatial index version {version} is not {INDEX_VERSION}'
        with pytest.raises(ValueError, match=refused):
            read_overview(path, 12)
        with pytest.raises(ValidationError, match=refused):
            validate(path)
        with pytest.raises(ValidationError, match=refused):
            validate(path, full=True)
        with pytest.raises(ValueError, match=refused):
            _decode_chunk(path)
        with pytest.raises(ValueError, match=refused):
            column_values(pq.ParquetFile(path), 0, {}, 'f12', pa.field('height', pa.int32()))
        out = tmp_path / f'laid-{version}.parquet'
        with pytest.raises(ValueError, match=refused):
            finalize_layout(path, out, workers=1, log=lambda *_: None)
        assert not out.exists()
        with pytest.raises(ValueError, match=f'unsupported spatial index version {version}'):
            tile_sizes(path)


@pytest.mark.parametrize('part', ['footer', 'root'])
def test_validation_refuses_a_footer_and_root_of_different_versions(versions, tmp_path, part):
    """The footer and the root state one version: quick and full validation
    refuse a file where either alone states another."""
    _, indexed, _ = versions
    mixed = _index_version(indexed, tmp_path / 'mixed.parquet', 5, parts=(part,))
    for full in (False, True):
        with pytest.raises(ValidationError, match='spatial index version 5'):
            validate(mixed, full=full)


def test_every_js_entry_point_refuses_another_index_version(versions):
    staged, indexed, others = versions
    result = subprocess.run(
        [
            'node',
            str(Path(__file__).resolve().parents[1] / 'js/test/index-version.test.mjs'),
            str(staged),
            str(indexed),
            *(f'{version}={path}' for version, path in others.items()),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'index versions: 2 refused' in result.stdout, result.stdout
