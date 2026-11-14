"""Attributes kept as columns read as packed ones, from every reader."""

import gzip
import importlib
import io
import itertools
import json
import struct
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from tiled_geoparquet import cluster, drop_rate, read_overview, tile_sizes, validate
from tiled_geoparquet._compact import get, loads
from tiled_geoparquet._layout import (
    _ROOT_TARGET_BYTES,
    _grouping,
    _json,
    _node,
    _Tally,
    finalize_layout,
    fit_root,
    read_attribute_leaf,
    read_attribute_slot,
    read_node,
    read_primary_node,
    read_row_node,
)
from tiled_geoparquet._validate_index import _check_attribute_slots, directory_leaves
from tiled_geoparquet.cli import cli
from tiled_geoparquet.key import hilbert_cell, hilbert_key
from tiled_geoparquet.validate import ValidationError
from tiled_geoparquet.writer import _write_unindexed

ROOT = Path(__file__).resolve().parents[1]
GEO = {
    'version': '1.1.0',
    'primary_column': 'geometry',
    'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
}
COLUMNS = ['height', 'kind', ('rps', 'halffloat'), 'name']


def _quiet(*_):
    pass


def _thinning(rows, zoom, ctx):
    """A tenth of the rows dropped at zoom 13, so its LOD shows most rows;
    zooms below thinned at a rate."""
    if zoom == 13:
        return rows.filter(pa.array(rows.column('id').to_numpy() % 10 != 0))
    return drop_rate(2.5)(rows, zoom, ctx)


def _source(path, n=2400, seed=5):
    rng = np.random.default_rng(seed)
    x, y = -105 + rng.random(n) * 0.25, 39.6 + rng.random(n) * 0.25
    # some shapes wide enough to reach into neighbouring tiles as boundary copies
    wide = rng.random(n) < 0.1
    geometry = shapely.box(x, y, x + np.where(wide, 0.02, 0.002), y + 0.001)
    table = pa.table(
        {
            'geometry': shapely.to_wkb(geometry),
            'name': [f'building-{i}' if i % 9 else None for i in range(n)],
            'kind': [['house', 'shed', None, 'garage'][i % 4] for i in range(n)],
            'height': pa.array([None if i % 7 == 0 else i % 80 for i in range(n)], pa.int32()),
            'rps': pa.array(np.where(np.arange(n) % 11 == 0, np.nan, rng.random(n) * 3)),
            'flag': pa.array([i % 3 == 0 for i in range(n)]),
            'code': pa.array(np.arange(n) * 7, pa.int64()),
        }
    )
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(GEO).encode()}), path)


def _build(src, out, **options):
    """`src` written and laid out with small leaves."""
    staged = out.with_suffix('.staged.parquet')
    settings = dict(
        zooms=range(10, 16),
        complete_from=14,
        row_group_rows=1024,
        page_bytes=1024,
        page_rows=32,
        batch_rows=32,
        workers=1,
        log=_quiet,
    )
    _write_unindexed(src, staged, **{**settings, **options})
    return finalize_layout(staged, out, leaf_pages=4, log=_quiet)


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp('columns')
    src = work / 'source.parquet'
    _source(src)
    layout = importlib.import_module('tiled_geoparquet._layout')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(layout, '_ROOT_TARGET_BYTES', 1500)
        packed = _build(src, work / 'packed.parquet', thinning=_thinning, carry=['flag', *COLUMNS])
        columns = _build(
            src,
            work / 'columns.parquet',
            thinning=_thinning,
            carry=['flag'],
            carry_columns=COLUMNS,
        )
    return src, packed, columns


@pytest.fixture(scope='module')
def hybrid(tmp_path_factory, built):
    """Files keeping every cheap column as a column, `auto`, or packing two
    and keeping the rest, `hybrid`, a file packing only those two, and one
    packing all of them in the same types, to read the others against."""
    src = built[0]
    work = tmp_path_factory.mktemp('hybrid')
    options = dict(thinning=_thinning)
    packed = _build(
        src, work / 'packed.parquet', carry=['kind', 'height', 'rps', 'flag'], **options
    )
    auto = _build(src, work / 'auto.parquet', carry_columns='auto', **options)
    mixed = _build(
        src, work / 'hybrid.parquet', carry=['flag', 'height'], carry_columns='auto', **options
    )
    plain = _build(
        src, work / 'plain.parquet', carry=['flag', 'height'], carry_columns=(), **options
    )
    return packed, auto, mixed, plain


def _same(a, b):
    """Whether two tables hold the same values, a float NaN equal to itself."""

    def bits(table):
        widths = {2: pa.int16(), 4: pa.int32(), 8: pa.int64()}
        return pa.table(
            {
                name: column.combine_chunks().view(widths[column.type.byte_width])
                if pa.types.is_floating(column.type)
                else column
                for name, column in zip(table.column_names, table.columns, strict=True)
            }
        )

    return a.schema == b.schema and bits(a).equals(bits(b))


def _root(path):
    raw = Path(path).read_bytes()
    return raw, json.loads(gzip.decompress(raw[16 : 16 + int.from_bytes(raw[12:16], 'little')]))


def _tiles(path, zooms=range(10, 17)):
    """Tiles holding rows and their neighbours, so copies are read too."""
    bbox = pq.read_table(path, columns=['bbox']).column('bbox').combine_chunks()
    x = (bbox.field('xmin').to_numpy() + bbox.field('xmax').to_numpy()) / 2
    y = (bbox.field('ymin').to_numpy() + bbox.field('ymax').to_numpy()) / 2
    tiles = set()
    for z in zooms:
        n = 2**z
        tx = ((x + 180) / 360 * n).astype(int)
        ty = ((1 - np.arcsinh(np.tan(np.radians(y))) / np.pi) / 2 * n).astype(int)
        for i in range(0, len(x), max(1, len(x) // 24)):
            tiles.update({(z, int(tx[i]), int(ty[i])), (z, int(tx[i]) + 1, int(ty[i]))})
    return sorted(tiles)


def test_metadata_names_the_column_lods(built):
    _, packed, columns = built
    lod = json.loads(pq.read_schema(columns).metadata[b'geo'])['lod_columns']
    storage = {a['name']: a.get('storage', 'packed') for a in lod['attributes']}
    assert storage == {
        'flag': 'packed',
        'height': 'column',
        'kind': 'column',
        'rps': 'column',
        'name': 'column',
    }
    # every LOD holds them, those showing every row too
    assert lod['attribute_columns'] == {'column': 'lod_attributes', 'lods': list(lod['lods'])}
    schema = pq.read_schema(columns).field('lod_attributes').type
    assert [f.name for f in schema] == list(lod['lods'])
    assert {f.name: f.type for f in schema.field('f10').type} == {
        'height': pa.int32(),
        'kind': pa.uint8(),
        'rps': pa.float16(),
        'name': pa.string(),
    }
    packed_lod = json.loads(pq.read_schema(packed).metadata[b'geo'])['lod_columns']
    assert 'attribute_columns' not in packed_lod
    assert all('storage' not in a for a in packed_lod['attributes'])


def test_columns_render_and_read_as_packed(built, tmp_path):
    src, packed, columns = built
    raw, root = _root(columns)
    assert any(top[0] > 1 for top in root['directories'].values()), 'LOD trees branch'
    assert validate(columns, full=True)['rows'] == validate(packed, full=True)['rows']
    for z in range(10, 16):
        a, b = read_overview(packed, z), read_overview(columns, z)
        assert _same(b.select(a.column_names), a), z
    # the source columns hold the values the packed file holds
    source = pq.read_table(packed)
    table = pq.read_table(columns)
    for name in ('height', 'kind', 'rps', 'name', 'flag', 'code', 'geometry'):
        assert _same(table.select([name]), source.select([name])), name
    query = {'bbox': [-104.95, 39.65, -104.9, 39.7], 'resolution': 360 / 512 / 2**12}
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'tiles': _tiles(packed),
                'styles': [[], ['height'], ['kind', 'rps'], ['flag', 'height', 'kind', 'name']],
                'query': {**query, 'columns': ['height', 'kind', 'name', 'flag']},
            }
        )
    )
    assert ' 0 boundary copies' not in _render_as_packed(packed, columns, cases)


