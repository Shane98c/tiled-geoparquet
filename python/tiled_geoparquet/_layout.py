"""Cluster the LOD column chunks and add compact spatial page directories.

The output is still the same Parquet table, including every LOD value. No
geometry, attribute or row id is changed. LOD pages and the attribute columns
beside them are re-encoded with zstd at tile boundaries; every other column
chunk is copied verbatim. A directory of boxed runs of rows locates the
primary geometry's own pages, a directory per flat column locates its pages
by row, and fixed-size slots after each group of directory leaves hold the
leaves listing the pages of the attributes.
Readers unaware of the directories follow the relocated standard footer.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import copy
import gzip
import itertools
import json
import os
import struct
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ._compact import dumps, get, loads, set_value
from .encoding import (
    INDEX_KEY_DEPTH,
    INDEX_VERSION,
    check_format,
    check_index_version,
    check_zooms,
)
from .key import hilbert_key
from .spill import ranges
from .unpack import PageBounds, leading_rows, leads, moved, shown_rows
from .writer import DEFAULT_WORKERS

BOOTSTRAP_BYTES = 16384
# reserve room for file_bytes, footer_bytes and the header
_ROOT_TARGET_BYTES = BOOTSTRAP_BYTES - 128
# the most the attributes' summaries add to the root, packed
_SUMMARY_BYTES = 4096
MAGIC = b'LODIDX01'
CODECS = ['UNCOMPRESSED', 'SNAPPY', 'GZIP', 'LZO', 'BROTLI', 'LZ4', 'ZSTD', 'LZ4_RAW']
BRANCH_ENTRIES = 64
# an attribute slot's length, attribute and leaf offset
SLOT_HEADER = 16
PAGE_FIELDS = ('low', 'high', 'offset', 'length', 'first', 'count', 'valid')
FENCE_FIELDS = ('fence_page', 'fence_row', 'fence_cell')
# primary directory boxes are whole cells of 2**-BOX_BITS degrees
BOX_BITS = 12
BOX_FIELDS = ('x0', 'y0', 'x1', 'y1')
SOURCE_FIELDS = ('offset', 'length', 'first', 'count')
# A geometry page is cut into runs of rows, in two and each half once more,
# where the cut makes the boxes cover at most RUN_SAVING of the tiles of
# RUN_TILE cells its box covers.
RUN_ROUNDS = 2
RUN_SAVING = 0.7
RUN_TILE = 32
# Uncompressed LOD bytes from which worker processes encode the LOD pages,
# about half a second of encoding in one process. Under half of it, starting
# the workers takes as long as they save.
PARALLEL_LOD_BYTES = 32 * 1024 * 1024


def _json(value):
    return gzip.compress(json.dumps(value, separators=(',', ':')).encode(), mtime=0)


def covering(geo):
    """The primary geometry's bbox covering: its struct column and the fields
    holding xmin, ymin, xmax and ymax."""
    paths = geo['columns'][geo['primary_column']].get('covering', {}).get('bbox')
    keys = ('xmin', 'ymin', 'xmax', 'ymax')
    if (
        not isinstance(paths, dict)
        or any(not isinstance(paths.get(k), list) or len(paths[k]) != 2 for k in keys)
        or len({paths[k][0] for k in keys}) != 1
    ):
        raise ValueError('the primary geometry needs a bbox covering of one struct column')
    return paths['xmin'][0], [paths[k][1] for k in keys]


def covering_cells(bbox, fields=('xmin', 'ymin', 'xmax', 'ymax')):
    """Each row's covering box in whole cells of 2**-BOX_BITS degrees, columns
    counted from longitude -180 and rows from latitude -90, as float64 arrays
    `x0, y0, x1, y1`. A cell holds its lower edges, so the cells of a box's
    minimum and maximum contain it. A row has no extent, and NaN cells, unless
    its four bounds are finite with each minimum at most its maximum; a null
    struct or value has none. `bbox` is the covering's struct array and
    `fields` name its xmin, ymin, xmax and ymax."""
    names = [bbox.type.field(i).name for i in range(bbox.type.num_fields)]
    values = dict(zip(names, bbox.flatten(), strict=True))
    xmin, ymin, xmax, ymax = (
        values[k].to_numpy(zero_copy_only=False).astype(np.float64) for k in fields
    )
    with np.errstate(invalid='ignore'):
        extent = (
            np.isfinite(xmin)
            & np.isfinite(ymin)
            & np.isfinite(xmax)
            & np.isfinite(ymax)
            & (xmin <= xmax)
            & (ymin <= ymax)
        )
    scale = float(1 << BOX_BITS)
    cells = []
    for v, origin in ((xmin, 180.0), (ymin, 90.0), (xmax, 180.0), (ymax, 90.0)):
        cell = np.floor((v + origin) * scale)
        cell[~extent] = np.nan
        cells.append(cell)
    return cells


def page_boxes(cells, bounds):
    """For page i holding rows `bounds[i]:bounds[i + 1]` of `cells`, the union
    of its rows' boxes as int64 `x0, y0, x1, y1`, and which pages have any row
    with an extent; the others' boxes are meaningless."""
    starts = np.asarray(bounds[:-1], np.int64)
    if not len(starts):
        empty = np.zeros(0, np.int64)
        return (empty,) * 4, np.zeros(0, bool)
    with np.errstate(invalid='ignore'):
        union = [
            np.fmin.reduceat(cells[0], starts),
            np.fmin.reduceat(cells[1], starts),
            np.fmax.reduceat(cells[2], starts),
            np.fmax.reduceat(cells[3], starts),
        ]
    shown = ~np.isnan(union[0])
    return tuple(np.where(shown, u, 0).astype(np.int64) for u in union), shown


def _within(ufunc, values, run, span):
    """`ufunc` accumulated over each stretch of equal, ascending `run` from the
    stretch's first value. Values lie in [0, span), and shifting each stretch
    past the ones before keeps one accumulation from mixing them."""
    shift = run * span if ufunc is np.maximum else -run * span
    return ufunc.accumulate(values + shift) - shift


def page_runs(cells, bounds, rounds=RUN_ROUNDS, saving=RUN_SAVING, tile=RUN_TILE):
    """Where pages split into runs of consecutive rows, each boxed on its
    own. A Hilbert curve leaving the data and re-entering it far away
    puts rows of distant places in one page, whose box then spans the empty
    space between them. Each round cuts every run once more, at the row
    minimizing its halves' cost `(width + tile) * (height + tile)`, roughly
    the tiles a box overlaps, when that cost is at most `saving` times the
    run's. Returns the cut rows, each the first row of a run after its
    page's first."""
    rows = np.flatnonzero(~np.isnan(cells[0]))
    cuts = np.zeros(0, np.int64)
    if len(rows) < 2:
        return cuts
    # rows with an extent, offset by run so that one accumulation goes
    # through every run without mixing them
    origin = np.nanmin(cells[0]), np.nanmin(cells[1])
    values = [c[rows] - origin[i % 2] for i, c in enumerate(cells)]
    span = max(float(np.max(v)) for v in values) + 1
    page = np.searchsorted(bounds, rows, side='right') - 1
    starts = np.r_[True, page[1:] != page[:-1]]
    union = (np.minimum, np.minimum, np.maximum, np.maximum)
    for _ in range(rounds):
        run = np.cumsum(starts) - 1
        last = np.r_[starts[1:], True]
        head = [_within(f, v, run, span) for f, v in zip(union, values, strict=True)]
        tail = [
            _within(f, v[::-1], run[-1] - run[::-1], span)[::-1]
            for f, v in zip(union, values, strict=True)
        ]
        left = (head[2] - head[0] + tile) * (head[3] - head[1] + tile)
        right = (tail[2] - tail[0] + tile) * (tail[3] - tail[1] + tile)
        whole = left[last][run]
        both = left[:-1] + right[1:]
        inside = np.flatnonzero(~last[:-1] & (both <= saving * whole[:-1]))
        inside = inside[np.lexsort((both[inside], run[inside]))]
        best = inside[np.r_[True, run[inside][1:] != run[inside][:-1]]] if len(inside) else inside
        if not len(best):
            break
        starts[best + 1] = True
        cuts = np.union1d(cuts, rows[best + 1])
    return cuts


def column_schema(metadata, columns):
    """The footer `metadata`'s schema cut down to the flat top-level `columns`
    as a Thrift FileMetaData without rows, base64 encoded, from which a
    reader decodes their pages without the footer."""
    elements = get(metadata, 2)[1]
    flat = flat_columns(metadata)
    root = dict(elements[0])
    set_value(root, 5, len(columns))
    schema = {
        1: metadata[1],
        2: (9, (12, [root, *(flat[name][0] for name in columns)])),
        3: (6, 0),
        4: (9, (12, [])),
    }
    return base64.b64encode(dumps(schema)).decode()


def grid_zoom(lod, name):
    """Zoom of the cells an LOD's directory uses: its finest, at most the key depth."""
    return min(lod['key']['depth'], max(lod['lods'][name]['zooms']))


