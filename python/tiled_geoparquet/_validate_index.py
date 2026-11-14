"""Exhaustive spatial-directory checks against ordinary Parquet page indexes."""

import gzip
import itertools
import json
from pathlib import Path

import numpy as np
import pyarrow as pa

from ._compact import get, loads, loads_prefix
from ._layout import (
    BOOTSTRAP_BYTES,
    BOX_FIELDS,
    CODECS,
    FENCE_FIELDS,
    MAGIC,
    PAGE_FIELDS,
    SLOT_HEADER,
    SOURCE_FIELDS,
    VERSION,
    attribute_cuts,
    coarsest_zoom,
    column_schema,
    covering,
    covering_cells,
    directory_zoom,
    flat_columns,
    grid_zoom,
    page_cells,
    pages_over,
    reached_leaves,
    read_attribute_leaf,
    read_attribute_slot,
    read_node,
    read_primary_node,
    read_row_node,
    slot_place,
    tile_edges,
)
from .unpack import shown_rows


def check_index(path, parquet, lod):
    """Return normally for a file without the spatial index; raise ValueError on
    corruption. Holds the decoded directories, then one row group's keys and
    one LOD column of it at a time."""
    size = Path(path).stat().st_size
    with open(path, 'rb') as source:
        source.seek(-8, 2)
        footer_size = int.from_bytes(source.read(4), 'little')
        footer_start = size - 8 - footer_size
        source.seek(footer_start)
        metadata = loads(source.read(footer_size))

        def within(start, length):
            """Raise unless every `[start, start + length)` of these arrays is
            a nonempty range between the leading magic and the footer."""
            start, length = np.asarray(start), np.asarray(length)
            # Bound each value on its own first: an int64 sum could wrap.
            for value in (start, length):
                if (
                    value.dtype.kind not in 'iu'
                    or (value < 0).any()
                    or (value > footer_start).any()
                ):
                    raise ValueError('index byte range outside data region')
            start, length = start.astype(np.int64), length.astype(np.int64)
            if (start < 4).any() or (length <= 0).any() or (length > footer_start - start).any():
                raise ValueError('index byte range outside data region')

        def read(start, length):
            # the same bounds for Python integers, which cannot wrap
            if not (
                type(start) is int
                and type(length) is int
                and 4 <= start
                and 0 < length <= footer_start - start
            ):
                raise ValueError('index byte range outside data region')
            source.seek(start)
            raw = source.read(length)
            if len(raw) != length:
                raise ValueError('truncated index range')
            return raw

        # Validate all standard offset indexes, including source columns.
        groups = get(metadata, 4)[1]
        starts, first, locations = [], 0, {name: [] for name in lod['lods']}
        sources = {}
        column_names = [a['name'] for a in lod['attributes'] if a.get('storage') == 'column']
        attribute_column = (lod.get('attribute_columns') or {}).get('column')
        column_lods = (lod.get('attribute_columns') or {}).get('lods', [])
        # (LOD, attribute) -> per row group, its attribute chunk's pages
        attribute_locations = {}
        for g, group in enumerate(groups):
            starts.append(first)
            count = get(group, 3)
            for chunk in get(group, 1)[1]:
                cm = get(chunk, 3)
                path_parts = [part.decode() for part in get(cm, 3)[1]]
                pages = get(loads(read(get(chunk, 4), get(chunk, 5))), 1)[1]
                byte, length, row = (
                    np.array([get(page, k) for page in pages], np.int64) for k in (1, 2, 3)
                )
                within(byte, length)
                # page i holds rows bounds[i]:bounds[i + 1]
                bounds = np.r_[row, count]
                if (
                    bounds[0] != 0
                    or (bounds[1:] < bounds[:-1]).any()
                    or (byte[1:] < (byte + length)[:-1]).any()
                ):
                    raise ValueError(f'row group {g}: invalid offset-index page ordering')
                if len(path_parts) == 2 and path_parts[0] == lod['column']:
                    locations[path_parts[1]].append((byte, length, bounds, cm))
                if len(path_parts) == 3 and path_parts[0] == attribute_column:
                    owned = attribute_locations.setdefault(tuple(path_parts[1:]), [])
                    owned.append((byte, length, bounds, cm, first))
                if len(path_parts) == 1:
                    sources.setdefault(path_parts[0], []).append((byte, length, bounds, cm, first))
            first += count

        source.seek(4)
        magic = source.read(8)
        # The footer's key/value metadata, which Arrow's schema metadata need not include.
        advertised = (parquet.metadata.metadata or {}).get(b'lod_spatial_index')
        if magic != MAGIC:
            if advertised is not None:
                raise ValueError('spatial index advertised but bootstrap magic missing')
            return
        if advertised is None or json.loads(advertised) != {
            'version': VERSION,
            'offset': 4,
            'length': BOOTSTRAP_BYTES - 4,
        }:
            raise ValueError('spatial index advertisement missing or of another version')
        if column_names and column_lods != list(lod['lods']):
            raise ValueError('a spatial index needs every LOD to hold the column attributes')
        root_length = int.from_bytes(source.read(4), 'little')
        if not 0 < root_length <= BOOTSTRAP_BYTES - 16:
            raise ValueError('invalid spatial root length')
        root = json.loads(gzip.decompress(read(16, root_length)))
        if root.get('version') != VERSION or root.get('file_bytes') != size:
            raise ValueError('spatial root version or file length disagrees with footer')
        if root.get('lod') != lod:
            raise ValueError('spatial root metadata disagrees with footer')
        geo = json.loads(parquet.schema_arrow.metadata[b'geo'])
        if root.get('primary') != geo.get('primary_column', 'geometry'):
            raise ValueError('spatial root primary disagrees with footer')
        if root.get('footer_bytes', footer_size) != footer_size:
            raise ValueError('spatial root footer_bytes disagrees with footer')
        directories = root.get('directories')
        if not isinstance(directories, dict) or set(directories) != set(lod['lods']):
            raise ValueError('spatial directories disagree with declared LODs')
        key = lod['key']
        depth = key['depth']
        if not isinstance(depth, int) or not 0 <= depth <= 26:
            raise ValueError('invalid Hilbert depth')
        sentinel = 4**depth

        # Every node of every tree, its length by offset: a tree reaches each
        # node once.
        reached = {}
        trees, leaf_lists = {}, {}
        for name, top in directories.items():
            level, entries = read_node(top)
            if level < 1:
                raise ValueError('a directory tree must start above its leaves')
            leaf_lists[name] = list(directory_leaves(read, level, entries, reached))
            trees[name] = _concat(leaf_lists[name])
        if column_names:
            codecs = {
                CODECS[get(chunk[3], 4)]
                for chunks in attribute_locations.values()
                for chunk in chunks
            }
            if root.get('attribute_codec') not in codecs or len(codecs) != 1:
                raise ValueError('spatial root attribute codec disagrees with footer')
        elif 'attribute_codec' in root:
            raise ValueError('spatial root names an attribute codec without attribute columns')

        primary = root['primary']
        columns = flat_columns(metadata)
        # the flat columns whose chunks decode on their own, in schema order
        eligible = [
            name
            for name in columns
            if all(
                4 in chunk and 11 not in get(chunk, 3) for chunk in _chunks(groups, columns, name)
            )
            and len({get(get(chunk, 3), 4) for chunk in _chunks(groups, columns, name)}) == 1
        ]
        column_index = root.get('column_index')
        if not isinstance(column_index, dict) or column_index.get('columns') != eligible:
            raise ValueError('row directories must list every flat column that decodes on its own')
        if column_index.get('schema') != column_schema(metadata, eligible):
            raise ValueError('row directory schema disagrees with footer')
        codecs = column_index.get('codecs')
        if not isinstance(codecs, list) or len(codecs) != len(eligible):
            raise ValueError('row directory codecs disagree with footer')
        tops = column_index.get('directories')
        if not isinstance(tops, list) or len(tops) != len(eligible):
            raise ValueError('row directories disagree with their columns')
        for name, codec, top in zip(eligible, codecs, tops, strict=True):
            for byte, length, bounds, cm, _ in sources[name]:
                if codec != CODECS[get(cm, 4)]:
                    raise ValueError('row directory codecs disagree with footer')
                _check_source_pages(read, cm, byte, length, np.diff(bounds))
            level, entries = read_row_node(top)
            if level < 1:
                raise ValueError('a directory tree must start above its leaves')
            leaves = list(row_leaves(read, within, level, entries, reached))
            listed = {
                k: np.concatenate([np.zeros(0, np.int64), *(leaf[k] for leaf in leaves)])
                for k in SOURCE_FIELDS
            }
            pages = [_index_part(*chunk) for chunk in sources[name]]
            if any(
                not np.array_equal(listed[k], np.concatenate([p[k] for p in pages]))
                for k in SOURCE_FIELDS
            ):
                raise ValueError(f'row directory of {name} disagrees with its offset indexes')
        index = root.get('primary_index')
        if not isinstance(index, dict):
            raise ValueError('spatial root has no primary directory')
        attributes = index.get('attributes')
        if (
            not isinstance(attributes, list)
            or len(set(map(str, attributes))) != len(attributes)
            or any(type(a) is not str or a == primary or a not in eligible for a in attributes)
        ):
            raise ValueError('primary directory attributes must be distinct indexed columns')
        if any(a in column_names for a in attributes):
            raise ValueError('the primary directory locates a column attribute')
        if primary not in eligible or get(columns[primary][0], 1) != 6:
            raise ValueError('the primary geometry is not an indexed binary column')

        zoom = directory_zoom(lod)

        def walk_primary(level, entries, low, high):
            """The leaves under one primary directory node holding cells
            `low..high`, in tree order, each with its first cell and its
            byte range under `range`."""
            if not level:
                return [(low, entries)]
            within(entries['offset'], entries['length'])
            if (
                not len(entries['low'])
                or (entries['low'][0], entries['high'][-1]) != (low, high)
                or (entries['low'][1:] != entries['high'][:-1] + 1).any()
            ):
                raise ValueError('primary directory leaves do not hold every cell once')
            leaves = []
            for i in range(len(entries['offset'])):
                offset = int(entries['offset'][i])
                if offset in reached:
                    raise ValueError('a directory node is referenced twice')
                reached[offset] = int(entries['length'][i])
                raw = read(offset, int(entries['length'][i]))
                child_level, child = read_primary_node(json.loads(gzip.decompress(raw)), attributes)
                if child_level != level - 1:
                    raise ValueError('inconsistent directory depth')
                child['range'] = (offset, int(entries['length'][i]))
                if level == 1:
                    child['group'] = _group(entries, i)
                low, high = int(entries['low'][i]), int(entries['high'][i])
                leaves += walk_primary(child_level, child, low, high)
            return leaves

        level, top = read_primary_node(index.get('directory'), attributes)
        if level < 1:
            raise ValueError('a directory tree must start above its leaves')
        found = walk_primary(level, top, 0, 4**zoom - 1)
        firsts = np.array([low for low, _ in found], np.int64)
        leaves = [leaf for _, leaf in found]
        geometry = {
            k: np.concatenate([_index_part(*chunk)[k] for chunk in sources[primary]])
            for k in SOURCE_FIELDS
        }
        # each leaf's pages, as leaf * pages + page, and every run listed
        listed, runs = [], []
        for i, leaf in enumerate(leaves):
            within(leaf['offset'], leaf['length'])
            page = np.searchsorted(geometry['offset'], leaf['offset'])
            if (page >= len(geometry['offset'])).any() or any(
                not np.array_equal(geometry[k][np.minimum(page, len(geometry[k]) - 1)], leaf[k])
                for k in ('offset', 'length')
            ):
                raise ValueError('primary directory run outside the geometry pages')
            new = np.r_[True, page[1:] != page[:-1]][: len(page)]
            heads = np.flatnonzero(new)
            if (leaf['first'][heads] != geometry['first'][page[heads]]).any() or (
                np.add.reduceat(leaf['count'], heads) != geometry['count'][page[heads]]
            ).any():
                raise ValueError("primary directory runs do not add up to their page's rows")
            listed.append(i * len(geometry['offset']) + page[heads])
            runs.append({k: leaf[k] for k in (*BOX_FIELDS, 'first', 'count')})
        listed = np.sort(np.concatenate([np.zeros(0, np.int64), *listed]))
        runs = {k: np.concatenate([np.zeros(0, np.int64), *(r[k] for r in runs)]) for k in runs[0]}
        # a page's runs are the same in every leaf that lists it
        order = np.argsort(runs['first'], kind='stable')
        runs = {k: v[order] for k, v in runs.items()}
        again = np.r_[False, runs['first'][1:] == runs['first'][:-1]]
        if any((v[1:][again[1:]] != v[:-1][again[1:]]).any() for v in runs.values()):
            raise ValueError('primary directory leaves cut a page differently')
        runs = {k: v[~again] for k, v in runs.items()}
        if (runs['first'][1:] < (runs['first'] + runs['count'])[:-1]).any():
            raise ValueError('primary directory leaves cut a page differently')
        for i, name in enumerate(attributes):
            parts = [_index_part(*chunk) for chunk in sources[name]]
            pages = {k: np.concatenate([part[k] for part in parts]) for k in SOURCE_FIELDS}
            for leaf in leaves:
                over = pages_over(pages, leaf['first'], leaf['first'] + leaf['count'])
                if any(
                    not np.array_equal(leaf['sources'][i][k], pages[k][over]) for k in SOURCE_FIELDS
                ):
                    raise ValueError(f'primary directory leaf misplaces {name} pages')
        reach = coarsest_zoom(firsts, zoom)
        coarse = firsts >> 2 * (zoom - reach)
        edges = tile_edges(reach) if len(leaves) > 1 else None
        bbox_column, bbox_fields = covering(geo)
        # Pages compare in tree order, which must be key and row order: each
        # row group's pages follow the pages of the row groups before it.
        checked = dict.fromkeys(trees, 0)
        # per LOD and row group, the indexes of the pages its directory lists
        shown_pages = {name: [] for name in trees}
        led = {name: [] for name in trees}
        last = 0
        for g in range(len(groups)):
            byte, length, bounds, _, start = sources[primary][g]
            bbox = parquet.read_row_group(g, columns=[bbox_column]).column(0).combine_chunks()
            if len(bbox) != bounds[-1]:
                raise ValueError('covering length disagrees with the row group')
            cells = covering_cells(bbox, bbox_fields)
            # each run of the row group's pages boxes its rows
            a, b = np.searchsorted(runs['first'], [start, start + bounds[-1]])
            first, end = runs['first'][a:b] - start, (runs['first'] + runs['count'])[a:b] - start
            cuts = np.ravel(np.column_stack((first, end)))[
                : 2 * (b - a) - (end[-1:] == len(bbox)).sum()
            ]
            with np.errstate(invalid='ignore'):
                box = [
                    reduce.reduceat(cell, cuts)[::2] if len(cuts) else np.zeros(0)
                    for reduce, cell in zip(
                        (np.fmin, np.fmin, np.fmax, np.fmax), cells, strict=True
                    )
                ]
            if np.isnan(box[0]).any() or any(
                not np.array_equal(runs[k][a:b], v) for k, v in zip(BOX_FIELDS, box, strict=True)
            ):
                raise ValueError("primary directory run box is not the union of its rows' covering")
            # each leaf a row's box reaches lists its page
            rows = np.flatnonzero(~np.isnan(cells[0]))
            if len(leaves) > 1:
                row, leaf = reached_leaves(
                    [c[rows].astype(np.int64) for c in cells], reach, coarse, edges
                )
            else:
                row, leaf = np.arange(len(rows)), np.zeros(len(rows), np.int64)
            page = np.searchsorted(geometry['first'], rows[row] + start, 'right') - 1
            need = np.unique(leaf * len(geometry['offset']) + page)
            at = np.minimum(np.searchsorted(listed, need), len(listed) - 1)
            if len(need) and (not len(listed) or (listed[at] != need).any()):
                raise ValueError('a primary directory leaf leaves out a page whose rows reach it')
            column = parquet.read_row_group(g, columns=[key['column']]).column(0)
            if not pa.types.is_integer(column.type):
                raise ValueError('Hilbert keys must be integers')
            keys = column.fill_null(sentinel).to_numpy()
            if len(keys) and (
                (keys < 0).any()
                or (keys > sentinel).any()
                or keys[0] < last
                or (keys[1:] < keys[:-1]).any()
            ):
                raise ValueError('Hilbert keys out of range or unsorted')
            last = keys[-1] if len(keys) else last
            for name, actual in trees.items():
                byte, length, bounds, cm = locations[name][g]
                if root.get('codecs', {}).get(name) != CODECS[get(cm, 4)]:
                    raise ValueError('spatial root codec disagrees with footer')
                _check_pages(read, cm, byte, length, np.diff(bounds))
                values = (
                    parquet.read_row_group(g, columns=[f'{lod["column"]}.{name}'])
                    .column(0)
                    .combine_chunks()
                    .field(name)
                )
                present = values.is_valid().to_numpy(zero_copy_only=False)
                own = shown_rows(values, lod['lods'][name]['geometry_type'])
                before = np.r_[0, np.cumsum(own, dtype=np.int64)]
                led[name].append(before[bounds[1:]] - before[bounds[:-1]])
                expected = page_cells(keys, bounds, present, depth, grid_zoom(lod, name))
                shown = expected.pop('pages')
                expected['offset'], expected['length'] = byte[shown], length[shown]
                expected['first'] += starts[g]
                shown_pages[name].append(shown)
                _compare(actual, checked[name], expected)
                checked[name] += len(shown)
        if any(checked[name] != len(actual['low']) for name, actual in trees.items()):
            raise ValueError('spatial directory omits, adds or reorders overview pages')
        _check_attribute_slots(
            read,
            reached,
            root,
            [leaf_lists[name] for name in lod['lods']] + [leaves],
            column_names,
            [
                [
                    _attribute_pages(
                        read,
                        parquet,
                        f'{attribute_column}.{name}.{attribute}',
                        locations[name],
                        attribute_locations[(name, attribute)],
                        shown_pages[name],
                        led[name],
                    )
                    for name in lod['lods']
                ]
                + [
                    {
                        k: np.concatenate([_index_part(*chunk)[k] for chunk in sources[attribute]])
                        for k in SOURCE_FIELDS
                    }
                ]
                for attribute in column_names
            ],
        )


