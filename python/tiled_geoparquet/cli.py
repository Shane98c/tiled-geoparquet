"""tiled-geoparquet: write, validate and inspect LOD column files."""

from __future__ import annotations

import json

import click

from . import thinning as th
from ._tile_sizes import format_bytes
from .validate import ValidationError, inspect, validate
from .writer import write


def parse_thinning(spec: str, where: str | None) -> th.Thin:
    """`drop[:rate]`, `density[:budget]`, `smallest[:bytes]`, `size[:pixels]`, `tiny[:pixels]`,
    `importance:column[:per_tile]`, `cluster[:pixels]`; `+` combines, e.g. `size:4+drop:2.5`;
    `>` chains, e.g. `smallest:600000>tiny`; `auto` is the family default. `where` restricts
    eligibility."""
    if spec == 'auto':
        thin = th.default_thinning('auto')
        return th.only(where, thin) if where else thin
    stages = [_parse_union(stage) for stage in spec.split('>')]
    thin = stages[0] if len(stages) == 1 else th.chain(*stages)
    return th.only(where, thin) if where else thin


def _parse_union(spec: str) -> th.Thin:
    parts = []
    for part in spec.split('+'):
        kind, _, rest = part.partition(':')
        if kind == 'drop':
            parts.append(th.drop_rate(float(rest or 2.5)))
        elif kind == 'density':
            parts.append(th.density_budget(int(rest or 60_000)))
        elif kind == 'smallest':
            parts.append(th.drop_smallest(int(rest or 600_000)))
        elif kind == 'size':
            parts.append(th.size_threshold(float(rest or 4)))
        elif kind == 'importance':
            column, _, per_tile = rest.partition(':')
            parts.append(th.importance(column, int(per_tile or 1000)))
        elif kind == 'tiny':
            parts.append(th.tiny_polygons(float(rest or th.TINY_PIXELS)))
        elif kind == 'cluster':
            parts.append(th.cluster(float(rest or 8)))
        else:
            raise click.BadParameter(f'unknown thinning {part!r}')
    return parts[0] if len(parts) == 1 else th.combine(*parts)


@click.group()
def cli():
    """Write GeoParquet files that a web map can read at tile cost."""


@cli.command('write')
@click.argument('src')
@click.argument('out')
@click.option(
    '--thinning',
    'thinning_spec',
    default='auto',
    show_default=True,
    help='auto | drop[:rate] | density[:budget] | smallest[:bytes] | size[:pixels] | tiny[:px] | importance:column[:n] | cluster[:px]; + unions, > chains',
)
@click.option(
    '--where', default=None, help='SQL predicate; rows failing it are never shown zoomed out'
)
@click.option('--columns', multiple=True, help='extra source columns the thinning reads')
@click.option(
    '--zooms',
    default='auto',
    show_default=True,
    help="inclusive range within 0-24, such as 2-16, or 'auto' to derive from the data",
)
@click.option(
    '--complete-from',
    default='auto',
    show_default=True,
    help="zoom from which every feature is present: a zoom, 'auto' (median feature spans a pixel) or 'never'",
)
@click.option(
    '--carry',
    multiple=True,
    help='column[:arrow type] packed into every overview row, e.g. risk:float32',
)
@click.option(
    '--carry-columns',
    multiple=True,
    help=(
        'column[:arrow type] kept as columns a tile reads only when a style asks. '
        "'auto', the default, keeps every cheap column; 'none' keeps none"
    ),
)
@click.option('--geometry', default='geometry', show_default=True)
@click.option('--row-group-rows', default=1_048_576, type=int, show_default=True)
@click.option(
    '--page-bytes',
    default=16384,
    type=click.IntRange(min=1),
    show_default=True,
    help='Analytical/staging page target in bytes',
)
@click.option(
    '--lod-page-bytes',
    default=65536,
    type=click.IntRange(min=1),
    show_default=True,
    help='Final tile-aligned overview page target in bytes',
)
@click.option('--compression-level', default=9, type=int, show_default=True, help='zstd level')
@click.option(
    '--batch-rows',
    default=None,
    type=int,
    help='rows per write batch; default sizes pages to --page-bytes',
)
@click.option(
    '--placeholder-pixels',
    default=None,
    type=click.FloatRange(min=0, min_open=True),
    help='width in pixels of the square a shape under a pixel draws as',
)
@click.option(
    '--margin-pixels',
    default=2,
    type=click.FloatRange(min=0, max=128),
    show_default=True,
    help='pixels past a tile edge within which a feature centred elsewhere is copied into the '
    'tile; lines and outlines up to twice this wide draw whole along tile edges',
)
@click.option('--workers', default=None, type=int)
@click.option('--memory-limit', default=None)
@click.option('--temp-dir', default=None)
def write_cmd(
    src,
    out,
    thinning_spec,
    where,
    columns,
    zooms,
    complete_from,
    carry,
    carry_columns,
    geometry,
    row_group_rows,
    page_bytes,
    lod_page_bytes,
    compression_level,
    batch_rows,
    placeholder_pixels,
    margin_pixels,
    workers,
    memory_limit,
    temp_dir,
):
    """Write SRC (GeoParquet with WKB geometry) to OUT with overviews and a spatial index."""
    if zooms != 'auto':
        lo, _, hi = zooms.partition('-')
        zooms = range(int(lo), int(hi or lo) + 1)
    if complete_from != 'auto':
        complete_from = None if complete_from == 'never' else int(complete_from)
    carried = [tuple(c.split(':', 1)) if ':' in c else c for c in carry]
    if carry_columns in ((), ('auto',)):
        kept = 'auto'
    elif carry_columns == ('none',):
        kept = ()
    else:
        kept = [tuple(c.split(':', 1)) if ':' in c else c for c in carry_columns]
    write(
        src,
        out,
        thinning=parse_thinning(thinning_spec, where),
        columns=columns,
        zooms=zooms,
        complete_from=complete_from,
        carry=carried,
        carry_columns=kept,
        geometry=geometry,
        row_group_rows=row_group_rows,
        page_bytes=page_bytes,
        lod_page_bytes=lod_page_bytes,
        compression_level=compression_level,
        batch_rows=batch_rows,
        placeholder_pixels=placeholder_pixels,
        margin_pixels=margin_pixels,
        workers=workers,
        memory_limit=memory_limit,
        temp_dir=temp_dir,
        log=click.echo,
    )


