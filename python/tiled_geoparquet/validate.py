"""Check the contract a renderer relies on, without reading the whole file."""

from __future__ import annotations

import itertools
import json
import math
import re
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely

from ._tile_sizes import _root, tile_sizes
from .encoding import (
    ENCODING,
    INDEX_KEY_DEPTH,
    INT32,
    MAX_ATTRIBUTES,
    ORDER,
    PACK_VERSION,
    SUMMARY_QUANTILES,
    TILE_SIZE,
    check_index_version,
    check_zooms,
    column_type,
    is_attribute_type,
    is_compatible_version,
    source_compatible,
)
from .pack import FAMILY_DEPTH, INT64_MAX
from .spill import MAX_MARGIN_PIXELS
from .unpack import (
    PageBounds,
    Unpacked,
    UnpackError,
    attribute_fields,
    leading_rows,
    leads,
    lod_values,
    unpack_batches,
)
from .writer import LOD_KEY, LOD_VERSION

BOUNDS = ('xmin', 'ymin', 'xmax', 'ymax')
# rows of covering and primary geometry parsed at a time
COVERING_BATCH_ROWS = 65_536

# the key order has reached its null keys, which every keyed row precedes
NULL_KEYS = float('inf')


class ValidationError(Exception):
    pass


def validate(path: str | Path, sample_rows: int = 2000, *, full: bool = False) -> dict:
    """Check metadata and a quick sample, or every packed row and index with full=True.

    The sample is the first sample_rows rows of the first row group: quick
    mode decodes their packed values, and both modes check that each one's
    covering is its primary geometry's bounds rounded outward to float32,
    null exactly where the geometry has no finite bounds: where it is null
    or empty, has no x or no y but NaN, or an infinite coordinate. Full mode
    checks every row's covering the same way, streams one LOD column of one
    row group at a time, decoding it in batches, and checks a row group at a
    time that the covering and the Hilbert key are null on the same rows.
    """
    if not isinstance(sample_rows, int) or sample_rows < 0:
        raise ValueError('sample_rows must be a nonnegative integer')
    f = pq.ParquetFile(path)
    md = f.metadata
    geo = json.loads(dict(f.schema_arrow.metadata or {}).get(b'geo', b'{}') or b'{}')
    lod = geo.get(LOD_KEY)
    if not lod:
        raise ValidationError(f'no geo.{LOD_KEY} metadata')
    version = lod.get('version')
    if not isinstance(version, str) or not is_compatible_version(version):
        raise ValidationError(f'version {version} is not compatible with {LOD_VERSION}')
    if lod.get('encoding') != ENCODING:
        raise ValidationError(f'geometry encoding {lod.get("encoding")!r} is not {ENCODING}')
    if type(lod.get('pack_version')) is not int or lod['pack_version'] != PACK_VERSION:
        raise ValidationError(f'pack_version {lod.get("pack_version")!r} is not {PACK_VERSION}')
    try:
        indexed = check_index_version(f)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if indexed:
        _check_root_version(path)
    margin = lod.get('margin')
    if not (
        isinstance(margin, (int, float))
        and not isinstance(margin, bool)
        and 0 <= margin <= MAX_MARGIN_PIXELS
    ):
        raise ValidationError(
            f'margin {margin!r} is not a number of pixels from 0 to {MAX_MARGIN_PIXELS}'
        )
    if md.num_row_groups == 0 or md.num_rows == 0:
        raise ValidationError('empty file: an empty dataset has no overviews')
    _check_declared(lod)
    if indexed:
        _check_root_copy(path, lod)
    column = lod.get('column')
    names = f.schema_arrow.names
    if column not in names:
        raise ValidationError(f'LOD column {column} missing')
    geometry = geo.get('primary_column', 'geometry')
    if geometry not in names:
        raise ValidationError(f'primary geometry column {geometry} missing')
    wkb = f.schema_arrow.field(geometry).type
    if not (pa.types.is_binary(wkb) or pa.types.is_large_binary(wkb)):
        raise ValidationError(f'primary geometry column {geometry} must be WKB binary, not {wkb}')
    if 'bbox' not in names:
        raise ValidationError('bbox covering column missing')
    covering = geo.get('columns', {}).get(geometry, {}).get('covering')
    if not covering:
        raise ValidationError(f'geo.columns.{geometry}.covering missing')
    version = tuple(int(part) for part in re.findall(r'\d+', str(geo.get('version', '')))[:2])
    if not (1, 1) <= version < (2,):
        raise ValidationError(
            f'geo version {geo.get("version")!r} is not GeoParquet 1.1 or a later 1.x'
        )
    _check_covering(f.schema_arrow.field('bbox'), covering, geometry)
    key = lod.get('key')
    key_column = key.get('column') if isinstance(key, dict) else None
    if key_column not in names:
        raise ValidationError(f'key column {key_column} missing')
    key_field = f.schema_arrow.field(key_column)
    if not (pa.types.is_int64(key_field.type) and key_field.nullable):
        raise ValidationError(f'key column {key_column} must be a nullable int64 column')
    lods = lod.get('lods')
    if not isinstance(lods, dict) or not lods:
        raise ValidationError('no lods declared')
    try:
        check_zooms(lod)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if indexed and key['depth'] > INDEX_KEY_DEPTH:
        raise ValidationError(
            f'key depth {key["depth"]} is past {INDEX_KEY_DEPTH}, the most the spatial index holds'
        )
    struct = f.schema_arrow.field(column)
    if struct.nullable:
        raise ValidationError(f'{column} must be a required struct')
    children = {
        struct.type.field(i).name: struct.type.field(i) for i in range(struct.type.num_fields)
    }
    for name, child in lods.items():
        if name not in children:
            raise ValidationError(f'lod {name} declared but column {column}.{name} missing')
        gtype = child.get('geometry_type')
        if gtype not in FAMILY_DEPTH:
            raise ValidationError(f'lod {name} has unknown geometry_type {gtype!r}')
        field = children[name]
        if not field.nullable or not pa.types.is_binary(field.type):
            raise ValidationError(f'{column}.{name} must be a nullable binary column')
        for key in ('scale', 'offset'):
            v = child.get(key)
            if not (
                isinstance(v, list)
                and len(v) == 2
                and all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                    for x in v
                )
            ):
                raise ValidationError(f'lod {name}: {key} must be an [x, y] pair')
        if min(child['scale']) <= 0:
            raise ValidationError(f'lod {name}: scale must be positive')
        placeholder = child.get('placeholder')
        if not (
            isinstance(placeholder, (int, float))
            and not isinstance(placeholder, bool)
            and math.isfinite(placeholder)
            and placeholder > 0
        ):
            raise ValidationError(f'lod {name}: placeholder must be a positive width in grid cells')
    _check_attributes(lod['attributes'], f.metadata.num_rows)
    columns = _check_attribute_columns(f.schema_arrow, lod)
    paths = [f.schema.column(i).path for i in range(len(f.schema))]
    shared = sorted(path for path in set(paths) if paths.count(path) > 1)
    if shared:
        raise ValidationError(f'two columns share the path {shared[0]}')

    # statistics on the covering and page indexes on every column, in every row group
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for i in range(rg.num_columns):
            c = rg.column(i)
            if c.path_in_schema.startswith('bbox.') or c.path_in_schema == key_column:
                if not c.is_stats_set:
                    raise ValidationError(f'row group {g}: {c.path_in_schema} has no statistics')
                if not c.has_column_index:
                    raise ValidationError(f'row group {g}: {c.path_in_schema} has no column index')
            if not c.has_offset_index:
                raise ValidationError(f'row group {g}: {c.path_in_schema} has no offset index')
    _check_coverings(f, geometry, 0, sample_rows)
    # every LOD's packed rows: non-empty parts, rings of three or more vertices, valid copies;
    # the attribute columns hold values only for the rows their LOD shows
    fields, dictionaries = attribute_fields(lod)
    column_lods = (lod.get('attribute_columns') or {}).get('lods', [])
    pages = PageBounds(path) if column_lods and leads(f) else None
    checked = dict.fromkeys(lods, 0)
    groups = range(md.num_row_groups) if full else range(1)
    wide = [a['name'] for a in lod.get('attributes', []) if a['type'] == 'uint64']
    finest = next(name for name, child in lods.items() if lod['zooms'][-1] in child['zooms'])
    last_key = -1
    for group in groups:
        if full:
            keyed, last_key = _check_key_covering(f, group, key_column, last_key)
            _check_coverings(f, geometry, group)
        # an LOD without an attribute column of its own shows the source column's values
        for name in wide:
            values = f.read_row_group(group, columns=[name]).column(0).combine_chunks()
            if not full:
                values = values.slice(0, sample_rows)
            past = pc.greater(values, pa.scalar(int(INT64_MAX), pa.uint64())).fill_null(False)
            rows = np.flatnonzero(past.to_numpy(zero_copy_only=False))
            if len(rows):
                raise ValidationError(
                    f'row group {group}, row {rows[0]}: {name} holds a uint64 past 2^63 - 1'
                )
        for name, child in lods.items():
            values = lod_values(f, group, column, name)
            if not full:
                values = values.slice(0, sample_rows)
            shown = np.zeros(len(values), bool)
            try:
                for decoded in unpack_batches(values, child['geometry_type'], fields, dictionaries):
                    problem = _check_records(decoded, child, md.num_rows)
                    if problem:
                        record, message = problem
                        raise ValidationError(
                            f'row group {group}, row {decoded.row[record]}, lod {name}: {message}'
                        )
                    shown[decoded.row[_shown(decoded, child)]] = True
            except UnpackError as exc:
                raise ValidationError(
                    f'row group {group}, row {exc.row}, lod {name}: invalid packed row: {exc}'
                ) from exc
            if full:
                stray = np.flatnonzero(shown & ~keyed)
                if len(stray):
                    raise ValidationError(
                        f'row group {group}, row {stray[0]}, lod {name}: '
                        'a row without finite bounds is shown'
                    )
                missing = np.flatnonzero(keyed & ~shown) if name == finest else ()
                if len(missing):
                    raise ValidationError(
                        f'row group {group}, row {missing[0]}: not shown at the finest LOD {name}'
                    )
            if name in column_lods:
                bounds = pages(group, f'{column}.{name}') if pages else None
                _check_column_values(f, group, lod, name, columns, shown, bounds)
            checked[name] += len(values) - values.null_count
    if pages:
        pages.close()
    if full:
        _check_spatial_index(path, f, lod)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'lods': list(lods),
        'mode': 'full' if full else 'quick',
        'checked_non_null': checked,
    }


