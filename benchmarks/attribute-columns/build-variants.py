"""Build the attribute-storage variants of one source with one thinning recipe.

python benchmarks/attribute-columns/build-variants.py SRC OUT_DIR --recipe japan \\
    [--carry height,class] [--variants packed,columns,auto,hybrid,none]

Variants, each written to OUT_DIR/<variant>.parquet:
  packed   carry=CARRY, the attributes packed into every overview row, and
           no column attributes
  columns  carry_columns=CARRY, a typed column per LOD
  auto     carry_columns='auto', the writer's default; with --explicit-auto,
           an explicit list of the source's numeric, boolean and
           low-cardinality string columns
  hybrid   carry=CARRY and carry_columns='auto': CARRY packed, every other
           cheap column kept as a column
  none     nothing carried, the geometry-only reference

SRC is anything `write` opens (a path, glob or s3:// URI); an http(s) URL is
first streamed to OUT_DIR/source.parquet by derive-source.py, which also drops
the map columns of a Tiled GeoParquet file. Build time, the writer's log and
the bytes of every column group are recorded in OUT_DIR/builds.json, keeping
the entries of variants not rebuilt.
"""

import argparse
import importlib.util
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from tiled_geoparquet import chain, drop_smallest, tiny_polygons, write
from tiled_geoparquet.thinning import describe

HERE = Path(__file__).parent
VARIANTS = ('packed', 'columns', 'auto', 'hybrid', 'none')
# the writer packs a string with at most this many distinct values as a dictionary index
LOW_CARDINALITY = 256


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


derive = _load(HERE / 'derive-source.py')


def denver_recipe():
    return {'thinning': chain(drop_smallest(850_000), tiny_polygons()), 'workers': 8}


def japan_recipe():
    """The options of benchmarks/write-japan-tippecanoe.py, in one `write` call."""
    budgets = _load(HERE.parent / 'write-japan-tippecanoe.py').calibrated_budgets()

    def budget(zoom):
        return budgets.get(zoom, 1_300_000 if zoom <= 8 else 850_000)

    return {
        'thinning': chain(drop_smallest(budget), tiny_polygons()),
        'workers': 12,
        'zooms': list(range(16)),
        'complete_from': 14,
        'page_bytes': 16384,
        'page_rows': 2048,
        'lod_page_bytes': 65536,
        'memory_limit': '40GB',
    }


RECIPES = {'denver': denver_recipe, 'japan': japan_recipe}


def cheap_columns(src: str) -> list[str]:
    """Numeric and boolean columns, and strings with few distinct values."""
    schema = pq.read_schema(src) if '://' not in src else None
    if schema is None:
        raise ValueError('an explicit auto list needs a local source')
    geo = json.loads((schema.metadata or {}).get(b'geo', b'{}'))
    geometry = set(geo.get('columns', {}))
    names, strings = [], []
    for field in schema:
        t = field.type
        if field.name in geometry:
            continue
        if pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_boolean(t):
            names.append(field.name)
        elif pa.types.is_string(t) or pa.types.is_large_string(t):
            names.append(field.name)
            strings.append(field.name)
    if strings:
        counts = ', '.join(
            f'(SELECT count(*) FROM (SELECT DISTINCT "{n}" FROM read_parquet(\'{src}\') '
            f'WHERE "{n}" IS NOT NULL LIMIT {LOW_CARDINALITY + 1})) AS "{n}"'
            for n in strings
        )
        distinct = dict(
            zip(strings, duckdb.connect().execute(f'SELECT {counts}').fetchone(), strict=True)
        )
        names = [n for n in names if distinct.get(n, 0) <= LOW_CARDINALITY]
    return names


def variant_options(name: str, carry: list[str], auto: list[str] | str) -> dict:
    if name == 'packed':
        return {'carry': carry, 'carry_columns': ()}
    if name == 'none':
        return {'carry_columns': ()}
    if name == 'hybrid':
        kept = auto if auto == 'auto' else [c for c in auto if c not in carry]
        return {'carry': carry, 'carry_columns': kept}
    return {'carry_columns': carry if name == 'columns' else auto}


