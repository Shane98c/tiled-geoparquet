"""An overview page whose rows hold no value of a column attribute has no
attribute page: its attribute leaf entry is empty, and its rows share one
page of nulls with the overview pages around it that hold none either."""

import gzip
import json
import struct
import subprocess
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from test_attribute_columns import _leaves, _offset_pages, _root, _slot_at, _tiles, _write_root
from tiled_geoparquet import tile_sizes, validate
from tiled_geoparquet._compact import get, loads
from tiled_geoparquet._layout import (
    finalize_layout,
    read_attribute_leaf,
    read_attribute_slot,
    read_node,
    read_row_node,
)
from tiled_geoparquet.validate import ValidationError
from tiled_geoparquet.writer import _write_unindexed

ROOT = Path(__file__).resolve().parents[1]
GEO = {
    'version': '1.1.0',
    'primary_column': 'geometry',
    'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
}
NAMES = ['rare', 'none', 'full']


def _quiet(*_):
    pass


def _source(path, n=6000, seed=11):
    """Shapes over a quarter degree with `rare` valued only in its south-west
    corner, about 1.5% of rows, `none` never and `full` always."""
    rng = np.random.default_rng(seed)
    x, y = -105 + rng.random(n) * 0.25, 39.6 + rng.random(n) * 0.25
    corner = (x < -104.97) & (y < 39.63)
    table = pa.table(
        {
            'geometry': shapely.to_wkb(shapely.box(x, y, x + 0.002, y + 0.001)),
            'rare': pa.array(np.where(corner, np.arange(n) % 50, 0), pa.int32(), mask=~corner),
            'none': pa.nulls(n, pa.float64()),
            'full': pa.array(np.arange(n) % 97, pa.int32()),
            'code': pa.array(np.arange(n) * 7, pa.int64()),
        }
    )
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(GEO).encode()}), path)


@pytest.fixture(scope='module')
def sparse(tmp_path_factory):
    work = tmp_path_factory.mktemp('sparse')
    src = work / 'source.parquet'
    _source(src)
    staged = work / 'staged.parquet'
    _write_unindexed(
        src,
        staged,
        zooms=range(10, 16),
        complete_from=10,
        row_group_rows=2048,
        page_bytes=1024,
        page_rows=32,
        batch_rows=32,
        carry_columns=NAMES,
        workers=1,
        log=_quiet,
    )
    out = finalize_layout(
        staged, work / 'sparse.parquet', leaf_pages=8, page_bytes=2048, log=_quiet
    )
    return src, out


def _present(path, lod, name):
    column = pq.read_table(path, columns=['lod_attributes']).column(0)
    values = column.combine_chunks().field(lod).field(name)
    return values, values.is_valid().to_numpy(zero_copy_only=False)


def _lod_pages(pages, lod, starts):
    """Global first and end rows of the LOD's pages, by byte offset."""
    found = {}
    for g, (rows_pages, rows) in enumerate(pages[f'lod.{lod}']):
        ends = np.r_[rows_pages[1:, 2], rows]
        for (offset, _, first), end in zip(rows_pages, ends, strict=True):
            found[int(offset)] = (starts[g] + int(first), starts[g] + int(end))
    return found


def test_attribute_leaves_are_empty_exactly_beside_pages_without_a_value(sparse):
    _, out = sparse
    validate(out, full=True)
    raw, root = _root(out)
    lods = list(root['lod']['lods'])
    trees = _leaves(raw, root)
    pages = _offset_pages(out)
    starts = np.cumsum([0] + [rows for _, rows in pages[f'lod.{lods[0]}']])
    source = pq.read_table(out, columns=NAMES)
    ducked = [row[0] for row in duckdb.sql(f"select lod_attributes from '{out}'").fetchall()]
    empties = dict.fromkeys(NAMES, 0)
    listed = 0
    for t, lod in enumerate(lods):
        rows = _lod_pages(pages, lod, starts)
        for a, name in enumerate(NAMES):
            values, present = _present(out, lod, name)
            # every row is shown, so the attribute column is its source column
            assert values.to_pylist() == source.column(name).to_pylist(), (lod, name)
            assert [row[lod][name] for row in ducked] == values.to_pylist(), (lod, name)
            for leaf in trees[t]:
                at, size, offset = _slot_at(root, t, leaf, a)
                got = read_attribute_leaf(read_attribute_slot(raw[at : at + size], a, offset))
                valued = np.array(
                    [present[slice(*rows[int(o)])].any() for o in leaf['offset']], bool
                )
                assert np.array_equal(got['length'] == 0, ~valued), (lod, name)
                empties[name] += int((~valued).sum())
                listed += len(valued) if a == 0 else 0
    assert empties['full'] == 0
    assert empties['none'] == listed
    assert listed // 2 < empties['rare'] < listed, empties