def directory_leaves(read, level, entries, reached):
    """Yield the leaf entries under one LOD directory node, in tree order,
    each with its byte range under `range` and, under `group`, the end of
    its level-one node's last leaf, that node's leaves and its position
    among them. `read(offset, length)` returns a node's bytes and refuses a
    range outside the file's data, and `reached` holds the lengths of the
    nodes read so far by offset: a tree reaches each node once."""
    if not level:
        yield entries
        return
    for i in range(len(entries['low'])):
        offset, low, high = (int(entries[k][i]) for k in ('offset', 'low', 'high'))
        if offset in reached:
            raise ValueError('a directory node is referenced twice')
        reached[offset] = int(entries['length'][i])
        raw = read(offset, int(entries['length'][i]))
        child_level, child = read_node(json.loads(gzip.decompress(raw)))
        child['range'] = (offset, int(entries['length'][i]))
        if level == 1:
            child['group'] = _group(entries, i)
        if child_level != level - 1:
            raise ValueError('inconsistent directory depth')
        if (
            not len(child['low'])
            or (child['low'][0], child['high'][-1]) != (low, high)
            or (child['low'] < low).any()
            or (child['high'] > high).any()
        ):
            raise ValueError('directory bounds do not cover children exactly')
        yield from directory_leaves(read, child_level, child, reached)


