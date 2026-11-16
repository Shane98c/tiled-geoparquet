"""Build the Japan file of the baked-PMTiles comparison.

Runs the scripts of a clone of gsueur/geoparquet-pmtiles (commit b8e4734) in
the order its parcels file was built: rows in Hilbert order with the widest 2%
last, 512-row pages in 16,384-row groups, the tippecanoe archive's zooms below
14 as overviews, zoom 14 tiles of the widest 2%, and the assembled archive
baked in before the footer.

    uv run --with geoarrow-pyarrow --with pmtiles python benchmarks/baked-pmtiles/build.py \\
        clone source.parquet japan_buildings.pmtiles work japan_buildings.baked.parquet

Needs tippecanoe on the PATH and about 25 GB free in `work`. The source is the
GeoParquet 1.1 file the other archives derive from, written first as
GeoParquet 2.0 with a bbox column, the input those scripts read. The archive
is the comparison's PMTiles file, so both hold the same overview tiles.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import duckdb

DETAIL_ZOOM = 14
WIDEST = 0.02
LAYER = 'buildings'
WIDTH = 'greatest(bbox.xmax - bbox.xmin, bbox.ymax - bbox.ymin)'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('clone', 'source', 'archive', 'work', 'out'):
        parser.add_argument(name)
    args = parser.parse_args()
    clone, work = Path(args.clone), Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    def log(message):
        print(f'[{time.time() - t0:6.0f}s] {message}', flush=True)

    def script(name, *rest):
        subprocess.run([sys.executable, str(clone / name), *map(str, rest)], check=True)

    con = duckdb.connect()
    con.execute(f"INSTALL spatial; LOAD spatial; SET temp_directory = '{work / 'tmp'}'")
    conformed, ordered, layout = (work / f'{n}.parquet' for n in ('source', 'sorted', 'layout'))
    con.execute(
        f"""
        COPY (
            SELECT * EXCLUDE (geometry), geometry,
                   {{'xmin': ST_XMin(geometry), 'ymin': ST_YMin(geometry),
                     'xmax': ST_XMax(geometry), 'ymax': ST_YMax(geometry)}} AS bbox
            FROM read_parquet('{args.source}')
        ) TO '{conformed}' (FORMAT parquet, COMPRESSION zstd, GEOPARQUET_VERSION 'V2')
        """
    )
    (wide,) = con.execute(
        f"SELECT round(quantile_cont({WIDTH}, {1 - WIDEST}), 6) FROM '{conformed}'"
    ).fetchone()
    log(f'GeoParquet 2.0 source with a bbox; the widest {WIDEST:.0%} are wider than {wide} degrees')

    con.execute(
        f"""
        COPY (
            WITH e AS (
                SELECT min(bbox.xmin) x0, min(bbox.ymin) y0, max(bbox.xmax) x1, max(bbox.ymax) y1
                FROM '{conformed}'
            )
            SELECT s.* FROM '{conformed}' s, e
            ORDER BY {WIDTH} > {wide},
                     ST_Hilbert((s.bbox.xmin + s.bbox.xmax) / 2, (s.bbox.ymin + s.bbox.ymax) / 2,
                                ST_Extent(ST_MakeEnvelope(e.x0, e.y0, e.x1, e.y1)))
        ) TO '{ordered}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 16384)
        """
    )
    script(
        'paginate.py', ordered, layout,
        '--rows-per-page', 512, '--row-group-rows', 16384, '--schema-from', conformed, '--plain', 'id',
    )  # fmt: skip
    ordered.unlink()
    log('rows sorted and paginated')

    rows, first = con.execute(
        f"""
        SELECT count(*), min(file_row_number) FILTER ({WIDTH} > {wide})
        FROM read_parquet('{layout}', file_row_number = true)
        """
    ).fetchone()
    (count,) = con.execute(f"SELECT count(*) FROM '{layout}' WHERE {WIDTH} > {wide}").fetchone()
    assert first == rows - count, 'the wide rows are not the last rows'
    features = work / 'wide.geojsonl'
    con.execute(
        f"""
        COPY (
            SELECT json_object(
                'type', 'Feature',
                'properties', json_object('class', "class", 'height', height, 'subtype', subtype),
                'geometry', ST_AsGeoJSON(geometry)::JSON)
            FROM '{layout}' WHERE {WIDTH} > {wide}
        ) TO '{features}' (FORMAT csv, HEADER false, QUOTE '', DELIMITER '\x01')
        """
    )
    tiles = work / 'wide.pmtiles'
    subprocess.run(
        ['tippecanoe', '-o', str(tiles), '-f', '-Z', str(DETAIL_ZOOM), '-z', str(DETAIL_ZOOM),
         '-l', LAYER, '-P', '-q', str(features)],
        check=True,
    )  # fmt: skip
    features.unlink()
    assembled = work / 'assembled.pmtiles'
    script('assemble.py', args.archive, tiles, assembled, DETAIL_ZOOM, LAYER, first)
    log(f'{count:,} wide rows from row {first:,} tiled at zoom {DETAIL_ZOOM}')

    script('bake.py', 'bake', layout, assembled, args.out)
    layout.unlink()
    log(f'{Path(args.out).stat().st_size:,} bytes -> {args.out}')


if __name__ == '__main__':
    main()
