"""Straddler copies: every tile a feature reaches holds it exactly once."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
import shapely
from tiled_geoparquet import read_overview, validate, write
from tiled_geoparquet.key import KEY_DEPTH, hilbert_key, tile_x, tile_y
from tiled_geoparquet.quantize import render_polygons
from tiled_geoparquet.spill import MARGIN_PIXELS, straddlers, take_ragged
from unpacker import Unpacker

MARGIN = MARGIN_PIXELS / 512


def key_in(x: int, y: int, zoom: int) -> np.ndarray:
    """A key whose cell is in tile (x, y) at `zoom`."""
    shift = KEY_DEPTH - zoom
    return hilbert_key(np.array([x << shift]), np.array([y << shift]), KEY_DEPTH)


def reached(xmin, ymin, xmax, ymax, zoom, margin=MARGIN):
    """The tiles a bbox reaches at `zoom`, each reaching `margin` past its edge, by trying each."""
    fx0, fx1, fy0, fy1 = (
        tile_x(xmin, zoom),
        tile_x(xmax, zoom),
        tile_y(ymax, zoom),
        tile_y(ymin, zoom),
    )
    return {
        (x, y)
        for x in range(int(fx0) - 2, int(fx1) + 3)
        for y in range(int(fy0) - 2, int(fy1) + 3)
        if fx0 - margin < x + 1 and x <= fx1 + margin and fy0 - margin < y + 1 and y <= fy1 + margin
    }


def tile_rows(keys, decoded, zoom, x, y):
    """File rows a reader finds in tile (x, y): the rows of its key interval
    with geometry, and the copies tagged with the tile there or on its host."""
    span = 4 ** (KEY_DEPTH - zoom)
    start = int(hilbert_key(np.array([x]), np.array([y]), zoom)[0]) * span
    inside = np.flatnonzero((keys >= start) & (keys < start + span))
    host = min(int(np.searchsorted(keys, start)), len(keys) - 1)
    rows = [int(r) for r in inside if decoded[r] and decoded[r]['geometry']]
    tag = (zoom << 48) | (x * 2**zoom + y)
    for r in np.unique(np.r_[inside, host]):
        if decoded[r]:
            rows += [c['id'] for c in decoded[r]['spill'] if c['tile'] == tag]
    return rows


def test_a_feature_centred_on_a_tile_edge_is_copied_where_its_key_is_not():
    """At an exact edge the key's arithmetic may put the feature on either side;
    the copy goes to the other side either way."""
    zoom, k = 14, 3413
    edge = -180 + k * 360 / 2**zoom
    bounds = [np.array([v]) for v in (edge - 2**-12, 39.7, edge + 2**-12, 39.7001)]
    assert (bounds[0] + bounds[2]) / 2 == edge
    (row,) = {y for _, y in reached(*(b[0] for b in bounds), zoom)}
    for own, other in ((k - 1, k), (k, k - 1)):
        rows, tiles = straddlers(*bounds, key_in(own, row, zoom), zoom, MARGIN)
        assert rows.tolist() == [0]
        assert tiles.tolist() == [other * 2**zoom + row]


def test_a_feature_short_of_a_tile_is_copied_there_within_the_margin():
    """A neighbour two pixels away still draws an outline across the edge; one
    in the encoder's 64/4096 buffer but past the margin draws nothing there."""
    zoom, k = 14, 3413
    edge = -180 + k * 360 / 2**zoom
    pixel = 360 / 2**zoom / 512
    row = int(tile_y(39.7, zoom))
    for gap, tiles in ((1, [k * 2**zoom + row]), (6, [])):
        bounds = [np.array([v]) for v in (edge - 40 * pixel, 39.7, edge - gap * pixel, 39.7001)]
        assert straddlers(*bounds, key_in(k - 1, row, zoom), zoom, MARGIN)[1].tolist() == tiles, gap


