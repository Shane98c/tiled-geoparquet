"""Arrow shape of an LOD column.

Each LOD is a nullable `binary` child of the `lod` struct: null means the row
is not shown there, and a value is the row packed as `pack` describes, so a
tile reads one leaf per LOD. Attributes are inside it only on boundary
copies; a row's own value is in an attribute column or, at an LOD without
one, the source column.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

LOD_VERSION = '0.5.0'
ENCODING = 'packed'
PACK_VERSION = 3
# a copy's presence bitmap is one 64-bit varint, a bit per attribute
MAX_ATTRIBUTES = 64
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