def _shown(u: Unpacked, child: dict) -> np.ndarray:
    """Which records are rows the LOD shows: own records with geometry."""
    if child['geometry_type'] == 'Point':
        return ~u.copy
    return ~u.copy & (np.diff(u.geometry[0]) > 0)


def _check_attribute_columns(schema: pa.Schema, lod: dict) -> list[str]:
    """Raise unless `attribute_columns` names, exactly when there are
    attributes, a struct of one required struct per listed LOD, each holding
    a nullable column per attribute in metadata order, of the type
    `column_type` gives it, and every attribute's source column is a flat
    column whose type the reader can take its values from. Returns the
    attributes' names."""
    columns = lod.get('attributes', [])
    spec = lod.get('attribute_columns')
    if not columns:
        if 'attribute_columns' in lod:
            raise ValidationError('attribute_columns without an attribute')
        return []
    if not isinstance(spec, dict) or not isinstance(spec.get('column'), str):
        raise ValidationError('attributes need attribute_columns naming their column')
    listed = spec.get('lods')
    lods = list(lod.get('lods') or {})
    if (
        not isinstance(listed, list)
        or len(set(map(str, listed))) != len(listed)
        or any(name not in lods for name in listed)
        or listed != [name for name in lods if name in listed]
    ):
        raise ValidationError('attribute_columns.lods must list declared LODs once, in order')
    name = spec['column']
    if not listed:
        if name in schema.names:
            raise ValidationError(f'{name} present though no LOD has attribute columns')
    else:
        if name not in schema.names:
            raise ValidationError(f'attribute column {name} missing')
        struct = schema.field(name)
        if struct.nullable or not pa.types.is_struct(struct.type):
            raise ValidationError(f'{name} must be a required struct')
        if [f.name for f in struct.type] != listed:
            raise ValidationError(f'{name} must have one child per LOD in attribute_columns.lods')
        for child in struct.type:
            if child.nullable or not pa.types.is_struct(child.type):
                raise ValidationError(f'{name}.{child.name} must be a required struct')
            if [f.name for f in child.type] != [a['name'] for a in columns]:
                raise ValidationError(
                    f'{name}.{child.name} must hold the attributes in metadata order'
                )
            for a, field in zip(columns, child.type, strict=True):
                expected = column_type(a)
                if not field.nullable or field.type != expected:
                    raise ValidationError(
                        f'{name}.{child.name}.{a["name"]} must be a nullable {expected} column'
                    )
    for a in columns:
        if a['name'] not in schema.names:
            raise ValidationError(f'attribute {a["name"]} has no source column')
        source = schema.field(a['name']).type
        if pa.types.is_nested(source) or not source_compatible(
            source, pa.type_for_alias(a['type'])
        ):
            raise ValidationError(
                f'attribute {a["name"]}: source column of type {source} cannot give '
                f'{a["type"]} values'
            )
    return [a['name'] for a in columns]