def _group(entries, i):
    """The `group` of the child at entry `i` of a level-one node."""
    return (
        int(entries['offset'][-1] + entries['length'][-1]),
        len(entries['offset']),
        i,
    )


def row_leaves(read, within, level, entries, reached):
    """Yield the leaves under one row directory node, in tree order, each a
    table of pages. `within` refuses byte ranges outside the data, and
    `reached` holds the lengths of the nodes read so far by offset: every
    node is reached once. A node's first and last rows are those its parent's entry
    states."""
    if not level:
        within(entries['offset'], entries['length'])
        yield entries
        return
    within(entries['offset'], entries['length'])
    for i in range(len(entries['low'])):
        offset, low, high = (int(entries[k][i]) for k in ('offset', 'low', 'high'))
        if offset in reached:
            raise ValueError('a directory node is referenced twice')
        reached[offset] = int(entries['length'][i])
        raw = read(offset, int(entries['length'][i]))
        child_level, child = read_row_node(json.loads(gzip.decompress(raw)))
        if child_level != level - 1:
            raise ValueError('inconsistent directory depth')
        first, last = (
            (child['first'], child['first'] + child['count'] - 1)
            if not child_level
            else (child['low'], child['high'])
        )
        if not len(first) or (first[0], last[-1]) != (low, high):
            raise ValueError('row directory node disagrees with its parent entry')
        yield from row_leaves(read, within, child_level, child, reached)