def _render_as_packed(packed, columns, cases):
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/columns.test.mjs'), str(packed), str(columns), str(cases)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_auto_and_hybrid_render_and_read_as_packed(hybrid, tmp_path):
    """Every cheap column kept as a column, or two of them packed and the
    rest kept, renders and reads as the same attributes all packed."""
    packed, auto, mixed, _ = hybrid
    for variant in (auto, mixed):
        validate(variant, full=True)
        for z in range(10, 16):
            a, b = read_overview(packed, z), read_overview(variant, z)
            assert _same(b.select(a.column_names), a), (variant.name, z)
    query = {'bbox': [-104.95, 39.65, -104.9, 39.7], 'resolution': 360 / 512 / 2**12}
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'tiles': _tiles(packed),
                'styles': [[], ['height'], ['kind', 'rps'], ['flag', 'height', 'kind', 'name']],
                'query': {**query, 'columns': ['height', 'kind', 'rps', 'flag']},
            }
        )
    )
    for variant in (auto, mixed):
        _render_as_packed(packed, variant, cases)


def _lod_trees(path):
    """Each LOD directory's nodes, top first: per node its level and cells,
    and for a leaf each page's rows and fences."""
    raw, root = _root(path)

    def nodes(level, entries):
        found = [(level, entries['low'].tolist(), entries['high'].tolist())]
        if not level:
            return found + [
                tuple(entries[k].tolist() for k in ('first', 'count', 'fence_row', 'fence_cell'))
            ]
        for offset, length in zip(entries['offset'], entries['length'], strict=True):
            found += nodes(*read_node(json.loads(gzip.decompress(raw[offset : offset + length]))))
        return found

    return {name: nodes(*read_node(top)) for name, top in root['directories'].items()}


def test_a_hybrid_files_lod_trees_are_a_packed_files(hybrid):
    """Keeping the other cheap columns as columns leaves the directories a
    tile not styled by them reads as in a file packing only the packed
    attributes."""
    _, _, mixed, plain = hybrid
    assert _lod_trees(mixed) == _lod_trees(plain)
    (_, ours), (_, theirs) = _root(mixed), _root(plain)
    assert ours['primary_index']['attributes'] == theirs['primary_index']['attributes']
    assert ours['primary_index']['directory'][0] == theirs['primary_index']['directory'][0]


def _chunk_pages(path, column):
    """Per row group, the first rows of `column`'s pages, from its offset index."""
    f = pq.ParquetFile(path)
    raw = Path(path).read_bytes()
    size = int.from_bytes(raw[-8:-4], 'little')
    metadata = loads(raw[-8 - size : -8])
    out = []
    for group in get(metadata, 4)[1]:
        for chunk in get(group, 1)[1]:
            if '.'.join(p.decode() for p in get(get(chunk, 3), 3)[1]) == column:
                index = loads(raw[get(chunk, 4) : get(chunk, 4) + get(chunk, 5)])
                out.append([get(page, 3) for page in get(index, 1)[1]])
    assert len(out) == f.metadata.num_row_groups
    return out


def _overview_runs(present, starts, rows):
    """The first rows of an attribute's pages beside overview pages starting
    at `starts` in a row group of `rows` rows, the attribute present on the
    rows `present`: a page per overview page, but one per run of overview
    pages without a value."""
    bounds = [*starts, rows]
    valued = [bool(present[a:b].any()) for a, b in itertools.pairwise(bounds)]
    return [s for i, s in enumerate(starts) if not i or valued[i - 1] or valued[i]]


def test_attribute_pages_are_cut_where_overview_pages_are(built):
    _, _, columns = built
    f = pq.ParquetFile(columns)
    merged = 0
    for lod in json.loads(pq.read_schema(columns).metadata[b'geo'])['lod_columns']['lods']:
        cut = _chunk_pages(columns, f'lod.{lod}')
        for name in ('height', 'kind', 'rps', 'name'):
            for g, (starts, got) in enumerate(
                zip(cut, _chunk_pages(columns, f'lod_attributes.{lod}.{name}'), strict=True)
            ):
                values = f.read_row_group(g, columns=[f'lod_attributes.{lod}.{name}'])
                column = values.column(0).combine_chunks().field(lod).field(name)
                present = column.is_valid().to_numpy(zero_copy_only=False)
                assert got == _overview_runs(present, starts, len(present)), (lod, name, g)
                merged += len(starts) - len(got)
    assert merged, 'some overview pages without a value share an attribute page'


def test_source_columns_keep_their_pages(built):
    """A column attribute's source column is copied as it was written."""
    _, packed, columns = built
    for name in ('height', 'kind', 'rps', 'name', 'code', 'geometry'):
        assert _chunk_pages(columns, name) == _chunk_pages(packed, name), name


def test_tile_sizes_report_attribute_pages(built):
    _, packed, columns = built
    for zoom in tile_sizes(columns):
        assert set(zoom['attributes']) == {'height', 'kind', 'rps', 'name'}
        if zoom['tiles']:
            assert all(a['max_bytes'] > 0 for a in zoom['attributes'].values())
    assert all('attributes' not in zoom for zoom in tile_sizes(packed))


def test_cluster_counts_are_kept_in_columns(tmp_path):
    """A thinning's override is what the column holds at its LOD, as packed
    rows and their copies hold it."""
    rng = np.random.default_rng(3)
    n = 2000
    x, y = -105 + rng.random(n) * 0.25, 39.6 + rng.random(n) * 0.25
    geo = {**GEO, 'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Point']}}}
    table = pa.table(
        {
            'geometry': shapely.to_wkb(shapely.points(x, y)),
            'count': pa.array(np.ones(n, np.int64)),
            'label': [f'p{i % 5}' for i in range(n)],
        }
    )
    src = tmp_path / 'points.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    options = dict(zooms=range(10, 15), complete_from=14, thinning=cluster(8, count='count'))
    packed = _build(src, tmp_path / 'packed.parquet', carry=['count', 'label'], **options)
    columns = _build(src, tmp_path / 'columns.parquet', carry_columns=['count', 'label'], **options)
    lod = json.loads(pq.read_schema(columns).metadata[b'geo'])['lod_columns']
    shown = {
        name: read_overview(columns, info['zooms'][0]).num_rows
        for name, info in lod['lods'].items()
    }
    assert any(0.8 * n <= shown[name] < n for name in lod['lods']), 'an LOD showing most rows'
    assert lod['attribute_columns']['lods'] == list(lod['lods'])
    for z in range(10, 15):
        a, b = read_overview(packed, z), read_overview(columns, z)
        assert _same(b.select(a.column_names), a)
        if z < 14:
            assert pa.compute.max(b.column('count')).as_py() > 1
    validate(columns, full=True)
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'tiles': _tiles(packed, range(10, 16)),
                'styles': [['count'], ['count', 'label']],
                'query': {
                    'bbox': [-104.95, 39.65, -104.9, 39.7],
                    'resolution': 360 / 512 / 2**11,
                    'columns': ['count'],
                },
            }
        )
    )
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/columns.test.mjs'), str(packed), str(columns), str(cases)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_auto_keeps_cheap_flat_columns(tmp_path, built):
    src, _, _ = built
    out = tmp_path / 'auto.parquet'
    logged = []
    _write_unindexed(
        src,
        out,
        zooms=range(10, 13),
        carry=['flag'],
        carry_columns='auto',
        workers=1,
        log=logged.append,
    )
    lod = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    kept = [a['name'] for a in lod['attributes'] if a.get('storage') == 'column']
    # not `name`, a string of many values, nor `code`, an integer of distinct values
    assert kept == ['kind', 'height', 'rps']
    assert any('carry_columns auto: kind, height, rps' in line for line in logged)