def _check_column_values(parquet, group, lod, name, columns, shown, bounds=None):
    """Raise unless, in the first `len(shown)` rows of row group `group`, each
    attribute column of LOD `name` holds values only for the rows `shown`
    marks, and a value table index lies in its table. `bounds` is None where
    a value is on its own row. Where values lead the LOD's pages it is the
    bounds of those pages, and the pages checked are those that end within
    the rows `shown` covers."""
    spec = lod['attribute_columns']
    holds, checked = shown, len(shown)
    if bounds is not None:
        whole = bounds[bounds <= len(shown)]
        checked = int(whole[-1])
        holds = np.zeros(checked, bool)
        holds[leading_rows(shown[:checked], whole)] = True
    for a in (a for a in lod['attributes'] if a['name'] in columns):
        path = f'{spec["column"]}.{name}.{a["name"]}'
        values = parquet.read_row_group(group, columns=[path]).column(0).combine_chunks()
        for part in path.split('.', 2)[1:]:
            values = values.field(part)
        values = values.slice(0, len(shown))
        valid = values.is_valid().to_numpy(zero_copy_only=False)
        stray = np.flatnonzero(valid[:checked] & ~holds[:checked])
        if len(stray):
            raise ValidationError(
                f'row group {group}, row {stray[0]}, lod {name}: {a["name"]} has a value for '
                'no row the LOD shows'
            )
        if 'values' in a:
            codes = values.to_numpy(zero_copy_only=False)[valid]
            if len(codes) and codes.max() >= len(a['values']):
                row = np.flatnonzero(valid)[np.argmax(codes >= len(a['values']))]
                raise ValidationError(
                    f'row group {group}, row {row}, lod {name}: {a["name"]} indexes past its '
                    'value table'
                )
        if pa.types.is_uint64(values.type):
            past = pc.greater(values, pa.scalar(int(INT64_MAX), pa.uint64())).fill_null(False)
            wide = np.flatnonzero(past.to_numpy(zero_copy_only=False))
            if len(wide):
                raise ValidationError(
                    f'row group {group}, row {wide[0]}, lod {name}: {a["name"]} holds a uint64 '
                    'past 2^63 - 1'
                )


