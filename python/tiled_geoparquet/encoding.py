"""Arrow shape of an LOD column.

Each LOD is a nullable `binary` child of the `lod` struct: null means the row
is not shown there, and a value is the row packed as `pack` describes, so a
tile reads one leaf per LOD. Attributes are inside it only on boundary
copies; a row's own value is in an attribute column or, at an LOD without
one, the source column.
"""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa

LOD_VERSION = '0.2.0'
ENCODING = 'packed'
PACK_VERSION = 3
ORDER = 'hilbert_webmercator'
# the pixels of a tile that zooms, margins and placeholder sizes are counted in
TILE_SIZE = 512
# a copy's tile tag holds x * 2^zoom + y in 48 bits
MAX_ZOOM = 24
# keys of a deeper curve are not exact JavaScript integers
INDEX_KEY_DEPTH = 26
# the spatial index profile version, which the footer's `lod_spatial_index` and the root state
INDEX_VERSION = 4
# a copy's presence bitmap is one 64-bit varint, a bit per attribute
MAX_ATTRIBUTES = 64
# the probabilities of the quantiles an attribute's summary holds
SUMMARY_QUANTILES = (0.02, 0.25, 0.5, 0.75, 0.98)
# the columns `read_overview` returns beside the attributes
OVERVIEW_COLUMNS = ('row', 'geometry')
GEOMETRY_TYPES = {'point': 'Point', 'line': 'MultiLineString', 'polygon': 'MultiPolygon'}


def is_compatible_version(version) -> bool:
    """Whether `lod_columns` metadata of `version` is this format: the same
    major version, and the same minor while the major is 0."""
    major, minor = (str(version).split('.') + [''])[:2]
    ours = LOD_VERSION.split('.')
    return major == ours[0] and (major != '0' or minor == ours[1])


def check_format(lod: dict) -> None:
    """Raise ValueError unless `lod`, a file's `lod_columns` metadata, is the
    version and packing this package reads."""
    if not is_compatible_version(lod.get('version')):
        raise ValueError(
            f'lod_columns version {lod.get("version")} is not compatible with {LOD_VERSION}'
        )
    if lod.get('encoding') != ENCODING or lod.get('pack_version') != PACK_VERSION:
        raise ValueError(
            f'lod_columns encoding {lod.get("encoding")}/{lod.get("pack_version")} is not '
            f'{ENCODING}/{PACK_VERSION}'
        )


def _run(zooms) -> bool:
    """Whether `zooms` is a non-empty list of consecutive ascending integers."""
    return (
        isinstance(zooms, list)
        and bool(zooms)
        and all(type(z) is int for z in zooms)
        and zooms == list(range(zooms[0], zooms[0] + len(zooms)))
    )


def check_zooms(lod: dict) -> None:
    """Raise ValueError unless `lod`, a file's `lod_columns` metadata, serves a
    contiguous run of zooms within the key's depth and MAX_ZOOM, each zoom in
    exactly one LOD."""
    key, lods = lod.get('key'), lod.get('lods')
    depth = key.get('depth') if isinstance(key, dict) else None
    if type(depth) is not int or depth < 0:
        raise ValueError(f'key depth {depth!r} is not a non-negative integer')
    if not isinstance(lods, dict) or not all(isinstance(c, dict) for c in lods.values()):
        raise ValueError('lods must map each LOD name to an object')
    finest = min(depth, MAX_ZOOM)
    zooms = lod.get('zooms')
    if not (_run(zooms) and zooms[0] >= 0 and zooms[-1] <= finest):
        raise ValueError(f'zooms must be contiguous and ascending within 0..{finest}')
    served = []
    for name, child in lods.items():
        if not _run(child.get('zooms')):
            raise ValueError(f'lod {name}: zooms must be contiguous and ascending')
        served += child['zooms']
    if sorted(served) != zooms:
        raise ValueError('every zoom in zooms must be in exactly one LOD')


def check_index_version(parquet) -> bool:
    """Whether the Parquet file `parquet` advertises the spatial index in its
    footer. Raise ValueError when it advertises a version other than
    INDEX_VERSION, whose pages this package does not read."""
    advertised = (parquet.metadata.metadata or {}).get(b'lod_spatial_index')
    if advertised is None:
        return False
    try:
        version = json.loads(advertised).get('version')
    except (ValueError, AttributeError):
        version = None
    if type(version) is not int or version != INDEX_VERSION:
        raise ValueError(f'spatial index version {version} is not {INDEX_VERSION}')
    return True


def lod_type() -> pa.DataType:
    return pa.binary()


def is_attribute_type(t: pa.DataType) -> bool:
    """Whether an attribute can have type `t`: integers, floats, booleans and
    strings."""
    return (
        pa.types.is_integer(t)
        or pa.types.is_floating(t)
        or pa.types.is_boolean(t)
        or pa.types.is_string(t)
        or pa.types.is_large_string(t)
    )


def infer_attributes(schema: pa.Schema, attributes) -> list[pa.Field]:
    """`attributes` is a list of column names or (name, Arrow type alias)
    pairs. Only scalar types are kept: integers, floats, booleans and strings."""
    out = []
    for item in attributes:
        if isinstance(item, pa.Field):
            out.append(pa.field(item.name, item.type, nullable=True))
        elif isinstance(item, tuple):
            name, alias = item
            out.append(pa.field(name, pa.type_for_alias(alias), nullable=True))
        else:
            out.append(pa.field(item, schema.field(item).type, nullable=True))
    if len(out) > MAX_ATTRIBUTES:
        raise ValueError(f'cannot keep {len(out)} attributes: at most {MAX_ATTRIBUTES}')
    for f in out:
        if not is_attribute_type(f.type):
            raise ValueError(
                f'cannot keep {f.name}: {f.type} is not an integer, float, bool or string'
            )
    return out


def column_type(attribute: dict) -> pa.DataType:
    """The Arrow type of an attribute's attribute columns, from its metadata
    entry: the declared type, a string with a value table as the uint8 index
    into it, and a large string as a string."""
    if 'values' in attribute:
        return pa.uint8()
    t = pa.type_for_alias(attribute['type'])
    return pa.string() if pa.types.is_large_string(t) else t


def source_compatible(source: pa.DataType, declared: pa.DataType) -> bool:
    """Whether a source column of type `source` can serve an attribute
    declared `declared` where an LOD reads the source column: the reader
    rounds a number to a declared float type, and takes anything else as it
    is, so the kinds must agree."""
    if pa.types.is_floating(declared):
        return pa.types.is_integer(source) or pa.types.is_floating(source)
    if pa.types.is_integer(declared):
        return pa.types.is_integer(source)
    if pa.types.is_boolean(declared):
        return pa.types.is_boolean(source)
    return pa.types.is_string(source) or pa.types.is_large_string(source)


INT32 = np.iinfo(np.int32)


def round_away(v: np.ndarray) -> np.ndarray:
    """Round half away from zero, as the encoding requires. Grid coordinates
    are int32, so a value outside it is an error rather than a wrapped one."""
    v = np.nan_to_num(np.asarray(v, dtype=np.float64))
    r = np.sign(v) * np.floor(np.abs(v) + 0.5)
    if r.size and (r.min() < INT32.min or r.max() > INT32.max):
        raise OverflowError('grid coordinate outside int32: the extent is too wide for the zoom')
    return r.astype(np.int32)


def point_parts(cx: np.ndarray, cy: np.ndarray, scale: float, offset) -> tuple:
    """Grid coordinates of points, as `pack` takes them."""
    return (round_away((cx - offset[0]) / scale), round_away((cy - offset[1]) / scale))