@pytest.mark.parametrize(
    'options,message',
    [
        (dict(carry=['height'], carry_columns=['height']), 'more than once'),
        (dict(carry_columns=[('kind', 'int32')]), 'cannot keep kind'),
        (dict(carry_columns='everything'), "'auto'"),
        (dict(carry_columns=['missing']), 'missing'),
    ],
)
def test_column_attributes_are_checked(tmp_path, built, options, message):
    src, _, _ = built
    with pytest.raises((ValueError, KeyError), match=message):
        _write_unindexed(src, tmp_path / 'out.parquet', zooms=range(10, 12), log=_quiet, **options)


def test_cli_takes_carry_columns(tmp_path, built, monkeypatch):
    src, _, _ = built
    given = {}
    monkeypatch.setattr(
        importlib.import_module('tiled_geoparquet.cli'),
        'write',
        lambda src, out, **options: given.update(options),
    )
    runner = CliRunner()
    args = ['write', str(src), str(tmp_path / 'out.parquet'), '--carry', 'flag']
    result = runner.invoke(
        cli, [*args, '--carry-columns', 'height', '--carry-columns', 'rps:halffloat']
    )
    assert result.exit_code == 0, result.output
    assert given['carry'] == ['flag']
    assert given['carry_columns'] == ['height', ('rps', 'halffloat')]
    result = runner.invoke(cli, [*args, '--carry-columns', 'auto'])
    assert result.exit_code == 0, result.output
    assert given['carry_columns'] == 'auto'


def _large_root(attributes):
    """The root of a file of fifteen LODs, twenty-five indexed columns and
    fifty million rows, carrying `attributes` as column attributes, before
    its directories are placed."""
    lods = {
        f'f{z}': {
            'zooms': [z] if z < 14 else [14, 15],
            'geometry_type': 'MultiPolygon',
            'scale': [0.3, 0.3],
            'offset': [-1.3e7, 2.8e6],
            'placeholder': 2,
        }
        for z in range(15)
    }
    lod = {
        'version': '0.4.0',
        'column': 'lod',
        'order': 'hilbert_webmercator',
        'key': {'column': 'hilbert', 'depth': 24},
        'zooms': list(range(16)),
        'tile_size': 512,
        'margin': 2,
        'encoding': 'packed',
        'pack_version': 2,
        'attributes': [{'name': 'height', 'type': 'float'}, *attributes],
        'thinning': {'name': 'chain(drop_smallest(850000), tiny_polygons())'},
        'lods': lods,
    }
    names = [f'attribute_{i}' for i in range(17)] + [f'class_{i}' for i in range(3)]
    indexed = ['hilbert', 'id', 'height', *names, 'region', 'geometry']
    root = {
        'version': 7,
        'lod': lod,
        'primary': 'geometry',
        'codecs': {name: 'ZSTD' for name in lods},
        'bounds': [-125.0, 24.5, -66.9, 49.4],
        'columns': indexed[:-1],
        'column_index': {
            'columns': indexed,
            'codecs': ['ZSTD'] * len(indexed),
            'schema': 'A' * 1600,
        },
        'primary_index': {'attributes': ['height']},
    }
    if attributes:
        lod['attribute_columns'] = {'column': 'lod_attributes', 'lods': list(lods)}
        root['attribute_codec'] = 'ZSTD'
        root['attribute_slots'] = [2300] * (len(lods) + 1)
    return root, list(lods), indexed