def _check_covering(field: pa.Field, covering, geometry: str) -> None:
    """Raise unless `field` is a nullable struct of four required float32
    bounds and the geometry's `covering` declares each at its own path."""
    if not pa.types.is_struct(field.type):
        raise ValidationError(f'bbox must be a struct column, not {field.type}')
    if not field.nullable:
        raise ValidationError(
            'bbox must be nullable: a geometry without finite bounds has a null covering'
        )
    names = [child.name for child in field.type]
    if sorted(names) != sorted(BOUNDS):
        raise ValidationError(
            f'bbox fields must be xmin, ymin, xmax and ymax, not {", ".join(names)}'
        )
    for child in field.type:
        if child.type != pa.float32():
            raise ValidationError(f'bbox.{child.name} must be float32, not {child.type}')
        if child.nullable:
            raise ValidationError(f'bbox.{child.name} must be required, not nullable')
    where = f'geo.columns.{geometry}.covering.bbox'
    declared = covering.get('bbox') if isinstance(covering, dict) else None
    if not isinstance(declared, dict) or sorted(declared) != sorted(BOUNDS):
        raise ValidationError(
            f'{where} must declare xmin, ymin, xmax and ymax, not {json.dumps(declared)}'
        )
    for bound in BOUNDS:
        if declared[bound] != ['bbox', bound]:
            raise ValidationError(
                f'{where}.{bound} must be ["bbox", "{bound}"], not {json.dumps(declared[bound])}'
            )


