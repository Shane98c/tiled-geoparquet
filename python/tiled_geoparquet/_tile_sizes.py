"""How heavy a file's tiles are at each zoom, from its spatial directory."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np

from ._layout import (
    BOOTSTRAP_BYTES,
    MAGIC,
    VERSION,
    grid_zoom,
    read_attribute_leaf,
    read_attribute_slot,
    read_node,
    slot_place,
)
from ._validate_index import directory_leaves
from .encoding import check_format
from .key import hilbert_cell


def tile_sizes(path: str | Path) -> list[dict] | None:
    """Per zoom of `lod_columns.zooms`, the compressed bytes of data pages a
    cold read of one tile fetches, or None for a file without the spatial
    index. Reads the bootstrap and the LOD directories, no data page.

    A tile fetches the pages of its zoom's LOD whose cells meet its key
    interval, the copy host among them, each page whole even where it also
    holds other tiles' rows. Directory node reads are not counted. Each zoom
    is a dict of `zoom`; `lod`, the LOD serving it; `tiles`, the tiles with a
    keyed row of their own in a listed page; `median_bytes`, `p99_bytes` and
    `max_bytes` over those tiles; and `largest`, the `[z, x, y]` of the
    heaviest, None at a zoom without tiles. A zoom past the key's depth reads
    by the depth cell holding the tile, so its tiles are those cells. In a
    file with column attributes, `attributes` gives, per attribute, the
    `median_bytes` and `max_bytes` of the attribute's pages over each tile's
    own rows, which a tile styled by it fetches beside the overview pages.
    """
    with open(path, 'rb') as source:
        root = _root(source)
        if root is None:
            return None
        sizes = {found[0]: _summary(*found) for found in _zoom_tiles(source, root)}
    return [sizes[zoom] for zoom in root['lod']['zooms']]


def _root(source):
    """The spatial index root of an open file, or None for a file without one."""
    source.seek(0)
    head = source.read(BOOTSTRAP_BYTES)
    if head[4:12] != MAGIC:
        return None
    root = json.loads(gzip.decompress(head[16 : 16 + int.from_bytes(head[12:16], 'little')]))
    if root.get('version') != VERSION:
        raise ValueError(f'unsupported spatial index version {root.get("version")}')
    check_format(root['lod'])
    return root


def _zoom_tiles(source, root):
    """Yield, for each zoom of the open file with spatial index `root`, the
    zoom, its LOD, the zoom its tiles are counted at, those tiles as ascending
    Hilbert indexes and the bytes a read of each fetches. Holds one LOD's
    pages at a time."""
    lod = root['lod']
    serving = {
        zoom: next((name for name, info in lod['lods'].items() if zoom in info['zooms']), None)
        for zoom in lod['zooms']
    }
    if None in serving.values():
        raise ValueError('a zoom has no LOD')

    def read(offset, length):
        if offset < BOOTSTRAP_BYTES:
            raise ValueError('directory node outside the data region')
        source.seek(offset)
        raw = source.read(length)
        if len(raw) != length:
            raise ValueError('truncated directory node')
        return raw

    columns = [a['name'] for a in lod['attributes'] if a.get('storage') == 'column']
    slots = root.get('attribute_slots') or [0] * (len(lod['lods']) + 1)
    slots = dict(zip(lod['lods'], slots[:-1], strict=True))
    for name in dict.fromkeys(serving.values()):
        pages, attributes = _pages(read, root['directories'][name], len(columns), slots[name])
        grid = grid_zoom(lod, name)
        for zoom in (zoom for zoom, served in serving.items() if served == name):
            tile_zoom = min(zoom, grid)
            tiles, fetched = _tile_bytes(pages, 2 * (grid - tile_zoom))
            own = _own_rows(pages, tiles, 2 * (grid - tile_zoom))
            styled = {a: _span_bytes(t, *own) for a, t in zip(columns, attributes, strict=True)}
            yield zoom, name, tile_zoom, tiles, fetched, styled


def _pages(read, top, attributes=0, slot=0):
    """An LOD's pages in key order, from its directory's top node: each one's
    minimum cell, maximum cell and compressed length, `cells`, the cells its
    pages hold keyed rows in, and its fences' cells and global rows with the
    end of the keyed rows of each one's page; then, per column attribute of
    `attributes`, the pages the attribute leaves in the slots of `slot` bytes
    of each LOD leaf list beside the LOD's. Holds one leaf's other fields
    at a time."""
    level, entries = read_node(top)
    fields = ('low', 'high', 'length', 'cells', 'fence_cell', 'fence_start', 'fence_end')
    parts = {k: [np.zeros(0, np.int64)] for k in fields}
    located = [
        {k: [np.zeros(0, np.int64)] for k in ('first', 'count', 'length')}
        for _ in range(attributes)
    ]
    for leaf in directory_leaves(read, level, entries, {}):
        keyed = leaf['fence_row'] < leaf['valid'][leaf['fence_page']]
        leaf['cells'] = np.unique(leaf['fence_cell'][keyed])
        page = leaf['fence_page']
        leaf['fence_start'] = leaf['first'][page] + leaf['fence_row']
        leaf['fence_end'] = leaf['first'][page] + leaf['valid'][page]
        for k in fields:
            parts[k].append(leaf[k])
        for a, table in enumerate(located):
            held = read(slot_place(leaf['group'], a, slot), slot)
            found = read_attribute_leaf(read_attribute_slot(held, a, leaf['range'][0]))
            if len(found['length']) != len(leaf['low']):
                raise ValueError('an attribute leaf disagrees with its LOD leaf')
            table['first'].append(leaf['first'])
            table['count'].append(leaf['count'])
            table['length'].append(found['length'])
    pages = {k: np.concatenate(v) for k, v in parts.items()}
    return pages, [{k: np.concatenate(v) for k, v in table.items()} for table in located]


def _tile_bytes(pages, shift):
    """The tiles holding any of the pages' cells, as ascending Hilbert indexes
    at the zoom whose tiles are `4 ** (shift / 2)` cells, and the bytes of the
    pages a read of each fetches."""
    tiles = np.unique(pages['cells'] >> shift)
    # Pages are in key order, so those meeting a tile's cells are one run. The
    # host page, the first whose maximum cell reaches the tile, starts it: a
    # tile with a row of its own has a page there.
    first = np.searchsorted(pages['high'], tiles << shift)
    end = np.searchsorted(pages['low'], (tiles + 1) << shift)
    total = np.r_[0, np.cumsum(pages['length'])]
    return tiles, total[end] - total[first]


def _own_rows(pages, tiles, shift):
    """Each tile's own rows, those keyed in its cells, as the global rows
    `start` up to `end`: the fences in key order put them in one run."""
    if not len(tiles):
        return tiles, tiles
    a = np.searchsorted(pages['fence_cell'], tiles << shift)
    b = np.searchsorted(pages['fence_cell'], (tiles + 1) << shift)
    start = pages['fence_start'][np.minimum(a, len(pages['fence_start']) - 1)]
    last = np.maximum(b - 1, 0)
    # the run ends at the next fence in its page, or at its page's keyed rows' end
    following = np.minimum(b, len(pages['fence_start']) - 1)
    same_page = (b < len(pages['fence_start'])) & (
        pages['fence_end'][following] == pages['fence_end'][last]
    )
    end = np.where(same_page, pages['fence_start'][following], pages['fence_end'][last])
    return start, end


def _span_bytes(pages, start, end):
    """The compressed bytes of `pages`, in row order, over each run of rows
    `start[i]` up to `end[i]`."""
    first = np.searchsorted(pages['first'] + pages['count'], start, 'right')
    last = np.searchsorted(pages['first'], end)
    total = np.r_[0, np.cumsum(pages['length'])]
    return total[np.maximum(last, first)] - total[first]


def _summary(zoom, name, tile_zoom, tiles, fetched, styled):
    """What `tile_sizes` reports for one zoom, from what `_zoom_tiles` yields."""
    if not len(tiles):
        empty = {'tiles': 0, 'median_bytes': 0, 'p99_bytes': 0, 'max_bytes': 0, 'largest': None}
        summary = {'zoom': zoom, 'lod': name, **empty}
    else:
        median, p99 = np.percentile(fetched, [50, 99])
        heaviest = int(np.argmax(fetched))
        x, y = hilbert_cell(tiles[heaviest : heaviest + 1], tile_zoom)
        summary = {
            'zoom': zoom,
            'lod': name,
            'tiles': len(tiles),
            'median_bytes': round(float(median)),
            'p99_bytes': round(float(p99)),
            'max_bytes': int(fetched[heaviest]),
            'largest': [tile_zoom, int(x[0]), int(y[0])],
        }
    if styled:
        summary['attributes'] = {
            a: {
                'median_bytes': round(float(np.median(b))) if len(b) else 0,
                'max_bytes': int(b.max()) if len(b) else 0,
            }
            for a, b in styled.items()
        }
    return summary


def format_bytes(count: float) -> str:
    """A byte count in decimal units: `68 B`, `1.2 KB`, `310 KB`, `2.6 MB`."""
    value, unit = float(count), 0
    while value >= 999.5 and unit < 3:
        value /= 1000
        unit += 1
    digits = 1 if unit and value < 9.95 else 0
    return f'{value:.{digits}f} {("B", "KB", "MB", "GB")[unit]}'


def log_tile_sizes(path: str | Path, log=print) -> list[dict] | None:
    """Log `tile_sizes(path)`, a heading and one line per zoom, and return it.
    Logs nothing for a file without the spatial index."""
    sizes = tile_sizes(path)
    if sizes is None:
        return None
    log('Tile sizes: compressed bytes of the pages a cold tile read fetches')
    for zoom in sizes:
        label = f'  z{zoom["zoom"]} {zoom["lod"]}'
        if not zoom['tiles']:
            log(f'{label}: no tiles')
            continue
        median, p99, largest = (
            format_bytes(zoom[k]) for k in ('median_bytes', 'p99_bytes', 'max_bytes')
        )
        log(
            f'{label}: {zoom["tiles"]:,} {"tile" if zoom["tiles"] == 1 else "tiles"}, '
            f'median {median}, p99 {p99}, largest {largest} at {"/".join(map(str, zoom["largest"]))}'
        )
        if zoom.get('attributes'):
            log(
                '    attribute pages, median: '
                + ', '.join(
                    f'{a} {format_bytes(b["median_bytes"])}' for a, b in zoom['attributes'].items()
                )
            )
    return sizes
