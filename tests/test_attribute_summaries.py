"""The summary a file's metadata holds of each attribute over the rows a map shows."""

import json
import math

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from tiled_geoparquet import validate, write
from tiled_geoparquet._tile_sizes import _root
from tiled_geoparquet.encoding import SUMMARY_QUANTILES
from tiled_geoparquet.validate import ValidationError, _check_attributes
from tiled_geoparquet.writer import _summaries

OPTIONS = dict(zooms=range(10, 13), complete_from=12, workers=1, log=lambda *_: None)
COUNT = 500
HEIGHTS = [None if i % 5 == 0 else 2.4 + (i % 37) * 0.7 for i in range(COUNT)]
HEIGHTS[1], HEIGHTS[2], HEIGHTS[3] = math.nan, math.inf, 634.0
FLOORS = [None if i % 3 == 0 else 1 + (i * 7) % 11 for i in range(COUNT)]
KINDS = [None if i % 4 == 0 else ('house', 'shop', 'school')[i % 3] for i in range(COUNT)]
FLAGS = [None if i % 10 == 0 else i % 7 == 0 for i in range(COUNT)]
# a row without a geometry, which no LOD shows and no summary counts
UNSHOWN = {'height': 1000.0, 'floors': 99, 'kind': 'ghost', 'label': 'ghost', 'flag': True}


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    geometry = [
        shapely.box(-105 + i * 0.001, 39.7, -104.9995 + i * 0.001, 39.7005) for i in range(COUNT)
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {
            'height': pa.array([*HEIGHTS, UNSHOWN['height']], pa.float64()),
            'floors': pa.array([*FLOORS, UNSHOWN['floors']], pa.int32()),
            'kind': [*KINDS, UNSHOWN['kind']],
            'label': [*(f'building-{i}' for i in range(COUNT)), UNSHOWN['label']],
            'flag': pa.array([*FLAGS, UNSHOWN['flag']], pa.bool_()),
            'empty': pa.array([None] * (COUNT + 1), pa.float64()),
            'geometry': pa.array([*shapely.to_wkb(geometry), None], pa.binary()),
        }
    )
    folder = tmp_path_factory.mktemp('summaries')
    pq.write_table(
        table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), folder / 'source.parquet'
    )
    return write(
        folder / 'source.parquet',
        folder / 'out.parquet',
        attributes=[('height', 'float'), 'floors', 'kind', 'label', 'flag', 'empty'],
        **OPTIONS,
    )


def _summary(path):
    lod = json.loads(pq.read_schema(path).metadata[b'geo'])['lod_columns']
    return {a['name']: a['summary'] for a in lod['attributes']}


def _quantiles(values):
    """The least value that at least each share of `values` does not exceed."""
    ordered = sorted(values)
    return [ordered[max(0, math.ceil(q * len(ordered)) - 1)] for q in SUMMARY_QUANTILES]


def test_a_number_is_summarised_over_its_finite_values_as_its_type_holds_them(built):
    finite = [v for v in HEIGHTS if v is not None and math.isfinite(v)]
    held = [float(np.float32(v)) for v in [min(finite), max(finite), *_quantiles(finite)]]
    assert _summary(built)['height'] == {
        'count': sum(v is not None for v in HEIGHTS),
        'min': held[0],
        'max': held[1],
        'quantiles': held[2:],
    }
    assert held[0] == 2.4000000953674316 and held[1] == 634.0

    floors = [v for v in FLOORS if v is not None]
    assert _summary(built)['floors'] == {
        'count': len(floors),
        'min': 1,
        'max': 11,
        'quantiles': _quantiles(floors),
    }


def test_a_value_table_and_a_boolean_are_summarised_by_the_rows_of_each_value(built):
    lod = json.loads(pq.read_schema(built).metadata[b'geo'])['lod_columns']
    kind = next(a for a in lod['attributes'] if a['name'] == 'kind')
    assert kind['values'] == ['ghost', 'house', 'school', 'shop']
    assert kind['summary'] == {
        'count': sum(v is not None for v in KINDS),
        'counts': [KINDS.count(v) for v in kind['values']],
    }
    assert _summary(built)['flag'] == {
        'count': sum(v is not None for v in FLAGS),
        'counts': [FLAGS.count(False), FLAGS.count(True)],
    }