def test_every_other_tile_a_feature_reaches_gets_one_copy():
    rng = np.random.default_rng(4)
    n, zoom = 600, 12
    tile = 360 / 2**zoom
    cx, cy = -105 + rng.random(n) * 0.5, 39.6 + rng.random(n) * 0.3
    half_w = rng.choice([0.2, 0.6, 1.1, 1.6, 2.2], n) * tile
    half_h = rng.choice([0.2, 0.6, 1.1], n) * tile * 0.77
    xmin, xmax, ymin, ymax = cx - half_w, cx + half_w, cy - half_h, cy + half_h
    kx = np.floor(tile_x((xmin + xmax) / 2, KEY_DEPTH)).astype(np.int64)
    ky = np.floor(tile_y((ymin + ymax) / 2, KEY_DEPTH)).astype(np.int64)
    rows, tiles = straddlers(xmin, ymin, xmax, ymax, hilbert_key(kx, ky, KEY_DEPTH), zoom, MARGIN)
    expected, shapes = [], set()
    for i in range(n):
        tiles_i = reached(xmin[i], ymin[i], xmax[i], ymax[i], zoom)
        xs, ys = {x for x, _ in tiles_i}, {y for _, y in tiles_i}
        shapes.add((len(xs), len(ys)))
        own = (kx[i] >> (KEY_DEPTH - zoom), ky[i] >> (KEY_DEPTH - zoom))
        assert own in tiles_i
        expected += [(i, x * 2**zoom + y) for x, y in tiles_i if (x, y) != own]
    assert {(3, 1), (4, 1), (2, 2), (5, 3)} <= shapes
    assert sorted(zip(rows.tolist(), tiles.tolist(), strict=True)) == sorted(expected)


def test_take_ragged_picks_whole_rows_in_order():
    polys = [
        shapely.MultiPolygon([shapely.box(i + j, 0, i + j + 0.5, 1) for j in range(i % 3 + 1)])
        for i in range(40)
    ]
    polys[7] = shapely.box(0, 0, 4, 4).difference(shapely.box(1, 1, 2, 2))
    zeros = np.zeros(len(polys))
    store = render_polygons(shapely.to_wkb(polys), zeros, zeros, 1000.0, (0.0, 0.0))
    picks = np.array([7, 3, 3, 39, 0])
    geom, poly, ring, x, y = take_ragged(store, picks)
    for i, p in enumerate(picks):
        alone = render_polygons(
            shapely.to_wkb([polys[p]]), zeros[:1], zeros[:1], 1000.0, (0.0, 0.0)
        )
        polygons = slice(geom[i], geom[i + 1] + 1)
        assert np.array_equal(np.diff(poly[polygons]), np.diff(alone[1]))
        rings = slice(poly[geom[i]], poly[geom[i + 1]] + 1)
        assert np.array_equal(np.diff(ring[rings]), np.diff(alone[2]))
        coords = slice(ring[poly[geom[i]]], ring[poly[geom[i + 1]]])
        assert np.array_equal(x[coords], alone[3]) and np.array_equal(y[coords], alone[4])
    assert [len(a) for a in take_ragged(store, np.zeros(0, np.int64))] == [1, 1, 1, 0, 0]


