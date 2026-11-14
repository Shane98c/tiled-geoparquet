"""In a file with the spatial index an attribute column holds the values of
the rows an overview page shows on that page's leading rows, in their order,
so a page spends a few bytes on which rows hold a value however few it shows."""

import importlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_attribute_columns import (
    COLUMNS,
    _build,
    _point_source,
    _quiet,
    _same,
    _source,
    _thinning,
    _tiles,
)
from tiled_geoparquet import drop_rate, read_overview, validate
from tiled_geoparquet._layout import finalize_layout
from tiled_geoparquet._validate_index import check_index
from tiled_geoparquet.unpack import PageBounds, leading_rows, leads, lod_values, moved, shown_rows
from tiled_geoparquet.validate import ValidationError

ROOT = Path(__file__).resolve().parents[1]
NAMES = [c if isinstance(c, str) else c[0] for c in COLUMNS]


@pytest.fixture(scope='module')
def shapes(tmp_path_factory):
    """A file keeping `COLUMNS` as columns, and the same source packing them."""
    work = tmp_path_factory.mktemp('leading')
    src = work / 'source.parquet'
    _source(src)
    options = dict(thinning=_thinning)
    packed = _build(src, work / 'packed.parquet', carry=['flag', *COLUMNS], **options)
    columns = _build(
        src, work / 'columns.parquet', carry=['flag'], carry_columns=COLUMNS, **options
    )
    return src, packed, columns


def _lods(path):
    lod = json.loads(pq.read_schema(path).metadata[b'geo'])['lod_columns']
    return lod['lods']


def _attribute(parquet, group, lod, name):
    """Row group `group` of attribute `name` at LOD `lod`, as stored."""
    leaf = f'lod_attributes.{lod}.{name}'
    return (
        parquet.read_row_group(group, columns=[leaf])
        .column(0)
        .combine_chunks()
        .flatten()[0]
        .flatten()[0]
    )


def _equal(a, b):
    """Whether two arrays hold the same values, a float NaN equal to itself."""
    return _same(pa.table({'values': a}), pa.table({'values': b}))


def _attributes(path):
    """Every attribute column of `path` as stored, a flat column each."""
    column = pq.read_table(path, columns=['lod_attributes']).column(0).combine_chunks()
    return pa.table(
        {
            f'{lod.name}.{leaf.name}': column.field(lod.name).field(leaf.name)
            for lod in column.type
            for leaf in lod.type
        }
    )


def _placement(path):
    """Per LOD and row group of the indexed file `path`: the rows the LOD
    shows, the rows with a value, and its page bounds."""
    parquet, pages = pq.ParquetFile(path), PageBounds(path)
    for name, child in _lods(path).items():
        for group in range(parquet.metadata.num_row_groups):
            values = lod_values(parquet, group, 'lod', name).combine_chunks()
            yield (
                name,
                group,
                shown_rows(values, child['geometry_type']),
                values.is_valid().to_numpy(zero_copy_only=False),
                pages(group, f'lod.{name}'),
            )
    pages.close()


def test_values_lead_their_overview_pages(shapes):
    """Each attribute chunk is the unindexed file's, whose values are on
    their own rows, with every shown row's value moved to its page's leading
    rows; rows that only host copies hold none and take no place."""
    _, _, columns = shapes
    staged = columns.with_suffix('.staged.parquet')
    assert leads(pq.ParquetFile(columns)) and not leads(pq.ParquetFile(staged))
    led, own = pq.ParquetFile(columns), pq.ParquetFile(staged)
    moved_somewhere = hosts = sparse_pages = 0
    for name, group, shown, present, bounds in _placement(columns):
        rows, leading = np.flatnonzero(shown), leading_rows(shown, bounds)
        hosts += int((present & ~shown).sum())
        counts = np.add.reduceat(shown, bounds[:-1])
        sparse_pages += int(((counts > 0) & (counts < np.diff(bounds))).sum())
        # a page's values are on as many leading rows as it shows
        expected = np.concatenate(
            [first + np.arange(n) for first, n in zip(bounds[:-1], counts, strict=True)]
        )
        assert np.array_equal(leading, expected), (name, group)
        moved_somewhere += int((leading != rows).sum())
        for attribute in NAMES:
            stored = _attribute(led, group, name, attribute)
            assert _equal(stored, moved(_attribute(own, group, name, attribute), rows, leading)), (
                name,
                attribute,
                group,
            )
            valued = np.flatnonzero(stored.is_valid().to_numpy(zero_copy_only=False))
            assert np.isin(valued, leading).all(), (name, attribute, group)
    assert moved_somewhere and sparse_pages, 'pages showing some of their rows'
    assert hosts, 'rows that only host copies'


