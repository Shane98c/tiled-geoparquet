"""The tile size report against the pages a tile read fetches, found without
the spatial directory and by the JS reader."""

import gzip
import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from tiled_geoparquet import _layout, drop_rate, inspect, tile_sizes, write
from tiled_geoparquet._compact import get, loads
from tiled_geoparquet._layout import _leaf, _node, finalize_layout, page_cells
from tiled_geoparquet._tile_sizes import (
    _pages,
    _root,
    _tile_bytes,
    _zoom_tiles,
    format_bytes,
    log_tile_sizes,
)
from tiled_geoparquet.cli import cli
from tiled_geoparquet.key import hilbert_cell, hilbert_key

ROOT = Path(__file__).resolve().parents[1]

# Prints, for each [z, x, y] case, the byte ranges a new reader slices for one
# tile read once it has opened.
COLD_READS = """
import { open } from 'node:fs/promises'
import { readFileSync, statSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const [index, path, casesPath] = process.argv.slice(2)
const { openReader } = await import(pathToFileURL(index).href)
const handle = await open(path, 'r')
const byteLength = statSync(path).size
const out = []
for (const [z, x, y] of JSON.parse(readFileSync(casesPath, 'utf8'))) {
  const slices = []
  const source = {
    byteLength,
    async slice(start, end = byteLength) {
      slices.push([start, end])
      const buffer = Buffer.alloc(end - start)
      await handle.read(buffer, 0, end - start, start)
      return buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)
    },
  }
  const reader = await openReader(source)
  const opened = slices.length
  await reader.readLodByTile(reader.lodForZoom(z), z, x, y)
  out.push(slices.slice(opened))
}
await handle.close()
console.log(JSON.stringify(out))
"""


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    """Boxes over a quarter degree and a dense spot, in three row groups."""
    work = tmp_path_factory.mktemp('tile_sizes')
    rng = np.random.default_rng(44)
    n = 3000
    x, y = -105 + rng.random(n) * 0.25, 39.6 + rng.random(n) * 0.25
    x[:600], y[:600] = -104.9 + rng.random(600) * 0.004, 39.7 + rng.random(600) * 0.004
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table({'geometry': shapely.to_wkb(shapely.box(x, y, x + 0.002, y + 0.001))})
    src = work / 'source.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    return write(
        src,
        work / 'built.parquet',
        zooms=range(9, 16),
        complete_from=14,
        thinning=drop_rate(2.5),
        row_group_rows=1000,
        lod_page_bytes=1024,
        workers=1,
        log=lambda *_: None,
    )


@pytest.fixture(scope='module')
def branched(built, tmp_path_factory):
    """`built` laid out again with a page per leaf under levels of branches."""
    out = tmp_path_factory.mktemp('branched') / 'branched.parquet'
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_layout, '_ROOT_TARGET_BYTES', 1000)
        finalize_layout(built, out, leaf_pages=1, page_bytes=1024, log=lambda *_: None)
    with out.open('rb') as source:
        levels = [top[0] for top in _root(source)['directories'].values()]
    assert max(levels) > 2, 'the fixture has branches above branches'
    return out