def _large_trees(lods, indexed, rows):
    """Top nodes of the directories of a file of `rows` rows, over leaves
    written from byte 10**9 on: LOD pages per leaf of 1024, dense LODs
    holding a page per tile of about 900 rows."""
    rng = np.random.default_rng(11)
    cell, at = 0, 10**9

    def tree(leaves):
        nonlocal cell, at
        span = rng.integers(0, 4000, leaves)
        low = cell + np.cumsum(rng.integers(1, 5000, leaves) + np.r_[0, span[:-1]])
        high = low + span
        cell = int(high[-1]) + 1
        length = rng.integers(3000, 9000, leaves)
        offset = at + np.r_[0, np.cumsum(length)[:-1]]
        at = int(offset[-1] + length[-1])
        return 1, {'low': low, 'high': high, 'offset': offset, 'length': length}

    leaves = [1, 1, 1, 1, 1, 1, 1, 2, 4, 8, 12, 20, 30, 40, 55]
    trees = {name: tree(max(1, leaves[z] * rows // 50_000_000)) for z, name in enumerate(lods)}
    trees[None] = tree(rows // 300 // 1024)
    for name in indexed:
        trees[('column', name)] = tree(rows // 1000 // 1024)
    return trees


def test_row_directories_branch_before_a_tree_overview_tiles_read():
    """A root too large for the bootstrap that fits once row directories
    branch keeps every LOD tree and the primary tree whole, so an overview
    tile reaches its leaf from the root."""
    root, lods, indexed = _large_root([])
    rng = np.random.default_rng(5)
    at = 10**9

    def tree(leaves):
        nonlocal at
        low = np.cumsum(rng.integers(1, 5000, leaves))
        length = rng.integers(3000, 9000, leaves)
        offset = at + np.r_[0, np.cumsum(length)[:-1]]
        at = int(offset[-1] + length[-1])
        return 1, {'low': low, 'high': low + 100, 'offset': offset, 'length': length}

    def trees():
        made = {name: tree(1) for name in lods[:-1]}
        made[lods[-1]] = tree(1100)
        made[None] = tree(150)
        made |= {('column', name): tree(100) for name in indexed}
        return made

    whole = trees()
    top = {name: _node(level, **entries) for name, (level, entries) in whole.items()}
    root['directories'] = {name: top[name] for name in lods}
    root['primary_index']['directory'] = top[None]
    root['column_index']['directories'] = [top[('column', name)] for name in indexed]
    assert len(_json(root)) > _ROOT_TARGET_BYTES, 'the fixture overflows the bootstrap'

    fitted = trees()
    target = io.BytesIO()
    target.write(bytes(16384))
    fit_root(target, root, fitted, lods, indexed)
    assert len(_json(root)) <= _ROOT_TARGET_BYTES
    assert [fitted[name][0] for name in lods] == [1] * len(lods)
    assert fitted[None][0] == 1
    assert max(fitted[('column', name)][0] for name in indexed) == 2


@pytest.mark.parametrize('rows', [50_000_000, 500_000_000])
def test_a_large_file_reads_its_lods_as_without_column_attributes(rows):
    """Fifteen LODs, twenty-five indexed columns and fifty or five hundred
    million rows, with and without twenty of the columns as column
    attributes: the root fits the bootstrap once the largest top nodes
    branch, and the LOD and primary trees branch the same way, so a tile not
    styled by a column attribute reads the same nodes in the same rounds."""
    attributes = [
        {'name': f'attribute_{i}', 'type': 'double', 'storage': 'column'} for i in range(17)
    ]
    attributes += [
        {
            'name': f'class_{i}',
            'type': 'string',
            'storage': 'column',
            'values': [f'value {j}' for j in range(12 * (i + 1))],
        }
        for i in range(3)
    ]
    found = []
    for carried in ([], attributes):
        root, lods, indexed = _large_root(carried)
        target = io.BytesIO()
        target.write(bytes(16384))
        fit_root(target, root, _large_trees(lods, indexed, rows), lods, indexed)
        assert len(_json(root)) <= _ROOT_TARGET_BYTES
        raw = target.getvalue()

        def nodes(level, entries, raw=raw):
            """The node's level and cells, then each node below it in turn,
            the leaves by their byte ranges."""
            out = [(level, entries['low'].tolist(), entries['high'].tolist())]
            for offset, length in zip(entries['offset'], entries['length'], strict=True):
                if level > 1:
                    node = json.loads(gzip.decompress(raw[offset : offset + length]))
                    out += nodes(*read_node(node))
                else:
                    out.append((int(offset), int(length)))
            return out

        found.append(
            (
                {name: nodes(*read_node(top)) for name, top in root['directories'].items()},
                nodes(*read_node(root['primary_index']['directory'])),
            )
        )
    (without, primary), (with_columns, primary_with) = found
    if rows > 100_000_000:
        assert primary[0][0] > 1, 'the primary tree branches'
    assert with_columns == without
    assert primary_with == primary


def _leaves(raw, root):
    """Per tree, each LOD's and then the primary directory's, its leaves in
    tree order, each with its byte range under `range` and its level-one
    node's last leaf's end, leaves and its position among them under
    `group`."""

    def read(offset, length):
        return raw[offset : offset + length]

    trees = []
    for top in root['directories'].values():
        level, entries = read_node(top)
        trees.append(list(directory_leaves(read, level, entries, {})))
    attributes = root['primary_index']['attributes']

    def primary(level, entries):
        if not level:
            return [entries]
        found = []
        end = int(entries['offset'][-1] + entries['length'][-1])
        for i, (offset, length) in enumerate(
            zip(entries['offset'], entries['length'], strict=True)
        ):
            node = json.loads(gzip.decompress(read(offset, length)))
            child_level, child = read_primary_node(node, attributes)
            child['range'] = (int(offset), int(length))
            child['group'] = (end, len(entries['offset']), i)
            found += primary(child_level, child)
        return found

    trees.append(primary(*read_primary_node(root['primary_index']['directory'], attributes)))
    return trees


def _slot_at(root, tree, leaf, attribute):
    """The byte range of the slot of the `attribute`th column attribute
    beside `leaf` of tree `tree`, and the leaf's offset its frame names."""
    end, count, index = leaf['group']
    size = root['attribute_slots'][tree]
    return end + (attribute * count + index) * size, size, leaf['range'][0]


def _blocks(root, trees, attributes):
    """Per tree, the byte range of each level-one node's leaves and then of
    its slot block, in tree order."""
    found = []
    for tree, size in zip(trees, root['attribute_slots'], strict=True):
        groups = {}
        for leaf in tree:
            groups.setdefault(leaf['group'][0], []).append(leaf['range'])
        found.append(
            [
                (ranges[0][0], end, end + attributes * len(ranges) * size)
                for end, ranges in groups.items()
            ]
        )
    return found


def _offset_pages(path):
    """Per column path, per row group, the byte offsets, lengths and first
    rows, within the row group, of its pages, from its offset index."""
    raw = Path(path).read_bytes()
    size = int.from_bytes(raw[-8:-4], 'little')
    metadata = loads(raw[-8 - size : -8])
    found = {}
    for group in get(metadata, 4)[1]:
        for chunk in get(group, 1)[1]:
            name = '.'.join(p.decode() for p in get(get(chunk, 3), 3)[1])
            index = loads(raw[get(chunk, 4) : get(chunk, 4) + get(chunk, 5)])
            pages = np.array([[get(p, k) for k in (1, 2, 3)] for p in get(index, 1)[1]])
            found.setdefault(name, []).append((pages, get(group, 3)))
    return found


def test_slots_beside_each_leaf_hold_its_attribute_leaves(built):
    """The leaves under each level-one node lie back to back, followed by
    the node's slot block: per column attribute, a slot per leaf, in leaf
    order, framed with the attribute's position and the leaf's offset.
    Beside each LOD leaf, an attribute leaf
    per column attribute lists the attribute column's page beside each page
    the LOD leaf lists, or none beside one without a value; beside each primary leaf, a row leaf lists the
    source column's pages over its rows, as the offset indexes give them. A
    tree's slots are all 16 bytes longer than its largest attribute leaf."""
    _, _, columns = built
    raw, root = _root(columns)
    assert 'attribute_directory' not in root
    trees = _leaves(raw, root)
    names = [a['name'] for a in root['lod']['attributes'] if a.get('storage') == 'column']
    assert names == ['height', 'kind', 'rps', 'name']
    lods = list(root['lod']['lods'])
    assert len(root['attribute_slots']) == len(trees) == len(lods) + 1
    blocks = _blocks(root, trees, len(names))
    assert any(len(tree) > 1 for tree in blocks), 'a tree has several level-one nodes'
    assert any(len({leaf['group'][0] for leaf in tree}) < len(tree) for tree in trees), (
        'a level-one node has several leaves'
    )
    spans = sorted(span for tree in blocks for span in tree)
    assert all(a[2] <= b[0] for a, b in itertools.pairwise(spans))
    for tree in trees:
        for leaf, after in itertools.pairwise(tree):
            same = leaf['group'][0] == after['group'][0]
            assert sum(leaf['range']) == (after['range'][0] if same else leaf['group'][0])
        assert sum(tree[-1]['range']) == tree[-1]['group'][0]
    pages = _offset_pages(columns)
    starts = np.cumsum([0] + [rows for _, rows in pages[names[0]]])

    table = pq.read_table(columns, columns=['lod_attributes'])
    flat = table.column(0).combine_chunks()

    def beside(lod, name, offsets):
        """The pages of `name`'s attribute column at `lod` beside the LOD
        pages at `offsets`: the one starting at the same row in the same row
        group, or none, of length 0 where the end of the page before it
        lies, beside a page whose rows hold no value."""
        present = flat.field(lod).field(name).is_valid().to_numpy(zero_copy_only=False)
        found, end = [], 0
        for offset in offsets:
            for g, (lod_pages, rows) in enumerate(pages[f'lod.{lod}']):
                i = np.flatnonzero(lod_pages[:, 0] == offset)
                if len(i):
                    first = lod_pages[i[0], 2]
                    last = lod_pages[i[0] + 1, 2] if i[0] + 1 < len(lod_pages) else rows
                    if present[starts[g] + first : starts[g] + last].any():
                        own = pages[f'lod_attributes.{lod}.{name}'][g][0]
                        page = own[np.flatnonzero(own[:, 2] == first)[0], :2]
                        end = page.sum()
                        found.append(page)
                    else:
                        found.append([end, 0])
                    break
        return np.array(found).reshape(-1, 2)

    def source(name, first, count):
        """The pages of the source column `name` holding a row of the runs."""
        found = []
        for g, (column_pages, rows) in enumerate(pages[name]):
            ends = np.r_[column_pages[1:, 2], rows]
            for (offset, length, start), end in zip(column_pages, ends, strict=True):
                start, end = start + starts[g], end + starts[g]
                if ((first < end) & (first + count > start)).any():
                    found.append((offset, length, start, end - start))
        return np.array(found).reshape(-1, 4)

    for t, tree in enumerate(trees):
        largest = 0
        for leaf in tree:
            for a, name in enumerate(names):
                at, size, offset = _slot_at(root, t, leaf, a)
                held = raw[at : at + size]
                assert struct.unpack('<IQ', held[4:16]) == (a, offset)
                largest = max(largest, int.from_bytes(held[:4], 'little'))
                node = read_attribute_slot(held, a, offset)
                if t < len(lods):
                    got = read_attribute_leaf(node)
                    expected = beside(lods[t], name, leaf['offset'])
                    assert len(expected) == len(leaf['offset'])
                    assert np.array_equal(np.c_[got['offset'], got['length']], expected)
                else:
                    level, got = read_row_node(node)
                    expected = source(name, leaf['first'], leaf['count'])
                    assert level == 0 and len(expected)
                    assert np.array_equal(
                        np.c_[got['offset'], got['length'], got['first'], got['count']], expected
                    )
        assert root['attribute_slots'][t] == 16 + largest


def test_slot_blocks_follow_the_level_one_nodes_the_root_fitting_makes():
    """Where grouping a tree's leaves by branch changes which trees the root
    fitting branches, back and forth, the writer branches that tree first,
    and `fit_root` gives it its level of branches."""
    rng = np.random.default_rng(3)

    def tree(entries):
        low = np.sort(rng.integers(0, 2**40, entries)).tolist()
        offset = np.cumsum(rng.integers(1, 2**20, entries)).tolist()
        length = rng.integers(1, 2**16, entries).tolist()
        return 1, {'low': low, 'high': low, 'offset': offset, 'length': length}

    calls = []

    def simulate(grouped, forced):
        # grouped, the leaves of `a` fit the root under its top node; not
        # grouped, they overflow it
        calls.append((grouped, forced))
        assert len(calls) < 8, 'the grouping does not settle'
        trees = {'a': tree(16 if 'a' in grouped else 4000), None: tree(4)}
        root = {'lod': {'attributes': []}, 'primary_index': {}, 'column_index': {}}
        fit_root(_Tally(0), root, trees, ['a'], [], forced)
        assert len(_json(root)) <= _ROOT_TARGET_BYTES
        return frozenset(name for name, (level, _) in trees.items() if level > 1)

    assert _grouping(simulate) == ({'a'}, {'a'})
    assert calls[-1] == ({'a'}, {'a'})
    assert _grouping(lambda grouped, forced: frozenset({'a', None})) == ({'a', None}, set())


def _write_root(path, raw, root):
    """`raw` with the root `root`, the rest of the bootstrap zeros."""
    head = bytearray(16384)
    encoded = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
    head[:12] = raw[:12]
    head[12:16] = struct.pack('<I', len(encoded))
    head[16 : 16 + len(encoded)] = encoded
    path.write_bytes(bytes(head) + raw[16384:])


def _root_of(raw):
    return json.loads(gzip.decompress(raw[16 : 16 + int.from_bytes(raw[12:16], 'little')]))


def _patch_root(path, raw, root):
    """`raw` with the root `root`, the rest of the bootstrap as it was."""
    encoded = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
    path.write_bytes(
        raw[:12] + struct.pack('<I', len(encoded)) + encoded + raw[16 + len(encoded) :]
    )


def _relabel(path, out, change):
    """The file `path` was laid out from, which has no index and holds each
    attribute value on its own row, written again with its `lod_columns`
    changed by `change`, which may also return a changed table."""
    table = pq.read_table(path.with_suffix('.staged.parquet'))
    geo = json.loads(table.schema.metadata[b'geo'])
    changed = change(geo['lod_columns'], table)
    table = changed if isinstance(changed, pa.Table) else table
    metadata = {**table.schema.metadata, b'geo': json.dumps(geo).encode()}
    pq.write_table(table.replace_schema_metadata(metadata), out, write_page_index=True)
    return out


def _set_attribute(table, lod, name, row, value):
    """`table` with `lod_attributes.<lod>.<name>` set to `value` at `row`."""
    struct = table.column('lod_attributes').combine_chunks()
    children = []
    for owner in struct.type:
        group = struct.field(owner.name)
        values = []
        for f in owner.type:
            column = group.field(f.name)
            if owner.name == lod and f.name == name:
                listed = column.to_pylist()
                listed[row] = value
                column = pa.array(listed, f.type)
            values.append(column)
        children.append(pa.StructArray.from_arrays(values, fields=list(owner.type)))
    column = pa.StructArray.from_arrays(children, fields=list(struct.type))
    index = table.schema.get_field_index('lod_attributes')
    return table.set_column(index, table.schema.field(index), column)


@pytest.mark.parametrize(
    'change,message',
    [
        (lambda lod, _: lod['attributes'][1].update(storage='columnar'), 'storage'),
        (lambda lod, _: lod.pop('attribute_columns'), 'need attribute_columns'),
        (lambda lod, _: lod['attribute_columns']['lods'].pop(), 'one child per LOD'),
        (lambda lod, _: lod['attribute_columns']['lods'].append('f99'), 'declared LODs'),
        (lambda lod, _: lod['attributes'][0].update(storage='column'), 'metadata order'),
        (lambda lod, _: lod['attributes'][1].update(type='int64'), 'nullable int64 column'),
        (
            lambda _, table: table.set_column(
                table.schema.get_field_index('height'),
                'height',
                table.column('height').cast(pa.string()),
            ),
            'cannot give int32 values',
        ),
    ],
)
def test_validator_refuses_attribute_column_metadata(built, tmp_path, change, message):
    _, _, columns = built
    bad = _relabel(columns, tmp_path / 'bad.parquet', change)
    with pytest.raises(ValidationError, match=message):
        validate(bad)


def test_validator_refuses_a_packed_own_value_of_a_column_attribute(built, tmp_path):
    _, packed, _ = built

    def change(lod, _):
        lod['attributes'][1]['storage'] = 'column'
        lod['attribute_columns'] = {'column': 'lod_attributes', 'lods': []}

    bad = _relabel(packed, tmp_path / 'bad.parquet', change)
    with pytest.raises(ValidationError, match='own row packs column attribute height'):
        validate(bad)


@pytest.mark.parametrize(
    'name,value,message',
    [('height', 5, 'for no row the LOD shows'), ('kind', 200, 'past its value table')],
)
def test_validator_refuses_stray_attribute_values(built, tmp_path, name, value, message):
    _, _, columns = built
    shown = pq.read_table(columns, columns=['lod']).column('lod').combine_chunks().field('f10')
    hidden = int(np.flatnonzero(shown.is_null().to_numpy(zero_copy_only=False))[0])
    row = hidden if name == 'height' else int(np.flatnonzero(shown.is_valid())[0])

    def change(_, table):
        return _set_attribute(table, 'f10', name, row, value)

    with pytest.raises(ValidationError, match=message):
        validate(_relabel(columns, tmp_path / 'bad.parquet', change))


def _slot_bytes(raw, root, tree=0, leaf=0, attribute=0):
    """The byte range of a slot, by default the first beside the first leaf
    of the first LOD, and the leaf's offset."""
    return _slot_at(root, tree, _leaves(raw, root)[tree][leaf], attribute)


def _rewrite_slot(raw, root, held):
    """`raw` with the first slot holding `held` padded to its size."""
    at, size, _ = _slot_bytes(raw, root)
    assert len(held) <= size
    return raw[:at] + held + bytes(size - len(held)) + raw[at + size :]


def _framed(node, attribute, leaf):
    packed = gzip.compress(json.dumps(node, separators=(',', ':')).encode(), mtime=0)
    return struct.pack('<IIQ', len(packed), attribute, leaf) + packed


def _slot_overrun(raw, root):
    at, size, _ = _slot_bytes(raw, root)
    return raw[:at] + struct.pack('<I', size - 15) + raw[at + 4 :]


def _slot_attribute(raw, root):
    at, _, _ = _slot_bytes(raw, root)
    return raw[: at + 4] + struct.pack('<I', 1) + raw[at + 8 :]


def _slot_leaf(raw, root):
    at, _, offset = _slot_bytes(raw, root)
    return raw[: at + 8] + struct.pack('<Q', offset + 1) + raw[at + 16 :]


def _slot_short(raw, root):
    at, size, offset = _slot_bytes(raw, root)
    node = read_attribute_slot(raw[at : at + size], 0, offset)
    return _rewrite_slot(raw, root, _framed([0, node[1][:-1], node[2][:-1]], 0, offset))


def _slot_swapped(raw, root):
    a, size, _ = _slot_bytes(raw, root)
    b = a + size
    return raw[:a] + raw[b : b + size] + raw[a:b] + raw[b + size :]


def _slot_exchanged(raw, root):
    """Two slots of one attribute swapped, each framed with the other's
    leaf, beside two leaves of an LOD tree of as many pages."""
    trees = _leaves(raw, root)[:-1]
    t, first = next(
        (t, i)
        for t, leaves in enumerate(trees)
        for i in range(len(leaves) - 1)
        if leaves[i]['group'][0] == leaves[i + 1]['group'][0]
        and len(leaves[i]['offset']) == len(leaves[i + 1]['offset'])
    )
    a, size, i = _slot_at(root, t, trees[t][first], 0)
    b, _, j = _slot_at(root, t, trees[t][first + 1], 0)
    assert b == a + size

    def reframed(held, leaf):
        return held[:8] + struct.pack('<Q', leaf) + held[16:]

    return raw[:a] + reframed(raw[b : b + size], i) + reframed(raw[a:b], j) + raw[b + size :]


def _slot_padding(raw, root):
    at, size, _ = _slot_bytes(raw, root)
    length = int.from_bytes(raw[at : at + 4], 'little')
    if length + 16 == size:
        at, size, _ = _slot_bytes(raw, root, attribute=1)
        length = int.from_bytes(raw[at : at + 4], 'little')
    assert length + 16 < size
    return raw[: at + size - 1] + b'\x01' + raw[at + size :]


@pytest.mark.parametrize(
    'mutate,message',
    [
        (_slot_overrun, "slot's length overruns its slot"),
        (_slot_attribute, 'holds another column attribute'),
        (_slot_leaf, 'is beside another leaf'),
        (_slot_short, 'lists a page per leaf page'),
        (_slot_swapped, 'beside another leaf|another column attribute'),
        (_slot_exchanged, 'attribute leaf of height disagrees with its pages'),
        (_slot_padding, 'padding is not zero'),
    ],
)
def test_validator_refuses_a_wrong_attribute_slot(built, tmp_path, mutate, message):
    _, _, columns = built
    raw, root = _root(columns)
    bad = tmp_path / 'bad.parquet'
    bad.write_bytes(mutate(raw, root))
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


@pytest.mark.parametrize(
    'mutate,message',
    [
        (lambda root: root.update(attribute_codec='SNAPPY'), 'attribute codec'),
        (lambda root: root.pop('attribute_slots'), 'no attribute slot size per tree'),
        (lambda root: root['attribute_slots'].pop(), 'no attribute slot size per tree'),
        (lambda root: root['attribute_slots'].__setitem__(0, 7), 'no attribute slot size'),
        (
            lambda root: root['attribute_slots'].__setitem__(0, 2 * root['attribute_slots'][0]),
            'beside another leaf|another column attribute|padding is not zero|overruns'
            '|byte range|slot block',
        ),
        (lambda root: root['attribute_slots'].__setitem__(-1, 2.5), 'no attribute slot size'),
        (lambda root: root['attribute_slots'].__setitem__(0, 10**6), 'attribute slot|byte range'),
        (lambda root: root['directories']['f10'].append([]), 'malformed directory node'),
    ],
)
def test_validator_refuses_wrong_attribute_slot_sizes(built, tmp_path, mutate, message):
    _, _, columns = built
    raw, root = _root(columns)
    mutate(root)
    bad = tmp_path / 'bad.parquet'
    _write_root(bad, raw, root)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


def _group(first, *lengths):
    """Leaves of the given lengths back to back from `first`, under one
    level-one node."""
    ends = np.cumsum((first, *lengths))
    return [
        {'range': (int(ends[i]), length), 'group': (int(ends[-1]), len(lengths), i)}
        for i, length in enumerate(lengths)
    ]


@pytest.mark.parametrize(
    'tree,reached,message',
    [
        (
            [_group(100, 10, 10)[0], {**_group(100, 10, 10)[1], 'range': (112, 8)}],
            {},
            'not back to back',
        ),
        (_group(100, 10, 10)[::-1], {}, 'not back to back'),
        (_group(100, 10, 10), {100: 10, 110: 10, 125: 4}, 'overlaps an attribute slot block'),
        (_group(100, 10, 10), {100: 10, 110: 10, 60: 140}, 'overlaps an attribute slot block'),
        (_group(100, 10, 10), {100: 10, 110: 10, 90: 40}, 'overlaps an attribute slot block'),
        (_group(100, 10, 10) + _group(140, 10), {}, 'overlaps attribute slots'),
    ],
)
def test_validator_places_leaves_and_slot_blocks(tree, reached, message):
    """A level-one node's leaves lie back to back, and its slot block after
    them overlaps no node, whether a node starts in it or before it, and
    meets no other leaves."""
    with pytest.raises(ValueError, match=message):
        _check_attribute_slots(None, reached, {'attribute_slots': [16]}, [tree], ['a'], None)


def test_validator_refuses_attribute_slots_without_column_attributes(built, tmp_path):
    _, packed, _ = built
    raw, root = _root(packed)
    assert 'attribute_slots' not in root
    root['attribute_slots'] = [64] * (len(root['lod']['lods']) + 1)
    bad = tmp_path / 'bad.parquet'
    _patch_root(bad, raw, root)
    with pytest.raises(ValidationError, match='without column attributes'):
        validate(bad, full=True)


def test_a_half_float_source_column_validates(tmp_path):
    src = tmp_path / 'source.parquet'
    _source(src, n=600)
    table = pq.read_table(src)
    half = pa.array(table.column('rps').to_numpy().astype(np.float16))
    table = table.set_column(table.schema.get_field_index('rps'), 'rps', half)
    pq.write_table(table, src)
    out = _build(src, tmp_path / 'out.parquet', thinning=_thinning, carry_columns=['rps'])
    validate(out, full=True)
    shown = read_overview(out, 13)
    assert shown.schema.field('rps').type == pa.float16()


def test_auto_leaves_out_a_covering_of_flat_columns(tmp_path):
    src = tmp_path / 'source.parquet'
    _source(src, n=600)
    table = pq.read_table(src)
    bounds = shapely.bounds(shapely.from_wkb(table.column('geometry').to_numpy(False))).T
    names = ('west', 'south', 'east', 'north')
    for name, values in zip(names, bounds, strict=True):
        table = table.append_column(name, pa.array(values))
    paths = dict(zip(('xmin', 'ymin', 'xmax', 'ymax'), ([name] for name in names), strict=True))
    geo = {
        **GEO,
        'columns': {'geometry': {**GEO['columns']['geometry'], 'covering': {'bbox': paths}}},
    }
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    out = tmp_path / 'auto.parquet'
    _write_unindexed(src, out, zooms=range(10, 13), carry_columns='auto', workers=1, log=_quiet)
    lod = json.loads(pq.read_schema(out).metadata[b'geo'])['lod_columns']
    assert [a['name'] for a in lod['attributes']] == ['kind', 'height', 'rps', 'flag']


def test_a_tile_reaching_more_leaves_than_the_reader_keeps_reads_its_attributes(tmp_path):
    """A tile reaching more leaves than the reader keeps reads a column
    attribute, and a style after a read reaching them, with geometry only
    or styled by another column attribute, loads the attribute's leaves
    beside every leaf the reader holds: a tile under any of them then takes
    at most one request round, none when its overview pages hold no value.
    Every styled tile holds the attribute's values."""
    src = tmp_path / 'source.parquet'
    _source(src)
    staged = tmp_path / 'staged.parquet'
    _write_unindexed(
        src,
        staged,
        zooms=range(14, 16),
        complete_from=14,
        carry_columns=['height', 'kind'],
        workers=1,
        log=_quiet,
    )
    out = finalize_layout(staged, tmp_path / 'out.parquet', leaf_pages=1, page_bytes=1, log=_quiet)
    values = pq.read_table(out, columns=['height', 'kind']).to_pydict()
    raw, root = _root(out)
    lod = root['lod']
    name = next(name for name, info in lod['lods'].items() if min(lod['zooms']) in info['zooms'])
    zoom = min(max(lod['lods'][name]['zooms']), lod['key']['depth'])
    leaves = _leaves(raw, root)[list(lod['lods']).index(name)]
    # per leaf, a tile of one cell keyed in it and in no other leaf
    held = []
    for i, leaf in enumerate(leaves):
        below = leaves[i - 1]['high'][-1] if i else -1
        above = leaves[i + 1]['low'][0] if i + 1 < len(leaves) else 4**zoom
        keyed = leaf['fence_cell'][leaf['fence_row'] < leaf['valid'][leaf['fence_page']]]
        cells = keyed[(keyed > below) & (keyed < above)]
        if len(cells):
            x, y = hilbert_cell(cells[:1], zoom)
            held.append([leaf['range'][0], [zoom, int(x[0]), int(y[0])]])
    assert len(held) > 300
    cases = tmp_path / 'held.json'
    cases.write_text(json.dumps({'attributes': ['height', 'kind'], 'values': values, 'held': held}))
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/many-leaves.test.mjs'), str(out), str(cases)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'styled by height first' in result.stdout, result.stdout


def _point_source(path, attributes, n, seed=3):
    """Points in half a degree square with `attributes` flat columns of
    doubles, integers and strings in turn."""
    rng = np.random.default_rng(seed)
    x, y = -105 + rng.random(n) * 0.5, 39.5 + rng.random(n) * 0.5
    table = pa.table({'geometry': shapely.to_wkb(shapely.points(x, y))})
    for i in range(attributes):
        if i % 3 == 0:
            values = pa.array(rng.random(n) * (i + 1))
        elif i % 3 == 1:
            values = pa.array(rng.integers(0, 1000, n), pa.int32())
        else:
            values = pa.array([f'class {j % (i + 2)}' for j in range(n)])
        table = table.append_column(f'c{i}', values)
    geo = {**GEO, 'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Point']}}}
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), path)


