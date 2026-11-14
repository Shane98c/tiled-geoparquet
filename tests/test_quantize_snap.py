"""Snapping shapes to an LOD's grid. A polygon has its vertices rounded to
cells and is rebuilt by GEOS's noding snap only where rounding leaves no valid
polygon or one about a cell wide; a line is rounded and loses its repeats and
collapsed parts. Whatever the route, the rows must be ones the packed format
takes."""

import numpy as np
import pytest
import shapely
from shapely import wkt
from tiled_geoparquet.key import EARTH_RADIUS
from tiled_geoparquet.pack import pack
from tiled_geoparquet.quantize import render_lines, render_polygons

SCALE = 10.0
# shapes are given in cells and sit this far from the origin, clear of negative coordinates
AT = 100
# every row's centroid is in cell (4, 4)
SQUARE = 'POLYGON((4 4, 6 4, 6 6, 4 6, 4 4))'
SEGMENT = [(4, 4), (6, 4)]
NAN = float('nan')


def render(shapes, lines=False, dot_cells=2.0):
    """Rendered rows of shapes given in grid cells, as WKT or geometries."""

    def unproject(c):
        m = (c + AT) * SCALE
        lat = 2 * np.arctan(np.exp(m[:, 1] / EARTH_RADIUS)) - np.pi / 2
        return np.stack([np.degrees(m[:, 0] / EARTH_RADIUS), np.degrees(lat)], axis=1)

    shapes = [wkt.loads(s) if isinstance(s, str) else s for s in shapes]
    lonlat = [None if s is None else shapely.transform(s, unproject) for s in shapes]
    centre = np.full(len(shapes), (AT + 4.5) * SCALE)
    return (render_lines if lines else render_polygons)(
        shapely.to_wkb(np.array(lonlat, dtype=object)),
        centre,
        centre,
        SCALE,
        (0.0, 0.0),
        dot_cells,
    )


def packable(parts):
    """Polygon rows as the packed format asks: closed rings of at least three
    stored vertices, no vertex twice in a row, each row a valid shape."""
    geom, poly, ring, x, y = parts
    first, last = ring[:-1], ring[1:] - 1
    assert (np.diff(ring) >= 4).all()
    assert (x[first] == x[last]).all() and (y[first] == y[last]).all()
    moves = (np.diff(x) != 0) | (np.diff(y) != 0)
    within = np.ones(len(x) - 1, bool)
    within[ring[1:-1] - 1] = False
    assert moves[within].all()
    assert shapely.is_valid(polygons(parts)).all()
    assert len(pack('MultiPolygon', parts, [], {})) == len(geom) - 1
    return True


def polygons(parts):
    geom, poly, ring, x, y = parts
    coords = np.stack([x, y], axis=1) - float(AT)
    return shapely.from_ragged_array(shapely.GeometryType.MULTIPOLYGON, coords, (ring, poly, geom))


def same_vertices(parts, expected):
    """Whether each row has exactly the expected rings and vertices, wherever
    a ring starts and whichever way it winds."""
    want = [wkt.loads(e) for e in expected]
    want = [shapely.MultiPolygon([w]) if w.geom_type == 'Polygon' else w for w in want]
    got = polygons(parts)
    assert len(got) == len(want)
    return [
        bool(shapely.equals_exact(shapely.normalize(g), shapely.normalize(w), 0.0))
        for g, w in zip(got, want, strict=True)
    ]


def line_rows(parts):
    geom, part, ring, x, y = parts
    assert np.array_equal(part, np.arange(len(part)))
    xy = list(zip((x - AT).tolist(), (y - AT).tolist(), strict=True))
    assert (np.diff(ring) >= 2).all()
    assert len(pack('MultiLineString', parts, [], {})) == len(geom) - 1
    return [
        [xy[ring[r] : ring[r + 1]] for r in range(geom[i], geom[i + 1])]
        for i in range(len(geom) - 1)
    ]


def test_a_polygon_keeps_its_vertices_rounded_to_cells():
    parts = render(['POLYGON((0.2 0.3, 6.4 0.1, 6.3 3.2, 3.1 2.9, 2.8 7.4, 0.1 7.2, 0.2 0.3))'])
    assert same_vertices(parts, ['POLYGON((0 0, 6 0, 6 3, 3 3, 3 7, 0 7, 0 0))']) == [True]
    assert packable(parts)


@pytest.mark.parametrize(
    'shape',
    [
        # a cut corner whose two vertices both round to (10, 10)
        'POLYGON((0 0, 10 0, 10.45 9.55, 9.55 10.45, 0 10, 0 0))',
        # the same at the ring's first and last vertices
        'POLYGON((0.45 -0.45, 10 0, 10 10, 0 10, -0.45 0.45, 0.45 -0.45))',
    ],
)
def test_vertices_that_round_to_one_cell_are_stored_once(shape):
    parts = render([shape])
    assert same_vertices(parts, ['POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))']) == [True]
    assert len(parts[3]) == 5
    assert packable(parts)


