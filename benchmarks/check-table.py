"""Compare all table values of a writer output and its optimized copy.

Every column holds the same value on the same row, except the attribute
columns: the optimized copy holds a page's attribute values on the page's
leading rows, so each of those is compared as its values in order.
"""

import sys
import time

import numpy as np
import pyarrow.parquet as pq

ATTRIBUTES = 'lod_attributes'


def leaves(column):
    """The non-null values of each attribute of each LOD, in row order."""
    lods = column.combine_chunks()
    for lod, values in zip(lods.type, lods.flatten(), strict=True):
        for attribute, leaf in zip(values.type, values.flatten(), strict=True):
            yield f'{lod.name}.{attribute.name}', leaf.drop_null()


if __name__ == '__main__':
    a, b = [pq.ParquetFile(path) for path in sys.argv[1:3]]
    started = time.monotonic()
    assert a.metadata.num_row_groups == b.metadata.num_row_groups
    assert a.schema_arrow.equals(b.schema_arrow, check_metadata=True)
    moved = ['bbox'] + [name for name in a.schema_arrow.names if name == ATTRIBUTES]
    rows = values = 0
    for group in range(a.metadata.num_row_groups):
        x, y = a.read_row_group(group), b.read_row_group(group)
        assert x.drop(moved).equals(y.drop(moved)), group
        for coordinate in ['xmin', 'ymin', 'xmax', 'ymax']:
            np.testing.assert_array_equal(
                x['bbox'].combine_chunks().field(coordinate).to_numpy(),
                y['bbox'].combine_chunks().field(coordinate).to_numpy(),
            )
        if ATTRIBUTES in moved:
            pairs = zip(leaves(x[ATTRIBUTES]), leaves(y[ATTRIBUTES]), strict=True)
            for (name, written), (_, optimized) in pairs:
                assert written.equals(optimized), (group, name)
                values += len(written)
        rows += x.num_rows
    print(
        f'All {rows:,} rows, every column identical, {values:,} attribute values '
        f'identical in order; {time.monotonic() - started:.1f}s'
    )