def _chunks(groups, columns, name):
    """The column chunks of the flat column `name` in every row group."""
    return [get(group, 1)[1][columns[name][1]] for group in groups]


def _index_part(byte, length, bounds, _cm, start):
    """A chunk's pages from its offset index, rows counted from `start`."""
    if (np.diff(bounds) <= 0).any():
        raise ValueError('an indexed column has an empty page')
    return {
        'offset': byte,
        'length': length,
        'first': bounds[:-1] + start,
        'count': np.diff(bounds),
    }


def _check_source_pages(read, cm, byte, length, rows):
    """Check that a primary or attribute chunk holds only the pages its offset
    index lists, back to back from its first data page to its end, each a
    data page, not dictionary encoded, of as many rows as the index says.
    The column is flat, so a page holds a value or a null per row."""
    start = get(cm, 9)
    if (
        11 in cm
        or not len(byte)
        or byte[0] != start
        or (byte[1:] != (byte + length)[:-1]).any()
        or byte[-1] + length[-1] != start + get(cm, 7)
    ):
        raise ValueError('an indexed column chunk holds pages its offset index omits')
    for offset, size, count in zip(byte.tolist(), length.tolist(), rows.tolist(), strict=True):
        try:
            header, header_size = loads_prefix(read(offset, min(size, 512)))
        except (ValueError, IndexError):
            header, header_size = loads_prefix(read(offset, size))
        kind, v1, v2 = get(header, 1), get(header, 5), get(header, 8)
        if kind == 0 and v1 is not None:
            values, nulls, rows, encoding = get(v1, 1), 0, get(v1, 1), get(v1, 2)
        elif kind == 3 and v2 is not None:
            values, nulls, rows, encoding = (get(v2, k) for k in (1, 2, 3, 4))
        else:
            raise ValueError('indexed column pages must be data pages')
        if encoding in (2, 8):
            raise ValueError('indexed column pages must not be dictionary encoded')
        if (
            values != count
            or rows != count
            or type(nulls) is not int
            or not 0 <= nulls <= values
            or header_size + get(header, 3) != size
        ):
            raise ValueError('indexed column page header disagrees with its offset index')