def test_read_overview_reads_the_leading_rows(shapes):
    _, packed, columns = shapes
    staged = columns.with_suffix('.staged.parquet')
    for zoom in range(10, 16):
        read = read_overview(columns, zoom)
        assert _same(read, read_overview(staged, zoom)), zoom
        expected = read_overview(packed, zoom)
        assert _same(read.select(expected.column_names), expected), zoom


def test_a_laid_out_file_lays_out_again(shapes, tmp_path):
    """The layout pass reads the values of a file that leads its pages from
    their leading rows, into pages cut elsewhere."""
    _, _, columns = shapes
    same = finalize_layout(columns, tmp_path / 'same.parquet', leaf_pages=4, log=_quiet)
    assert _same(_attributes(same), _attributes(columns))
    other = finalize_layout(
        columns, tmp_path / 'other.parquet', leaf_pages=2, page_bytes=300, log=_quiet
    )
    validate(other, full=True)
    recut = sum(
        not np.array_equal(a[4], b[4])
        for a, b in zip(_placement(columns), _placement(other), strict=True)
    )
    assert recut, 'pages cut elsewhere'
    assert not _same(_attributes(other), _attributes(columns))
    for zoom in range(10, 16):
        assert _same(read_overview(other, zoom), read_overview(columns, zoom)), zoom


@pytest.fixture(scope='module')
def misplaced(shapes, tmp_path_factory):
    """`shapes` laid out with each value left on its own row, as no file with
    the spatial index holds them."""
    src, _, columns = shapes
    layout = importlib.import_module('tiled_geoparquet._layout')
    work = tmp_path_factory.mktemp('misplaced')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(layout, 'leading_rows', lambda shown, bounds: np.flatnonzero(shown))
        out = finalize_layout(
            columns.with_suffix('.staged.parquet'),
            work / 'misplaced.parquet',
            leaf_pages=4,
            log=_quiet,
        )
    return out


def test_leading_values_take_fewer_bytes_where_few_rows_show(shapes, misplaced):
    _, _, columns = shapes

    def sizes(path):
        metadata, out = pq.ParquetFile(path).metadata, {}
        for g in range(metadata.num_row_groups):
            for c in range(metadata.row_group(g).num_columns):
                column = metadata.row_group(g).column(c)
                parts = column.path_in_schema.split('.')
                if parts[0] == 'lod_attributes':
                    out[parts[1]] = out.get(parts[1], 0) + column.total_compressed_size
        return out

    led, own = sizes(columns), sizes(misplaced)
    shown = {}
    for name, _, rows, _, _ in _placement(columns):
        shown.setdefault(name, []).append(rows)
    sparse = [name for name, parts in shown.items() if np.concatenate(parts).mean() < 0.5]
    assert sparse, 'LODs showing under half their rows'
    for name in sparse:
        assert led[name] < 0.9 * own[name], (name, led[name], own[name])
    for name in set(shown) - set(sparse):
        assert led[name] <= own[name], name


def test_the_validator_refuses_values_off_their_leading_rows(misplaced):
    with pytest.raises(ValidationError, match='has a value for no row the LOD shows'):
        validate(misplaced)
    with pytest.raises(ValidationError, match='has a value for no row the LOD shows'):
        validate(misplaced, full=True)
    parquet = pq.ParquetFile(misplaced)
    lod = json.loads(parquet.schema_arrow.metadata[b'geo'])['lod_columns']
    with pytest.raises(ValueError, match="holds a value past the rows its LOD's page shows"):
        check_index(misplaced, parquet, lod)


def test_the_python_reader_refuses_values_off_their_leading_rows(misplaced):
    with pytest.raises(ValueError, match="holds a value past the rows its LOD's page shows"):
        for zoom in range(10, 16):
            read_overview(misplaced, zoom)


def test_a_count_of_no_parts_shows_no_row_in_any_bytes():
    """A row shows when its leading varint, its count of parts, is not zero,
    whether zero is written in one byte or more."""
    rows = [
        (b'\x00\x00\x00', False),
        (b'\x80\x00\x00\x00', False),
        (b'\x80\x80\x00\x00', False),
        (b'\x01\x03\x00\x00\x04\x00\x04\x04\x00\x00', True),
        (b'\x81\x00\x03\x00\x00\x04\x00\x04\x04\x00\x00', True),
        (b'\x80\x01', True),
        (None, False),
        (b'', False),
        (b'\x80', False),
    ]
    values = pa.array([value for value, _ in rows], pa.binary())
    expected = np.array([shown for _, shown in rows])
    for geometry_type in ('MultiPolygon', 'MultiLineString'):
        assert np.array_equal(shown_rows(values, geometry_type), expected)
        assert np.array_equal(shown_rows(values.slice(2, 5), geometry_type), expected[2:7])
        assert np.array_equal(shown_rows(pa.chunked_array([values]), geometry_type), expected)
        wide = values.cast(pa.large_binary())
        assert np.array_equal(shown_rows(wide, geometry_type), expected)
        assert np.array_equal(shown_rows(wide.slice(3, 4), geometry_type), expected[3:7])
    present = np.array([value is not None for value, _ in rows])
    assert np.array_equal(shown_rows(values, 'Point'), present)
    assert not len(shown_rows(pa.array([], pa.binary()), 'MultiPolygon'))