@pytest.fixture
def edge_features(tmp_path):
    """Shapes centred exactly on tile edges, shapes three, four and two by two
    tiles across, and shapes ending 1, 6, 12 and 20 pixels short of a tile, at
    zoom 14."""
    zoom = 14
    tile = 360 / 2**zoom
    pixel = tile / 512
    # an edge of tiles at zooms 12 and 13 too
    k = int(tile_x(-105.0, zoom)) // 4 * 4
    edge = -180 + k * tile
    row = int(tile_y(39.7, zoom))
    lat_edge = float(np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * row / 2**zoom)))))
    middle = edge + tile / 2
    boxes = {
        'x edge': (edge - 2**-12, 39.7005, edge + 2**-12, 39.7008),
        'y edge': (edge + 0.3 * tile, lat_edge - 2**-13, edge + 0.4 * tile, lat_edge + 2**-13),
        'equator': (edge + 0.3 * tile, -(2**-13), edge + 0.4 * tile, 2**-13),
        'three across': (middle - 1.3 * tile, 39.7005, middle + 1.3 * tile, 39.7008),
        'four across': (edge - 1.6 * tile, 39.7005, edge + 1.6 * tile, 39.7008),
        'corner': (
            edge - 0.2 * tile,
            lat_edge - 0.2 * tile,
            edge + 0.2 * tile,
            lat_edge + 0.2 * tile,
        ),
        **{
            f'{gap} short': (edge - 0.3 * tile, 39.7005, edge - gap * pixel, 39.7008)
            for gap in (1, 6, 12, 20)
        },
    }
    table = pa.table(
        {
            'name': list(boxes),
            'geometry': shapely.to_wkb([shapely.box(*b) for b in boxes.values()]),
        }
    )
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    src = tmp_path / 'edges.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)
    return src, boxes


@pytest.mark.parametrize(
    'margin, copied',
    [(None, {1}), (0, set()), (16, {1, 6, 12}), (9.5, {1, 6})],
)
def test_every_tile_a_feature_reaches_holds_it_once(edge_features, tmp_path, margin, copied):
    """Every tile a feature reaches, or comes within the margin of, holds it
    once at every zoom: at zoom 14 the shapes short of a tile by at most the
    margin are copied there and the others are not."""
    src, boxes = edge_features
    options = {} if margin is None else {'margin_pixels': margin}
    out = write(
        src,
        tmp_path / 'edges.lod.parquet',
        zooms=[12, 13, 14],
        complete_from=12,
        attributes=(),
        workers=1,
        log=lambda *_: None,
        **options,
    )
    validate(out, full=True)
    pixels = MARGIN_PIXELS if margin is None else margin
    share = pixels / 512
    for gap in (1, 6, 12, 20):
        assert len(reached(*boxes[f'{gap} short'], 14, share)) == 1 + (gap in copied)
    table = pq.read_table(out)
    meta = json.loads(table.schema.metadata[b'geo'])['lod_columns']
    assert meta['margin'] == pixels
    keys = table.column('hilbert').to_numpy()
    names = table.column('name').to_pylist()
    for zoom in (12, 13, 14):
        name, info = next((n, i) for n, i in meta['lods'].items() if zoom in i['zooms'])
        decoded = [
            Unpacker(blob, info['geometry_type'], []).row() if blob is not None else None
            for blob in table.column('lod').combine_chunks().field(name).to_pylist()
        ]
        for row, label in enumerate(names):
            tiles = reached(*boxes[label], zoom, share)
            xs, ys = {x for x, _ in tiles}, {y for _, y in tiles}
            near = {
                (x, y)
                for x in range(min(xs) - 1, max(xs) + 2)
                for y in range(min(ys) - 1, max(ys) + 2)
            }
            for x, y in near:
                found = tile_rows(keys, decoded, zoom, x, y)
                assert found.count(row) == ((x, y) in tiles), (label, zoom, x, y)


