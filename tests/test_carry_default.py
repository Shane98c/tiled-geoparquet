"""The attributes a file keeps when the caller names none, or only packed ones."""

import importlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from tiled_geoparquet import read_overview, validate, write
from tiled_geoparquet.cli import cli

OPTIONS = dict(zooms=range(10, 13), complete_from=12, workers=1, log=lambda *_: None)


@pytest.fixture(scope='module')
def source(tmp_path_factory):
    count = 400
    geometry = [
        shapely.box(-105 + i * 0.001, 39.7, -104.9995 + i * 0.001, 39.7005) for i in range(count)
    ]
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {
            'id': [f'building-{i}' for i in range(count)],
            'height': pa.array([3.0 + i % 40 for i in range(count)], pa.float64()),
            'kind': [('house', 'shop', 'school')[i % 3] for i in range(count)],
            'row': pa.array([float(i % 7) for i in range(count)], pa.float64()),
            'geometry': shapely.to_wkb(geometry),
        }
    )
    path = tmp_path_factory.mktemp('default') / 'source.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), path)
    return path


def _attributes(path):
    lod = json.loads(pq.read_schema(path).metadata[b'geo'])['lod_columns']
    return [(a['name'], a.get('storage', 'packed')) for a in lod['attributes']]


def test_every_cheap_column_is_a_column_attribute_by_default(source, tmp_path):
    out = write(source, tmp_path / 'out.parquet', **OPTIONS)
    validate(out, full=True)
    assert _attributes(out) == [('height', 'column'), ('kind', 'column')]


def test_a_column_named_as_read_overview_names_its_own_is_left_out(source, tmp_path):
    out = write(source, tmp_path / 'out.parquet', **OPTIONS)
    overview = read_overview(out, 12)
    assert overview.num_rows == 400
    assert overview.column_names == ['row', 'geometry', 'height', 'kind']
    assert overview.column('row').to_pylist() == list(range(400))


def test_carry_packs_what_it_names_and_the_rest_stay_columns(source, tmp_path):
    out = write(source, tmp_path / 'out.parquet', carry=['height'], **OPTIONS)
    validate(out, full=True)
    assert _attributes(out) == [('height', 'packed'), ('kind', 'column')]


def test_no_column_attributes_when_none_are_asked_for(source, tmp_path):
    out = write(source, tmp_path / 'out.parquet', carry_columns=(), **OPTIONS)
    assert _attributes(out) == []
    assert 'lod_attributes' not in pq.read_schema(out).names


def test_the_cli_keeps_cheap_columns_unless_told_none(source, tmp_path, monkeypatch):
    given = {}
    monkeypatch.setattr(
        importlib.import_module('tiled_geoparquet.cli'),
        'write',
        lambda src, out, **options: given.update(options),
    )
    runner = CliRunner()
    args = ['write', str(source), str(tmp_path / 'out.parquet')]
    assert runner.invoke(cli, args).exit_code == 0
    assert given['carry_columns'] == 'auto'
    assert runner.invoke(cli, [*args, '--carry-columns', 'none']).exit_code == 0
    assert given['carry_columns'] == ()