@cli.command('validate')
@click.argument('path')
@click.option('--full', is_flag=True, help='Decode every overview row and verify spatial indexes.')
@click.option('--sample-rows', default=2000, type=click.IntRange(min=0), show_default=True)
def validate_cmd(path, full, sample_rows):
    """Check the layout contract and decode a sample of every LOD."""
    try:
        summary = validate(path, sample_rows=sample_rows, full=full)
    except ValidationError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f'OK {path}: ' + json.dumps(summary))


@cli.command('inspect')
@click.argument('path')
def inspect_cmd(path):
    """Print the lod_columns metadata, per-column bytes of one row group, and
    how heavy the tiles of each zoom are."""
    info = inspect(path)
    rows = info['rows_in_sampled_group']
    tables = ('compressed_bytes_by_column', 'tile_sizes')
    click.echo(json.dumps({k: v for k, v in info.items() if k not in tables}, indent=2))
    click.echo(f'bytes per row in row group {info["row_group_sampled"]}:')
    for col, nbytes in info['compressed_bytes_by_column'].items():
        click.echo(f'  {col:40s} {nbytes / max(rows, 1):8.2f}')
    if info['tile_sizes'] is None:
        click.echo('tile sizes: none, the file has no spatial index')
        return
    click.echo('compressed bytes of the pages a cold tile read fetches, per zoom:')
    for line in _tile_size_table(info['tile_sizes']):
        click.echo('  ' + line)


def _tile_size_table(sizes: list[dict]) -> list[str]:
    """`tile_sizes()` as lines of aligned columns under a header."""
    table = [('zoom', 'lod', 'tiles', 'median', 'p99', 'max', 'largest tile')]
    for zoom in sizes:
        fetched, largest = ['-'] * 3, '-'
        if zoom['tiles']:
            fetched = [format_bytes(zoom[k]) for k in ('median_bytes', 'p99_bytes', 'max_bytes')]
            largest = '/'.join(map(str, zoom['largest']))
        table.append((str(zoom['zoom']), zoom['lod'], f'{zoom["tiles"]:,}', *fetched, largest))
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    # numbers align right; the LOD name and the tile read left
    align = ('>', '<', '>', '>', '>', '>', '<')
    return [
        '  '.join(f'{cell:{a}{w}}' for cell, a, w in zip(row, align, widths, strict=True)).rstrip()
        for row in table
    ]