def _chunk_bytes(path, prefix):
    f = pq.ParquetFile(path)
    return sum(
        f.metadata.row_group(g).column(c).total_compressed_size
        for g in range(f.metadata.num_row_groups)
        for c in range(f.metadata.num_columns)
        if f.metadata.row_group(g).column(c).path_in_schema.startswith(prefix)
    )


def test_an_attribute_without_values_takes_a_page_per_row_group(sparse):
    """The all-null attribute holds one page of nulls per row group and LOD,
    far less than a page header per overview page, and a tile styled by it
    fetches none of it."""
    _, out = sparse
    pages = _offset_pages(out)
    lods = list(_root(out)[1]['lod']['lods'])
    overview = sum(len(p) for lod in lods for p, _ in pages[f'lod.{lod}'])
    chunks = sum(len(pages[f'lod.{lod}']) for lod in lods)
    for lod in lods:
        assert all(len(p) == 1 for p, _ in pages[f'lod_attributes.{lod}.none'])
    none = sum(_chunk_bytes(out, f'lod_attributes.{lod}.none') for lod in lods)
    assert overview > 20 * chunks
    assert none <= 40 * chunks
    assert none < overview, (none, overview)
    sizes = [zoom['attributes'] for zoom in tile_sizes(out) if zoom['tiles']]
    assert all(zoom['none']['max_bytes'] == 0 for zoom in sizes)
    assert all(zoom['full']['max_bytes'] > 0 for zoom in sizes)


def _rewrite_leaf_slot(path, out, lod, name, change):
    """`path` written to `out` with the attribute leaf of `name` beside the
    first leaf of `lod` holding one valued page changed by `change`."""
    raw, root = _root(path)
    lods = list(root['lod']['lods'])
    t, a = lods.index(lod), NAMES.index(name)
    for leaf in _leaves(raw, root)[t]:
        at, size, offset = _slot_at(root, t, leaf, a)
        node = read_attribute_slot(raw[at : at + size], a, offset)
        if any(node[2]):
            break
    changed = change(node)
    packed = gzip.compress(json.dumps(changed, separators=(',', ':')).encode(), mtime=0)
    held = struct.pack('<IIQ', len(packed), a, offset) + packed
    assert len(held) <= size
    out.write_bytes(raw[:at] + held + bytes(size - len(held)) + raw[at + size :])
    return out


def _emptied(node):
    """The attribute leaf with its first valued page listed as empty."""
    i = next(i for i, length in enumerate(node[2]) if length)
    gaps, lengths = list(node[1]), list(node[2])
    if i + 1 < len(gaps):
        gaps[i + 1] += gaps[i] + lengths[i]
    gaps[i], lengths[i] = 0, 0
    return [0, gaps, lengths]


def test_validator_refuses_an_empty_entry_over_rows_with_a_value(sparse, tmp_path):
    _, out = sparse
    lod = list(_root(out)[1]['lod']['lods'])[-1]
    bad = _rewrite_leaf_slot(out, tmp_path / 'bad.parquet', lod, 'rare', _emptied)
    with pytest.raises(ValidationError, match='lists no page beside rows with a value'):
        validate(bad, full=True)


def test_validator_refuses_an_empty_entry_with_a_gap(sparse, tmp_path):
    _, out = sparse
    lod = list(_root(out)[1]['lod']['lods'])[-1]

    def gapped(node):
        gaps, lengths = list(node[1]), list(node[2])
        i = lengths.index(0)
        gaps[i] = 1
        return [0, gaps, lengths]

    bad = _rewrite_leaf_slot(out, tmp_path / 'bad.parquet', lod, 'none', gapped)
    with pytest.raises(ValidationError, match='overlap or are empty'):
        validate(bad, full=True)
    with pytest.raises(ValueError, match='overlap or are empty'):
        read_attribute_leaf([0, [0, 1], [5, 0]])
    assert read_attribute_leaf([0, [3, 0, 2], [5, 0, 4]])['offset'].tolist() == [3, 8, 10]