def _check_attributes(attributes, rows: int | None = None) -> None:
    """Raise unless every attribute has a distinct name, the Arrow name of a
    type an attribute can have, a value table only when it is a string, and a
    summary, where it has one, of the shape its type takes in a file of
    `rows` rows."""
    if not isinstance(attributes, list) or not all(isinstance(a, dict) for a in attributes):
        raise ValidationError('attributes must be a list of {name, type} objects')
    if len(attributes) > MAX_ATTRIBUTES:
        raise ValidationError(f'{len(attributes)} attributes, more than {MAX_ATTRIBUTES}')
    for a in attributes:
        name, kind = a.get('name'), a.get('type')
        if not isinstance(name, str):
            raise ValidationError(f'attribute {json.dumps(a)} has no name')
        try:
            t = pa.type_for_alias(kind) if isinstance(kind, str) else None
        except ValueError:
            t = None
        if t is None or not is_attribute_type(t):
            raise ValidationError(
                f'attribute {name}: type {json.dumps(kind)} is not an integer, float, bool '
                'or string type'
            )
        values = a.get('values')
        if 'values' in a and (
            not (pa.types.is_string(t) or pa.types.is_large_string(t))
            or not isinstance(values, list)
            or not all(isinstance(v, str) for v in values)
            or not 0 < len(values) <= 256
        ):
            raise ValidationError(f'attribute {name}: values must be a table of 1 to 256 strings')
        if 'summary' in a:
            _check_summary(name, t, values, a['summary'], rows)
    names = [a['name'] for a in attributes]
    if len(set(names)) != len(names):
        raise ValidationError('attribute names must be distinct')


def _check_summary(name: str, t: pa.DataType, values, summary, rows: int | None) -> None:
    """Raise unless an attribute's `summary` holds a `count` of rows; on a
    number, `min`, `max` and ascending `quantiles` between them, values its
    type holds, all three or none and all three where an integer has a
    value, and on no other type; and `counts` adding up to `count`, one for
    each entry of a string's value table or two for a boolean, on those and
    no other."""

    def whole(v) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0

    def held(v) -> bool:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return False
        if pa.types.is_integer(t):
            low, high = np.iinfo(t.to_pandas_dtype()).min, np.iinfo(t.to_pandas_dtype()).max
            return isinstance(v, int) and low <= v <= high
        with np.errstate(over='ignore'):
            return float(np.dtype(t.to_pandas_dtype()).type(v)) == v

    def fail(why: str):
        raise ValidationError(f'attribute {name}: summary {why}')

    if not isinstance(summary, dict):
        fail('must be an object')
    count = summary.get('count')
    if not whole(count) or (rows is not None and count > rows):
        fail('count must be a number of the rows')
    number = pa.types.is_integer(t) or pa.types.is_floating(t)
    spread = [key for key in ('min', 'max', 'quantiles') if key in summary]
    if spread:
        quantiles = summary.get('quantiles')
        steps = [
            summary.get('min'),
            *(quantiles if isinstance(quantiles, list) else []),
            summary.get('max'),
        ]
        if (
            not number
            or len(spread) != 3
            or count == 0
            or len(steps) != len(SUMMARY_QUANTILES) + 2
            or not all(held(v) for v in steps)
            or any(a > b for a, b in itertools.pairwise(steps))
        ):
            fail(
                f'must hold min, {len(SUMMARY_QUANTILES)} ascending quantiles and max together, '
                'values of its type, on a number with a value'
            )
    elif pa.types.is_integer(t) and count:
        fail('must hold min, max and quantiles on an integer with a value')
    slots = len(values) if values is not None else 2 if pa.types.is_boolean(t) else None
    if slots is None:
        if 'counts' in summary:
            fail('counts go with a value table or a boolean')
    else:
        counts = summary.get('counts')
        if (
            not isinstance(counts, list)
            or len(counts) != slots
            or not all(whole(v) for v in counts)
            or sum(counts) != count
        ):
            fail(f'counts must be {slots} numbers of rows adding up to count')