@pytest.mark.parametrize(
    'shape',
    [
        # nine cells long and a third of a cell wide: rounds onto a line
        'POLYGON((0.1 0.1, 9.4 0.1, 9.4 0.4, 0.1 0.4, 0.1 0.1))',
        # rounds onto a point
        'POLYGON((0.1 0.1, 0.4 0.1, 0.4 0.4, 0.1 0.4, 0.1 0.1))',
    ],
)
def test_a_ring_that_collapses_becomes_the_placeholder(shape):
    parts = render([shape])
    assert same_vertices(parts, [SQUARE]) == [True]
    assert packable(parts)


def test_a_ring_that_rounding_pinches_splits_in_two():
    # the waist, 3.6 to 4.4, closes on (4, 4)
    parts = render(['POLYGON((0 0, 8 0, 4.4 4, 8 8, 0 8, 3.6 4, 0 0))'])
    two = 'MULTIPOLYGON(((0 0, 8 0, 4 4, 0 0)), ((0 8, 4 4, 8 8, 0 8)))'
    assert same_vertices(parts, [two]) == [True]
    assert packable(parts)


def test_a_hole_that_closes_is_dropped_and_one_that_stays_is_kept():
    shell = '(0 0, 10 0, 10 10, 0 10, 0 0)'
    parts = render(
        [
            f'POLYGON({shell}, (4.1 4.1, 4.4 4.1, 4.4 4.4, 4.1 4.4, 4.1 4.1))',
            f'POLYGON({shell}, (3.2 3.1, 3.3 6.8, 6.9 7.1, 7.2 2.9, 3.2 3.1))',
            # a hole that rounds onto the shell's edge opens into a notch
            f'POLYGON({shell}, (0.3 2, 4 2, 4 6, 0.3 6, 0.3 2))',
        ]
    )
    assert same_vertices(
        parts,
        [
            f'POLYGON({shell})',
            f'POLYGON({shell}, (3 3, 3 7, 7 7, 7 3, 3 3))',
            'POLYGON((0 0, 10 0, 10 10, 0 10, 0 6, 4 6, 4 2, 0 2, 0 0))',
        ],
    ) == [True, True, True]
    assert np.diff(parts[1]).tolist() == [1, 2, 1]
    assert packable(parts)


def test_multipolygon_parts_stay_drop_or_merge():
    box = '((0.2 0.1, 4.1 0.2, 3.9 4.2, 0.1 3.8, 0.2 0.1))'
    parts = render(
        [
            f'MULTIPOLYGON({box}, ((6.2 6.1, 9.1 5.9, 8.8 9.2, 6.1 8.9, 6.2 6.1)))',
            f'MULTIPOLYGON({box}, ((6.1 6.1, 6.4 6.1, 6.4 6.4, 6.1 6.4, 6.1 6.1)))',
            # a gap of a third of a cell closes
            'MULTIPOLYGON(((0 0, 4 0, 4 4, 0 4, 0 0)), ((4.3 0, 8 0, 8 4, 4.3 4, 4.3 0)))',
        ]
    )
    assert same_vertices(
        parts,
        [
            'MULTIPOLYGON(((0 0, 4 0, 4 4, 0 4, 0 0)), ((6 6, 9 6, 9 9, 6 9, 6 6)))',
            'POLYGON((0 0, 4 0, 4 4, 0 4, 0 0))',
            'POLYGON((0 0, 4 0, 8 0, 8 4, 4 4, 0 4, 0 0))',
        ],
    ) == [True, True, True]
    assert np.diff(parts[0]).tolist() == [2, 1, 1]
    assert packable(parts)


def test_a_sliver_a_cell_wide_collapses_to_the_placeholder():
    # rounds to a valid triangle of half a square cell, (0 0, 7 2, 4 1); noding
    # pulls its long edge onto (4, 1) and leaves nothing
    parts = render(['POLYGON((0 0, 7 2, 4.2 1.4, 0 0))'])
    assert same_vertices(parts, [SQUARE]) == [True]
    assert packable(parts)


def test_a_long_sliver_keeps_its_rounded_shape():
    # forty cells long and under half a cell wide, eight square cells once
    # rounded; noding would pull the long edge onto (24, 1) and leave nothing
    parts = render(['POLYGON((0 0, 40 1, 24.2 0.9, 0 0))'])
    assert same_vertices(parts, ['POLYGON((0 0, 40 1, 24 1, 0 0))']) == [True]
    assert packable(parts)


def test_a_shape_under_the_placeholder_becomes_it():
    small = 'POLYGON((0.2 0.2, 1.4 0.2, 1.4 1.4, 0.2 1.4, 0.2 0.2))'
    long = 'POLYGON((0 0, 3 0, 3 1, 0 1, 0 0))'
    three = 'POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))'
    parts = render([small, long, three])
    assert same_vertices(parts, [SQUARE, long, three]) == [True, True, True]
    assert packable(parts)
    wide = render([small, long, three], dot_cells=4.0)
    assert same_vertices(wide, ['POLYGON((3 3, 7 3, 7 7, 3 7, 3 3))'] * 3) == [True] * 3
    assert packable(wide)