def _compare(actual, start, expected):
    """Check the directory pages `actual` from index `start` against the next
    row group's `expected` pages, whose fences number its pages from zero."""
    end = start + len(expected['low'])
    if not np.array_equal(actual['offset'][start:end], expected['offset']):
        raise ValueError('spatial directory omits, adds or reorders overview pages')
    if not expected['valid'].all():
        raise ValueError('a displayed row has a null Hilbert key')
    first, last = np.searchsorted(actual['fence_page'], [start, end])
    found = {k: actual[k][start:end] for k in PAGE_FIELDS}
    found.update({k: actual[k][first:last] for k in FENCE_FIELDS})
    found['fence_page'] = found['fence_page'] - start
    for fields, message in (
        (('length', 'first', 'count'), 'spatial leaf disagrees with Parquet page index'),
        (('low', 'high', 'valid'), 'spatial leaf key bounds or valid-row count incorrect'),
        (FENCE_FIELDS, 'spatial leaf fences disagree with source keys'),
    ):
        if any(not np.array_equal(found[k], expected[k]) for k in fields):
            raise ValueError(message)


def _concat(parts):
    """Page tables in order, as one table with fences numbered across it."""
    pages = {
        k: np.concatenate([np.zeros(0, np.int64)] + [p[k] for p in parts]) for k in PAGE_FIELDS
    }
    counts = np.cumsum([0] + [len(p['low']) for p in parts])
    for k in FENCE_FIELDS:
        pages[k] = np.concatenate(
            [np.zeros(0, np.int64)]
            + [p[k] + (counts[i] if k == 'fence_page' else 0) for i, p in enumerate(parts)]
        )
    return pages