def storage(path: Path, log_lines: list[str]) -> dict:
    """Compressed column chunk bytes grouped by what they hold, and the rest of the file."""
    parquet = pq.ParquetFile(path)
    metadata = parquet.metadata
    geo = json.loads(parquet.schema_arrow.metadata[b'geo'])
    lod = geo.get(derive.LOD_KEY, {})
    lod_column = lod.get('column')
    key_column = lod.get('key', {}).get('column')
    covering = {
        p[0]
        for c in geo['columns'].values()
        for p in c.get('covering', {}).get('bbox', {}).values()
    }
    attribute_structs = derive._named_columns(lod) - {lod_column, key_column}
    chunks = defaultdict(int)
    for group in range(metadata.num_row_groups):
        row_group = metadata.row_group(group)
        for index in range(row_group.num_columns):
            column = row_group.column(index)
            chunks[column.path_in_schema] += column.total_compressed_size
    report = {
        'file_bytes': path.stat().st_size,
        'lod_geometry': {},
        'attribute_columns': defaultdict(int),
        'attribute_columns_by_lod': defaultdict(int),
        'covering': 0,
        'key': 0,
        'source_columns': defaultdict(int),
    }
    for chunk, size in chunks.items():
        top, *rest = chunk.split('.')
        if top == lod_column:
            report['lod_geometry'][rest[0]] = size
        elif top in attribute_structs:
            report['attribute_columns_by_lod'][rest[0]] += size
            report['attribute_columns'][rest[1] if len(rest) > 1 else rest[0]] += size
        elif top in covering:
            report['covering'] += size
        elif top == key_column:
            report['key'] += size
        else:
            report['source_columns'][top] += size
    report['column_chunk_bytes'] = sum(chunks.values())
    # footer, page indexes, and the spatial index root and directories
    report['other_bytes'] = report['file_bytes'] - report['column_chunk_bytes']
    for line in log_lines:
        match = re.search(r'([\d,]+) directory bytes, ([\d,]+) root bytes', line)
        if match:
            report['directory_bytes'], report['root_bytes'] = (
                int(g.replace(',', '')) for g in match.groups()
            )
    return {k: dict(v) if isinstance(v, defaultdict) else v for k, v in report.items()}


def build(src: str, out_dir: Path, name: str, recipe: dict, options: dict) -> tuple:
    """Write one variant over any earlier build of it; its seconds and log lines."""
    out = out_dir / f'{name}.parquet'
    lines = []
    started = time.monotonic()

    def log(message):
        line = f'[{time.monotonic() - started:7.1f}s] {message}'
        lines.append(line)
        print(f'{name} {line}', flush=True)

    write(src, out, **recipe, **options, log=log)
    seconds = round(time.monotonic() - started, 1)
    (out_dir / f'{name}.log').write_text('\n'.join(lines) + '\n')
    return seconds, lines


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('src')
    parser.add_argument('out_dir', type=Path)
    parser.add_argument('--recipe', choices=sorted(RECIPES), required=True)
    parser.add_argument('--carry', default='height,class', help='attributes of packed and columns')
    parser.add_argument('--variants', default=','.join(VARIANTS))
    parser.add_argument(
        '--repeat',
        type=int,
        default=1,
        help='build the variants this many times, round-robin; the fastest time counts',
    )
    parser.add_argument('--workers', type=int, help="override the recipe's worker count")
    parser.add_argument('--memory-limit', help="override the recipe's DuckDB memory limit")
    parser.add_argument(
        '--explicit-auto',
        action='store_true',
        help="pass the cheap columns as a list even when the writer takes 'auto'",
    )
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    src = args.src
    if src.startswith(('http://', 'https://')):
        local = args.out_dir / 'source.parquet'
        if not local.exists():
            derive.derive(src, str(local))
        src = str(local)
    recipe = RECIPES[args.recipe]()
    if args.workers:
        recipe['workers'] = args.workers
    if args.memory_limit:
        recipe['memory_limit'] = args.memory_limit
    carry = [c for c in args.carry.split(',') if c]
    variants = [v for v in args.variants.split(',') if v]
    unknown = set(variants) - set(VARIANTS)
    if unknown:
        raise SystemExit(f'unknown variants {sorted(unknown)}')
    auto = cheap_columns(src) if args.explicit_auto else 'auto'
    if 'auto' in variants:
        print(f'auto: {auto}', flush=True)
    results_path = args.out_dir / 'builds.json'
    results = json.loads(results_path.read_text()) if results_path.exists() else {}
    results.setdefault('variants', {})
    results.update(
        source=src,
        recipe=args.recipe,
        thinning=describe(recipe['thinning']),
        carry=carry,
        auto=auto,
    )
    times = defaultdict(list)
    for _ in range(args.repeat):
        for name in variants:
            options = variant_options(name, carry, auto)
            seconds, lines = build(src, args.out_dir, name, recipe, options)
            times[name].append(seconds)
            results['variants'][name] = {
                'file': f'{name}.parquet',
                'build_s': min(times[name]),
                'build_runs_s': times[name],
                'options': options,
                'storage': storage(args.out_dir / f'{name}.parquet', lines),
            }
            results_path.write_text(json.dumps(results, indent=2) + '\n')
            print(
                f'{name}: {seconds} s, {results["variants"][name]["storage"]["file_bytes"]:,} bytes'
            )