def _check_coverings(
    parquet: pq.ParquetFile, geometry: str, group: int, rows: int | None = None
) -> None:
    """Raise unless, in the first `rows` rows of row group `group`, or all of
    them, each row's covering is its primary geometry's bounds rounded
    outward to float32, and null exactly where those bounds are not all
    finite. Reads COVERING_BATCH_ROWS rows at a time."""
    if rows == 0:
        return
    size = COVERING_BATCH_ROWS if rows is None else min(rows, COVERING_BATCH_ROWS)
    start = 0
    for batch in parquet.iter_batches(size, row_groups=[group], columns=['bbox', geometry]):
        if rows is not None:
            batch = batch.slice(0, rows - start)
        wkb = batch.column(geometry)
        null = wkb.is_null().to_numpy(zero_copy_only=False)
        shapes = shapely.from_wkb(wkb.to_numpy(zero_copy_only=False), on_invalid='ignore')
        invalid = np.flatnonzero(shapely.is_missing(shapes) & ~null)
        if len(invalid):
            row = start + invalid[0]
            raise ValidationError(
                f'row group {group}, row {row}: primary geometry is not valid WKB'
            )
        bounds = _bounds(shapes)
        present = np.isfinite(bounds).all(axis=1)
        bbox = batch.column('bbox')
        covered = bbox.is_valid().to_numpy(zero_copy_only=False)
        wrong = np.flatnonzero(covered != present)
        if len(wrong):
            row = wrong[0]
            if present[row]:
                message = 'null bbox covering on a geometry with finite bounds'
            elif null[row]:
                message = 'bbox covering on a null geometry'
            elif shapely.is_empty(shapes[row]):
                message = 'bbox covering on an empty geometry'
            else:
                message = 'bbox covering on a geometry without finite bounds'
            raise ValidationError(f'row group {group}, row {start + row}: {message}')
        stored = np.column_stack([bbox.field(k).to_numpy(zero_copy_only=False) for k in BOUNDS])
        exact = bounds[present]
        expected = _outward(exact)
        stored = stored[present]
        differs = stored != expected
        bad = np.flatnonzero(differs.any(axis=1))
        if len(bad):
            i = bad[0]
            k = int(np.argmax(differs[i]))
            row = start + np.flatnonzero(present)[i]
            raise ValidationError(
                f'row group {group}, row {row}: bbox.{BOUNDS[k]} is {float(stored[i, k])!r}, '
                f"not its geometry's {float(exact[i, k])!r} rounded outward to float32, "
                f'{float(expected[i, k])!r}'
            )
        start += len(batch)
        if rows is not None and start >= rows:
            break