def page_cells(keys, bounds, present, depth, zoom):
    """Directory values of one row group's pages. Page i holds rows
    `bounds[i]:bounds[i + 1]` of the sorted Hilbert `keys`, null keys stored as
    4**depth, and `present` marks the rows with a value. Pages without a value
    are left out. For the rest: their index among the row group's pages, first
    row and row count, valid (non-null) key rows, minimum and maximum cell at
    `zoom`, and their fences as flat arrays: the page, relative row and cell of
    each page's first row and of every later row whose cell differs from the
    row before.
    """
    keys = np.asarray(keys, np.uint64)
    filled = np.r_[0, np.cumsum(present, dtype=np.int64)]
    shown = filled[bounds[1:]] > filled[bounds[:-1]]
    firsts, ends = bounds[:-1][shown], bounds[1:][shown]
    cells = (keys >> np.uint64(2 * (depth - zoom))).astype(np.int64)
    starts = np.union1d(firsts, np.flatnonzero(cells[1:] != cells[:-1]) + 1)
    page = np.searchsorted(firsts, starts, side='right') - 1
    inside = page >= 0
    inside[inside] = starts[inside] < ends[page[inside]]
    starts, page = starts[inside], page[inside]
    valid = np.clip(np.searchsorted(keys, np.uint64(4**depth)) - firsts, 0, ends - firsts)
    return {
        'pages': np.flatnonzero(shown),
        'low': cells[firsts],
        'high': cells[firsts + np.maximum(valid, 1) - 1],
        'first': firsts,
        'count': ends - firsts,
        'valid': valid,
        'fence_page': page,
        'fence_row': starts - firsts[page],
        'fence_cell': cells[starts],
    }


def _ranges(offset, length):
    """Byte ranges as gaps after the previous range's end, the first after
    zero, and lengths."""
    offset, length = np.asarray(offset, np.int64), np.asarray(length, np.int64)
    return [(offset - np.r_[0, offset + length][: len(offset)]).tolist(), length.tolist()]


def _node(level, low, high, offset, length):
    """A directory node's leading columns, one value per entry in key order:
    minimum cell minus the previous entry's, cell span, bytes between the
    previous entry's end and this entry's start, and compressed length. The
    first entry counts from zero."""
    low, high = np.asarray(low, np.int64), np.asarray(high, np.int64)
    return [
        level,
        np.diff(low, prepend=0).tolist(),
        (high - low).tolist(),
        *_ranges(offset, length),
    ]


def _leaf(pages, start, end):
    """A level-zero node over pages `start:end`, adding per page its first
    global row minus the previous page's end, row count, fences after the
    implied [minimum cell, 0] as pairs minus the pair before, and rows with a
    null key."""
    first, count = pages['first'][start:end], pages['count'][start:end]
    a, b = np.searchsorted(pages['fence_page'], [start, end])
    page, row, cell = (pages[k][a:b] for k in FENCE_FIELDS)
    later = row > 0
    pairs = np.column_stack((np.diff(cell, prepend=0), np.diff(row, prepend=0)))[later]
    fences = [[] for _ in range(end - start)]
    for p, pair in zip((page[later] - start).tolist(), pairs.tolist(), strict=True):
        fences[p].extend(pair)
    return [
        *_node(0, *(pages[k][start:end] for k in ('low', 'high', 'offset', 'length'))),
        (first - np.r_[0, first + count][: len(first)]).tolist(),
        count.tolist(),
        fences,
        (count - pages['valid'][start:end]).tolist(),
    ]