def _attribute_pages(read, parquet, leaf, lod_chunks, chunks, shown, led):
    """The pages of the attribute column `leaf` of an LOD, one row group's
    chunk of each in `lod_chunks` and `chunks`, beside the overview pages
    the directory lists, the indexes `shown` per row group, each of length 0
    beside an overview page without a value. Raises unless every chunk's
    pages hold the rows its LOD chunk's do, but a page for each run of
    overview pages without a value, each a v1 data page of PLAIN or
    BYTE_STREAM_SPLIT values with RLE definition levels, and unless its
    values lead their overview pages: `led` counts, per row group, the rows
    each overview page shows, and a value lies on one of that many leading
    rows of its page."""
    parts = []
    for g, ((_, _, lod_bounds, _), chunk, listed, counts) in enumerate(
        zip(lod_chunks, chunks, shown, led, strict=True)
    ):
        byte, length, bounds, cm, _ = chunk
        values = parquet.read_row_group(g, columns=[leaf]).column(0).combine_chunks()
        for part in leaf.split('.')[1:]:
            values = values.field(part)
        if len(values) != lod_bounds[-1]:
            raise ValueError(f'{leaf} disagrees with its row group')
        valid = values.is_valid().to_numpy(zero_copy_only=False)
        held = np.flatnonzero(valid)
        page = np.searchsorted(lod_bounds, held, 'right') - 1
        if (held - lod_bounds[page] >= counts[page]).any():
            raise ValueError(f"{leaf} holds a value past the rows its LOD's page shows")
        valued, cut = attribute_cuts(valid, lod_bounds)
        if not np.array_equal(bounds, cut):
            raise ValueError(
                "an attribute column's pages are not cut where its LOD's are, "
                'with a page for each run of overview pages without a value'
            )
        _check_pages(read, cm, byte, length, np.diff(bounds), (0, 9))
        part = _index_part(*chunk)
        beside = np.searchsorted(bounds, lod_bounds[listed], 'right') - 1
        parts.append(
            {
                'offset': part['offset'][beside],
                'length': np.where(valued[listed], part['length'][beside], 0),
            }
        )
    return {
        k: np.concatenate([np.zeros(0, np.int64)] + [p[k] for p in parts])
        for k in ('offset', 'length')
    }