def _reference(path):
    """Each zoom's tiles and the bytes of the pages a read of each fetches,
    as `{zoom: {Hilbert index: bytes}}`, from the Parquet offset indexes, the
    key column and the LOD values. A page is listed when it holds a value,
    and a tile reads the listed pages whose keys meet its interval, plus the
    first whose last key reaches the interval's start."""
    raw = Path(path).read_bytes()
    size = int.from_bytes(raw[-8:-4], 'little')
    footer = loads(raw[-8 - size : -8])
    table = pq.read_table(path)
    lod = json.loads(table.schema.metadata[b'geo'])['lod_columns']
    depth = lod['key']['depth']
    keys = table.column(lod['key']['column']).to_pylist()
    out = {}
    for name, info in lod['lods'].items():
        shown = table.column(lod['column']).combine_chunks().field(name).is_valid().to_pylist()
        low, high, length, listed, start = [], [], [], [], 0
        for group in get(footer, 4)[1]:
            rows = get(group, 3)
            for chunk in get(group, 1)[1]:
                if [part.decode() for part in get(get(chunk, 3), 3)[1]] != [lod['column'], name]:
                    continue
                offset = get(chunk, 4)
                pages = get(loads(raw[offset : offset + get(chunk, 5)]), 1)[1]
                firsts = [get(page, 3) for page in pages] + [rows]
                for page, a, b in zip(pages, firsts[:-1], firsts[1:], strict=True):
                    if not any(shown[start + a : start + b]):
                        continue
                    keyed = [k for k in keys[start + a : start + b] if k is not None]
                    low.append(keyed[0])
                    high.append(keyed[-1])
                    length.append(get(page, 2))
                    listed += keyed
            start += rows
        low, high, length = np.array(low), np.array(high), np.array(length)
        cell = 4 ** (depth - max(info['zooms']))
        aligned = (low // cell == high // cell).all()
        for zoom in info['zooms']:
            span = 4 ** (depth - zoom)
            tiles = sorted({k // span for k in listed})
            valued = {k // span for k, s in zip(keys, shown, strict=True) if s}
            assert valued <= set(tiles) and (not aligned or valued == set(tiles))
            out[zoom] = {}
            for tile in tiles:
                first, end = tile * span, (tile + 1) * span
                fetched = (low < end) & (high >= first)
                reaching = np.flatnonzero(high >= first)
                fetched[reaching[0] if len(reaching) else -1] = True
                out[zoom][tile] = int(length[fetched].sum())
    return out


def _percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q / 100
    below = int(position)
    above = min(below + 1, len(values) - 1)
    return values[below] + (values[above] - values[below]) * (position - below)


def _tile(z, x, y):
    return int(hilbert_key(np.array([x]), np.array([y]), z)[0])


def _lod_ranges(path):
    """Byte ranges of the LOD column's chunks, those back to back as one, and
    the end of the last column chunk of the file."""
    parquet = pq.ParquetFile(path)
    column = json.loads(parquet.schema_arrow.metadata[b'geo'])['lod_columns']['column']
    chunks = [
        parquet.metadata.row_group(g).column(c)
        for g in range(parquet.metadata.num_row_groups)
        for c in range(parquet.metadata.num_columns)
    ]
    ranges = []
    for chunk in sorted(chunks, key=lambda chunk: chunk.data_page_offset):
        if not chunk.path_in_schema.startswith(column + '.'):
            continue
        start = chunk.data_page_offset
        if ranges and ranges[-1][1] == start:
            ranges[-1][1] = start + chunk.total_compressed_size
        else:
            ranges.append([start, start + chunk.total_compressed_size])
    return ranges, max(chunk.data_page_offset + chunk.total_compressed_size for chunk in chunks)


@pytest.mark.parametrize('layout', ['built', 'branched'])
def test_report_matches_offset_indexes(layout, request):
    path = request.getfixturevalue(layout)
    expected = _reference(path)
    with path.open('rb') as source:
        root = _root(source)
        found = {
            zoom: dict(zip(tiles.tolist(), fetched.tolist(), strict=True))
            for zoom, _, _, tiles, fetched, _ in _zoom_tiles(source, root)
        }
    assert found == expected
    report = tile_sizes(path)
    assert [zoom['zoom'] for zoom in report] == list(range(9, 16))
    assert [zoom['lod'] for zoom in report] == ['f9', 'f10', 'f11', 'f12', 'f13', 'f14', 'f14']
    for zoom in report:
        tiles = expected[zoom['zoom']]
        assert zoom['tiles'] == len(tiles)
        assert zoom['max_bytes'] == max(tiles.values())
        assert zoom['largest'][0] == zoom['zoom']
        assert tiles[_tile(*zoom['largest'])] == zoom['max_bytes']
        for key, q in (('median_bytes', 50), ('p99_bytes', 99)):
            assert abs(zoom[key] - _percentile(tiles.values(), q)) <= 0.5
    json.dumps(report)
    assert report[-1]['tiles'] > 300 and report[-1]['p99_bytes'] < report[-1]['max_bytes']


@pytest.mark.parametrize('layout', ['built', 'branched'])
def test_report_matches_js_reader(layout, request, tmp_path):
    """The bytes reported for a tile are the bytes of LOD pages the JS reader
    fetches for a cold read of it; its other reads are directory nodes."""
    path = request.getfixturevalue(layout)
    with path.open('rb') as source:
        tiles = list(_zoom_tiles(source, _root(source)))
    cases, expected = [], []
    for zoom, _, _, indexes, fetched, _ in tiles:
        # every tile of the coarse zooms, a spread of the fine ones
        for i in range(0, len(indexes), max(1, len(indexes) // 60)):
            x, y = hilbert_cell(indexes[i : i + 1], zoom)
            cases.append([zoom, int(x[0]), int(y[0])])
            expected.append(int(fetched[i]))
    script, cases_path = tmp_path / 'cold-reads.mjs', tmp_path / 'cases.json'
    script.write_text(COLD_READS)
    cases_path.write_text(json.dumps(cases))
    result = subprocess.run(
        ['node', str(script), str(ROOT / 'js/dist/index.js'), str(path), str(cases_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ranges, data_end = _lod_ranges(path)
    measured, nodes = [], 0
    for slices in json.loads(result.stdout):
        pages = [s for s in slices if any(a <= s[0] and s[1] <= b for a, b in ranges)]
        assert all(s[0] >= data_end for s in slices if s not in pages)
        measured.append(sum(end - start for start, end in pages))
        nodes += len(slices) - len(pages)
    assert measured == expected
    assert nodes >= len(cases)


def test_tiles_sharing_pages_count_them_whole():
    """Pages cut at any row hold several tiles' rows. Every tile with a keyed
    row in a listed page counts each page meeting its interval whole, under
    leaves and branches alike."""
    rng = np.random.default_rng(8)
    depth, grid = 10, 6
    sentinel = 4**depth
    centres = rng.integers(0, sentinel, 30)
    keys = np.sort(np.clip(rng.choice(centres, 2500) + rng.integers(-2000, 2000, 2500), 0, None))
    keys = np.r_[np.minimum(keys, sentinel - 1), [sentinel] * 20].astype(np.uint64)
    bounds = np.unique(np.r_[0, rng.integers(1, len(keys) - 30, 200), len(keys)])
    present = (rng.random(len(keys)) < 0.3) & (keys < sentinel)
    present[bounds[30] : bounds[36]] = False
    pages = page_cells(keys, bounds, present, depth, grid)
    shown = pages.pop('pages')
    assert (pages['high'] > pages['low']).any() and (pages['valid'] < pages['count']).any()
    pages['length'] = rng.integers(40, 9000, len(shown))
    pages['offset'] = 20000 + np.cumsum(pages['length']) - pages['length'] + np.arange(len(shown))

    nodes = {}

    def store(level, parts, starts):
        """Nodes of `level` over consecutive runs of `parts`, as entries."""
        entries = {k: [] for k in ('low', 'high', 'offset', 'length')}
        for a, b in zip(starts[:-1], starts[1:], strict=True):
            node = _node(level, *(v[a:b] for v in parts.values())) if level else _leaf(parts, a, b)
            raw = gzip.compress(json.dumps(node).encode())
            offset = 10**9 + sum(len(v) for v in nodes.values())
            nodes[offset] = raw
            entries['low'].append(parts['low'][a])
            entries['high'].append(parts['high'][b - 1])
            entries['offset'].append(offset)
            entries['length'].append(len(raw))
        return entries

    leaves = store(0, pages, [*range(0, len(shown), 7), len(shown)])
    branches = store(1, leaves, [*range(0, len(leaves['low']), 3), len(leaves['low'])])
    table, _ = _pages(lambda offset, length: nodes[offset][:length], _node(2, *branches.values()))
    assert all(np.array_equal(table[k], pages[k]) for k in ('low', 'high', 'length'))

    first = pages['first']
    low = keys[first].astype(np.int64)
    high = keys[first + pages['valid'] - 1].astype(np.int64)
    listed = np.concatenate([keys[a : a + n] for a, n in zip(first, pages['valid'], strict=True)])
    shared = 0
    for zoom in range(grid + 1):
        span = 4 ** (depth - zoom)
        tiles, fetched = _tile_bytes(table, 2 * (grid - zoom))
        assert tiles.tolist() == sorted({int(k) // span for k in listed})
        read = np.zeros(len(shown), int)
        for tile, size in zip(tiles.tolist(), fetched.tolist(), strict=True):
            own = (low < (tile + 1) * span) & (high >= tile * span)
            assert own[int(np.searchsorted(high, tile * span))], 'the host is one of them'
            assert size == pages['length'][own].sum()
            read += own
        shared += int((read > 1).sum())
    assert shared, 'tiles share pages'


def test_inspect_reports_tile_sizes(built, tmp_path):
    assert inspect(built)['tile_sizes'] == tile_sizes(built)
    result = CliRunner().invoke(cli, ['inspect', str(built)])
    assert result.exit_code == 0, result.output
    report = tile_sizes(built)
    table = result.output[result.output.index('compressed bytes of the pages') :].splitlines()
    assert table[1].split() == ['zoom', 'lod', 'tiles', 'median', 'p99', 'max', 'largest', 'tile']
    assert len(table) == 2 + len(report)
    for line, zoom in zip(table[2:], report, strict=True):
        assert line.split()[:3] == [str(zoom['zoom']), zoom['lod'], f'{zoom["tiles"]:,}']
        assert line.split()[-1] == '/'.join(map(str, zoom['largest']))
        assert format_bytes(zoom['max_bytes']) in line
    lines = []
    assert log_tile_sizes(built, lines.append) == report
    assert len(lines) == 1 + len(report)
    z, x, y = report[-1]['largest']
    assert lines[-1].startswith(f'  z15 f14: {report[-1]["tiles"]:,} tiles, median ')
    assert lines[-1].endswith(f'largest {format_bytes(report[-1]["max_bytes"])} at {z}/{x}/{y}')

    plain = tmp_path / 'plain.parquet'
    pq.write_table(pq.read_table(built), plain)
    assert tile_sizes(plain) is None
    assert inspect(plain)['tile_sizes'] is None
    assert log_tile_sizes(plain, lines.append) is None and len(lines) == 1 + len(report)
    result = CliRunner().invoke(cli, ['inspect', str(plain)])
    assert result.exit_code == 0, result.output
    assert 'tile sizes: none' in result.output


def test_zoom_without_rows(tmp_path):
    """A zoom whose LOD shows no row has no tiles and no largest one."""
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    x = -105 + np.arange(40) * 0.01
    table = pa.table({'geometry': shapely.to_wkb(shapely.box(x, 39.7, x + 0.002, 39.701))})
    src = tmp_path / 'source.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    out = write(
        src,
        tmp_path / 'sparse.parquet',
        zooms=[10, 11],
        complete_from=11,
        thinning=lambda rows, zoom, ctx: rows.slice(0, 0) if zoom == 10 else rows,
        workers=1,
        log=lambda *_: None,
    )
    empty, full = tile_sizes(out)
    assert empty == {
        'zoom': 10,
        'lod': 'f10',
        'tiles': 0,
        'median_bytes': 0,
        'p99_bytes': 0,
        'max_bytes': 0,
        'largest': None,
    }
    assert full['tiles'] > 0 and full['largest'][0] == 11
    lines = []
    log_tile_sizes(out, lines.append)
    assert lines[1] == '  z10 f10: no tiles'
    result = CliRunner().invoke(cli, ['inspect', str(out)])
    assert result.exit_code == 0, result.output
    assert ['10', 'f10', '0', '-', '-', '-', '-'] in map(str.split, result.output.splitlines())


@pytest.mark.parametrize(
    'count, text',
    [
        (0, '0 B'),
        (999, '999 B'),
        (1000, '1.0 KB'),
        (9949, '9.9 KB'),
        (12_345, '12 KB'),
        (999_499, '999 KB'),
        (999_500, '1.0 MB'),
        (2_620_000, '2.6 MB'),
        (3_100_000_000, '3.1 GB'),
    ],
)
def test_format_bytes(count, text):
    assert format_bytes(count) == text