@pytest.mark.parametrize('override', [99, None])
def test_a_copy_holds_the_value_its_row_has_at_the_lod(tmp_path, override):
    """A thinning's value for an attribute, null included, reaches the copy in
    the next tile as well as the row's attribute column, so the feature reads
    the same on both sides of the edge, here on a host the LOD does not show."""
    zoom = 14
    edge = -180 + 3413 * 360 / 2**zoom
    row = int(tile_y(39.7, zoom)) + 0.5
    lat = float(np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * row / 2**zoom)))))
    boxes = [
        shapely.box(edge - 0.003, lat - 0.0005, edge + 0.001, lat + 0.0005),
        shapely.box(edge + 0.005, lat - 0.0005, edge + 0.006, lat + 0.0005),
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table({'value': pa.array([1, 2], pa.int64()), 'geometry': shapely.to_wkb(boxes)})
    src = tmp_path / 'override.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), src)

    def first(rows, zoom, ctx):
        kept = rows.filter(pc.equal(rows.column('value'), 1))
        values = pa.array([override] * kept.num_rows, pa.int64())
        return kept.set_column(kept.schema.get_field_index('value'), 'value', values)

    out = write(
        src,
        tmp_path / 'override.lod.parquet',
        thinning=first,
        zooms=[zoom, zoom + 1],
        complete_from=zoom + 1,
        attributes=['value'],
        single_lod_bytes=0,
        workers=1,
        log=lambda *_: None,
    )
    validate(out, full=True)
    table = pq.read_table(out)
    meta = json.loads(table.schema.metadata[b'geo'])['lod_columns']
    name, info = next((n, i) for n, i in meta['lods'].items() if zoom in i['zooms'])
    decoded = [
        Unpacker(blob, info['geometry_type'], [pa.field('value', pa.int64())]).row()
        for blob in table.column('lod').combine_chunks().field(name).to_pylist()
    ]
    straddler, host = (table.column('value').to_pylist().index(v) for v in (1, 2))
    assert read_overview(out, zoom).to_pydict() == {
        'row': [straddler],
        'geometry': read_overview(out, zoom).column('geometry').to_pylist(),
        'value': [override],
    }
    assert decoded[host]['geometry'] == [], 'the host is not shown at the LOD'
    (copy,) = decoded[host]['spill']
    assert copy['id'] == straddler
    assert copy['attrs'] == {'value': override}


@pytest.mark.parametrize('margin', [None, 16])
def test_the_js_reader_draws_copies_out_to_the_margin(edge_features, tmp_path, margin):
    """Tiles from both JS readers hold the shapes short of their west edge by at
    most the margin, unclipped out to it, at the LOD zooms 12 to 14 and from the
    primary geometry at zoom 15, where the tile reads as far as the margin or
    8 pixels, whichever is more. A file of another version or without a
    margin is refused."""
    src, boxes = edge_features
    options = {} if margin is None else {'margin_pixels': margin}
    out = write(
        src,
        tmp_path / 'edges.lod.parquet',
        zooms=[12, 13, 14],
        complete_from=12,
        workers=1,
        log=lambda *_: None,
        **options,
    )
    pixels = MARGIN_PIXELS if margin is None else margin
    reach = max(8, pixels)
    names = pq.read_table(out, columns=['name']).column('name').to_pylist()
    cases = []
    for gap in (1, 6, 12, 20):
        _, ymin, xmax, ymax = boxes[f'{gap} short']
        for z in (12, 13, 14, 15):
            fx = float(tile_x(np.array([xmax]), z)[0])
            short = (np.floor(fx) + 1 - fx) * 512
            cases.append(
                {
                    'z': z,
                    'x': int(fx) + 1,
                    'y': int(tile_y(np.array([(ymin + ymax) / 2]), z)[0]),
                    'id': names.index(f'{gap} short'),
                    'present': bool(short <= (reach if z == 15 else pixels)),
                    'reach': reach * 8,
                }
            )
    present = {(c['z'], c['id']) for c in cases if c['present']}
    assert len(present) == (13 if margin else 5), present
    (tmp_path / 'cases.json').write_text(json.dumps(cases))
    table = pq.read_table(out)
    refused = []
    for name, change in (
        ('version', lambda lod: lod.update(version='0.1.0')),
        ('margin', lambda lod: lod.pop('margin')),
    ):
        geo = json.loads(table.schema.metadata[b'geo'])
        change(geo['lod_columns'])
        path = tmp_path / f'{name}.parquet'
        metadata = {**table.schema.metadata, b'geo': json.dumps(geo).encode()}
        pq.write_table(table.replace_schema_metadata(metadata), path)
        refused.append(str(path))
    script = Path(__file__).resolve().parents[1] / 'js' / 'test' / 'margin.test.mjs'
    result = subprocess.run(
        ['node', str(script), str(out), str(tmp_path / 'cases.json'), *refused],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert 'margin tests passed' in result.stdout