def _bounds(shapes: np.ndarray) -> np.ndarray:
    """Rows of each geometry's `xmin, ymin, xmax, ymax`: the least and
    greatest x and y of its coordinates, NaN values left out one axis at a
    time, and NaN for an axis with none."""
    coords, index = shapely.get_coordinates(shapes, return_index=True)
    out = np.full((len(shapes), 4), np.nan)
    if len(coords):
        starts = np.flatnonzero(np.r_[True, index[1:] != index[:-1]])
        for k, (reduce, axis) in enumerate(
            ((np.fmin, 0), (np.fmin, 1), (np.fmax, 0), (np.fmax, 1))
        ):
            out[index[starts], k] = reduce.reduceat(coords[:, axis], starts)
    return out


def _outward(bounds: np.ndarray) -> np.ndarray:
    """Rows of float64 `xmin, ymin, xmax, ymax` rounded outward to float32:
    each minimum to the greatest float32 at most it, each maximum to the
    least float32 at least it."""
    rounded = bounds.astype(np.float32)
    outward = np.array([-np.inf, -np.inf, np.inf, np.inf], np.float32)
    past = np.where(outward < 0, rounded > bounds, rounded < bounds)
    return np.where(past, np.nextafter(rounded, outward), rounded)


def _check_declared(lod: dict) -> None:
    """The required fields of `lod_columns` that locate nothing in the file."""
    if lod.get('order') != ORDER:
        raise ValidationError(f'order {lod.get("order")!r} is not {ORDER}')
    size = lod.get('tile_size')
    if type(size) is not int or size != TILE_SIZE:
        raise ValidationError(f'tile_size {size!r} is not {TILE_SIZE}')
    if not isinstance(lod.get('thinning'), dict):
        raise ValidationError('thinning must be an object describing the rule')
    if not isinstance(lod.get('attributes'), list):
        raise ValidationError('attributes must be a list, empty without attributes')


def _check_key_covering(parquet: pq.ParquetFile, group: int, key_column: str, last_key: int):
    """Raise unless, in row group `group`, the covering and the Hilbert key
    are null on the same rows, those whose geometry has no finite bounds, and
    the keys ascend from `last_key`, the greatest of the row groups before,
    with the null keys after them. Returns which rows have a key, and the
    greatest key so far."""
    table = parquet.read_row_group(group, columns=['bbox', key_column])
    covered = table.column('bbox').is_valid().to_numpy(zero_copy_only=False)
    keys = table.column(key_column).combine_chunks()
    keyed = keys.is_valid().to_numpy(zero_copy_only=False)
    wrong = np.flatnonzero(covered != keyed)
    if len(wrong):
        row = wrong[0]
        if keyed[row]:
            message = 'null bbox covering on a row with a Hilbert key'
        else:
            message = 'bbox covering on a row with a null Hilbert key'
        raise ValidationError(f'row group {group}, row {row}: {message}')
    count = int(keyed.sum())
    if not keyed[:count].all() or (last_key == NULL_KEYS and count):
        row = int(np.flatnonzero(keyed)[-1]) if last_key != NULL_KEYS else 0
        raise ValidationError(f'row group {group}, row {row}: a Hilbert key after a null key')
    values = keys.drop_null().to_numpy()
    if count and (values[0] < last_key or (np.diff(values) < 0).any()):
        row = 0 if values[0] < last_key else int(np.flatnonzero(np.diff(values) < 0)[0]) + 1
        raise ValidationError(f'row group {group}, row {row}: Hilbert keys do not ascend')
    if count < len(keyed):
        return keyed, NULL_KEYS
    return keyed, int(values[-1]) if count else last_key


def _check_root_version(path) -> None:
    """Raise unless the index root at the head of the file states the
    version its footer advertises."""
    try:
        with open(path, 'rb') as source:
            if _root(source) is None:
                raise ValueError('spatial index advertised but bootstrap magic missing')
    except Exception as exc:
        raise ValidationError(f'spatial index: {exc}') from exc


def _check_root_copy(path, lod: dict) -> None:
    """Raise unless the index root holds a copy of the footer's LOD metadata,
    which a map reads in the footer's place."""
    from ._validate_index import _copies

    with open(path, 'rb') as source:
        root = _root(source)
    if not _copies(root.get('lod'), lod):
        raise ValidationError('spatial index: spatial root metadata disagrees with footer')


