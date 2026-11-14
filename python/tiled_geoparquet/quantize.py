"""Geometry on an LOD's grid.

An LOD's grid is a quarter of the Web Mercator pixel at its finest zoom, in
EPSG:3857 metres, anchored at the dataset's minimum corner, so a feature a
pixel or two across keeps its own shape rather than snapping to a square, as
tippecanoe's eighth-pixel tolerance does. Footprints are projected,
simplified and snapped to that grid; anything that collapses becomes a small
square around its centroid, half a pixel wide, so it still draws.
"""

from __future__ import annotations

import numpy as np
import shapely

from .encoding import round_away
from .key import mercator, resolution

SUBPIXELS = 4
DOT_CELLS = 2
SLIVER_CELLS = 2.0


def grid(zoom: int, tile_size: int = 512) -> float:
    """Metres per grid cell of an LOD whose finest zoom is `zoom`."""
    return resolution(zoom, tile_size) / SUBPIXELS


def _to_grid(wkb, scale: float, offset: tuple[float, float]):
    def project(c):
        x, y = mercator(c[:, 0], c[:, 1])
        return np.stack([(x - offset[0]) / scale, (y - offset[1]) / scale], axis=1)

    return shapely.transform(shapely.from_wkb(wkb), project)


def _dot_squares(qx, qy, cells: float):
    """A `cells`-wide square on the centre of the grid cell holding each
    centroid. It reaches at most `1 + cells / 2` cells past the centroid,
    which the reader pads tile bounds by."""
    h = max(cells / 2, 0.5)
    corners = [(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)]
    ring = np.stack(
        [np.stack([qx + 0.5 + dx * h, qy + 0.5 + dy * h], axis=1) for dx, dy in corners],
        axis=1,
    )
    return shapely.polygons(ring)


def _each(op, g):
    """`op` over an array of shapes, None in the rows GEOS raises on."""
    try:
        return op(g)
    except shapely.errors.GEOSException:
        out = np.empty(len(g), dtype=object)
        for i, geom in enumerate(g):
            try:
                out[i] = op(geom)
            except shapely.errors.GEOSException:
                out[i] = None
        return out


def _simplify(g):
    return shapely.simplify(g, 0.7, preserve_topology=True)


def _quantize_lines(g):
    """Simplified lines on the integer grid. GEOS rounds a line's vertices,
    drops the repeats and removes a part left with a single point."""
    return _each(lambda s: shapely.set_precision(_simplify(s), 1.0), g)


def _under(g, cells: float):
    """Rows narrower than `cells` in both directions."""
    minx, miny, maxx, maxy = shapely.bounds(g).T
    with np.errstate(invalid='ignore'):
        return (maxx - minx < cells) & (maxy - miny < cells)


def _quantize_polygons(g, dot_cells: float):
    """Simplified polygons on the integer grid, each valid or None.

    Rounding every vertex to its cell keeps most polygons valid and is several
    times cheaper than noding them. The noding snap rebuilds the rest: those
    rounding leaves invalid, where a ring collapses, touches or crosses itself
    or two parts meet, and those under `SLIVER_CELLS` square cells, about a
    cell wide, which noding mostly collapses and rounding would keep as
    hairlines. A rounded polygon can keep a vertex twice in a row;
    `render_polygons` stores it once."""
    simple = _each(_simplify, g)
    snapped = _each(lambda s: shapely.set_precision(s, 1.0, mode='pointwise'), simple)
    # noding puts vertices only on rounded ones, so a shape under the placeholder
    # stays under it and is drawn as one however it is snapped
    dot = _under(snapped, dot_cells)
    rest = np.flatnonzero(~dot)
    keep = shapely.is_valid(snapped[rest])
    keep[keep] = shapely.area(snapped[rest[keep]]) >= SLIVER_CELLS
    redo = rest[~keep]
    if len(redo):
        noded = _each(lambda s: shapely.set_precision(s, 1.0), simple[redo])
        noded[~shapely.is_valid(noded)] = None
        snapped[redo] = noded
    snapped[dot] = None
    return snapped


def render_polygons(
    wkb, cx, cy, scale: float, offset: tuple[float, float], dot_cells: float = DOT_CELLS
):
    """Quantized MultiPolygon per row as ragged int32 arrays:
    (geometry offsets, polygon offsets, ring offsets, x, y). `wkb` is in
    longitude and latitude; `cx`, `cy` are centroids in metres."""
    g = _quantize_polygons(_to_grid(wkb, scale, offset), dot_cells)
    small = shapely.is_missing(g) | shapely.is_empty(g) | _under(g, dot_cells)
    if small.any():
        g = g.copy()
        g[small] = _dot_squares(
            np.floor((cx[small] - offset[0]) / scale),
            np.floor((cy[small] - offset[1]) / scale),
            dot_cells,
        )
    polys, poly_parent = shapely.get_parts(g, return_index=True)
    rings, ring_parent = shapely.get_rings(polys, return_index=True)
    coords, ring = shapely.get_coordinates(rings, return_index=True)
    repeat = np.zeros(len(coords), bool)
    repeat[1:] = (ring[1:] == ring[:-1]) & (coords[1:] == coords[:-1]).all(axis=1)
    coords, ring = coords[~repeat], ring[~repeat]
    geom_offsets = np.zeros(len(g) + 1, dtype=np.int64)
    np.add.at(geom_offsets, poly_parent + 1, 1)
    poly_offsets = np.zeros(len(polys) + 1, dtype=np.int64)
    np.add.at(poly_offsets, ring_parent + 1, 1)
    ring_offsets = np.concatenate([[0], np.bincount(ring, minlength=len(rings))])
    return (
        np.cumsum(geom_offsets),
        np.cumsum(poly_offsets),
        np.cumsum(ring_offsets),
        round_away(coords[:, 0]),
        round_away(coords[:, 1]),
    )


def render_lines(
    wkb, cx, cy, scale: float, offset: tuple[float, float], dot_cells: float = DOT_CELLS
):
    """Quantized MultiLineString per row in the polygon tree shape: each part is
    one 'ring' that is not closed. A line that collapses on the grid becomes a
    two-cell segment at its centroid so it still draws."""
    g = _quantize_lines(_to_grid(wkb, scale, offset))
    small = shapely.is_missing(g) | shapely.is_empty(g) | _under(g, 1)
    if small.any():
        g = g.copy()
        qx = np.floor((cx[small] - offset[0]) / scale)
        qy = np.floor((cy[small] - offset[1]) / scale)
        g[small] = shapely.linestrings(
            np.stack([np.stack([qx, qy], 1), np.stack([qx + dot_cells, qy], 1)], 1)
        )
    parts, parent = shapely.get_parts(g, return_index=True)
    coords = shapely.get_coordinates(parts)
    geom_offsets = np.zeros(len(g) + 1, dtype=np.int64)
    np.add.at(geom_offsets, parent + 1, 1)
    part_offsets = np.arange(len(parts) + 1, dtype=np.int64)
    ring_offsets = np.concatenate([[0], shapely.get_num_coordinates(parts)])
    return (
        np.cumsum(geom_offsets),
        part_offsets,
        np.cumsum(ring_offsets),
        round_away(coords[:, 0]),
        round_away(coords[:, 1]),
    )