def test_a_geometry_read_coalesces_as_without_column_attributes(tmp_path):
    """Twenty column attributes over LOD trees of many leaves under one
    level-one node: those leaves lie back to back as in the same source
    written without column attributes, so concurrent tiles styled by no
    column attribute, reaching consecutive leaves, take as many requests
    and bytes through the default coalescing from both files, read leaves
    and pages, and hold the same features."""
    names = [f'c{i}' for i in range(20)]
    src = tmp_path / 'source.parquet'
    _point_source(src, len(names), 30000)
    built = []
    for carried in (names, ()):
        staged = tmp_path / f'staged-{len(carried)}.parquet'
        _write_unindexed(
            src, staged, zooms=range(10, 17), carry_columns=carried, workers=1, log=_quiet
        )
        out = tmp_path / f'out-{len(carried)}.parquet'
        built.append(finalize_layout(staged, out, leaf_pages=64, page_bytes=1, log=_quiet))
    validate(built[0], full=True)
    (raw, root), (plain_raw, plain_root) = (_root(path) for path in built)
    assert 'attribute_slots' in root and 'attribute_slots' not in plain_root
    trees, plain_trees = _leaves(raw, root), _leaves(plain_raw, plain_root)
    for tree, plain in zip(trees[:-1], plain_trees[:-1], strict=True):
        assert [leaf['group'][1:] for leaf in tree] == [leaf['group'][1:] for leaf in plain]
        assert [raw[slice(a, a + n)] for a, n in (leaf['range'] for leaf in tree)] == [
            plain_raw[slice(a, a + n)] for a, n in (leaf['range'] for leaf in plain)
        ]
        for leaf, after in itertools.pairwise(tree):
            if leaf['group'][0] == after['group'][0]:
                assert sum(leaf['range']) == after['range'][0]
    finest = list(root['lod']['lods']).index(
        next(name for name, info in root['lod']['lods'].items() if 16 in info['zooms'])
    )
    leaves = trees[finest]
    assert len(leaves) > 8 and len({leaf['group'][0] for leaf in leaves}) == 1
    # a block of 8 by 6 tiles at zoom 16 in the middle of the points
    x0 = int((-104.75 + 180) / 360 * 2**16)
    y0 = int((1 - np.arcsinh(np.tan(np.radians(39.75))) / np.pi) / 2 * 2**16)
    tiles = [[16, x0 + i, y0 + j] for i in range(8) for j in range(6)]
    cells = hilbert_key(*np.array([t[1:] for t in tiles]).T, 16)
    reached = sorted(
        {
            i
            for cell in cells.tolist()
            for i, leaf in enumerate(leaves)
            if leaf['low'][0] <= cell <= leaf['high'][-1]
        }
    )
    assert len(reached) > 1 and reached == list(range(reached[0], reached[-1] + 1))

    def ranges(path, raw, root):
        """The byte ranges of the file's directory leaves and LOD pages."""
        leaves = [[a, a + n] for tree in _leaves(raw, root) for a, n in (x['range'] for x in tree)]
        pages = [
            [int(offset), int(offset + length)]
            for name, groups in _offset_pages(path).items()
            if name.startswith('lod.')
            for listed, _ in groups
            for offset, length, _ in listed
        ]
        return {'leaves': sorted(leaves), 'pages': sorted(pages)}

    cases = tmp_path / 'tiles.json'
    cases.write_text(
        json.dumps(
            {
                'tiles': tiles,
                'files': [ranges(built[0], raw, root), ranges(built[1], plain_raw, plain_root)],
            }
        )
    )
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/coalesced-leaves.test.mjs'), *map(str, built), str(cases)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'coalesced leaves' in result.stdout


