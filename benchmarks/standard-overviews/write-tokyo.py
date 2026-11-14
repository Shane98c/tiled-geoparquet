"""Build the Tokyo file the standard-overview measurements read.

Cuts the Tokyo subset (4,881,955 buildings whose bbox meets 139.39–140.10 E,
35.45–35.90 N) from the hosted Japan Tiled GeoParquet file, then writes it with
the Japan recipe's calibrated budgets in one Hilbert order:

    uv run python benchmarks/standard-overviews/write-tokyo.py tokyo.source.parquet tokyo.packed.parquet

The coarse-prefix file of the measurements stores the rows shown through z10
in a leading row group. It was written from the same source by the writer at
commit `0a06e75^`, whose `write` takes `prefix_zoom=10`.

An existing source file is reused. A rebuild matches the recorded request
counts and comes within about 1% of the recorded bytes and feature counts; the
measured files were cut in the source's original row order. measure-packed.mjs
reads either file with tiles.json.
"""

import argparse
import importlib.util
import time
from pathlib import Path

import duckdb
from tiled_geoparquet import chain, drop_smallest, tiny_polygons
from tiled_geoparquet._layout import finalize_layout
from tiled_geoparquet.writer import _write_unindexed

JAPAN = 'https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/0fcab25/japan_buildings.parquet'
BOX = (139.39, 35.45, 140.10, 35.90)


def cut(out: Path):
    con = duckdb.connect()
    con.execute('INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;')
    xmin, ymin, xmax, ymax = BOX
    con.execute(
        f"""
        COPY (
            SELECT * EXCLUDE (bbox, hilbert, lod) FROM read_parquet('{JAPAN}')
            WHERE bbox.xmax >= {xmin} AND bbox.xmin <= {xmax}
              AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}
              AND ST_XMax(geometry) >= {xmin} AND ST_XMin(geometry) <= {xmax}
              AND ST_YMax(geometry) >= {ymin} AND ST_YMin(geometry) <= {ymax}
        ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
        """
    )


def budgets():
    recipe = Path(__file__).parents[1] / 'write-japan-tippecanoe.py'
    spec = importlib.util.spec_from_file_location('recipe', recipe)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calibrated = module.calibrated_budgets()
    return lambda z: calibrated.get(z, 1_300_000 if z <= 8 else 850_000)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('out')
    args = parser.parse_args()
    t0 = time.time()

    def log(message):
        print(f'[{time.time() - t0:6.0f}s] {message}', flush=True)

    source, out = Path(args.source), Path(args.out)
    if not source.exists():
        log(f'cutting {source}')
        cut(source)
    staged = out.with_suffix('.unindexed.parquet')
    _write_unindexed(
        str(source),
        staged,
        thinning=chain(drop_smallest(budgets()), tiny_polygons()),
        attributes=['height', 'class'],
        zooms=list(range(8, 17)),
        complete_from=14,
        page_bytes=16384,
        page_rows=2048,
        workers=8,
        log=log,
    )
    finalize_layout(staged, out, page_bytes=65536, log=log)
    staged.unlink()