@pytest.mark.parametrize('unreadable', [False, True])
def test_rows_geos_refuses_become_placeholders_and_the_rest_are_kept(unreadable):
    ell = 'POLYGON((0.2 0.3, 6.4 0.1, 6.3 3.2, 3.1 2.9, 2.8 7.4, 0.1 7.2, 0.2 0.3))'
    shapes = [
        ell,
        # crosses itself in the source: noding raises on it
        'POLYGON((0 0, 8 8, 8 0, 0 8, 0 0))',
        'POLYGON((0 0, 8 0, 4.4 4, 8 8, 0 8, 3.6 4, 0 0))',
        None,
        'POLYGON EMPTY',
        'POLYGON((0 0, 8 0, 8 8, 0 8, 0 0))',
    ]
    expected = [
        'POLYGON((0 0, 6 0, 6 3, 3 3, 3 7, 0 7, 0 0))',
        SQUARE,
        'MULTIPOLYGON(((0 0, 8 0, 4 4, 0 0)), ((0 8, 4 4, 8 8, 0 8)))',
        SQUARE,
        SQUARE,
        'POLYGON((0 0, 8 0, 8 8, 0 8, 0 0))',
    ]
    if unreadable:
        # simplifying raises on a coordinate that is not a number
        shapes.append(shapely.Polygon([(0, 0), (4, 0), (NAN, 4), (0, 4)]))
        expected.append(SQUARE)
    parts = render(shapes)
    assert same_vertices(parts, expected) == [True] * len(shapes)
    assert packable(parts)


def test_random_polygons_always_pack():
    rng = np.random.default_rng(11)

    def star(cx, cy, radius):
        n = int(rng.integers(3, 12))
        angle = np.sort(rng.uniform(0, 2 * np.pi, n))
        r = radius * rng.uniform(0.3, 1.0, n)
        return np.stack([cx + r * np.cos(angle), cy + r * np.sin(angle)], axis=1)

    shapes = []
    for i in range(600):
        radius = 10 ** rng.uniform(-0.5, 1.0)
        ring = star(0, 0, radius)
        if i % 4 == 1:
            shapes.append(shapely.Polygon(ring, [ring * rng.uniform(0.2, 0.9)]))
        elif i % 4 == 2:
            away = rng.uniform(0, 3 * radius)
            shapes.append(
                shapely.MultiPolygon([shapely.Polygon(ring), shapely.Polygon(ring + away)])
            )
        else:
            shapes.append(shapely.Polygon(ring))
    for dot_cells in (1.0, 2.0, 4.0):
        parts = render(shapes, dot_cells=dot_cells)
        assert len(parts[0]) == len(shapes) + 1 and (np.diff(parts[0]) >= 1).all()
        assert packable(parts)


def test_line_vertices_that_round_to_one_cell_are_stored_once():
    rows = line_rows(render(['LINESTRING(0 0, 9.55 -0.45, 10.45 0.45, 10 10)'], lines=True))
    assert rows == [[[(0, 0), (10, 0), (10, 10)]]]


def test_a_line_is_rounded_without_noding():
    rows = line_rows(
        render(
            ['LINESTRING(0 0, 10 10, 10 0, 0 10)', 'LINESTRING(0 0, 6 0.2, 0.2 0.3)'], lines=True
        )
    )
    assert rows == [[[(0, 0), (10, 10), (10, 0), (0, 10)]], [[(0, 0), (6, 0), (0, 0)]]]


def test_a_line_that_collapses_becomes_the_placeholder_and_a_part_is_dropped():
    rows = line_rows(
        render(
            [
                'LINESTRING(0.1 0.1, 0.2 0.3, 0.4 0.2)',
                'MULTILINESTRING((0 0, 5.2 4.9), (7.1 7.1, 7.3 7.2))',
                'MULTILINESTRING((0.1 0, 0.2 0.1), (7.1 7.1, 7.3 7.2))',
                None,
                'LINESTRING EMPTY',
                shapely.LineString([(0, 0), (NAN, 4)]),
                'LINESTRING(0 0, 3.2 0.1)',
            ],
            lines=True,
        )
    )
    assert rows == [
        [SEGMENT],
        [[(0, 0), (5, 5)]],
        [SEGMENT],
        [SEGMENT],
        [SEGMENT],
        [SEGMENT],
        [[(0, 0), (3, 0)]],
    ]


def test_random_lines_always_pack():
    rng = np.random.default_rng(5)
    shapes = []
    for i in range(600):
        n = int(rng.integers(2, 12))
        step = 10 ** rng.uniform(-1.5, 1.0)
        line = shapely.LineString(np.cumsum(rng.normal(0, step, (n, 2)), axis=0))
        if i % 3 == 0:
            other = shapely.LineString(np.cumsum(rng.normal(0, step, (3, 2)), axis=0) + step)
            line = shapely.MultiLineString([line, other])
        shapes.append(line)
    parts = render(shapes, lines=True)
    rows = line_rows(parts)
    assert len(rows) == len(shapes) and all(rows)
    for row in rows:
        for part in row:
            assert all(a != b for a, b in zip(part, part[1:], strict=False))