def _check_attribute_slots(read, reached, root, leaves, columns, pages):
    """Raise unless the leaves under each level-one node of each tree, each
    LOD's and then the primary directory's, lie back to back, and the root
    gives attribute slot sizes exactly when there are column attributes
    `columns`. With them, each level-one node's leaves are followed by its
    slot block: per attribute, a slot of its tree's size per leaf, in leaf
    order, framed with the attribute's position and the leaf's offset and
    holding an attribute leaf listing the leaf's pages: in `pages`, per
    attribute and tree, the attribute column's pages beside the LOD's listed
    pages, or the source column's pages. `reached` holds the length of every
    directory node by offset, none of which may overlap a slot block."""
    groups = []
    for t, tree in enumerate(leaves):
        held = {}
        for leaf in tree:
            held.setdefault(leaf['group'][0], []).append(leaf)
        for end, members in held.items():
            ranges = [member['range'] for member in members]
            if (
                [member['group'][2] for member in members] != list(range(len(members)))
                or any(a + n != b for (a, n), (b, _) in itertools.pairwise(ranges))
                or ranges[-1][0] + ranges[-1][1] != end
            ):
                raise ValueError("a level-one node's leaves are not back to back")
            groups.append((t, ranges[0][0], end, len(members)))
    slots = root.get('attribute_slots')
    if not columns:
        if slots is not None:
            raise ValueError('the spatial root gives attribute slots without column attributes')
        return
    if (
        not isinstance(slots, list)
        or len(slots) != len(leaves)
        or any(type(v) is not int or v < SLOT_HEADER for v in slots)
    ):
        raise ValueError('the spatial root gives no attribute slot size per tree')
    starts = np.array(sorted(reached), np.int64)
    # the furthest end of the nodes starting at or before each node's start
    reach = np.maximum.accumulate(starts + np.array([reached[s] for s in starts.tolist()]))
    blocks = []
    for t, start, end, count in groups:
        last = end + len(columns) * count * slots[t]
        before = np.searchsorted(starts, last)
        if before and reach[before - 1] > end:
            raise ValueError('a directory node overlaps an attribute slot block')
        blocks.append((start, last))
    blocks.sort()
    if any(b[0] < a[1] for a, b in itertools.pairwise(blocks)):
        raise ValueError('a directory leaf overlaps attribute slots')
    for t, (tree, slot) in enumerate(zip(leaves, slots, strict=True)):
        primary = t == len(leaves) - 1
        at = 0
        largest = 0
        for leaf in tree:
            for a, name in enumerate(columns):
                held = read(slot_place(leaf['group'], a, slot), slot)
                largest = max(largest, int.from_bytes(held[:4], 'little'))
                node = read_attribute_slot(held, a, leaf['range'][0])
                wanted = pages[a][t]
                if primary:
                    level, got = read_row_node(node)
                    if level:
                        raise ValueError('an attribute leaf must be a leaf')
                    over = pages_over(wanted, leaf['first'], leaf['first'] + leaf['count'])
                    fields = SOURCE_FIELDS
                else:
                    got = read_attribute_leaf(node)
                    if len(got['offset']) != len(leaf['low']):
                        raise ValueError(f'an attribute leaf of {name} lists a page per leaf page')
                    over = np.arange(at, at + len(leaf['low']))
                    fields = ('offset', 'length')
                if over.size and over[-1] >= len(wanted['offset']):
                    raise ValueError(f'an attribute leaf of {name} disagrees with its pages')
                if not primary:
                    if ((got['length'] == 0) & (wanted['length'][over] > 0)).any():
                        raise ValueError(
                            f'an attribute leaf of {name} lists no page beside rows with a value'
                        )
                    # an empty page's offset is the end of the page before it
                    kept = got['length'] > 0
                    got = {k: v[kept] for k, v in got.items()}
                    over = over[kept]
                if any(not np.array_equal(got[k], wanted[k][over]) for k in fields):
                    raise ValueError(f'an attribute leaf of {name} disagrees with its pages')
            if not primary:
                at += len(leaf['low'])
        if slot != SLOT_HEADER + largest:
            raise ValueError('an attribute slot size is not 16 plus its largest attribute leaf')


def _check_pages(read, cm, byte, length, rows, encodings=(0,)):
    """Check that an LOD chunk holds only the pages its offset index lists,
    back to back from its first data page to its end, each a v1 data page of
    PLAIN values, or of one of `encodings`, with RLE definition levels and as
    many values as rows."""
    start = get(cm, 9)
    if (
        11 in cm
        or not len(byte)
        or byte[0] != start
        or (byte[1:] != (byte + length)[:-1]).any()
        or byte[-1] + length[-1] != start + get(cm, 7)
    ):
        raise ValueError('LOD chunk holds pages its offset index omits')
    for offset, size, count in zip(byte.tolist(), length.tolist(), rows.tolist(), strict=True):
        header, header_size = loads_prefix(read(offset, size))
        page = get(header, 5)
        if (
            get(header, 1) != 0
            or page is None
            or get(page, 2) not in encodings
            or get(page, 3) != 3
        ):
            raise ValueError('LOD pages must be v1 data pages of PLAIN values')
        if get(page, 1) != count or header_size + get(header, 3) != size:
            raise ValueError('LOD page header disagrees with its offset index')