def read_node(node):
    """A directory node's level and its entries as arrays: cell bounds, byte
    offsets and lengths, and for a leaf the page fields and fences that
    `page_cells` returns. Entries must be in key order, each starting at or
    after the previous one's maximum cell."""
    if not isinstance(node, list) or len(node) < 5:
        raise ValueError('malformed directory node')
    level = node[0]
    if type(level) is not int or not 0 <= level <= 32:
        raise ValueError('invalid directory level')
    if len(node) != (5 if level else 9):
        raise ValueError('malformed directory node')
    cells, spans, gaps, length = (_column(values) for values in node[1:5])
    if not len(cells) == len(spans) == len(gaps) == len(length) or (spans < 0).any():
        raise ValueError('malformed directory node')
    if len(cells) and (cells[0] < 0 or (cells[1:] < spans[:-1]).any()):
        raise ValueError('directory entries out of key order')
    if (gaps < 0).any() or (length <= 0).any():
        raise ValueError('directory byte ranges overlap or are empty')
    low = np.cumsum(cells)
    end = np.cumsum(gaps + length)
    entries = {'low': low, 'high': low + spans, 'offset': end - length, 'length': length}
    if level:
        return level, entries
    rows, count, nulls = (_column(node[i]) for i in (5, 6, 8))
    fences = node[7]
    if (
        not isinstance(fences, list)
        or not len(low) == len(rows) == len(count) == len(fences) == len(nulls)
        or not all(isinstance(f, list) and len(f) % 2 == 0 for f in fences)
        or (rows < 0).any()
    ):
        raise ValueError('malformed directory leaf')
    per_page = np.array([len(f) // 2 + 1 for f in fences], np.int64)
    heads = np.cumsum(per_page) - per_page
    delta = np.zeros((int(per_page.sum()), 2), np.int64)
    later = np.ones(len(delta), bool)
    later[heads] = False
    delta[later] = _column([v for f in fences for v in f]).reshape(-1, 2)
    total = np.cumsum(delta, axis=0)
    fence = total - np.repeat(total[heads], per_page, axis=0)
    page = np.repeat(np.arange(len(fences)), per_page)
    return 0, {
        **entries,
        'first': np.cumsum(rows + count) - count,
        'count': count,
        'valid': count - nulls,
        'fence_page': page,
        'fence_row': fence[:, 1],
        'fence_cell': fence[:, 0] + low[page],
    }


def _column(values):
    array = np.asarray(values)
    if not isinstance(values, list) or array.ndim != 1 or (array.size and array.dtype.kind != 'i'):
        raise ValueError('directory columns hold integers')
    return array.astype(np.int64)


def _source_columns(offset, length, first, count):
    """Byte ranges and rows of consecutive pages: each one's bytes after the
    previous one's end, compressed length, first row after the previous one's
    end row, and rows. The first counts from zero."""
    offset, length, first, count = (np.asarray(v, np.int64) for v in (offset, length, first, count))
    return [
        (offset - np.r_[0, offset + length][: len(offset)]).tolist(),
        length.tolist(),
        (first - np.r_[0, first + count][: len(first)]).tolist(),
        count.tolist(),
    ]


def _read_sources(columns):
    """The pages `_source_columns` stores, as arrays, in byte and row order."""
    gaps, length, rows, count = (_column(values) for values in columns)
    if not len(gaps) == len(length) == len(rows) == len(count):
        raise ValueError('malformed source pages')
    if (gaps < 0).any() or (length <= 0).any() or (rows < 0).any() or (count <= 0).any():
        raise ValueError('source pages overlap or are empty')
    end, last = np.cumsum(gaps + length), np.cumsum(rows + count)
    return {'offset': end - length, 'length': length, 'first': last - count, 'count': count}


def row_leaf(pages):
    """A row directory leaf over consecutive pages of one column, in row
    order: level 0, then the pages as `_source_columns` stores them."""
    return [0, *_source_columns(*(pages[k] for k in SOURCE_FIELDS))]


def read_row_node(node):
    """A row directory node's level and entries as arrays: above the leaves,
    nodes as `read_node` reads them, whose cells are rows; in a leaf, pages'
    byte offsets and lengths, first rows and rows, in row order."""
    if isinstance(node, list) and node and node[0] != 0:
        return read_node(node)
    if not isinstance(node, list) or len(node) != 5 or type(node[0]) is not int:
        raise ValueError('malformed row directory leaf')
    return 0, _read_sources(node[1:])


def _read_ranges(pair, empty=False):
    """Byte ranges `_ranges` stores, as offsets and lengths. With `empty`, a
    range may have length 0 when its gap is 0."""
    if not isinstance(pair, list) or len(pair) != 2:
        raise ValueError('malformed byte ranges')
    gaps, length = (_column(v) for v in pair)
    if len(gaps) != len(length) or (gaps < 0).any() or (length < 0).any():
        raise ValueError('byte ranges overlap or are empty')
    empties = length == 0
    if empties.any() and (not empty or (gaps[empties] != 0).any()):
        raise ValueError('byte ranges overlap or are empty')
    end = np.cumsum(gaps + length)
    return {'offset': end - length, 'length': length}


def attribute_leaf(pages):
    """An attribute leaf beside an LOD leaf: level 0, then the byte ranges of
    the attribute pages beside the leaf's pages, one each and in order. A
    length 0 marks an overview page with no value of the attribute; it takes
    no bytes, so its gap is 0 and the next gap counts from the end before."""
    offset, length = np.asarray(pages['offset'], np.int64), np.asarray(pages['length'], np.int64)
    end = np.maximum.accumulate(np.where(length > 0, offset + length, 0))
    gaps = np.where(length > 0, offset - np.r_[0, end[:-1]], 0)
    return [0, gaps.tolist(), length.tolist()]


def read_attribute_leaf(node):
    """The pages an attribute leaf beside an LOD leaf lists, as byte ranges;
    an empty one, of length 0, starts where the page before it ends."""
    if not isinstance(node, list) or len(node) != 3 or type(node[0]) is not int or node[0]:
        raise ValueError('malformed attribute leaf')
    return _read_ranges(node[1:], empty=True)


def attribute_slot(raw, size, attribute, leaf):
    """An attribute slot of `size` bytes holding the gzip attribute leaf
    `raw`: its length, the position of its attribute, the byte offset
    of the directory leaf it is beside, the leaf, then zeros."""
    return (
        struct.pack('<IIQ', len(raw), attribute, leaf) + raw + bytes(size - SLOT_HEADER - len(raw))
    )


def slot_place(group, attribute, size):
    """The byte offset of the slot of the attribute at position
    `attribute` beside a leaf, from the leaf's `group`: the end of its
    level-one node's last leaf, that node's leaves and the leaf's position
    among them."""
    end, count, index = group
    return end + (attribute * count + index) * size


def read_attribute_slot(raw, attribute, leaf):
    """The attribute leaf an attribute slot's bytes hold, decoded, for the
    slot of the attribute at position `attribute` beside the
    directory leaf at byte `leaf`."""
    length = int.from_bytes(raw[:4], 'little') if len(raw) >= SLOT_HEADER else 0
    if not 0 < length <= len(raw) - SLOT_HEADER:
        raise ValueError("an attribute slot's length overruns its slot")
    if int.from_bytes(raw[4:8], 'little') != attribute:
        raise ValueError('an attribute slot holds another attribute')
    if int.from_bytes(raw[8:SLOT_HEADER], 'little') != leaf:
        raise ValueError('an attribute slot is beside another leaf')
    if any(raw[SLOT_HEADER + length :]):
        raise ValueError("an attribute slot's padding is not zero")
    return json.loads(gzip.decompress(raw[SLOT_HEADER : SLOT_HEADER + length]))


def directory_zoom(lod):
    """Zoom of the primary directory's cells: the finest zoom, at most the key depth."""
    return min(lod['key']['depth'], max(lod['zooms']))


def leaf_starts(low, high, leaf_pages, cells):
    """The first cells of the leaves of a primary directory over pages whose
    keyed rows lie in cells `low[i]..high[i]` of the `cells` cells, in key
    order. A leaf takes about `leaf_pages` pages: it ends before the coarsest
    aligned cell among those of its last half, and a page holding that cell is
    in both leaves."""
    starts, s = [0], 0
    while len(low) - s > leaf_pages:
        a, b = max(int(low[s + leaf_pages // 2]), starts[-1]), int(high[s + leaf_pages - 1])
        level = ((a ^ b).bit_length() - 1) // 2
        cut = (b >> 2 * level) << 2 * level if a < b else a + 1
        if cut >= cells:
            break
        starts.append(cut)
        s = max(s + 1, int(np.searchsorted(high, cut)))
    return np.array(starts, np.int64)


def coarsest_zoom(starts, zoom):
    """The coarsest zoom whose tiles each lie in one leaf, for leaves starting
    at cells `starts` of `zoom`."""
    levels = [((c & -c).bit_length() - 1) // 2 for c in map(int, starts) if c]
    return zoom - min([zoom, *levels])


def tile_edges(zoom):
    """The cells of the edges of the tiles at `zoom`: tile column t spans the
    cell columns `columns[t]..columns[t + 1]`, and tile row t, counted from
    the north, the cell rows `rows[t + 1]..rows[t]`, the outer rows reaching
    latitudes 90 and -90. Neighbouring tiles share the cells of their edge."""
    t = np.arange(2**zoom + 1, dtype=np.int64)
    columns = (t * (360 << BOX_BITS)) >> zoom
    latitude = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * t / 2**zoom))))
    rows = np.floor((latitude + 90) * (1 << BOX_BITS)).astype(np.int64)
    rows[0], rows[-1] = 180 << BOX_BITS, 0
    return columns, rows


def reached_tiles(cells, zoom, edges=None):
    """The tiles at `zoom` whose cells the boxes `cells` (int64 `x0, y0, x1,
    y1`) reach, as tile columns `tx0..tx1` and rows `ty0..ty1`. `edges` are
    `tile_edges(zoom)`."""
    columns, rows = tile_edges(zoom) if edges is None else edges
    x0, y0, x1, y1 = cells
    reach = (
        np.searchsorted(columns[1:], x0),
        np.searchsorted(-rows[1:], -y1),
        np.searchsorted(columns[:-1], x1, 'right') - 1,
        np.searchsorted(-rows[:-1], -y0, 'right') - 1,
    )
    return tuple(np.clip(v, 0, 2**zoom - 1) for v in reach)


def _rect_leaves(x0, y0, x1, y1, zoom, starts):
    """The leaves holding the cells of tiles `x0..x1`, `y0..y1` at `zoom`, for
    leaves starting at cells `starts` of `zoom`."""
    found, stack = set(), [(0, 0, 0)]
    while stack:
        z, x, y = stack.pop()
        shift = zoom - z
        if x << shift > x1 or (x + 1) << shift <= x0 or y << shift > y1 or (y + 1) << shift <= y0:
            continue
        h = int(hilbert_key(np.array([x]), np.array([y]), z)[0])
        ends = [h << 2 * shift, ((h + 1) << 2 * shift) - 1]
        first, last = (int(v) for v in np.searchsorted(starts, ends, 'right') - 1)
        inside = (
            x << shift >= x0
            and (x + 1) << shift <= x1 + 1
            and y << shift >= y0
            and (y + 1) << shift <= y1 + 1
        )
        if first == last or inside:
            found.update(range(first, last + 1))
        else:
            stack += [(z + 1, 2 * x + dx, 2 * y + dy) for dx in (0, 1) for dy in (0, 1)]
    return np.array(sorted(found), np.int64)


def reached_leaves(cells, zoom, starts, edges=None):
    """The leaves the boxes `cells` (int64 `x0, y0, x1, y1`) reach, as pairs
    of arrays: box index and leaf, each pair once. Leaf i holds the cells of
    `zoom` from `starts[i]` up to the next start, and every tile of `zoom`
    lies in one leaf. `edges` are `tile_edges(zoom)`."""
    tx0, ty0, tx1, ty1 = reached_tiles(cells, zoom, edges)
    near = (tx1 - tx0 <= 1) & (ty1 - ty0 <= 1)
    boxes, leaves = [], []
    for dx in (0, 1):
        for dy in (0, 1):
            at = np.flatnonzero(near & (tx0 + dx <= tx1) & (ty0 + dy <= ty1))
            boxes.append(at)
            key = hilbert_key(tx0[at] + dx, ty0[at] + dy, zoom)
            leaves.append(np.searchsorted(starts, key, 'right') - 1)
    for i in np.flatnonzero(~near):
        found = _rect_leaves(tx0[i], ty0[i], tx1[i], ty1[i], zoom, starts)
        boxes.append(np.full(len(found), i))
        leaves.append(found)
    pair = np.unique(np.concatenate(boxes) * len(starts) + np.concatenate(leaves))
    return pair // len(starts), pair % len(starts)


def pages_over(pages, first, end):
    """Indexes of `pages`, in row order, overlapping any rows `first[i]` up to
    `end[i]`."""
    a = np.searchsorted(pages['first'] + pages['count'], first, 'right')
    b = np.searchsorted(pages['first'], end)
    marks = np.zeros(len(pages['first']) + 1, np.int64)
    np.add.at(marks, a, 1)
    np.add.at(marks, b, -1)
    return np.flatnonzero(np.cumsum(marks)[:-1] > 0)


def primary_leaf(entries):
    """A primary directory leaf over `entries`, runs of rows each within one
    geometry page in row order: their boxes as `_box_columns` stores them,
    their pages' byte ranges, each page's after the previous entry's page's
    end and of length 0 when it is that page, then their first rows after the
    previous entry's end row and their rows."""
    offset, length, first, count = (np.asarray(entries[k], np.int64) for k in SOURCE_FIELDS)
    again = np.r_[False, offset[1:] == offset[:-1]]
    previous = np.r_[0, offset + length][: len(offset)]
    return [
        0,
        *_box_columns(*(entries[k] for k in BOX_FIELDS)),
        np.where(again, 0, offset - previous).tolist(),
        np.where(again, 0, length).tolist(),
        (first - np.r_[0, first + count][: len(first)]).tolist(),
        count.tolist(),
    ]


def _box_columns(x0, y0, x1, y1):
    """Boxes in cells, one per entry: minimum column and row minus the
    previous entry's, then width and height. The first counts from zero."""
    x0, y0, x1, y1 = (np.asarray(v, np.int64) for v in (x0, y0, x1, y1))
    return [
        np.diff(x0, prepend=0).tolist(),
        np.diff(y0, prepend=0).tolist(),
        (x1 - x0).tolist(),
        (y1 - y0).tolist(),
    ]


def read_primary_node(node):
    """A primary directory node's level and entries as arrays: above the
    leaves, nodes as `read_node` reads them; in a leaf, runs of rows with
    boxes `x0, y0, x1, y1` in cells, their pages' byte offsets and lengths,
    first rows and rows. Entries are in row order."""
    if isinstance(node, list) and node and node[0] != 0:
        return read_node(node)
    if not isinstance(node, list) or len(node) != 9 or type(node[0]) is not int:
        raise ValueError('malformed primary directory leaf')
    west, south, width, height, gaps, length, rows, count = (_column(v) for v in node[1:9])
    if len({len(v) for v in (west, south, width, height, gaps, length, rows, count)}) != 1:
        raise ValueError('malformed primary directory leaf')
    if (width < 0).any() or (height < 0).any():
        raise ValueError('primary directory box with a negative extent')
    again = length == 0
    if (
        (gaps < 0).any()
        or (length < 0).any()
        or (rows < 0).any()
        or (count <= 0).any()
        or (again & ((gaps != 0) | (rows != 0))).any()
        or (len(again) and again[0])
    ):
        raise ValueError('primary directory byte or row ranges overlap or are empty')
    page = np.cumsum(~again) - 1
    end = np.cumsum((gaps + length)[~again])
    x0, y0 = np.cumsum(west), np.cumsum(south)
    entries = {
        'x0': x0,
        'y0': y0,
        'x1': x0 + width,
        'y1': y0 + height,
        'offset': (end - length[~again])[page],
        'length': length[~again][page],
        'first': np.cumsum(rows + count) - count,
        'count': count,
    }
    return 0, entries


def primary_leaves(parquet, covering, pages, keys, depth, zoom, leaf_pages):
    """The primary directory's leaves: the first cell of each among the cells
    of `zoom`, a table of runs of geometry rows in row order, and the runs
    each leaf lists. `covering` names the covering's column and fields,
    `pages` holds each row group's geometry pages as `_index_pages` returns
    them, and `keys` every row's key at `depth`, a null key as 4**depth. A
    page whose rows the curve took from distant places is cut into runs,
    and a leaf lists every run of each page holding a row whose box reaches
    its cells."""
    column, fields = covering
    table = {k: np.concatenate([part[k] for part in pages]) for k in SOURCE_FIELDS}
    keyed = int(np.searchsorted(keys, np.uint64(4**depth)))
    first = table['first'][table['first'] < keyed]
    last = np.minimum(first + table['count'][: len(first)], keyed) - 1
    shift = np.uint64(2 * (depth - zoom))
    low, high = ((keys[rows] >> shift).astype(np.int64) for rows in (first, last))
    starts = leaf_starts(low, high, leaf_pages, 4**zoom)
    # tiles of the coarsest zoom at which each lies in one leaf
    reach = coarsest_zoom(starts, zoom)
    coarse = starts >> 2 * (zoom - reach)
    edges = tile_edges(reach) if len(starts) > 1 else None
    runs, pairs, numbered = [], [], 0
    for g, part in enumerate(pages):
        bbox = parquet.read_row_group(g, columns=[column]).column(0).combine_chunks()
        cells = covering_cells(bbox, fields)
        start = int(part['first'][0])
        bounds = np.r_[part['first'] - start, len(bbox)]
        cuts = np.union1d(bounds, page_runs(cells, bounds))
        box, filled = page_boxes(cells, cuts)
        page = np.searchsorted(bounds, cuts[:-1], 'right') - 1
        run = {
            **dict(zip(BOX_FIELDS, box, strict=True)),
            'offset': part['offset'][page],
            'length': part['length'][page],
            'first': cuts[:-1] + start,
            'count': np.diff(cuts),
            'page': page + numbered,
        }
        runs.append({k: v[filled] for k, v in run.items()})
        rows = np.flatnonzero(~np.isnan(cells[0]))
        page = np.searchsorted(bounds, rows, 'right') - 1 + numbered
        if len(starts) > 1:
            row, leaf = reached_leaves(
                [c[rows].astype(np.int64) for c in cells], reach, coarse, edges
            )
            pairs.append(np.unique(page[row] * len(starts) + leaf))
        else:
            pairs.append(np.unique(page))
        numbered += len(bounds) - 1
    table = {k: np.concatenate([r[k] for r in runs]) for k in runs[0]}
    pair = np.concatenate(pairs)
    leaf, page = pair % len(starts), pair // len(starts)
    order = np.lexsort((page, leaf))
    leaf, page = leaf[order], page[order]
    bounds = np.searchsorted(leaf, np.arange(len(starts) + 1))
    # each listed page's runs, which are consecutive in the table
    a, b = np.searchsorted(table['page'], page), np.searchsorted(table['page'], page, 'right')
    members = [ranges(a[i:j], b[i:j]) for i, j in zip(bounds[:-1], bounds[1:], strict=True)]
    del table['page']
    return starts, table, members


def _index_pages(index, rows, start):
    """The pages a chunk's offset index lists, for a chunk of `rows` rows whose
    first global row is `start`, and the chunk's page bounds: page i holds its
    rows `bounds[i]:bounds[i + 1]`."""
    locations = get(index, 1)[1]
    bounds = np.array([get(p, 3) for p in locations] + [rows], np.int64)
    if not len(locations) or bounds[0] != 0 or (np.diff(bounds) <= 0).any():
        raise ValueError('an indexed column has an invalid offset index')
    part = {
        'offset': np.array([get(p, 1) for p in locations], np.int64),
        'length': np.array([get(p, 2) for p in locations], np.int64),
        'first': bounds[:-1] + start,
        'count': np.diff(bounds),
    }
    return part, bounds


def _branches(target, level, entries):
    """Write nodes of up to BRANCH_ENTRIES of a tree's top `entries` at
    `level`, and return the next level's entries."""
    parents = {k: [] for k in entries}
    for first in range(0, len(entries['low']), BRANCH_ENTRIES):
        batch = {k: v[first : first + BRANCH_ENTRIES] for k, v in entries.items()}
        raw = _json(_node(level, **batch))
        parents['low'].append(batch['low'][0])
        parents['high'].append(batch['high'][-1])
        parents['offset'].append(target.tell())
        parents['length'].append(len(raw))
        target.write(raw)
    return level + 1, parents


def _copy(src, dst, start, size):
    src.seek(start)
    while size:
        data = src.read(min(size, 8 * 1024 * 1024))
        if not data:
            raise ValueError('truncated column chunk')
        dst.write(data)
        size -= len(data)


def _subtree(elements, i):
    """Schema elements and leaves in the subtree at flat schema index `i`."""
    size, leaves = 1, 0 if 5 in elements[i] else 1
    for _ in range(get(elements[i], 5, 0)):
        child_size, child_leaves = _subtree(elements, i + size)
        size, leaves = size + child_size, leaves + child_leaves
    return size, leaves


def flat_columns(metadata):
    """The flat (non-nested, non-repeated) top-level columns of the footer
    `metadata`, in schema order: name to schema element and column chunk
    position."""
    elements = get(metadata, 2)[1]
    flat, i, leaf = {}, 1, 0
    while i < len(elements):
        size, count = _subtree(elements, i)
        element = elements[i]
        if 5 not in element and get(element, 3) != 2:
            flat[get(element, 4).decode()] = (element, leaf)
        i, leaf = i + size, leaf + count
    return flat


def _footer(encoded):
    """The Thrift footer of the open Parquet file `encoded`."""
    encoded.seek(-8, 2)
    size = int.from_bytes(encoded.read(4), 'little')
    encoded.seek(-8 - size, 2)
    return loads(encoded.read(size))


def _page_bounds(path):
    """Row bounds of the pages of the one-column Parquet file `path`, across
    its row groups: page i holds rows `bounds[i]:bounds[i + 1]`."""
    with open(path, 'rb') as encoded:
        metadata = _footer(encoded)
        bounds, row = [], 0
        for group in get(metadata, 4)[1]:
            chunk = get(group, 1)[1][0]
            encoded.seek(get(chunk, 4))
            index = loads(encoded.read(get(chunk, 5)))
            bounds.extend(get(page, 3) + row for page in get(index, 1)[1])
            row += get(group, 3)
    return np.array([*bounds, row], np.int64)


def _leaf_type(parquet, leaf):
    """The Arrow type of the dotted leaf path `leaf`."""
    parts = leaf.split('.', 2)
    field = parquet.schema_arrow.field(parts[0])
    for part in parts[1:]:
        field = field.type.field(part)
    return field.type


def attribute_cuts(present, bounds):
    """Which of the overview pages bounded by `bounds` hold a value of an
    attribute present on the rows `present`, and the attribute's page
    bounds: those of the overview pages, but one page for each maximal run of
    consecutive overview pages without a value."""
    held = np.r_[0, np.cumsum(present, dtype=np.int64)]
    valued = held[bounds[1:]] > held[bounds[:-1]]
    inner = valued[:-1] | valued[1:]
    return valued, bounds[np.r_[True, inner, True]]


def _nested(values, schema, leaf):
    """A table of `schema`, whose one column nests the dotted path `leaf`,
    holding `values` there."""
    fields = [schema.field(leaf.split('.', 2)[0])]
    for part in leaf.split('.', 2)[1:]:
        fields.append(fields[-1].type.field(part))
    for field in reversed(fields[1:]):
        values = pa.StructArray.from_arrays([values], fields=[field])
    return pa.Table.from_arrays([values], schema=schema)


_bounds = {}


def _source_bounds(path=None):
    """The page bounds of the file at `path`, read once by each process and
    held open until it asks for another file's, or for none."""
    if path not in _bounds:
        for held in _bounds.values():
            held.close()
        _bounds.clear()
        if path is not None:
            _bounds[path] = PageBounds(path)
    return _bounds.get(path)


def _splits(value_type):
    """Whether an attribute column of `value_type` is tried BYTE_STREAM_SPLIT:
    FLOAT and DOUBLE, the types DuckDB 1.5 reads in that encoding."""
    return pa.types.is_float32(value_type) or pa.types.is_float64(value_type)


def _attribute_pages(parquet, group, leaf, bounds, path, compression_level, held, leading):
    """Encode the attribute column `leaf` of row group `group` into the
    Parquet file `path` with a page per span of `bounds`, its LOD's page
    bounds, so a tile's attribute pages are cut where its overview pages are,
    except that each run of those spans without a value is one page of
    nulls. The values lead their pages: the i-th of a span's rows that the
    LOD shows has its value on the span's i-th row, so a page states which
    rows hold a value in a few bytes however few of its rows are shown.
    `held` is the row of `parquet` holding the value of each row the LOD
    shows, in their order, and `leading` the row holding it here. No
    dictionary, so each page decodes alone. A FLOAT or DOUBLE column is
    written PLAIN and BYTE_STREAM_SPLIT and keeps the smaller: split bytes
    compress better for continuous values, PLAIN for values that repeat
    whole. Every other column is PLAIN. Returns `path` and which spans hold
    a value."""
    table = parquet.read_row_group(group, columns=[leaf])
    value_type = _leaf_type(parquet, leaf)
    split = _splits(value_type)
    values = table.column(0).combine_chunks()
    for part in leaf.split('.', 2)[1:]:
        values = values.field(part)
    valid = values.is_valid().to_numpy(zero_copy_only=False)
    if valid.sum() != valid[held].sum():
        raise ValueError(f'{leaf} holds a value for a row its LOD does not show')
    values = moved(values, held, leading)
    table = _nested(values, table.schema, leaf)
    present = values.is_valid().to_numpy(zero_copy_only=False)
    valued, bounds = attribute_cuts(present, bounds)
    longest = max(int(np.diff(bounds).max()) if len(bounds) > 1 else 1, 1)
    written = []
    for encoding in ('PLAIN', 'BYTE_STREAM_SPLIT') if split else ('PLAIN',):
        out = path.with_suffix(f'.{encoding}.parquet')
        with pq.ParquetWriter(
            out,
            table.schema,
            compression='zstd',
            compression_level=compression_level,
            use_dictionary=False,
            column_encoding={leaf: encoding},
            data_page_version='1.0',
            write_statistics=False,
            write_page_index=True,
            data_page_size=1 << 30,
            max_rows_per_page=longest,
            write_batch_size=longest,
        ) as writer:
            for first, end in zip(bounds[:-1], bounds[1:], strict=True):
                writer.write_table(
                    table.slice(int(first), int(end - first)), row_group_size=int(end - first)
                )
        written.append(out)
    smallest = min(written, key=lambda out: out.stat().st_size)
    for out in written:
        if out != smallest:
            out.unlink()
    smallest.rename(path)
    if not np.array_equal(_page_bounds(path), bounds):
        raise ValueError(f"{leaf} pages are not cut at its LOD's page bounds")
    return path, valued


def _tile_pages(
    parquet,
    group,
    column,
    name,
    key_file,
    start,
    shift,
    path,
    page_bytes,
    level,
    attributes=(),
    led=None,
):
    """Use Arrow to encode tile-aligned pages of row group `group` of the LOD
    `name` of the struct `column` into the Parquet file `path`, one temporary
    column chunk per run of tiles, for `_append_page_chunks` to concatenate.
    The row group's keys are those from entry `start` of the uint64 file
    `key_file`, and a key without its low `shift` bits is its tile. Pages are
    v1 data pages of PLAIN values without a dictionary, as the profile
    requires, so every page is independently decodable. Consecutive all-null
    tiles share a run; they hold no overview payload. The LOD's attribute
    columns, the dotted leaf paths `attributes`, are encoded beside it with
    the same page bounds but for runs of pages without a value, their values
    leading each page; `led` is the path of `parquet` when its attribute
    columns lead their pages already. Returns `path`, which rows have a
    value, and each attribute's file and which overview pages hold a value
    of it.
    """
    table = parquet.read_row_group(group, columns=[f'{column}.{name}'])
    array = table.column(0).combine_chunks().field(name)
    present = array.is_valid().to_numpy(zero_copy_only=False)
    # Read into memory: mapping a file from Python makes macOS flush the
    # drive (F_FULLFSYNC), milliseconds for every job.
    keys = np.fromfile(key_file, np.uint64, len(present), offset=8 * start)
    cells = keys >> np.uint64(shift)
    boundaries = np.r_[0, np.flatnonzero(cells[1:] != cells[:-1]) + 1, len(keys)]
    # A sparse overview may contain only a handful of values per 2048 source
    # rows. Give its compression pages a useful value budget while retaining
    # tile boundaries, the byte target, and a bounded decoded row span.
    page_rows = min(262144, max(2048, int(2048 * len(present) / max(1, present.sum()))))
    with pq.ParquetWriter(
        path,
        table.schema,
        compression='zstd',
        compression_level=level,
        use_dictionary=False,
        data_page_version='1.0',
        write_statistics=False,
        write_page_index=True,
        data_page_size=page_bytes,
        max_rows_per_page=page_rows,
        write_batch_size=128,
    ) as writer:
        null_start = None
        for first, end in zip(boundaries[:-1], boundaries[1:], strict=True):
            first, end = int(first), int(end)
            if not present[first:end].any():
                if null_start is None:
                    null_start = first
                continue
            if null_start is not None:
                writer.write_table(
                    table.slice(null_start, first - null_start), row_group_size=first - null_start
                )
                null_start = None
            writer.write_table(table.slice(first, end - first), row_group_size=end - first)
        if null_start is not None:
            writer.write_table(table.slice(null_start), row_group_size=len(keys) - null_start)
    paths = {}
    if attributes:
        bounds = _page_bounds(path)
        lods = json.loads(parquet.schema_arrow.metadata[b'geo'])['lod_columns']['lods']
        shown = shown_rows(array, lods[name]['geometry_type'])
        held = np.flatnonzero(shown)
        if led:
            held = leading_rows(shown, _source_bounds(led)(group, f'{column}.{name}'))
        leading = leading_rows(shown, bounds)
        for leaf in attributes:
            paths[leaf] = _attribute_pages(
                parquet,
                group,
                leaf,
                bounds,
                path.with_suffix(f'.{leaf}.parquet'),
                level,
                held,
                leading,
            )
    return path, present, paths


_source = None


def _open_source(path):
    """A worker process's initializer: open the file its jobs read."""
    global _source
    _source = pq.ParquetFile(path)


def _run(parquet, job):
    """One layout job: `_tile_pages` of `parquet` with the arguments `job`."""
    return _tile_pages(parquet, *job)


def _run_job(job):
    return _run(_source, job)


def _encoded_pages(parquet, src, jobs, workers, held):
    """An iterator of `_run(parquet, job)` for each of `jobs`, in order. With
    `workers` under 2 each job runs here as its result is taken. Otherwise
    `workers` processes run them from this call on, each opening `src`, the
    file `parquet` reads, and `held` shuts them down. At most `2 * workers`
    jobs are submitted and not yet taken, which keeps the workers busy and
    bounds the files waiting to be taken."""
    if workers < 2:
        return (_run(parquet, job) for job in jobs)
    pool = ProcessPoolExecutor(max_workers=workers, initializer=_open_source, initargs=(src,))
    held.callback(pool.shutdown, cancel_futures=True)
    waiting = iter(jobs)
    ahead = collections.deque(
        pool.submit(_run_job, job) for job in itertools.islice(waiting, 2 * workers)
    )

    def results():
        while ahead:
            result = ahead.popleft().result()
            ahead.extend(pool.submit(_run_job, job) for job in itertools.islice(waiting, 1))
            yield result

    return results()


def _append_page_chunks(path, target, statistics=None):
    """Append the column chunks of the one-column Parquet file `path` to
    `target` as one chunk and delete the file. Returns the chunk's metadata,
    its offset index, and, when the file's chunks have column indexes, its
    column index joined from theirs, with no claim on the order of its
    pages' bounds. `statistics` become the chunk's: those of the values the
    pages hold, which joining the file's chunks cannot give."""
    pages, row, combined = [], 0, None
    # the column index's null pages, minimums, maximums and null counts
    bounds = {1: None, 2: None, 3: None, 5: None}
    indexed = True
    with path.open('rb') as encoded:
        metadata = _footer(encoded)
        for group in get(metadata, 4)[1]:
            chunk = get(group, 1)[1][0]
            cm = get(chunk, 3)
            old_start, new_start = get(cm, 9), target.tell()
            _copy(encoded, target, old_start, get(cm, 7))
            encoded.seek(get(chunk, 4))
            index = loads(encoded.read(get(chunk, 5)))
            for page in get(index, 1)[1]:
                set_value(page, 1, get(page, 1) + new_start - old_start)
                set_value(page, 3, get(page, 3) + row)
                pages.append(page)
            column_index = {}
            if 6 in chunk:
                encoded.seek(get(chunk, 6))
                column_index = loads(encoded.read(get(chunk, 7)))
            if all(k in column_index for k in bounds):
                for k in bounds:
                    child, values = get(column_index, k)
                    bounds[k] = (child, (bounds[k] or (child, []))[1] + values)
            else:
                indexed = False
            if combined is None:
                combined = cm
                set_value(combined, 9, new_start)
                for optional in (12, 13, 16):
                    combined.pop(optional, None)
            else:
                for field in (5, 6, 7):
                    set_value(combined, field, get(combined, field) + get(cm, field))
            row += get(group, 3)
    path.unlink()
    if statistics is not None:
        combined[12] = statistics
        combined = dict(sorted(combined.items()))
    joined = None
    if indexed and pages and all(bounds.values()):
        joined = {1: (9, bounds[1]), 2: (9, bounds[2]), 3: (9, bounds[3]), 4: (5, 0)}
        joined[5] = (9, bounds[5])
    return combined, {1: (9, (12, pages))}, joined


def _without_attributes(root):
    """`root` as the same file without attributes would have it: no
    attribute in its LOD metadata, and no attribute codec or attribute slot
    sizes."""
    lean = {k: v for k, v in root.items() if k not in ('attribute_codec', 'attribute_slots')}
    lod = {k: v for k, v in root['lod'].items() if k != 'attribute_columns'}
    lod['attributes'] = []
    lean['lod'] = lod
    return lean


def _without_summaries(root):
    """`root` with no attribute's summary in its LOD metadata."""
    attributes = [{k: v for k, v in a.items() if k != 'summary'} for a in root['lod']['attributes']]
    return {**root, 'lod': {**root['lod'], 'attributes': attributes}}


def fit_root(target, root, trees, names, indexed, branched=()):
    """Put the top nodes of `trees` in `root`: each LOD's under `directories`
    in the order of `names`, the primary directory's, under None, and the row
    directories', under `('column', name)` for each of `indexed`. A tree is
    `(level, entries)`, the level of its top node and that node's entries.
    While the root would exceed _ROOT_TARGET_BYTES, a top node's entries go
    under a new level of branches written to `target`, fetched lazily and
    then shared by a pan; the trees are updated in place. The row directories
    branch first, largest top node first, since no overview tile reads them,
    and the LOD and primary trees only once no row directory can. The trees
    of `branched` take a level of branches before any other.

    The trees first fit the root without attributes, so they branch as in
    the same file without them, and then with the attributes' metadata apart
    from their summaries. The root's copy of the metadata then keeps the
    summaries that pack to _SUMMARY_BYTES, the largest left out first, and
    only a row directory branches to hold them: where none can, the largest
    summary is left out, so the LOD and primary trees are as without any."""

    def place():
        tops = {name: _node(level, **shards) for name, (level, shards) in trees.items()}
        root['directories'] = {name: tops[name] for name in names}
        root['primary_index']['directory'] = tops[None]
        root['column_index']['directories'] = [tops[('column', n)] for n in indexed]
        return tops

    def branch(tops, among):
        shrinkable = [name for name in among if len(trees[name][1]['low']) > 1]
        if not shrinkable:
            return False
        largest = max(shrinkable, key=lambda name: len(_json(tops[name])))
        trees[largest] = _branches(target, *trees[largest])
        return True

    for name in trees:
        if name in branched:
            trees[name] = _branches(target, *trees[name])
    tops = place()
    rows = [('column', n) for n in indexed]
    for fitted in (_without_attributes, _without_summaries):
        while len(_json(fitted(root))) > _ROOT_TARGET_BYTES:
            if not branch(tops, rows) and not branch(tops, list(trees)):
                raise ValueError('LOD metadata exceeds the 16 KiB bootstrap')
            tops = place()
    attributes = [dict(a) for a in root['lod']['attributes']]
    root['lod'] = {**root['lod'], 'attributes': attributes}

    def leave_out():
        largest = max(
            (a for a in attributes if 'summary' in a), key=lambda a: len(_json(a['summary']))
        )
        del largest['summary']

    while len(_json(root)) - len(_json(_without_summaries(root))) > _SUMMARY_BYTES:
        leave_out()
    while len(_json(root)) > _ROOT_TARGET_BYTES:
        if branch(tops, rows):
            tops = place()
        else:
            leave_out()


class _Tally:
    """A write target that only counts the bytes written to it."""

    def __init__(self, at):
        self.at = at

    def tell(self):
        return self.at

    def write(self, raw):
        self.at += len(raw)


def _write_leaves(target, leaves, beside, size, grouped):
    """Write the gzip `leaves` of a tree to `target`, each level-one node's
    back to back, BRANCH_ENTRIES of them when `grouped` and otherwise all,
    each group followed by its slot block: per attribute, the slot of
    `size` bytes holding the gzip attribute leaf in each leaf's entry of the
    list `beside`, in leaf order. Returns the leaves' offsets and lengths."""
    offset, length = [], []
    step = BRANCH_ENTRIES if grouped else max(len(leaves), 1)
    for first in range(0, len(leaves), step):
        group = range(first, min(first + step, len(leaves)))
        for i in group:
            offset.append(target.tell())
            length.append(len(leaves[i]))
            target.write(leaves[i])
        attributes = len(beside[first])
        if isinstance(target, _Tally):
            target.at += attributes * len(group) * size
            continue
        for a in range(attributes):
            for i in group:
                target.write(attribute_slot(beside[i][a], size, a, offset[i]))
    return offset, length


def _grouping(simulate):
    """The slotted trees whose leaves to group by BRANCH_ENTRIES, and those of
    them `fit_root` branches first. `simulate(grouped, branched)` lays the
    directories out with the trees of `grouped` grouped and returns the
    slotted trees `fit_root` then branches, each grouped exactly when
    branched. A grouping that changes which trees branch is tried again;
    one tried before, or any while trees branch first, adds the trees to
    branch first."""
    grouped, forced, tried = frozenset(), frozenset(), set()
    while True:
        branched = simulate(grouped, forced)
        if branched == grouped:
            return grouped, forced
        tried.add(grouped)
        if forced or branched in tried:
            forced = grouped | branched
            grouped = forced
        else:
            grouped = branched


def finalize_layout(
    src,
    out,
    *,
    leaf_pages=1024,
    page_bytes=65536,
    compression_level=9,
    workers: int | None = None,
    log=print,
):
    """Write a separate optimized copy, atomically. `workers` processes encode
    the LOD pages (None: one per CPU, at most DEFAULT_WORKERS), which are
    appended in file order, so the output is the same for any `workers`. This
    process encodes them itself when `workers` is 1 or the file holds under
    PARALLEL_LOD_BYTES of LOD values. Working memory is bounded by one row
    group of one LOD column in each encoding process plus the file's page
    directory and indexes; keys are staged in a disk mmap. Encrypted/external
    chunks and bloom filters are unsupported, and so are attributes without
    attribute columns at every LOD.
    """
    src, out = Path(src), Path(out)
    if src.resolve() == out.resolve():
        raise ValueError('layout finalization requires a separate output path')
    if leaf_pages < 1:
        raise ValueError('leaf_pages must be positive')
    if page_bytes < 1:
        raise ValueError('page_bytes must be positive')
    workers = workers or min(os.cpu_count() or 1, DEFAULT_WORKERS)
    if workers < 1:
        raise ValueError('workers must be positive')
    parquet = pq.ParquetFile(src)
    check_index_version(parquet)
    geo = json.loads(parquet.schema_arrow.metadata[b'geo'])
    lod = geo['lod_columns']
    check_format(lod)
    check_zooms(lod)
    key_column = lod['key']['column']
    if lod['key']['depth'] > INDEX_KEY_DEPTH:
        raise ValueError('Hilbert depth must fit exact JavaScript integers (0..26)')
    primary = geo['primary_column']
    root = {'version': INDEX_VERSION, 'lod': lod, 'primary': primary, 'codecs': {}}
    flat = [f.name for f in parquet.schema_arrow if not pa.types.is_nested(f.type)]
    root['columns'] = [name for name in flat if name != primary]
    bbox_column, bbox_fields = covering(geo)
    bounds = [180.0, 90.0, -180.0, -90.0]
    for g in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(g)
        columns = {
            group.column(c).path_in_schema: group.column(c) for c in range(group.num_columns)
        }
        for i, leaf in enumerate(f'{bbox_column}.{field}' for field in bbox_fields):
            stats = columns[leaf].statistics
            if stats and stats.has_min_max:
                value = stats.min if i < 2 else stats.max
                if np.isfinite(value):
                    bounds[i] = min(bounds[i], value) if i < 2 else max(bounds[i], value)
    root['bounds'] = bounds if bounds[0] <= bounds[2] else [-180, -85.051129, 180, 85.051129]
    names = list(lod['lods'])
    column_names = [a['name'] for a in lod['attributes']]
    attribute_column = (lod.get('attribute_columns') or {}).get('column')
    column_lods = list((lod.get('attribute_columns') or {}).get('lods', []))
    if column_names and column_lods != names:
        raise ValueError('the spatial index needs every LOD to hold the attributes')
    with src.open('rb') as original:
        if original.read(4) != b'PAR1':
            raise ValueError('not an unencrypted Parquet file')
        original.seek(-8, 2)
        tail = original.read(8)
        if tail[4:] != b'PAR1':
            raise ValueError('not an unencrypted Parquet file')
        original.seek(-8 - int.from_bytes(tail[:4], 'little'), 2)
        metadata = loads(original.read(int.from_bytes(tail[:4], 'little')))
        if 8 in metadata or 9 in metadata:
            raise ValueError('encrypted Parquet is unsupported')
        groups = get(metadata, 4)[1]
        # Reject layouts the fast decoder cannot faithfully reconstruct.
        lod_type = parquet.schema_arrow.field(lod['column'])
        if lod_type.nullable:
            raise ValueError('LOD parent must be required')
        for name in names:
            field = lod_type.type.field(name)
            if not pa.types.is_binary(field.type) or not field.nullable:
                raise ValueError('LOD children must be nullable binary')
        for group in groups:
            for chunk in get(group, 1)[1]:
                if 1 in chunk or 8 in chunk or 9 in chunk:
                    raise ValueError('external or encrypted column chunks are unsupported')
                cm = get(chunk, 3)
                if 14 in cm or 10 in cm:
                    raise ValueError('bloom filters and legacy index pages are unsupported')
        # Row directories locate the pages of the flat columns that decode on
        # their own; the primary directory, those of the primary geometry; and
        # the attribute slots beside the directory leaves those of the attributes.
        flat_chunks = flat_columns(metadata)
        if primary not in flat_chunks or get(flat_chunks[primary][0], 1) != 6:
            raise ValueError('the primary geometry must be a flat binary column')

        def standalone(name):
            leaf = flat_chunks[name][1]
            return all(
                4 in chunk and 11 not in get(chunk, 3)
                for chunk in (get(group, 1)[1][leaf] for group in groups)
            )

        def codecs(name):
            leaf = flat_chunks[name][1]
            return {get(get(get(group, 1)[1][leaf], 3), 4) for group in groups}

        if not standalone(primary):
            raise ValueError('the primary geometry needs offset indexes and no dictionary')
        if len(codecs(primary)) != 1:
            raise ValueError('the primary geometry codec must agree across row groups')
        indexed = [name for name in flat_chunks if standalone(name) and len(codecs(name)) == 1]
        tracked = {flat_chunks[name][1]: name for name in indexed}
        root['column_index'] = {
            'columns': indexed,
            'codecs': [CODECS[next(iter(codecs(name)))] for name in indexed],
            'schema': column_schema(metadata, indexed),
        }
        root['primary_index'] = {}
        if any(name not in indexed for name in column_names):
            raise ValueError('an attribute needs a flat source column of its name')
        with tempfile.TemporaryDirectory(prefix='.lod-layout-', dir=out.parent) as work:
            work = Path(work)
            total_rows = parquet.metadata.num_rows
            if not total_rows:
                raise ValueError('cannot index an empty file')
            key_file = work / 'keys'
            keys = np.memmap(key_file, mode='w+', dtype=np.uint64, shape=(total_rows,))
            starts, start = [], 0
            last = None
            for g, _group in enumerate(groups):
                values = parquet.read_row_group(g, columns=[key_column]).column(0)
                # Null/empty source geometries remain analytical rows at the end
                # of the file, but are absent from every overview.
                values = values.fill_null(4 ** lod['key']['depth']).to_numpy()
                if np.any(values[1:] < values[:-1]) or (last is not None and values[0] < last):
                    raise ValueError('keys are not sorted')
                last = values[-1]
                starts.append(start)
                keys[start : start + len(values)] = values
                start += len(values)
            keys.flush()
            # Keep all original chunks, sorted physically by column. Their logical
            # row-group/column positions in the standard footer remain unchanged.
            columns = len(get(groups[0], 1)[1])
            pending_indexes = []
            pages = {name: [] for name in names}
            runs = {name: [] for name in tracked.values()}
            depth = lod['key']['depth']

            def lod_name(chunk):
                path = [p.decode() for p in get(get(chunk, 3), 3)[1]]
                return path[1] if len(path) == 2 and path[0] == lod['column'] else None

            def column_leaf(chunk):
                path = [p.decode() for p in get(get(chunk, 3), 3)[1]]
                if len(path) == 3 and path[0] == attribute_column:
                    return path[1], path[2]
                return None

            # (LOD, attribute, row group) -> encoded file and which overview
            # pages hold a value; (LOD, attribute) -> pages
            attribute_files = {}
            attribute_pages = {(b, a): [] for b in column_lods for a in column_names}
            attribute_codecs = set()
            shown_pages = {name: [] for name in names}
            lod_bounds = {name: [] for name in names}

            # one job per LOD chunk, in the order the output holds the chunks
            led = str(src) if column_names and leads(parquet) else None
            jobs, lod_bytes = [], 0
            for c in range(columns):
                for g, group in enumerate(groups):
                    chunk = get(group, 1)[1][c]
                    if name := lod_name(chunk):
                        leaves = ()
                        if name in column_lods:
                            leaves = tuple(f'{attribute_column}.{name}.{a}' for a in column_names)
                        shift = 2 * (depth - grid_zoom(lod, name))
                        path = work / f'pages-{c}-{g}.parquet'
                        jobs.append(
                            (g, lod['column'], name, key_file, starts[g], shift, path)
                            + (page_bytes, compression_level, leaves, led)
                        )
                        lod_bytes += get(get(chunk, 3), 6)
            workers = min(workers, len(jobs)) if lod_bytes >= PARALLEL_LOD_BYTES else 1
            output = work / 'output.parquet'
            with output.open('w+b') as target, contextlib.ExitStack() as held:
                encoded = _encoded_pages(parquet, src, jobs, workers, held)
                target.write(b'PAR1' + bytes(BOOTSTRAP_BYTES - 4))
                for c in range(columns):
                    for g, group in enumerate(groups):
                        chunk = get(group, 1)[1][c]
                        cm = get(chunk, 3)
                        name = lod_name(chunk)
                        if name:
                            group_rows = get(group, 3)
                            local_keys = keys[starts[g] : starts[g] + group_rows]
                            path, present, encoded_attributes = next(encoded)
                            for leaf, encoded_attribute in encoded_attributes.items():
                                attribute_files[(name, leaf.split('.', 2)[2], g)] = (
                                    encoded_attribute
                                )
                            cm, index, _ = _append_page_chunks(path, target)
                            set_value(chunk, 3, cm)
                            chunk.pop(6, None)
                            chunk.pop(7, None)
                            locations = get(index, 1)[1]
                            bounds = np.array(
                                [get(p, 3) for p in locations] + [group_rows], np.int64
                            )
                            found = page_cells(
                                local_keys, bounds, present, depth, grid_zoom(lod, name)
                            )
                            if not found['valid'].all():
                                raise ValueError('a displayed row has a null Hilbert key')
                            shown = found.pop('pages')
                            shown_pages[name].append(shown)
                            lod_bounds[name].append(bounds)
                            found['offset'] = np.array(
                                [get(locations[i], 1) for i in shown], np.int64
                            )
                            found['length'] = np.array(
                                [get(locations[i], 2) for i in shown], np.int64
                            )
                            found['first'] += starts[g]
                            found['fence_page'] += sum(len(part['low']) for part in pages[name])
                            pages[name].append(found)
                            codec = CODECS[get(cm, 4)]
                            if name in root['codecs'] and root['codecs'][name] != codec:
                                raise ValueError('LOD codec must agree across row groups')
                            root['codecs'][name] = codec
                        elif leaf := column_leaf(chunk):
                            owner, attribute = leaf
                            attribute_path, valued = attribute_files.pop((owner, attribute, g))
                            cm, index, _ = _append_page_chunks(attribute_path, target)
                            set_value(chunk, 3, cm)
                            chunk.pop(6, None)
                            chunk.pop(7, None)
                            part, cut = _index_pages(index, get(group, 3), starts[g])
                            # the pages beside the overview pages the directory
                            # lists, of length 0 beside those without a value
                            shown = shown_pages[owner][g]
                            beside = np.searchsorted(cut, lod_bounds[owner][g][shown], 'right') - 1
                            attribute_pages[(owner, attribute)].append(
                                {
                                    'offset': part['offset'][beside],
                                    'length': np.where(valued[shown], part['length'][beside], 0),
                                }
                            )
                            attribute_codecs.add(CODECS[get(cm, 4)])
                        else:
                            old_start = min(get(cm, 9), get(cm, 11, get(cm, 9)))
                            delta = target.tell() - old_start
                            _copy(original, target, old_start, get(cm, 7))
                            for field_id in (9, 11):
                                if field_id in cm:
                                    set_value(cm, field_id, get(cm, field_id) + delta)
                            index = None
                            if 4 in chunk:
                                original.seek(get(chunk, 4))
                                index = loads(original.read(get(chunk, 5)))
                                for page in get(index, 1)[1]:
                                    set_value(page, 1, get(page, 1) + delta)
                            if c in tracked:
                                part, _ = _index_pages(index, get(group, 3), starts[g])
                                runs[tracked[c]].append(part)
                            if 6 in chunk:
                                original.seek(get(chunk, 6))
                                raw = original.read(get(chunk, 7))
                                pending_indexes.append(((6, g, c), chunk, 6, 7, raw))
                        set_value(chunk, 2, 0)
                        if index is not None:
                            pending_indexes.append(((4, g, c), chunk, 4, 5, dumps(index)))
                    log(f'  clustered column {c + 1}/{columns}')
                # The workers exit, freeing their memory for the directories built next.
                held.close()
                # Offset indexes, then column indexes, each by row group, so one
                # row's offset indexes across columns are read together.
                pending_indexes.sort(key=lambda p: p[0])
                for _, chunk, offset_field, length_field, raw in pending_indexes:
                    set_value(chunk, offset_field, target.tell())
                    set_value(chunk, length_field, len(raw))
                    target.write(raw)
                directory_start = target.tell()
                # Each tree starts as one level of leaves below a top node in the
                # root; the primary directory's is under None. Beside each leaf
                # of an LOD or the primary directory, a slot per column
                # attribute holds an attribute leaf listing the pages holding
                # its values for the leaf's rows: at an LOD, the attribute
                # column's page beside each of the leaf's pages; past the
                # finest LOD, the source column's pages over the leaf's runs.
                # The slots of a level-one node's leaves follow its last leaf.
                slotted, slots = {}, []
                for name, parts in pages.items():
                    table = {k: np.concatenate([part[k] for part in parts]) for k in parts[0]}
                    listed = [
                        {
                            k: np.concatenate([part[k] for part in attribute_pages[(name, a)]])
                            for k in ('offset', 'length')
                        }
                        for a in column_names
                    ]
                    count = len(table['low'])
                    cuts = [(i, min(i + leaf_pages, count)) for i in range(0, count, leaf_pages)]
                    slotted[name] = (
                        [table['low'][first] for first, _ in cuts],
                        [table['high'][last - 1] for _, last in cuts],
                        [_json(_leaf(table, first, last)) for first, last in cuts],
                        [
                            [
                                _json(attribute_leaf({k: v[first:last] for k, v in a.items()}))
                                for a in listed
                            ]
                            for first, last in cuts
                        ],
                    )
                zoom = directory_zoom(lod)
                first_cells, entries, members = primary_leaves(
                    parquet,
                    (bbox_column, bbox_fields),
                    runs[primary],
                    keys,
                    depth,
                    zoom,
                    leaf_pages,
                )
                columnar = [
                    {k: np.concatenate([part[k] for part in runs[a]]) for k in SOURCE_FIELDS}
                    for a in column_names
                ]
                ends = entries['first'] + entries['count']
                slotted[None] = (
                    first_cells,
                    np.r_[first_cells[1:], 4**zoom] - 1,
                    [_json(primary_leaf({k: v[m] for k, v in entries.items()})) for m in members],
                    [
                        [
                            _json(
                                row_leaf(
                                    {
                                        k: source[k][
                                            pages_over(source, entries['first'][m], ends[m])
                                        ]
                                        for k in SOURCE_FIELDS
                                    }
                                )
                            )
                            for source in columnar
                        ]
                        for m in members
                    ],
                )
                for _, _, _, beside in slotted.values():
                    slots.append(
                        SLOT_HEADER
                        + max((len(raw) for attached in beside for raw in attached), default=0)
                    )
                row_trees = {}
                for name in indexed:
                    table = {
                        k: np.concatenate([part[k] for part in runs[name]]) for k in SOURCE_FIELDS
                    }
                    cuts = [
                        (first, min(first + leaf_pages, len(table['first'])))
                        for first in range(0, len(table['first']), leaf_pages)
                    ]
                    row_trees[name] = (
                        [table['first'][first] for first, _ in cuts],
                        [
                            table['first'][last - 1] + table['count'][last - 1] - 1
                            for _, last in cuts
                        ],
                        [
                            _json(row_leaf({k: v[first:last] for k, v in table.items()}))
                            for first, last in cuts
                        ],
                    )

                def lay_out(target, grouped):
                    trees = {}
                    for (name, (low, high, leaves, beside)), size in zip(
                        slotted.items(), slots, strict=True
                    ):
                        offset, length = _write_leaves(
                            target, leaves, beside, size, name in grouped
                        )
                        trees[name] = (
                            1,
                            {'low': low, 'high': high, 'offset': offset, 'length': length},
                        )
                    for name, (low, high, leaves) in row_trees.items():
                        offset, length = _write_leaves(target, leaves, [[]] * len(leaves), 0, False)
                        trees[('column', name)] = (
                            1,
                            {'low': low, 'high': high, 'offset': offset, 'length': length},
                        )
                    return trees

                def branched(trees):
                    return frozenset(name for name in slotted if trees[name][0] > 1)

                grouped = forced = frozenset()
                if column_names:
                    if len(attribute_codecs) != 1:
                        raise ValueError('attribute columns must share one codec')
                    root['attribute_codec'] = next(iter(attribute_codecs))
                    root['attribute_slots'] = slots

                    def simulate(grouped, forced):
                        tally = _Tally(directory_start)
                        trees = lay_out(tally, grouped)
                        fit_root(tally, copy.deepcopy(root), trees, names, indexed, forced)
                        return branched(trees)

                    grouped, forced = _grouping(simulate)
                trees = lay_out(target, grouped)
                fit_root(target, root, trees, names, indexed, forced)
                if column_names and branched(trees) != grouped:
                    raise ValueError('attribute slot blocks disagree with the level-one nodes')
                for group in groups:
                    chunks = get(group, 1)[1]
                    set_value(group, 2, sum(get(get(c, 3), 6) for c in chunks))
                    if 6 in group:
                        set_value(group, 6, sum(get(get(c, 3), 7) for c in chunks))
                # RowGroup.file_offset is optional and must describe the new placement.
                for group in groups:
                    if 5 in group:
                        offsets = [get(get(c, 3), 11, get(get(c, 3), 9)) for c in get(group, 1)[1]]
                        set_value(group, 5, min(offsets))
                # Discoverable to analytical tooling without changing the GeoParquet contract.
                kv = get(metadata, 5, (12, []))[1]
                kv = [v for v in kv if get(v, 1) != b'lod_spatial_index']
                kv.append(
                    {
                        1: (8, b'lod_spatial_index'),
                        2: (8, f'{{"version":{INDEX_VERSION},"offset":4,"length":16380}}'.encode()),
                    }
                )
                metadata[5] = (9, (12, kv))
                footer = dumps(metadata)
                root['footer_bytes'] = len(footer)
                root['file_bytes'] = target.tell() + len(footer) + 8
                packed = _json(root)
                directory_bytes = target.tell() - directory_start
                target.write(footer)
                target.write(struct.pack('<I', len(footer)) + b'PAR1')
                if len(packed) + 16 > BOOTSTRAP_BYTES:
                    raise ValueError('root exceeds 16 KiB; increase leaf_pages')
                target.seek(4)
                target.write(MAGIC + struct.pack('<I', len(packed)) + packed)
                target.flush()
                os.fsync(target.fileno())
            del keys
            # Read the relocated footer before publishing the completed file.
            check = pq.ParquetFile(output)
            if check.metadata.num_rows != total_rows or check.schema_arrow != parquet.schema_arrow:
                raise ValueError('relocated table schema or row count changed')
            os.replace(output, out)
    log(f'  {directory_bytes:,} directory bytes, {len(packed):,} root bytes -> {out}')
    _source_bounds()
    return out