def _check_spatial_index(path, parquet, lod):
    from ._validate_index import check_index

    try:
        check_index(path, parquet, lod)
    except Exception as exc:
        raise ValidationError(f'spatial index: {exc}') from exc


def _check_records(u: Unpacked, child: dict, num_rows: int):
    """The first record breaking the geometry or copy rules, and how, or None."""
    found = []
    if child['geometry_type'] == 'Point':
        x, y = u.geometry
    else:
        found += _check_geometry(u, child['geometry_type'])
        x, y = u.geometry[3:]
    outside = np.flatnonzero((np.minimum(x, y) < INT32.min) | (np.maximum(x, y) > INT32.max))
    if len(outside):
        found.append((_record_of_vertex(u, outside[0]), 'a grid coordinate outside int32'))
    zoom = (u.tile >> np.uint64(48)).astype(np.int64)
    cell = u.tile & np.uint64((1 << 48) - 1)
    cells = np.uint64(1) << (2 * np.minimum(zoom, 24)).astype(np.uint64)
    tag = u.copy & (~np.isin(zoom, child['zooms']) | (cell >= cells))
    found += [(i, 'invalid spill tile tag') for i in np.flatnonzero(tag)[:1]]
    rows = u.copy & (u.id >= num_rows)
    found += [(i, 'spill row id out of range') for i in np.flatnonzero(rows)[:1]]
    return min(found, default=None)


def _check_geometry(u: Unpacked, gtype: str) -> list:
    """Empty geometry on a record that needs some, empty polygons, and rings
    and parts too short. A polygon ring's closing vertex is implicit, so it
    stores three vertices or more and decodes with one more."""
    geom, poly, ring = u.geometry[:3]
    found = []
    # a row shown only to host straddler copies has no geometry of its own
    hosts = np.append(u.copy[1:], False) & ~u.copy
    empty = np.flatnonzero((np.diff(geom) == 0) & ~hosts)
    if len(empty):
        what = 'a copy with empty geometry' if u.copy[empty[0]] else 'empty geometry on a shown row'
        found.append((empty[0], what))
    vertices = np.diff(ring)
    if gtype == 'MultiPolygon':
        polygons = np.flatnonzero(np.diff(poly) == 0)
        if len(polygons):
            found.append((_parent(geom, polygons[0]), 'empty polygon'))
        short = np.flatnonzero(vertices < 4)
        what = 'polygon ring with fewer than three vertices'
    else:
        short = np.flatnonzero(vertices < 2)
        what = 'line with fewer than two vertices'
    if len(short):
        found.append((_parent(geom, _parent(poly, short[0])), what))
    return found


def _parent(offsets: np.ndarray, child: int) -> int:
    return int(np.searchsorted(offsets, child, side='right') - 1)


def _record_of_vertex(u: Unpacked, vertex: int) -> int:
    if len(u.geometry) == 2:
        return int(vertex)
    geom, poly, ring = u.geometry[:3]
    return _parent(geom, _parent(poly, _parent(ring, vertex)))


def inspect(path: str | Path) -> dict:
    """Metadata, per-leaf column sizes in the first row group, and under
    `tile_sizes` what `tile_sizes(path)` reports for each zoom, None for a
    file without the spatial index."""
    f = pq.ParquetFile(path)
    md = f.metadata
    geo = json.loads(dict(f.schema_arrow.metadata or {}).get(b'geo', b'{}') or b'{}')
    lod = geo.get(LOD_KEY, {})
    g = 0
    rg = md.row_group(g)
    sizes = {}
    for i in range(rg.num_columns):
        c = rg.column(i)
        sizes[c.path_in_schema] = int(c.total_compressed_size)
    return {
        'rows': md.num_rows,
        'row_groups': md.num_row_groups,
        'footer_bytes': md.serialized_size,
        'lod_columns': lod,
        'row_group_sampled': g,
        'compressed_bytes_by_column': sizes,
        'rows_in_sampled_group': rg.num_rows,
        'tile_sizes': tile_sizes(path),
    }


__all__ = ['ValidationError', 'inspect', 'validate']
