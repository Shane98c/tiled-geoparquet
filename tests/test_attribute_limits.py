"""A copy's presence bitmap holds 64 attributes."""

import numpy as np
import pyarrow as pa
import pytest
from tiled_geoparquet.encoding import MAX_ATTRIBUTES, infer_attributes
from tiled_geoparquet.pack import pack
from tiled_geoparquet.validate import ValidationError, _check_attributes
from unpacker import Unpacker


def _fields(count):
    return [pa.field(f'a{i}', pa.int32()) for i in range(count)]


def test_the_writer_refuses_more_attributes_than_the_bitmap_holds():
    schema = pa.schema(_fields(MAX_ATTRIBUTES + 1))
    assert len(infer_attributes(schema, schema.names[:MAX_ATTRIBUTES])) == MAX_ATTRIBUTES
    with pytest.raises(ValueError, match='at most 64'):
        infer_attributes(schema, schema.names)


def test_the_validator_refuses_more_attributes_than_the_bitmap_holds():
    described = [{'name': f.name, 'type': str(f.type)} for f in _fields(MAX_ATTRIBUTES + 1)]
    _check_attributes(described[:MAX_ATTRIBUTES])
    with pytest.raises(ValidationError, match='more than 64'):
        _check_attributes(described)


def test_the_last_attribute_of_a_full_bitmap_packs_and_unpacks():
    fields = _fields(MAX_ATTRIBUTES)
    values = {f.name: pa.array([None, None], pa.int32()) for f in fields}
    values['a0'] = pa.array([7, None], pa.int32())
    values['a63'] = pa.array([None, -9], pa.int32())
    point = (np.array([3]), np.array([5]))
    copies = (np.array([2]), np.array([1, 2]), np.array([10, 11]), (np.array([4, 6]),) * 2, values)
    packed = pack('Point', point, fields, copies)
    first, second = (
        copy['attrs'] for copy in Unpacker(packed[0].as_py(), 'Point', fields).row()['spill']
    )
    assert first['a0'] == 7 and first['a63'] is None
    assert second['a0'] is None and second['a63'] == -9