def _wide_source(path, attributes, n=2400, seed=7):
    """`_source`'s shapes with `attributes` more flat columns: doubles,
    integers and strings of a few values in turn."""
    _source(path, n, seed)
    table = pq.read_table(path)
    rng = np.random.default_rng(seed)
    for i in range(attributes):
        if i % 3 == 0:
            values = pa.array(rng.random(n) * (i + 1))
        elif i % 3 == 1:
            values = pa.array(rng.integers(0, 1000, n), pa.int32())
        else:
            values = pa.array([f'class {j % (i + 2)}' for j in range(n)])
        table = table.append_column(f'c{i}', values)
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(GEO).encode()}), path)


def _separate_directory(raw, root, trees):
    """The size of the gzip directory of these trees' leaves and attribute
    leaves a separate directory would take: per tree its leaves, then per
    attribute and tree the attribute leaves, each as gaps and lengths, the
    leaves of a tree and the attribute leaves of an attribute back to back."""
    attributes = len([a for a in root['lod']['attributes'] if a.get('storage') == 'column'])

    def ranges(first, lengths):
        return [[first] + [0] * (len(lengths) - 1), lengths]

    def held(t, leaf, a):
        at, _, _ = _slot_at(root, t, leaf, a)
        return int.from_bytes(raw[at : at + 4], 'little')

    leaves = [ranges(tree[0]['range'][0], [leaf['range'][1] for leaf in tree]) for tree in trees]
    located = [
        [ranges(10**8, [held(t, leaf, a) for leaf in tree]) for t, tree in enumerate(trees)]
        for a in range(attributes)
    ]
    return len(_json([leaves, located]))


