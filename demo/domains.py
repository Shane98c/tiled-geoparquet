"""The colours a map gives each flat column of a GeoParquet file that the
file's metadata holds no summary of, fixed from its values: the commonest of
a text column, and four quantiles of a number.

    uv run python demo/domains.py file.parquet > domains.json

`demo/build-share.mjs --domains domains.json` puts them in the comparison
page, which then colours such a column from its first tile, as it does an
attribute from its summary. A column whose values are all distinct, such as
an id, is left out.
"""

import json
import sys

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

VALUES = 8
QUANTILES = (0.02, 0.35, 0.7, 0.98)
SKIPPED = ('geometry', 'bbox', 'hilbert', 'lod', 'lod_attributes')


def main():
    path = sys.argv[1]
    schema = pq.read_schema(path)
    geo = json.loads((schema.metadata or {}).get(b'geo', b'{}'))
    summarised = {
        a['name'] for a in geo.get('lod_columns', {}).get('attributes', []) if 'summary' in a
    }
    con = duckdb.connect()
    source = f"read_parquet('{path}')"
    domains = {}
    for field in schema:
        if field.name in SKIPPED or field.name in summarised:
            continue
        name = '"' + field.name.replace('"', '""') + '"'
        if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
            rows = con.sql(
                f'SELECT {name}, count(*) AS n FROM {source} WHERE {name} IS NOT NULL '
                f'GROUP BY 1 ORDER BY n DESC, 1 LIMIT {VALUES}'
            ).fetchall()
            if rows and rows[0][1] > 1:
                domains[field.name] = {'values': [value for value, _ in rows]}
        elif pa.types.is_integer(field.type) or pa.types.is_floating(field.type):
            quantiles = ', '.join(f'quantile_cont({name}, {q})' for q in QUANTILES)
            found = con.sql(
                f'SELECT {quantiles} FROM {source} WHERE {name} IS NOT NULL AND isfinite({name})'
            ).fetchall()[0]
            stops = sorted({round(float(value), 6) for value in found if value is not None})
            if len(stops) > 1:
                domains[field.name] = {'stops': stops}
            elif stops:
                domains[field.name] = {'values': stops}
    json.dump(domains, sys.stdout, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
