# Writer guide

How to write a Tiled GeoParquet file with the Python package, check it, and
read its overviews as tables. An overview is the thinned, simplified copy of
the data that a map draws at a zoom, and an LOD (level of detail) is the
stored column that serves one or more zooms. The [README](../README.md)
describes the format and its measurements, and the
[JavaScript package's README](../js/README.md) covers the reader.

## Install

Python 3.12 or later. From a clone:

```bash
uv sync --group dev
```

That sets up an environment with the package and its test dependencies. To
install it into another environment, use `uv pip install -e .` or
`pip install -e .` there; the package is not on PyPI yet.

## Quick start

Pull a city of buildings with the
[Overture CLI](https://docs.overturemaps.org/getting-data/overturemaps-py/)
and write it. The example guesses zooms from the data and keeps height and
class as attributes:

```bash
pip install overturemaps
overturemaps download --bbox=-105.1,39.6,-104.8,39.9 -f geoparquet --type=building -o overture_denver.parquet
uv run python examples/overture_buildings.py overture_denver.parquet overture_denver.lod.parquet
uv run tiled-geoparquet validate overture_denver.lod.parquet
uv run tiled-geoparquet inspect overture_denver.lod.parquet
```

Another file, with a `risk` column, written from the CLI and by the same
call from Python:

```bash
uv run tiled-geoparquet write buildings.parquet buildings.lod.parquet \
  --thinning size:4+drop:2.5 --where "risk > 0" --attributes risk
```

```python
from tiled_geoparquet import write, combine, drop_rate, only, size_threshold

if __name__ == '__main__':
    write(
        'buildings.parquet',
        'buildings.lod.parquet',
        thinning=only('risk > 0', combine(size_threshold(4), drop_rate(2.5))),
        attributes=['risk'],
    )
```

`write()` packs overviews in worker processes, which import the calling
script again unless Python forks them, so a script calls it under an
`if __name__ == "__main__":` guard.

The input is any GeoParquet with a WKB geometry column in OGC:CRS84 or
EPSG:4326 with planar edges, as a path or a `*` glob, local or on S3
(`s3://`, with credentials from the usual AWS chain). It holds one family of
geometry: points, lines or polygons. The output is a local path. `metadata=`
adds key-value metadata to the file.

## Thinning

A _thinning function_ decides which rows appear at which zoom:

```python
def thin(rows: pa.Table, zoom: int, ctx: Context) -> pa.Table
```

The writer calls it once per zoom with every row, and it returns the rows to
show there. The finest zoom always shows every row, and membership need not
nest across zooms.

The built-ins are a few lines each and meant to be copied:

| thinning                        | rule                                                                                                                                                                 | good for                                            |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| `drop_rate(2.5)`                | each zoom coarser keeps 1/rate, uniformly, so relative density survives (tippecanoe `-r`); the default                                                               | points, small polygons                              |
| `size_threshold(4)`             | keep a feature while it still spans 4 pixels                                                                                                                         | large polygons and lines first                      |
| `tiny_polygons(0.25)`           | tippecanoe's tiny-polygon reduction: areas under a quarter-pixel square accumulate per tile and one square is kept per square's worth                                | area-true coverage of buildings zoomed out          |
| `importance('population', 300)` | keep the top N per tile by a column                                                                                                                                  | named places, ranked facilities                     |
| `cluster(8, count='count')`     | one representative per 8 px cell holding how many it stands for                                                                                                      | dense points                                        |
| `density_budget(60_000)`        | one uniform fraction per zoom, sized so the busiest tile holds at most the budget of rows (tippecanoe `--drop-fraction-as-needed`); takes a function of the zoom too | dense points or sub-pixel buildings at coarse zooms |
| `drop_smallest(600_000)`        | one area threshold per zoom, raised until the busiest tile's estimated bytes fit; inspired by tippecanoe, with different cost estimation                             | buildings, before `tiny_polygons`                   |
| `only('risk > 0', inner)`       | restrict eligibility before `inner`                                                                                                                                  | partial datasets                                    |
| `combine(a, b)`                 | keep what any part keeps                                                                                                                                             | size plus sample, etc.                              |
| `chain(a, b)`                   | apply `b` to what `a` kept                                                                                                                                           | `drop_smallest` then `tiny_polygons`                |

In a CLI spec the built-ins are `drop[:rate]`, `size[:pixels]`,
`tiny[:pixels]`, `importance:column[:per_tile]`, `cluster[:pixels]`,
`density[:budget]` and `smallest[:bytes]`. `+` unions thinnings (`combine`)
and `>` chains them (`chain`), as in `smallest:600000>tiny`, and `--where`
restricts eligibility as `only` does.

### Writing your own

Pass any function of that shape as `thinning=`, on its own or inside `chain`,
`combine` or `only`. The CLI's `--thinning` names the built-ins only. This
one keeps a building while it spans 4 pixels, and a building with a `height`
of 30 or more at every zoom:

```python
import pyarrow as pa
from tiled_geoparquet import write


def tall_or_wide(rows, zoom, ctx):
    wide = ctx.pixel_diag(rows, zoom) >= 4
    tall = rows['height'].to_numpy(zero_copy_only=False) >= 30
    return rows.filter(pa.array(wide | tall))


if __name__ == '__main__':
    write('buildings.parquet', 'buildings.lod.parquet', thinning=tall_or_wide)
```

- `rows` has `id`, `cx` and `cy` (the centroid), `diag` (the bbox diagonal)
  and `area`, all in Web Mercator metres, `nv` (the vertex count), `u` (a
  uniform random value in [0, 1) hashed from the geometry, so stable across
  rebuilds), and every attribute. `columns=` on `write()` loads other source columns.
- `ctx`, a `tiled_geoparquet.Context`, converts to the screen at a zoom:
  `pixel_diag(rows, zoom)`, `pixel_area(rows, zoom)`, and `resolution(zoom)`
  in metres per pixel. `tile_of(rows, zoom)` and
  `cell_of(rows, zoom, pixels)` group rows by tile or by grid cell, and
  `finest_zoom` holds the complete zoom ([Zooms](#zooms)).
- The returned table must keep `id`. Any attribute column it includes
  overrides that attribute at that zoom, which is how a cluster passes its
  count along.

## Zooms

Zooms are derived from the data unless given:

- The coarsest zoom is one coarser than where the dataset's whole extent
  spans about a pixel.
- The complete zoom, from which every feature is shown and which a rate-based
  thinning counts from, is where the median feature spans a pixel.
- The finest zoom is where the median vertex spacing spans four pixels, at
  most 15.

The writer logs the guess; pass `zooms=` or `complete_from=` to override.
Zooms are consecutive and lie within 0 to 24, and the writer refuses others
before it starts.

## Attributes

Source columns are kept as they are and stay queryable. The attributes a map
styles by, `attributes=` (`--attributes`), are also held per zoom, in columns
a tile reads only when the reader's request for it names the attribute, in
`properties`. A file has at most 64.

- `'auto'` (`--attributes auto`), the default, keeps every flat number and
  boolean column, except integer columns whose values are mostly distinct,
  such as ids, and every string column with at most 256 distinct values.
- `attributes=()` (`--attributes none`) keeps none.
- A list keeps the columns it names, as names or `(name, Arrow type)` pairs.
- `'auto'` in a list stands for the default's columns, so `['auto', 'id']`
  keeps an id beside them (`--attributes auto --attributes id`).

A column that is not an attribute can still be read with `readRow` or
`readColumns`, or named in a tile's `properties`. That takes one more request
round than an attribute and, at zooms where thinning drops most rows, up to
5.8 times the bytes on 1.75 million Denver buildings.

### Cost

A tile styled by an attribute reads the attribute's pages beside its
geometry, in the same request rounds. On the Denver buildings a cold tile
styled by `height` takes one or two more requests than with geometry only and
reads 14 to 50% more bytes. Attributes also cost a tile that names none a
little, because the copies of features from neighbouring tiles pack them: at
most 1.3% more bytes when the file keeps two attributes, and 5 to 12% more at
zooms 12 to 15 when it keeps eight. Where the default would keep many columns
and styles use few, name those in `attributes`.
[What attribute columns cost](design-alternatives.md#what-attribute-columns-cost)
has the measurements.

An id is the costly one. A map needs it in its tiles only when it joins on it
in the browser, colouring features from a table keyed by id or giving the
source a `promoteId`. On 4.9 million Tokyo buildings a 36-character id's
columns take 290 MB of the 972 MB file. Reading one feature's id needs none
of that: `readRow(url, feature.id, ['id'])`.

### Types

- The JS reader decodes an integer attribute as a JavaScript number, exactly
  only up to 2^53 in magnitude. Keep integer attributes inside that range and
  read wider ones, such as 64-bit ids, with `readRow` or `readColumns`, which
  return the source value as a BigInt. The writer refuses a uint64 attribute
  value above 2^63 - 1.
- A float column can be kept at half precision, as
  `attributes=[('rps_2011', 'halffloat')]` or
  `--attributes rps_2011:halffloat`. Tiles then get each value rounded to
  about three significant digits, in two bytes, and the source column keeps
  its full precision.
- A string with 1 to 256 distinct values is held as an index into the
  `values` the metadata lists.

### Summaries

The metadata has a `summary` for each attribute, which a reader gets in its
first request, so a map can choose a colour ramp or a legend before it
requests a tile:

- `count` is the number of rows with a value.
- For a number, `min` and `max` are its smallest and largest values, and
  `quantiles` are its values at the 0.02, 0.25, 0.5, 0.75 and 0.98 quantiles.
- For a string with `values`, `counts` is the number of rows with each value.
  For a boolean it is the number of rows that are false, then the number that
  are true.

In JavaScript, `readMetadata(url)` returns them at
`lod.attributes[i].summary`. In Python they are in the footer:
`json.loads(pq.read_schema(path).metadata[b'geo'])['lod_columns']['attributes']`.

### In analysis

Leave the `lod` and `lod_attributes` columns, which hold the overview tiles,
out of every analysis. A query
of the table, in DuckDB or Arrow, that reads an attribute column pairs each
value with a row it does not describe and reports no error. A row's attribute
is in the source column of the same name.

## Geometry

A family keeps its geometry type at every zoom. Lines and polygons keep a
quantized copy per LOD: one per zoom below the complete zoom, where the
thinning changes the row set, and one per two zooms from it up. When the
full-detail shapes of the whole dataset are small (under `single_lod_bytes`,
16 MiB by default) a single LOD serves every zoom from the complete zoom up.

A shape too small for a zoom's quarter-pixel grid is stored as a square on
its centroid's cell, so a thinned building set zoomed out reads as a faint
density texture. `placeholder_pixels` (`--placeholder-pixels`) sets the
square's width, half a pixel by default; thinnings that fold shapes into
squares, like `tiny_polygons`, set their own.

Finer than the finest zoom the reader draws the exact source geometry, so
full detail is always reachable and costs nothing to store. Those tiles cost
more to read: on a file of US buildings a cold one reads 230 to 650 KB in up
to five requests, where a tile at the finest LOD reads 30 to 45 KB in two. A
map source's `maxzoom` decides whether the map requests them or overzooms the
finest LOD ([in a MapLibre app](../js/README.md#in-a-maplibre-app)).

### Margins

`margin_pixels` (`--margin-pixels`, 2 by default, from 0 to 128) sets how
close to a tile's edge a feature lying outside the tile must come to be
copied into it. tippecanoe's default buffer is 10 pixels of the same tile.

A renderer that clips each tile's drawing to the tile, as MapLibre does for
fill and line layers, can cut a line or outline drawn wider than twice the
margin where a feature runs along a tile edge. A style with wide strokes
therefore wants a wider margin: 16 pixels draws 32-pixel strokes whole. Every
copy is a whole feature, so a wider margin costs copies and bytes. For
146,155 Denver buildings with two attributes:

| `margin_pixels` | copies | overview bytes | file bytes | median z14 tile |
| --------------- | -----: | -------------: | ---------: | --------------: |
| 2               | 14,703 |        6.12 MB |   21.89 MB |           41 KB |
| 8               | 38,622 |        6.53 MB |   22.29 MB |           48 KB |
| 16              | 69,747 |        7.05 MB |   22.81 MB |           54 KB |

## Build time and memory

- Writing a 155.6 million building national file with nine attributes took
  66 minutes on a 16-core machine with 128 GB of memory.
- The build needs free disk of about twice the output for its intermediate
  files.
- Workers each grow to about 1 GB, so `workers` (`--workers`, at most 8 by
  default) bounds that part of the memory. `memory_limit` (`--memory-limit`,
  25% of RAM by default) caps DuckDB.

## Validation

`tiled-geoparquet validate file.parquet` checks the layout metadata, the
attributes against it, the bbox covering, the page indexes, and a sample of
up to 2,000 rows from the first row group (`--sample-rows N` changes it).

`tiled-geoparquet validate file.parquet --full` (Python:
`validate(path, full=True)`) reads the whole file. It decodes every overview
value, checks every row's bbox covering and Hilbert key, and checks the
complete spatial index against the footer.

This is a layout and reader contract check; it does not prove polygon
topology or compare simplification accuracy with the source geometry. The
specification lists every check, for [the table](../spec/SPEC.md#validation)
and for [the spatial index](../spec/SPATIAL-INDEX.md#validation).

## Tile sizes

`write()` logs, and `tiled-geoparquet inspect file.parquet` ends with, how
heavy the tiles of each zoom are: the LOD serving the zoom, the tiles holding
rows of their own, the median, 99th percentile and maximum compressed bytes
of overview pages a cold read of one tile fetches, and the heaviest tile.
These are the lines for zooms 8 to 15 of 1.75 million Denver buildings
written with `chain(drop_smallest(850_000), tiny_polygons())` and no
attributes:

```text
  zoom  lod  tiles  median     p99     max  largest tile
     8  f8       2  185 KB  257 KB  258 KB  8/53/97
     9  f9       4  218 KB  329 KB  330 KB  9/107/194
    10  f10      4  206 KB  305 KB  306 KB  10/214/388
    11  f11     12  209 KB  308 KB  309 KB  11/427/776
    12  f12     48  180 KB  302 KB  303 KB  12/857/1554
    13  f13    150  123 KB  192 KB  199 KB  13/1709/3102
    14  f14    532   49 KB  109 KB  113 KB  14/3418/6216
    15  f14  1,980   13 KB   30 KB   36 KB  15/6828/12416
```

Here the LOD `f14` serves zooms 14 and 15. The figures count the overview
pages alone. The directory nodes that locate them are not counted, and in a file with attributes the log adds a line with
each attribute's median page bytes. `tile_sizes(path)` returns the same in
Python.

Tune the thinning against it: a zoom whose heaviest tile costs more than a
tile should wants a smaller budget there, and `drop_smallest` and
`density_budget` both take a function of the zoom. For scale, tippecanoe
holds a compressed tile to 500 KB unless told otherwise.

## Overviews in Python

`read_overview(path, zoom)` returns what a zoom shows as an ordinary pyarrow
table, one row per shown feature:

- `row`, the file row index to join source columns on;
- `geometry`, the overview shape as WKB in EPSG:3857 metres on that zoom's
  grid;
- the attributes as that zoom's LOD holds them.

Boundary copies are left out, since each repeats a row the table already has.

```python
import shapely
from tiled_geoparquet import read_overview

z10 = read_overview('buildings.lod.parquet', 10)
shapes = shapely.from_wkb(z10['geometry'].to_numpy(zero_copy_only=False))
```

`zoom` must be one of the file's zooms; any other raises `ValueError`. So
does a file with an attribute named `row` or `geometry`, which would collide
with those columns.