def _exclusive_tiles(leaves, lod):
    """Per leaf of an LOD tree, a tile of one cell at the LOD's finest zoom
    keyed in that leaf and in no other, or None."""
    zoom = min(max(lod['lods'][lod['name']]['zooms']), lod['key']['depth'])
    found = []
    for i, leaf in enumerate(leaves):
        below = leaves[i - 1]['high'][-1] if i else -1
        above = leaves[i + 1]['low'][0] if i + 1 < len(leaves) else 4**zoom
        keyed = leaf['fence_cell'][leaf['fence_row'] < leaf['valid'][leaf['fence_page']]]
        cells = keyed[(keyed > below) & (keyed < above)]
        x, y = hilbert_cell(cells[:1], zoom) if len(cells) else ([], [])
        found.append([zoom, int(x[0]), int(y[0])] if len(cells) else None)
    return found


def _node_ranges(raw, root):
    """The byte ranges of the nodes of every LOD and the primary directory,
    sorted."""
    attributes = root['primary_index']['attributes']

    def walk(level, entries, decode):
        if not level:
            return []
        found = []
        for offset, length in zip(entries['offset'], entries['length'], strict=True):
            offset, length = int(offset), int(length)
            found.append([offset, offset + length])
            node = json.loads(gzip.decompress(raw[offset : offset + length]))
            found += walk(*decode(node), decode)
        return found

    found = []
    for top in root['directories'].values():
        found += walk(*read_node(top), read_node)
    found += walk(
        *read_primary_node(root['primary_index']['directory'], attributes),
        lambda node: read_primary_node(node, attributes),
    )
    return sorted(found)