def _zeroed_leaf(path, out, locate, read):
    """`path` written to `out` with the length of the first page of a leaf
    `locate(root)` gives, `(top, entry)`, set to 0 and its top node naming
    the leaf's new length. The leaf is rewritten in place, so it must not
    grow; `read` decodes it."""
    raw, root = _root(path)
    top = locate(root)
    level, entries = read(top)
    assert level == 1
    offset, length = int(entries['offset'][0]), int(entries['length'][0])
    leaf = json.loads(gzip.decompress(raw[offset : offset + length]))
    leaf[4 if len(leaf) == 9 else 2][0] = 0
    packed = gzip.compress(json.dumps(leaf, separators=(',', ':')).encode(), mtime=0)
    assert len(packed) <= length
    top[4][0] = len(packed)
    if len(top[4]) > 1:
        top[3][1] += length - len(packed)
    raw = raw[:offset] + packed + raw[offset + len(packed) :]
    _write_root(out, raw, root)
    return out


def test_validator_refuses_an_empty_page_in_an_lod_leaf(sparse, tmp_path):
    _, out = sparse

    def first(root):
        name = next(n for n, top in root['directories'].items() if top[0] == 1)
        return root['directories'][name]

    bad = _zeroed_leaf(out, tmp_path / 'bad.parquet', first, read_node)
    with pytest.raises(ValidationError, match='directory byte ranges overlap or are empty'):
        validate(bad, full=True)


def test_validator_refuses_an_empty_page_in_a_row_directory(sparse, tmp_path):
    _, out = sparse

    def code(root):
        index = root['column_index']
        return index['directories'][index['columns'].index('code')]

    bad = _zeroed_leaf(out, tmp_path / 'bad.parquet', code, read_row_node)
    with pytest.raises(ValidationError, match='source pages overlap or are empty'):
        validate(bad, full=True)


def _page_ranges(path, chunk_path, keep):
    """Byte ranges of the pages of `chunk_path` whose global rows `keep`
    takes, from the offset index."""
    raw = Path(path).read_bytes()
    metadata = loads(raw[-8 - int.from_bytes(raw[-8:-4], 'little') : -8])
    found, start = [], 0
    for group in get(metadata, 4)[1]:
        rows = get(group, 3)
        for chunk in get(group, 1)[1]:
            if '.'.join(p.decode() for p in get(get(chunk, 3), 3)[1]) != chunk_path:
                continue
            index = loads(raw[get(chunk, 4) : get(chunk, 4) + get(chunk, 5)])
            listed = get(index, 1)[1]
            ends = [get(p, 3) for p in listed[1:]] + [rows]
            for page, end in zip(listed, ends, strict=True):
                if keep(start + get(page, 3), start + end):
                    found.append([get(page, 1), get(page, 1) + get(page, 2)])
        start += rows
    return found


def test_a_styled_tile_fetches_only_pages_with_a_value(sparse, tmp_path):
    """Read by the JS reader, a tile styled by `rare` holds its values and
    nulls, requests no byte of a page of nulls, and requests no attribute
    page at all where its overview pages hold no value."""
    _, out = sparse
    lods = list(_root(out)[1]['lod']['lods'])
    empty, chunks = [], []
    for lod in lods:
        _, present = _present(out, lod, 'rare')
        held = np.r_[0, np.cumsum(present)]
        leaf = f'lod_attributes.{lod}.rare'
        empty += _page_ranges(out, leaf, lambda a, b, held=held: held[b] == held[a])
        chunks += _page_ranges(out, leaf, lambda a, b: True)
    assert empty and len(empty) < len(chunks)
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'attribute': 'rare',
                'values': pq.read_table(out, columns=['rare']).column(0).to_pylist(),
                'tiles': _tiles(out, zooms=(10, 12, 13, 14, 15)),
                'empty': empty,
                'chunks': chunks,
            }
        )
    )
    result = subprocess.run(
        ['node', str(ROOT / 'js/test/empty-attributes.test.mjs'), str(out), str(cases)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'empty attribute pages' in result.stdout, result.stdout