def test_a_host_written_with_two_bytes_of_zero_takes_no_place(shapes, tmp_path):
    """A host-only row whose count of no parts takes two bytes is still no
    row its page shows: the values after it keep their rows' places."""
    _, packed, columns = shapes
    table = pq.read_table(columns.with_suffix('.staged.parquet'))
    struct = table.column('lod').combine_chunks()
    rewritten, children = 0, []
    for field in struct.type:
        cells = struct.field(field.name).to_pylist()
        for i, cell in enumerate(cells):
            if cell is not None and cell[:1] == b'\x00':
                cells[i] = b'\x80' + cell
                rewritten += 1
        children.append(pa.array(cells, field.type))
    assert rewritten, 'rows that only host copies'
    index = table.schema.get_field_index('lod')
    lod = pa.StructArray.from_arrays(children, fields=list(struct.type))
    staged = tmp_path / 'two.staged.parquet'
    pq.write_table(
        table.set_column(index, table.schema.field(index), lod),
        staged,
        row_group_size=1024,
        use_dictionary=False,
        write_page_index=True,
    )
    out = finalize_layout(staged, tmp_path / 'two.parquet', leaf_pages=4, log=_quiet)
    validate(out, full=True)
    for zoom in range(10, 16):
        assert _same(read_overview(out, zoom), read_overview(columns, zoom)), zoom
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'tiles': _tiles(packed, range(10, 15)),
                'styles': [['height'], ['kind', 'rps']],
                'query': {
                    'bbox': [-104.95, 39.65, -104.9, 39.7],
                    'resolution': 360 / 512 / 2**12,
                    'columns': ['height'],
                },
            }
        )
    )
    result = _render('columns.test.mjs', packed, out, cases)
    assert result.returncode == 0, result.stdout + result.stderr


def _render(script, *arguments):
    return subprocess.run(
        ['node', str(ROOT / 'js/test' / script), *map(str, arguments)],
        cwd=ROOT / 'js',
        capture_output=True,
        text=True,
    )


def test_the_reader_refuses_a_page_with_values_past_the_rows_it_shows(shapes, misplaced, tmp_path):
    _, packed, _ = shapes
    cases = tmp_path / 'cases.json'
    cases.write_text(json.dumps({'tiles': _tiles(packed, range(10, 14)), 'attributes': NAMES}))
    result = _render('leading-attributes.test.mjs', misplaced, cases)
    assert result.returncode == 0, result.stdout + result.stderr


def test_points_lead_their_pages(tmp_path):
    """A point LOD hosts no copies, so every value of it shows its row."""
    src = tmp_path / 'points.parquet'
    _point_source(src, 3, 6000)
    options = dict(thinning=drop_rate(2.5), zooms=range(9, 14), complete_from=13)
    packed = _build(src, tmp_path / 'packed.parquet', carry=['c0', 'c1', 'c2'], **options)
    columns = _build(src, tmp_path / 'columns.parquet', carry_columns=['c0', 'c1', 'c2'], **options)
    validate(columns, full=True)
    staged, led = pq.ParquetFile(columns.with_suffix('.staged.parquet')), pq.ParquetFile(columns)
    moved_somewhere = 0
    for name, group, shown, present, bounds in _placement(columns):
        assert np.array_equal(shown, present), 'every value of a point LOD shows its row'
        rows, leading = np.flatnonzero(shown), leading_rows(shown, bounds)
        moved_somewhere += int((leading != rows).sum())
        own = _attribute(staged, group, name, 'c1')
        assert _equal(_attribute(led, group, name, 'c1'), moved(own, rows, leading))
    assert moved_somewhere, 'pages showing some of their rows'
    for zoom in range(9, 14):
        a, b = read_overview(packed, zoom), read_overview(columns, zoom)
        assert _same(b.select(a.column_names), a), zoom
    cases = tmp_path / 'cases.json'
    cases.write_text(
        json.dumps(
            {
                'tiles': _tiles(packed, range(9, 15)),
                'styles': [['c1'], ['c0', 'c2']],
                'query': {
                    'bbox': [-104.9, 39.6, -104.8, 39.7],
                    'resolution': 360 / 512 / 2**11,
                    'columns': ['c1'],
                },
            }
        )
    )
    result = _render('columns.test.mjs', packed, columns, cases)
    assert result.returncode == 0, result.stdout + result.stderr