def test_a_styled_tile_takes_the_rounds_of_a_geometry_tile(tmp_path):
    """Twenty-four column attributes over many leaves, too many for a
    separate attribute directory to fit the bootstrap beside the root: a
    cold tile styled by one of them, and the next tile on the same reader,
    take as many request rounds as with geometry only, and a geometry-only
    reader requests no byte of any slot. The styled tiles hold the
    attribute's values. Through the default coalescing, a geometry-only
    view under one level-one node takes the requests and bytes of the same
    source written without column attributes; a view over the leaves of two
    level-one nodes takes one more request, or, with one column attribute,
    fetches the slot block between them as filler."""
    names = [f'c{i}' for i in range(24)]
    src = tmp_path / 'source.parquet'
    _wide_source(src, len(names), n=12000)
    built = {}
    layout = importlib.import_module('tiled_geoparquet._layout')
    for label, carried in (('wide', names), ('single', ['c5']), ('plain', ())):
        staged = tmp_path / f'staged-{label}.parquet'
        _write_unindexed(
            src,
            staged,
            zooms=range(10, 16),
            complete_from=14,
            row_group_rows=1024,
            page_bytes=1024,
            page_rows=32,
            batch_rows=32,
            thinning=_thinning,
            carry_columns=carried,
            workers=1,
            log=_quiet,
        )
        # the finest LOD's tree takes a level of branches in every file
        finest = list(json.loads(pq.read_schema(staged).metadata[b'geo'])['lod_columns']['lods'])[
            -1
        ]
        fit = layout.fit_root

        def branching(target, root, trees, lods, indexed, branched=(), fit=fit, finest=finest):
            return fit(target, root, trees, lods, indexed, (*branched, finest))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(layout, 'fit_root', branching)
            built[label] = finalize_layout(
                staged, tmp_path / f'{label}-branched.parquet', leaf_pages=1, log=_quiet
            )
        if label == 'wide':
            out = finalize_layout(staged, tmp_path / 'wide.parquet', leaf_pages=1, log=_quiet)
    validate(out, full=True)
    validate(built['wide'], full=True)
    validate(built['single'], full=True)
    raw, root = _root(out)
    trees = _leaves(raw, root)
    root_bytes = int.from_bytes(raw[12:16], 'little')
    assert 16 + root_bytes + _separate_directory(raw, root, trees) > 16384
    slots = [[end, block] for tree in _blocks(root, trees, len(names)) for _, end, block in tree]
    tiles = _tiles(out, zooms=(11, 13, 15, 16))
    pairs = [tiles[i : i + 2] for i in range(0, len(tiles), max(2, len(tiles) // 6))][:6]
    values = pq.read_table(out, columns=['c5']).column('c5').to_pylist()
    # an LOD tree whose first level-one node locates 64 leaves, and tiles
    # each under one of its leaves: four consecutive leaves of the first
    # node; the last leaf of the first node with the first of the next; and
    # earlier and later leaves of those two nodes, whose tiles load the nodes
    raw, root = _root(built['wide'])
    trees = _leaves(raw, root)
    lod = root['lod']
    t = next(
        t
        for t, tree in enumerate(trees[:-1])
        if sum(leaf['group'][0] == tree[0]['group'][0] for leaf in tree) == 64 < len(tree)
    )
    exclusive = _exclusive_tiles(trees[t], {**lod, 'name': list(lod['lods'])[t]})
    within = [exclusive[i] for i in range(20, 32) if exclusive[i]][:4]
    warm = [
        next(exclusive[i] for i in range(62, 40, -1) if exclusive[i]),
        next(exclusive[i] for i in range(65, 100) if exclusive[i]),
    ]
    assert len(within) == 4 and exclusive[63] and exclusive[64]
    across = []
    for label, cost in (('wide', 'request'), ('single', 'filler'), ('plain', None)):
        path = built[label]
        file_raw, file_root = _root(path)
        file_trees = _leaves(file_raw, file_root)
        nodes = _node_ranges(file_raw, file_root)
        if cost is None:
            plain = {'path': str(path), 'nodes': nodes}
            continue
        blocks = _blocks(file_root, file_trees, len(names) if label == 'wide' else 1)
        assert [len(tree) for tree in blocks] == [
            len({leaf['group'][0] for leaf in tree}) for tree in trees
        ]
        _, start, stop = blocks[t][0]
        assert start == sum(file_trees[t][63]['range']) and stop == file_trees[t][64]['range'][0]
        across.append(
            {
                'path': str(path),
                'nodes': nodes,
                'slots': [[end, block] for tree in blocks for _, end, block in tree],
                'block': [start, stop],
                'cost': cost,
            }
        )
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'attribute': 'c5',
                'pairs': pairs,
                'slots': slots,
                'values': values,
                'plain': plain,
                'within': within,
                'across': {'tiles': [exclusive[63], exclusive[64]], 'warm': warm, 'files': across},
            }
        )
    )
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/attribute-rounds.test.mjs'), str(out), str(cases)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'request no slot byte' in result.stdout
    assert 'two level-one nodes' in result.stdout, result.stdout