def test_a_plain_string_and_a_column_without_values_hold_only_a_count(built):
    assert _summary(built)['label'] == {'count': COUNT}
    assert _summary(built)['empty'] == {'count': 0}


def test_the_index_root_holds_the_summaries_and_the_file_validates(built):
    validate(built, full=True)
    with open(built, 'rb') as source:
        root = _root(source)
    assert {a['name']: a['summary'] for a in root['lod']['attributes']} == _summary(built)


def test_a_value_only_a_thinning_gives_counts_no_rows(tmp_path):
    path = tmp_path / 'column.parquet'
    pq.write_table(pa.table({'kind': ['house', 'shop', 'house', None]}), path)
    with duckdb.connect() as con:
        summaries = _summaries(
            con, f"'{path}'", [pa.field('kind', pa.string())], {'kind': ['house', 'merged', 'shop']}
        )
    assert summaries == {'kind': {'count': 3, 'counts': [2, 0, 1]}}


QUANTILES = [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    'attribute',
    [
        {'name': 'a', 'type': 'double', 'summary': []},
        {'name': 'a', 'type': 'double', 'summary': {}},
        {'name': 'a', 'type': 'double', 'summary': {'count': -1}},
        {'name': 'a', 'type': 'double', 'summary': {'count': 11}},
        {'name': 'a', 'type': 'double', 'summary': {'count': 4, 'min': 1, 'max': 5}},
        {
            'name': 'a',
            'type': 'double',
            'summary': {'count': 4, 'min': 2, 'max': 5, 'quantiles': QUANTILES},
        },
        {
            'name': 'a',
            'type': 'double',
            'summary': {'count': 4, 'min': 1, 'max': 5, 'quantiles': QUANTILES[:4]},
        },
        {
            'name': 'a',
            'type': 'double',
            'summary': {'count': 0, 'min': 1, 'max': 5, 'quantiles': QUANTILES},
        },
        {
            'name': 'a',
            'type': 'string',
            'summary': {'count': 4, 'min': 1, 'max': 5, 'quantiles': QUANTILES},
        },
        {'name': 'a', 'type': 'double', 'summary': {'count': 4, 'counts': [4]}},
        {'name': 'a', 'type': 'double', 'summary': {'count': 4, 'counts': None}},
        {'name': 'a', 'type': 'int32', 'summary': {'count': 4}},
        {
            'name': 'a',
            'type': 'int8',
            'summary': {'count': 4, 'min': 1, 'max': 300, 'quantiles': QUANTILES},
        },
        {
            'name': 'a',
            'type': 'int32',
            'summary': {'count': 4, 'min': 0.5, 'max': 5, 'quantiles': QUANTILES},
        },
        {
            'name': 'a',
            'type': 'float',
            'summary': {'count': 4, 'min': 0.1, 'max': 5, 'quantiles': QUANTILES},
        },
        {'name': 'a', 'type': 'bool', 'summary': {'count': 4}},
        {'name': 'a', 'type': 'bool', 'summary': {'count': 4, 'counts': [1, 2]}},
        {
            'name': 'a',
            'type': 'string',
            'values': ['x', 'y'],
            'summary': {'count': 4, 'counts': [4]},
        },
    ],
)
def test_a_malformed_summary_is_refused(attribute):
    with pytest.raises(ValidationError, match='attribute a: summary'):
        _check_attributes([attribute], rows=10)


def test_well_formed_summaries_and_attributes_without_one_pass():
    _check_attributes(
        [
            {'name': 'a', 'type': 'double'},
            {'name': 'b', 'type': 'double', 'summary': {'count': 0}},
            {
                'name': 'c',
                'type': 'int32',
                'summary': {'count': 4, 'min': 1, 'max': 5, 'quantiles': QUANTILES},
            },
            {'name': 'd', 'type': 'bool', 'summary': {'count': 4, 'counts': [1, 3]}},
            {
                'name': 'e',
                'type': 'string',
                'values': ['x', 'y'],
                'summary': {'count': 4, 'counts': [4, 0]},
            },
            {'name': 'f', 'type': 'string', 'summary': {'count': 4}},
            {'name': 'g', 'type': 'int32', 'summary': {'count': 0}},
            {'name': 'h', 'type': 'double', 'summary': {'count': 4}},
            {
                'name': 'i',
                'type': 'halffloat',
                'summary': {'count': 4, 'min': 0.5, 'max': 5, 'quantiles': QUANTILES},
            },
        ],
        rows=10,
    )
