# Writer and reader guide

The [README](../README.md) covers the recommended workflow and current
measurements. This guide explains how the writer chooses and encodes overviews,
how to install and run the packages, and what the reader APIs return.

## How it works

Rows are sorted once along a Hilbert curve over the Web Mercator world
square in large row groups, so a tile's rows are one contiguous run and a
city tile touches one or two row groups. A float32 bbox covering with page
statistics, null for a geometry without finite bounds (null, empty, NaN on a
whole axis or with an infinite coordinate), which no zoom shows, lets a
reader pick pages without reading geometry. Each LOD is a nullable binary
child of the required `lod` struct. Present values pack whole quantized
geometry and boundary copies, and attribute columns beside them hold each
LOD's attribute values; the spatial index selects these pages directly.

Which rows appear at which zoom is a _thinning function_, one shape:

```python
def thin(rows: pa.Table, zoom: int, ctx: Context) -> pa.Table
```

The writer calls it once per zoom with every row, as tippecanoe builds each
zoom from the whole dataset, and it returns the rows to show there; the
finest zoom always shows every row. Membership need not nest across zooms.
Shapes stay shapes at every zoom on a quarter-pixel grid; one too small for
it is stored as a square on its centroid's cell, so zoomed out a thinned
building set reads as a faint density texture in the same fill layer, as
tippecanoe draws it. `rows` carries `id`, centroid, bbox diagonal and area in
Web Mercator metres, vertex count, a per-row uniform `u` hashed from the
geometry, and the attributes; `Context` converts area and size to
screen pixels at a zoom. Returning an attribute column overrides its value in that
zoom's LOD, on the row and on its copies in neighbouring tiles, which is how a
cluster passes its count along. Built-ins are a few lines each and meant to be
copied:

| thinning                        | rule                                                                                                                                                 | good for                                            |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| `drop_rate(2.5)`                | each zoom coarser keeps 1/rate, uniformly, so relative density survives (tippecanoe `-r`); the default                                               | points, small polygons                              |
| `size_threshold(4)`             | keep a feature while it still spans 4 pixels                                                                                                         | large polygons and lines first                      |
| `tiny_polygons(0.25)`           | tippecanoe's tiny-polygon reduction: areas under a quarter-pixel square accumulate per tile and one square is kept per square's worth                | area-true coverage of buildings zoomed out          |
| `importance('population', 300)` | keep the top N per tile by a column                                                                                                                  | named places, ranked facilities                     |
| `cluster(8, count='count')`     | one representative per 8 px cell holding how many it stands for                                                                                      | dense points                                        |
| `density_budget(60_000)`        | one uniform fraction per zoom, sized so the busiest tile holds the budget (tippecanoe `--drop-fraction-as-needed`); takes a function of the zoom too | dense points or sub-pixel buildings at coarse zooms |
| `drop_smallest(600_000)`        | one area threshold per zoom, raised until the busiest tile's estimated bytes fit; inspired by tippecanoe, with different cost estimation             | buildings, before `tiny_polygons`                   |
| `only('risk > 0', inner)`       | restrict eligibility before `inner`                                                                                                                  | partial datasets                                    |
| `combine(a, b)`                 | keep what any part keeps                                                                                                                             | size plus sample, etc.                              |
| `chain(a, b)`                   | apply `b` to what `a` kept                                                                                                                           | `drop_smallest` then `tiny_polygons`                |

Anything the built-ins cannot express is a short function of a table. The
writer's other options (row group and page sizes, compression level) shape
the file, not the row selection. `placeholder_pixels` (`--placeholder-pixels`)
sets the width of the square a shape under a pixel draws as; thinnings that
fold shapes into squares, like `tiny_polygons`, set their own.
`margin_pixels` (`--margin-pixels`, 2 by default) sets how close to a tile a
feature centred elsewhere must come to be copied into it; see
[Geometry](#geometry).

Zooms are derived from the data unless given: the coarsest zoom is one
coarser than where the dataset's whole extent spans about a pixel, the base
zoom a rate-based thinning counts from is where the median feature spans a
pixel, and the finest zoom is where the median vertex spacing spans four
pixels, at most 15. The writer logs the guess and records which zooms actually keep every
row; pass `zooms=` or `complete_from=` to override. Zooms run from 0 to 24,
the depth of the Hilbert key, so every tile is one interval of keys; the
writer refuses others before it starts.

### Attributes

Source columns are kept as they are and stay queryable. The attributes a
renderer styles with, `attributes=` (`--attributes`), are also held per zoom,
in columns a tile reads only when it names the attribute. Every LOD holds
the values of the rows it shows in a nullable typed column whose pages are
cut where the LOD's overview pages are, and slots after the directory leaves
locate them. A row's boundary copies pack its values, so a feature reads the
same in every tile it reaches. A tile that names no attribute requests what
it would from a file without them, apart from the values its boundary copies
pack; where a view spans two level-one directory nodes, a coalescing source
may also fetch the slot block between them.

The default, `'auto'` (`--attributes auto`), keeps every flat number and
boolean column, except integer columns whose values are mostly distinct,
such as ids, and every string column with at most 256 distinct values. It
leaves out the geometry and covering columns, the names thinnings reserve,
and `row` and `geometry`, which `read_overview` returns as its own columns.
`attributes=()` (`--attributes none`) keeps none, and a list keeps the
columns it names, as names or `(name, Arrow type)` pairs. On the Denver
buildings below `'auto'` keeps `height` and `class` and leaves out the string
`id`. A string with at most 256 distinct values is held as an index into the
`values` the metadata lists.

A tile holds the attributes its request names in `properties`, and any
source columns named there. A request that names none, to `renderTile`, the
worker behind `vectorSource` and `tileUrl`, or `tiled-geoparquet-serve`, gets
geometry only, so a map names the attributes it styles by.

On 1,753,860 Denver buildings written with
`chain(drop_smallest(850_000), tiny_polygons())`, a cold tile on a fresh
reader and the next tile east on the same reader, read through the spatial
index (requests / KB; rounds are two cold and one next except where given in
brackets). `columns` keeps `height`; `auto`, the default, keeps `height` and
`class`, and reads a tile styled by `height`, or by nothing, as `columns`
does, within 0.2%; `none` keeps no attribute and reads `height` from its
source column:

| zoom | tile               |   columns: geometry |   columns: `height` |       auto: `class` |              none: `height` |
| ---- | ------------------ | ------------------: | ------------------: | ------------------: | --------------------------: |
| 8    | metro, Denver      |   2 / 259 · 1 / 3.7 |   3 / 388 · 1 / 3.7 |   3 / 284 · 1 / 3.7 |      4 / 2253 (3) · 1 / 3.7 |
| 11   | city, Denver       |   2 / 119 · 1 / 216 |   3 / 145 · 2 / 274 |   3 / 126 · 2 / 224 |   8 / 318 (3) · 8 / 502 (2) |
| 12   | city, Denver       |   2 / 245 · 1 / 265 |   3 / 291 · 2 / 332 |   3 / 256 · 2 / 270 |   9 / 426 (3) · 6 / 424 (2) |
| 13   | downtown           |   2 / 109 · 1 / 186 |   3 / 125 · 2 / 208 |   3 / 119 · 2 / 193 |   7 / 162 (3) · 7 / 214 (2) |
| 13   | suburbs, Montbello |   2 / 138 · 1 / 166 |   3 / 159 · 2 / 188 |   3 / 148 · 2 / 173 |   7 / 197 (3) · 5 / 198 (2) |
| 14   | downtown           |     2 / 48 · 1 / 74 |     4 / 55 · 2 / 82 |     4 / 51 · 2 / 76 |     6 / 73 (3) · 3 / 77 (2) |
| 15   | street, downtown   | 2 / 11.7 · 1 / 15.0 | 4 / 14.7 · 2 / 16.9 | 4 / 14.2 · 2 / 15.5 | 6 / 28.5 (3) · 2 / 18.1 (2) |
| 15   | street, Highland   |   2 / 36 · 1 / 19.4 |     4 / 44 · 2 / 21 |   4 / 40 · 2 / 19.9 |     5 / 60 (3) · 2 / 35 (2) |

A tile styled by an attribute reads, beside its overview leaf and pages, the
leaf's slot holding the attribute leaf that lists the attribute's pages, and
the pages beside its own, in the rounds a geometry-only tile takes: one more
request cold at zooms 8 to 13, two more at zooms 14 and 15, and one more for
the next tile. Styled by `height`, a cold tile reads 14 to 50% more bytes
than with geometry only, the most at zoom 8, where a tile shows 60,105
features. At the zooms showing every row the first styled tile under an
overview leaf reads that leaf's attribute slot, a length per overview page,
about 2 KB at zoom 15, which is most of the 3 KB the small downtown tile
adds there.

Styled by `class`, which only `auto` keeps, a tile reads in two rounds what
`columns` reads from the source column in three, with one to four fewer
requests and 11 to 67% fewer bytes cold; on a reader that has styled another
tile by it, `auto` takes two requests in one round where `columns` takes
three to six in two. A style switch to an attribute fetches its pages over
the tiles already drawn, never their geometry: in the session below,
restyling six drawn zoom 13 tiles by `height` took 4 requests and 154 KB in
two rounds, 0.26 s at 100 ms per request.

The leaves under one level-one directory node lie back to back, followed by
a block of slots: per attribute, one per leaf. A reader finds a leaf's slot
for an attribute from the level-one node locating the leaf, so it fetches the
slot in the leaf's request round, whatever the number of attributes, and a
geometry-only read finds the leaves where it would in a file without
attributes. A slot is a 16-byte header, naming the attribute leaf's length,
the attribute and the leaf, plus the length of the largest attribute leaf of
its directory, so a styled tile reads that many bytes per attribute and leaf,
the slots of consecutive leaves in one range. Each time a tile needs an
attribute leaf, the reader also fetches the attribute's leaves beside the
other overview leaves it holds, so zooming back out over held leaves costs no
extra round.

An overview page none of whose rows has a value of an attribute has no page
of it: its attribute leaf lists it with length 0, a styled tile fetches
nothing for it and gives its own features there a null, and a tile whose
overview pages all lack a value requests no page of the attribute. In the
attribute column, each run of such overview pages in a row group shares one
page of nulls, so an attribute valued on few rows costs few page headers.

In a file with the spatial index, an attribute column holds the values of
the rows an overview page shows on that page's leading rows, in their order,
and nulls after them, so a page says which of its rows hold a value in a few
bytes however few of them the zoom shows. The column is an ordinary nullable
Parquet column, but its values belong to the overviews, whose readers match
them to rows.

Leave the `lod` and `lod_attributes` columns out of every analysis. A query
of the table, in DuckDB or Arrow, that reads an attribute column pairs each
value with a row it does not describe and reports no error. A row's
attribute is in the source column of the same name, which holds every row's
on its own row.

Replaying a 14-step session with 100 ms per request, `auto` took 2.10 s, and
2.20 s with six connections per host, in 31 requests and 5.26 MB; `columns`,
which reads `class` from its source column, took 2.29 s and 2.56 s in 49
requests and 5.56 MB, and PMTiles 0.66 s and 0.96 s in 41 requests and
8.09 MB.

On 155,597,622 US buildings read from S3, with eight float attributes kept
as half floats, a cold 1280 by 800 view styled by two of them took 3
requests at zoom 4, 11 at zoom 7, 15 at zoom 11 and 8 to 11 at zooms 13 and
14, in two rounds, reading 1.52 MB at zoom 4, 0.58 MB at zoom 7 and 3.89 MB
at zoom 11, against 2 to 5 requests and 0.85, 0.36 and 3.12 MB with geometry
only. In Chrome on one laptop a first view, from opening the file to its
last tile, took 0.75 to 1.06 s across those views styled by two attributes,
0.69 to 1.11 s styled by one and 0.70 to 1.00 s with geometry only, read as
[Requests](#requests) describes. The file is 24.4 GB.

Decoding and encoding a warm tile styled by `height` takes 0.1 to 3.5 ms
more than with geometry only from zoom 11, and 20.0 ms against 11.7 ms for
the 60,105 features of the zoom 8 tile; `class` as an attribute takes 30 to
84% less than reading it from the source column. A decoded tile holds arrays
only for the attributes it has read, and geometry-only tiles of `columns`
and `auto` decode within 0.1 ms of `none` ones from zoom 11 and within
0.5 ms at zoom 8. The files are 253.2 MB `columns`, 254.8 MB `auto` and
243.7 MB `none`. `height` takes 9.0 MB of attribute columns and 0.5 MB in
boundary copies, since every LOD, those showing every row too, holds its own
column of each attribute.

Boundary copies pack the attributes, so a geometry-only tile still reads
their values: at most 1.3% more bytes with `height` and `class` than with no
attributes. With six more numeric columns, eight attributes in all
(364.4 MB against 271.5 MB with none), a geometry-only tile takes the
requests and rounds of the file without attributes but reads 2% more bytes
at zoom 11 and 5 to 12% more at zooms 12 to 15, and a styled tile takes the
requests and rounds it takes with two attributes. Where a source has many
cheap columns and styles use few, name those in `attributes`.

Columns that are not attributes, such as strings with more than 256
distinct values, can be read with `readRow`, batched with `readColumns`, or
named in a tile's `properties`. The spatial index gives every flat source
column a directory of its pages by row, so a tile styled by one fetches that
column's pages for its own rows alongside its geometry pages, then the pages
holding its boundary copies' rows once it is decoded. In the table above,
`none` styled by `height` from its source column takes 5 or 6 requests in 3
rounds cold at zooms 14 and 15, where the attribute takes 4 in 2, and the
next tile over 2 or 3 requests in 2 rounds, where the attribute takes 2 in 1.
At sparse zooms the source pages also hold rows thinning dropped, so such a
tile fetches more bytes: 318 KB against 145 KB at zoom 11, and 2,253 KB
against 388 KB at zoom 8.
Nested columns read the footer first. Thinning functions can read any source
column via `columns=` without keeping it as an attribute.
Integer attributes are packed as int64 on boundary copies, so the writer
refuses a uint64 attribute value above 2^63 - 1, on any row, with an error
naming the column and the value. The JS reader decodes them as JavaScript
numbers, exactly only up to 2^53 in magnitude. Past that it raises no error,
and overview tiles and overview `readFeatures` get the value rounded to 53
significant bits. Tiles past the finest zoom read the source column, where a
value beyond 2^53 - 1 in magnitude arrives as a string of its exact digits.
Keep integer attributes inside that range and read wider ones, such as
64-bit ids, with `readRow` or `readColumns`, which return the source value as
a BigInt. Python's `read_overview` returns every integer attribute exactly. A
file has at most 64 attributes.

A float column can be kept at half precision, as
`attributes=[('rps_2011', 'halffloat')]` or `--attributes rps_2011:halffloat`.
The source column keeps its full precision for analysis and row reads, while
tiles, those past the finest zoom too, get each value rounded to the nearest
float16, about three significant digits, two bytes in its attribute columns,
and packed on boundary copies in at most two bytes from 0 up to 2 and three
otherwise. A style threshold compares with the rounded value, so a value
that close to a threshold can land on its other side, and a magnitude under
about 3e-8 rounds to 0. Rounding is to nearest, ties to even, so magnitudes
from 65504 up to 65520 round to 65504, the largest float16; the writer
refuses a finite value that would round to infinity with an error naming the
column and the value, and holds NaN and the infinities as they are.

## Geometry

Points, lines and polygons, one family per file, and a family keeps its
geometry type at every zoom, as tippecanoe does. Lines and polygons keep a
quantized copy per LOD: one per zoom below the complete zoom, where the
thinning changes the row set, and one per two zooms from it up. Each LOD is
one binary column: a row's value packs its geometry on the LOD's pixel grid
as varint ring steps and copies, with their attributes, of neighbouring
features that reach into its tile or within two pixels of it, so a tile can
read a contiguous range of that column. Page, directory, and row-group
boundaries can add requests. Grid coordinates decode as
`offset + scale * integer`; the transform and grid are
COGP's `quantized_geoarrow`, the byte format is this layout's own, a page of
spec. When the full-detail shapes of the whole dataset are small (16 MiB by
default) a single LOD serves every zoom from the complete zoom up instead of
copies, which is what happens for things like counties. Finer than the finest
zoom the reader draws the exact primary geometry, so full detail is always
reachable and costs nothing to store. A directory over the primary geometry's
own pages, routed by key as the overview directories are, sends a tile to the
leaf holding it, which lists every WKB page with a row whose box reaches the
leaf's tiles, cut into boxed runs where the curve jumps. The tile reads the
pages whose runs reach it, together, and the pages of the source columns of
the attributes it returns over the same rows, one read per attribute
alongside, and casts each value to its declared type as the LODs hold it, so
a feature's tile properties match on both sides of the finest zoom, except
an integer outside
the range the reader decodes exactly ([Attributes](#attributes));
`readPrimary`, primary `readFeatures` and row reads return the source values.
WKB pages hold a few hundred features at full precision, about 45 KB each for
OCR buildings, and features reaching in from a neighbouring tile are often in
another page, so these tiles cost more than overview tiles. A tile reads the
leaf holding it, or two when its buffer crosses into another leaf's tiles.
On the US buildings file, whose finest LOD is z14, a cold z15 tile over
Dover, Delaware reads 470 KB in five requests (490 KB in eleven styled by two
attributes) and the next tile east 116 KB in two, and a cold z16 tile 233 KB
in two requests and the next 56 KB, where a cold z14 tile reads 29 KB in two.
In Los Angeles a cold z15 tile reads 651 KB in five requests and a cold z16
tile 448 KB in three, where a cold z14 tile reads 45 KB in two. The reader
keeps up to 16 MiB of the compressed pages it fetched, so zooming further in
over the same place, or across tiles of one large feature, reuses them instead
of fetching them again. To overzoom instead, as PMTiles does, set the map
source's `maxzoom` to the finest zoom in the file's `lod_columns` metadata,
which [`vectorSource`](#in-a-maplibre-app) does.

The writer's margin, `margin_pixels`, sets which features an overview tile
holds beyond its own: a feature lying wholly outside the tile is copied into it
only when it reaches within that many pixels of the tile's edge, 2 by default,
and the file records it as `margin` in its `lod_columns` metadata. A renderer
that clips each tile's drawing to the tile, as MapLibre does for fill and line
layers, can cut a line or outline drawn wider than twice the margin where such
a feature runs along a tile edge, so a style with wide strokes wants a wider
margin: 16 pixels draws 32-pixel strokes whole. tippecanoe's default buffer is
10 pixels of the same tile. Every copy is a whole feature, so a wider margin
costs copies and bytes. For 146,155 Overture building footprints in Denver,
built with `chain(drop_smallest(850_000), tiny_polygons())` and two
attributes, with the bytes of the LOD and attribute columns and the median
overview bytes a cold z14 tile reads (see [Tile sizes](#tile-sizes)):

| `margin_pixels` | copies | overview bytes | file bytes | median z14 tile |
| --------------- | -----: | -------------: | ---------: | --------------: |
| 2               | 14,703 |        6.12 MB |   21.89 MB |           41 KB |
| 8               | 38,622 |        6.53 MB |   22.29 MB |           48 KB |
| 16              | 69,747 |        7.05 MB |   22.81 MB |           54 KB |

The margin runs from 0, where a tile holds only the features that reach into
it, to 128 pixels, a quarter of the tile, where a small feature already
averages more than one copy per zoom. The reader clips each tile's geometry
the margin or 8 pixels past its edge, whichever is more, which adds no
features, and past the finest zoom a tile reads the primary geometry of
features as far out.

## Install

Python 3.12 and Node 22 or later. From a clone:

```bash
uv sync --group dev          # the writer, validator and CLI, with test deps
npm --prefix js ci
npm --prefix js run build
```

Neither package is on PyPI or npm yet; install the Python package with
`uv pip install -e .` or `pip install -e .`, and the JS package from a local
path after building it (`npm install ../tiled-geoparquet/js`).

## Quick start

Pull a city of buildings with the [Overture CLI](https://docs.overturemaps.org/getting-data/overturemaps-py/)
and write it. The example guesses zooms from the data and carries height and
class:

```bash
pip install overturemaps
overturemaps download --bbox=-105.1,39.6,-104.8,39.9 -f geoparquet --type=building -o overture_denver.parquet
uv run python examples/overture_buildings.py overture_denver.parquet overture_denver.lod.parquet
uv run tiled-geoparquet validate overture_denver.lod.parquet
uv run tiled-geoparquet inspect overture_denver.lod.parquet
```

The same from the CLI with a thinning spec, or from Python. In a spec `+`
unions thinnings (`combine`) and `>` chains them (`chain`), as in
`smallest:600000>tiny`:

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
EPSG:4326 with planar edges, as a path or a `*` glob, local or on S3 (`s3://`, with credentials
from the usual AWS chain); the output is a local path. `metadata=` adds
key-value metadata to the file. `write()` builds the overviews, then the tile-aligned pages and
spatial directory, in one call; `lod_page_bytes` (`--lod-page-bytes`) sets the
overview page target, 64 KiB by default. Writing a 155.6 million building
national file (OCR's buildings, nine attributes) took 66 minutes on a
16-core machine with 128 GB of memory, and the build needs free disk of
about twice the output for its intermediate files. The main process sorts,
thins and writes; worker processes quantize and pack the overviews, each row
group split evenly among them in runs of at most 65,536 rows. For the
tile-aligned pages, each worker process encodes one row group of one overview
at a time and the main process appends them in file order, so that pass
writes the same bytes for any `workers`; the main process encodes them itself
when the file holds under 32 MiB of overview values. Workers each
grow to about 1 GB, so `workers` (`--workers`, at most 8 by default) bounds
that part of the memory; `memory_limit` (`--memory-limit`, 25% of RAM by
default) caps DuckDB.

### In a MapLibre app

```js
import * as maplibregl from 'maplibre-gl'
import { registerProtocol, vectorSource, prefetch, readRow } from 'tiled-geoparquet/tiles'

const url = 'https://example.com/buildings.lod.parquet'
prefetch(url) // optional: opens the file in the worker before the map exists
registerProtocol(maplibregl)

map.addSource('buildings', await vectorSource({ url, layer: 'buildings', properties: ['risk'] }))
map.addLayer({ id: 'fill', type: 'fill', source: 'buildings', 'source-layer': 'buildings' })

// the MVT feature id is the file row; read any source columns for it
const row = await readRow(url, feature.id, ['name', 'risk'])
```

`vectorSource(config, { maxzoom })` opens the file and returns a MapLibre
vector source, `{ type: 'vector', tiles: [tileUrl(config)], minzoom, maxzoom,
bounds }`, with limits from the file's metadata. `properties` in the config
names the attributes and source columns its tiles hold. Without it they hold
geometry only, so a style on an attribute names it there. `bounds` is the data's
extent, so the map requests no tile outside it. `maxzoom` is the finest LOD
zoom: past it the map overzooms that LOD's tiles, as it does a PMTiles
archive, and requests nothing more. Pass a higher `maxzoom`, as in
`vectorSource(config, { maxzoom: 18 })`, when shapes must be exact further in:
tiles from the finest LOD zoom up to it draw the primary geometry, a cold one
reading 230 to 650 KB in up to five requests on the US buildings file, where
a finest-LOD tile reads 30 to 45 KB in two ([Geometry](#geometry)). A source made from `tileUrl`
alone has MapLibre's default `maxzoom` of 22 and draws exact geometry at every
zoom past the finest LOD. A file without LODs has only exact geometry and
gets 22. `minzoom` is 0: below the coarsest LOD zoom a tile draws that LOD
over its whole area, so the map does not blank there, though shapes smaller
than the tile's grid drop out as it zooms further out. A `minzoom` on the
source's layers stops those reads.

A map already drawn changes what it styles by without the file changing: give
the source the tile URL of a config naming the new columns,
`map.getSource('buildings').setTiles([tileUrl({ ...config, properties: ['height'] })])`,
and the reader fetches those columns for the tiles in view, keeping the
geometry it has decoded. Change the layers' paint once the map is next idle:
when a paint change follows `setTiles` in the same task, MapLibre 6.18 parses
the tiles it holds again and does not fetch the new ones.

Tile properties keep their types: numbers are MVT floats when float32 holds
them exactly, doubles otherwise, and integers below 2^52 are integers;
booleans stay booleans and bigints a number holds exactly become that number.
An integer attribute past 2^53 in magnitude is rounded in overview tiles
([Attributes](#attributes)).
Other values, such as strings, dates and nested values, are display strings.
Style expressions therefore compare numbers with numbers, as in
`['>', ['get', 'risk'], 0.01]`. MVT has no null, so a null value leaves its
property out of the feature: `['has', 'risk']` is false there and
`['coalesce', ['get', 'risk'], 0]` falls back to 0.

### In other map libraries

The tiles are standard Mapbox Vector Tiles on a 512 px Web Mercator grid.
`fetchTile(url, signal)` returns the bytes of one tile from a `tileUrl`
template with its `{z}`, `{x}` and `{y}` filled in, so any library that lets
you load a tile's bytes yourself can draw the file. In OpenLayers, whose
vector tile grid is 512 px by default:

```js
import VectorTileLayer from 'ol/layer/VectorTile.js'
import VectorTileSource from 'ol/source/VectorTile.js'
import MVT from 'ol/format/MVT.js'
import { fetchTile, vectorSource } from 'tiled-geoparquet/tiles'

const { tiles, maxzoom } = await vectorSource({ url, layer: 'buildings', properties: ['risk'] })
const layer = new VectorTileLayer({
  source: new VectorTileSource({
    format: new MVT(),
    url: tiles[0],
    maxZoom: maxzoom,
    tileLoadFunction: (tile, src) => {
      tile.setLoader(async (extent, resolution, projection) => {
        try {
          const data = await fetchTile(src)
          tile.setFeatures(
            tile.getFormat().readFeatures(data, { extent, featureProjection: projection }),
          )
        } catch {
          tile.onError()
        }
      })
    },
  }),
})
```

`maxZoom` has OpenLayers overzoom the finest LOD, as `maxzoom` does in
MapLibre. Libraries that fetch tile URLs themselves with no loader hook, and
desktop or native clients, read the tiles
[over HTTP](#serving-tiles-over-http).

Tiles are fetched through one shared worker and decoded and encoded in a
small worker pool. `setWorkerCount(1 | 2 | 3 | 4)` from the tiles entry point
overrides the default before calling `prefetch`, `readMetadata`,
`registerProtocol` or `fetchTile`; one disables the pool. Four helps dense viewports but costs
more memory. Exact geometry and analytical row reads stay on the coordinator.
`setSourceOptions({ connections, cache })`, set at the same point, is how files
are requested; see [Requests](#requests).
CPU workers reuse the same `worker.js` URL, so there is no additional worker
asset to deploy. zstd pages decode with WebAssembly, embedded in the bundle as a
`data:` URL; a Content Security Policy must allow `'wasm-unsafe-eval'` in
`script-src` and `data:` in `connect-src`, or pages decode in JavaScript, about
three times slower.

The server must answer HTTP range requests with CORS and expose
`Content-Range`, which a suitably configured object store can do. `tileUrl`
also takes `pointLayer` for point datasets. Opening probes the first 16 KiB for
the spatial index. Indexed files need only that request, and no tile, exact
geometry included, reads their footer. A row read of a flat column fetches a
leaf of that column's row directory, once per reader, and the page holding the
row; a nested column opens the footer in one more request, and files without
the index read a 64 KiB tail and any missing footer bytes. A HEAD is needed only when CORS hides
`Content-Range`.

### Serving tiles over HTTP

`tiled-geoparquet-serve` serves one file, a local path or an http(s) URL, as
tiles any client can fetch. It is the JS package's command: `npx
tiled-geoparquet-serve` where the package is installed, or
`node js/dist/serve-cli.js` from a built clone.

```bash
npx tiled-geoparquet-serve buildings.lod.parquet --layer buildings --properties height,class
```

```
Serving buildings.lod.parquet
  TileJSON  http://127.0.0.1:8080/tiles.json
  Tiles     http://127.0.0.1:8080/{z}/{x}/{y}.mvt
```

| Option               | Default                       | Notes                                                              |
| -------------------- | ----------------------------- | ------------------------------------------------------------------ |
| `--port <number>`    | 8080                          | 0 picks a free port                                                |
| `--host <address>`   | 127.0.0.1                     | loopback only; `0.0.0.0` serves other machines                     |
| `--layer <name>`     | `features`                    | the MVT layer name                                                 |
| `--properties <a,b>` | none: geometry only           | attributes or source columns                                       |
| `--maxzoom <zoom>`   | the finest LOD zoom           | `maxzoom` in `tiles.json`; 22 for a file without LODs              |
| `--public-url <url>` | the address a request came to | where clients reach the server, when a proxy or tunnel is in front |

`/{z}/{x}/{y}.mvt` (or `.pbf`) is the tile `renderTile` gives, as
`application/vnd.mapbox-vector-tile`, at any zoom to 30: the LOD for the zoom,
and exact primary geometry past the finest. A tile with no features is a 200
like any other, a few bytes holding an empty layer, which clients read as an
empty tile; the server sends no 204 or 404 for one. `/tiles.json` is
TileJSON 3.0.0 with the absolute tile URL, the `minzoom`, `maxzoom` and
`bounds` that `vectorSource` gives a map, and the layer's fields in
`vector_layers`. A client that follows it overzooms the finest LOD; raise
`--maxzoom` to have it request exact geometry. A tile address no tile has is
a 400, any other path a 404, and a read that fails a 500 with the error on
stderr.

Tiles are gzipped for clients that accept gzip, which about halves a dense
tile; tiles under 1 KiB are sent as they are. Every response carries
`Access-Control-Allow-Origin: *`, so a page on any origin can read the tiles,
and none carries `Cache-Control`. The command reads a local file as it was at
startup, so restart it after rebuilding the file, and renders each tile on
its one thread with the page and directory caches of [Reader
behaviour](#reader-behaviour) shared between requests.

The command is a thin wrapper. `tileHandler(reader, options)` from
`tiled-geoparquet/server` is the same service as a function from a `Request`
to a `Response`, for a server of your own:

```js
import { createServer } from 'node:http'
import { openReader } from 'tiled-geoparquet'
import { fileSource, nodeListener } from 'tiled-geoparquet/node'
import { tileHandler } from 'tiled-geoparquet/server'

const reader = await openReader(await fileSource('buildings.lod.parquet'))
const handler = tileHandler(reader, { layer: 'buildings', properties: ['height'] })
createServer(nodeListener(handler)).listen(8080, '127.0.0.1')
```

Its options are `layer`, `properties` and `maxzoom` as above, `base` for the
path it is mounted under (`/buildings` answers `/buildings/tiles.json`),
`publicUrl`, and `compress: false` where a proxy or CDN in front compresses.
The request's abort signal cancels the tile's reads. `fileSource(path)` is a
local file as a byte source and `nodeListener(handler)` a `node:http`
listener; a remote file opens with `coalescing(await httpSource([url]))`. The
handler imports nothing from Node, so it is also the fetch handler of
`Bun.serve` or `Deno.serve`. Cloudflare Workers refuses one request the I/O
objects of another, and the reader shares its pending fetches and caches
between tiles, so a Worker opens the reader inside each request, at one more
16 KiB range request per tile, and with `cache: 'default'` so that each tile
sends that request alone and names no cache mode, which a Worker with a
compatibility date before 2024-11-11 refuses:

```js
import { coalescing, httpSource, openReader } from 'tiled-geoparquet'
import { tileHandler } from 'tiled-geoparquet/server'

export default {
  async fetch(request, env) {
    const file = await httpSource([env.FILE_URL], { signal: request.signal, cache: 'default' })
    return tileHandler(await openReader(coalescing(file)), { layer: 'buildings' })(request)
  },
}
```

### Reader behaviour

The coordinator caches up to 16 MiB of compressed data pages and 128 nodes of
each directory kind, and retains up to 64 decoded tiles within an estimated 32 MiB, so a
style change on a loaded view reuses decoded geometry and only fetches and
decodes the new attribute: a source column's pages by row, or a column
attribute's pages over the tile's own rows and, the first time, the attribute
leaf listing them; the CPU workers decode those pages as they decode tiles. A
file with LODs and no spatial index, read through its footer, reads a column
attribute's own values by row once a tile is decoded, a further request round. Neighbouring tiles share pending fetches. When the last consumer of a
pending fetch or decode leaves, that work is cancelled; completed cache entries
stay reusable. These budgets bound cache accounting, not total browser memory.
The sizes are fixed in the reader.

Integer attributes decode as JavaScript numbers, exact up to 2^53 in magnitude;
see [Attributes](#attributes).

### Requests

A tile's requests go out together. S3 and most object stores answer over
HTTP/1.1, one request to a connection, and a browser allows a host six
connections. Whether the requests then run side by side, and whether a later
visit finds them in the browser's cache, rests on three options of
`httpSource`, which `setSourceOptions({ cache, connections, rangeParam })`
sets for the tiles entry point before a file opens:

- `cache`, the cache mode of every request, is `no-store`. Chrome's HTTP
  cache has one request of a URL in flight at a time, so range requests to
  one file sent through it are answered in sequence however many connections
  are open: nine sent together came back 80 ms apart. Past the cache they run
  side by side. A view opened again then reads from the network where the
  cache answers from disk, about 0.5 s against 0.14 s for a zoom 4 view of
  the US buildings. `cache: 'default'` keeps the cache, and without a
  `rangeParam` the worker then spreads requests over the three hostnames S3
  answers on, three at a time.
- `connections`, six, is how many connections to the host are open once the
  file is: five one-byte requests sent beside the file's first read open the
  others, so the first tiles' requests wait on no handshake. One sends none,
  and it is one through the cache without a `rangeParam`.
- `rangeParam`, unset, names a query parameter that carries each request's
  range, as in `?range=20000-20009`. Every range then has a URL of its own,
  so the cache has them in flight side by side and holds each for a later
  visit: requests go through the cache, with six connections open. On the
  Japan buildings in Chrome from R2, a first 1280 by 800 view styled by three
  attributes took 1.19 to 1.52 s with it and 1.24 to 1.54 s past the cache,
  and the same view opened again 0.10 to 0.14 s and 1.08 to 1.27 s. The
  server has to ignore the parameter, as R2 and S3 do, and a signed URL does
  not survive one. A CDN that keys its cache on the query string stores each
  range apart, and a later visit finds in the browser's cache the ranges it
  asks for again byte for byte. Use it for a file that never changes at its
  URL: each range is cached on its own, so once a file is replaced a browser
  can answer some ranges from the file it held and the rest from the new one.
  It is measured in Chrome alone.

First views of the US buildings styled by two attributes, in Chrome from S3,
took 1.21 to 2.31 s through the cache on one URL, 0.99 to 1.57 s through it
over S3's three hostnames, and 0.75 to 1.06 s past it with six connections
opened.

### Plain GeoParquet

The reader also maps a WKB GeoParquet file without LODs, in OGC:CRS84 or
EPSG:4326, through the same `openReader`, `renderTile`, `readFeatures` and
tile entry point. Every zoom then draws the exact primary geometry of the rows that
meet the tile, with no properties unless `properties` names source columns,
and `readFeatures` returns primary rows. Rows are chosen by the bbox covering:
the column the `geo` metadata names (`bbox`, GDAL's `geometry_bbox`), or a
`bbox` column where it names none, as in DuckDB's GeoParquet 2.0. Row groups
are chosen by the covering's statistics and pages by its column index, and a
group without covering statistics is read. A file with no covering is chosen
by Parquet's geospatial statistics on its `GEOMETRY` column, which bound a row
group, and then by each row's WKB bounds, so every page of a chosen group is
read. A chunk without a page index is read whole. Covering and geometry pages
are kept in the page cache (4 and 16 MiB) and attribute values decoded (32 MiB
of chunks), least recently used first out, so a later tile reuses them; a
chunk larger than its budget is fetched again for every tile. That suits files
of a few MB. 31,000 IBTrACS track segments in one row group, sorted by storm
and written with zstd, are 452 KB; a session's tiles read 409 KB in 11
requests, and a tile then renders in about a millisecond, as overview tiles
do.

### Demo

`demo/index.html` is a MapLibre page that reads a file straight from a URL
and shows which LOD each zoom is drawing. Click a feature to read its row.

```bash
cd js && npm run demo
```

That bundles the reader into `demo/dist`, serves the repo at
`http://localhost:8091`, and opens the page pointed at `buildings.lod.parquet`
in the repo root; pass `?url=` for another file, such as the
`overture_denver.lod.parquet` written above. The page must be served; module
scripts do not load from `file://`.

To compare with a local PMTiles archive:

```bash
npm --prefix js run demo:compare -- --lod /path/current.parquet --pmtiles /path/baseline.pmtiles
```

Open the printed localhost URL. The server supports range reads without copying
the files, binds to loopback, and adds the same selectable 0/50/100 ms delay to
each data request. It does not simulate a bandwidth cap. The synchronized panes
show response-body bytes, requests, cumulative tile feature deliveries, and map
idle times. These are not unique visible-feature counts or wire-transfer
measurements. The default blank background keeps basemap loading out of those
timings; add `basemap=1` for a basemap. Map idle includes rendering and is not
a precise first-paint measurement. The style menu lists the GeoParquet file's
attributes and its other source columns. A numeric attribute is coloured by
quantiles of the values in view, or by the stops a `stops=name:0,10,40` query
parameter gives, once per attribute. In the Japan building fixtures the
GeoParquet file keeps 17 of its columns as attributes, the writer's default,
among them `class`, `height` and `subtype`, the three the PMTiles archive
holds in its tiles; `id` is read from the table's rows.

Use **Fresh load** to recreate readers at the current location and style;
**Reset counters** keeps caches warm. Browser HTTP caches can survive Fresh
load. Pan away and return, then switch between geometry, height, and subtype to
feel navigation and attribute reuse. The files have different thinning and
overzoom rules, so compare visual coverage as well as speed. GeoParquet retains
whole overview features and original source rows; this PMTiles archive stops at
z14 and overzooms beyond it. **Copy diagnostics** reports what the page, the
map and the reader's worker are waiting on, for a view that does not finish
loading.

### Validation

`tiled-geoparquet validate file.parquet` checks the layout metadata, the
spatial index version in the footer and in the index root, the bbox
covering's schema and declaration, required page indexes, and up to 2,000 rows
of the first row group: their overview values decode, and each one's covering
is its geometry's bounds rounded outward to float32, null exactly where the
geometry has no finite bounds. Use `--sample-rows N` to change that quick
sample. Use `tiled-geoparquet validate file.parquet --full` (Python:
`validate(path, full=True)`) to decode every overview value in every row group,
including boundary copies. Full mode checks every row's covering the same
way, that the covering and the Hilbert key are null on the same rows, that
polygon rings store
at least three vertices (the closing one is implicit), that grid coordinates fit
int32 and copies' attribute values their declared types, packed byte consumption, copy
source ids and tile tags, finite transforms,
standard page ranges, and the complete spatial directory against the footer and
source keys: metadata, codecs, v1 PLAIN overview page headers, page membership
and order, row spans, cell bounds, fences, and branch bounds. The primary
directory is checked against the geometry's page index and covering, one row
group at a time: its leaves hold every cell of its zoom once, each listing
every page with a row whose box reaches the leaf's tiles, cut into the same
runs wherever it is listed; each run's box is the union of its rows' covering
boxes and holds a row with an extent. The row directories list exactly the flat
columns whose pages decode on their own, with codecs and schema matching the
footer, and each lists every page of its column once, in row order, with the
byte range and rows of its offset index, under branches whose rows match the
nodes they locate. Every indexed page is a data page, not dictionary encoded,
of the rows its index says, its header counting a value or a null for each.
Attributes are checked in both modes against the metadata: the attribute
columns' struct holds a column of the declared type per attribute for
exactly the LODs `attribute_columns` lists, each source column can give the
declared type, and an attribute column holds values only for rows its LOD
shows, on the leading rows of their overview pages in a file with the
spatial index, value indexes within the attribute's `values`. Full mode also
checks that every LOD holds the attributes, that the root gives an attribute
slot size per directory exactly when there are attributes, that the leaves
under each level-one node of every LOD and of the primary directory lie back
to back followed by a slot per attribute and leaf, each holding a length
within the slot, the attribute's position, the leaf's offset, zero padding and an attribute
leaf, the one of each LOD leaf listing the attribute pages beside its pages
and the one of each primary leaf the source pages over its runs' rows, that
each directory's slots are 16 bytes longer than its largest attribute leaf
and no directory node overlaps a slot block, and
that attribute pages are cut where their LOD's pages are, but for one page
per run of overview pages without a value, read from the attribute column,
that an attribute leaf lists length 0 exactly beside those overview pages,
and that attribute pages
are v1 PLAIN pages, or BYTE_STREAM_SPLIT ones in a FLOAT or DOUBLE column, in
the root's attribute codec. Files
without a
spatial directory get the packed-value and offset-index
checks. This is a layout and reader contract check; it does not
prove polygon topology or compare simplification accuracy with primary WKB. Full
mode reads the complete data, holds one LOD column of one row group at a time,
decoding it two megabytes of packed values at a time, parses the geometry
65,536 rows at a time, and holds one row group's covering and keys at a time,
checking the spatial directory, held in memory, against those keys. The returned
`mode` and `checked_non_null` describe the work performed.

### Tile sizes

`write()` logs, and `tiled-geoparquet inspect file.parquet` ends with, how
heavy the tiles of each zoom are, read from the spatial directory without
fetching a data page: the
LOD serving the zoom, the tiles holding rows of their own, the median, 99th
percentile and maximum compressed bytes of overview pages a cold read of one
tile fetches, and the heaviest tile. For 1.75 million buildings around Denver
written with `chain(drop_smallest(850_000), tiny_polygons())` and no
attributes:

```text
compressed bytes of the pages a cold tile read fetches, per zoom:
  zoom  lod  tiles  median     p99     max  largest tile
     0  f0       1    58 B    58 B    58 B  0/0/0
     1  f1       1   290 B   290 B   290 B  1/0/0
     2  f2       1   629 B   629 B   629 B  2/0/1
     3  f3       1  1.1 KB  1.1 KB  1.1 KB  3/1/3
     4  f4       1  2.7 KB  2.7 KB  2.7 KB  4/3/6
     5  f5       1  8.3 KB  8.3 KB  8.3 KB  5/6/12
     6  f6       1   29 KB   29 KB   29 KB  6/13/24
     7  f7       1  102 KB  102 KB  102 KB  7/26/48
     8  f8       2  185 KB  257 KB  258 KB  8/53/97
     9  f9       4  218 KB  329 KB  330 KB  9/107/194
    10  f10      4  206 KB  305 KB  306 KB  10/214/388
    11  f11     12  209 KB  308 KB  309 KB  11/427/776
    12  f12     48  180 KB  302 KB  303 KB  12/857/1554
    13  f13    150  123 KB  192 KB  199 KB  13/1709/3102
    14  f14    532   49 KB  109 KB  113 KB  14/3418/6216
    15  f14  1,980   13 KB   30 KB   36 KB  15/6828/12416
```

A tile's bytes are the pages the reader fetches for it: every page of the
zoom's LOD whose cells meet the tile's key interval, counted whole, boundary
copies included. The directory nodes that locate those pages are not counted,
nor are source columns read for columns that are not attributes. In a
file with attributes a second line per zoom gives, for each, the median
bytes of its pages over a tile's own rows, which a tile styled by it fetches
beside these, none for an overview page without a value; `tile_sizes`
returns them under `attributes`, with the maximum. A tile is listed when the directory has a keyed row inside it.

`tile_sizes(path)` returns the same in Python, a list with one dict per zoom
of `zoom`, `lod`, `tiles`, `median_bytes`, `p99_bytes`, `max_bytes` and
`largest` (`[z, x, y]`), or `None` for a file without a spatial directory;
`inspect(path)['tile_sizes']` holds it too. Tune the thinning against it: a
zoom whose heaviest tile costs more than a tile should wants a smaller budget
there, and `drop_smallest` and `density_budget` both take a function of the
zoom. For scale, tippecanoe holds a compressed tile to 500 KB unless told
otherwise.

### Overviews in Python

`read_overview(path, zoom)` returns what a zoom shows as an ordinary pyarrow
table, one row per shown feature: `row`, the file row index to join source
columns on; `geometry`, the overview shape as WKB in EPSG:3857 metres on that
zoom's grid, its CRS stated in the field's GeoArrow (`geoarrow.wkb`) metadata;
and the attributes as that zoom's LOD holds them, from the LOD's attribute
column or, at an LOD without one, the source column cast to its declared
type. Boundary copies are left out, since each repeats a
row the table already has.

`zoom` must be one of the file's zooms; any other raises `ValueError`. The JS
`readFeatures` clamps an overview query to the file's coarsest or finest LOD,
which suits drawing; an analysis asking for z18 of a file that stops at z15
gets an error rather than z15. A file with an attribute named `row` or
`geometry` also raises `ValueError`, naming the attribute, since it would
collide with those columns; rename it in the source and rewrite the file to
read its overviews here.

```python
import shapely
from tiled_geoparquet import read_overview

z10 = read_overview('buildings.lod.parquet', 10)
shapes = shapely.from_wkb(z10['geometry'].to_numpy(zero_copy_only=False))
```

### Whole-feature reads

```js
import {
  openReader,
  httpSource,
  readFeatures,
  batchToGeoJSON,
  batchToGeoArrow,
  batchToMvt,
} from 'tiled-geoparquet'

const reader = await openReader(await httpSource([url]))
const batch = await readFeatures(reader, {
  bbox: [-105.1, 39.6, -104.9, 39.8],
  resolution: 360 / (512 * 2 ** 12), // longitude degrees per pixel
  columns: ['details.height'],
  geometry: 'overview', // or 'primary' for original WKB and source attributes
})
const features = batchToGeoJSON(batch)
const arrowIPC = batchToGeoArrow(batch, { crs: 'OGC:CRS84' })
const mvt = batchToMvt(batch, { z: 12, x: 852, y: 1555 })
```

Overview queries return bbox-overlapping, deduplicated features and retain LOD
thinning. They do not perform exact intersection tests. The query enumerates
the intersecting tiles, up to 256 by default; see the `maxTiles` option.
Attributes in overview queries are the values the LOD holds; other columns
are projected from the source by row id. Primary queries return original source
attributes. Sparse zoom profiles select the first declared zoom at least as
detailed as the requested resolution, clamped at the finest profile; queries
enumerate tiles at that same declared zoom so boundary copies match.

The result is a renderer-neutral batch with `geometry`, `rowIndex`, `columns`
and `source`. Geometry uses typed x/y buffers and
feature→polygon→ring/line→vertex offsets, with coordinates
`offset + scale * [x, y]`. Overview coordinates remain quantized `Int32Array`
in EPSG:3857 metres, marked by `geometry.crs`; primary WKB becomes
`Float64Array` in longitude/latitude and retains supported Z coordinates.
`batchInDegrees` converts an overview batch to longitude/latitude. Overview
batches may be thinned and simplified.

GeoJSON is an optional object adapter for small results; attribute values retain
their decoded JS types, so BigInt needs conversion before JSON serialization.
GeoArrow produces an Arrow IPC stream through the `@cogp/reader` converter. Both
adapters return longitude/latitude; GeoArrow dequantizes coordinates, uses a GeoArrow multi geometry family, and preserves
scalar attribute types where supported; nested and mixed attributes become
display strings. Mixed geometry families cannot be written in one GeoArrow
geometry column. `batchToMvt` clips the same buffers to one tile, defaults to
layer `shapes`, and uses the same MVT encoder and property semantics as
`renderTile`, which remains the optimized tile-serving path.

### Nested source attributes

`reader.readColumns(rowIds, ['details.height', 'floors[0].name', 'tags.name'])`
preserves requested row order and duplicates. A struct path selects only its
requested descendant leaves; requesting a whole struct reconstructs all its
children. Lists use zero-based nonnegative bracket indexes; requesting the list
itself returns its full array. Maps use string keys with dot notation and return
objects when requested whole. Missing map keys, out-of-range list indexes, null
ancestors and null elements return `null`; empty lists remain `[]` and empty
maps remain `{}`. Unknown struct fields and malformed paths raise errors before
source reads. Literal top-level scalar names take priority over path syntax,
including names containing dots or brackets. Nested names or map keys containing
dots/brackets cannot be addressed individually; request their parent container.
Map keys follow the Parquet decoder's object representation (string coercion and
last value for duplicate keys); non-string or reserved JavaScript object keys
should be accessed with a dedicated Arrow/Parquet analytical reader.

Flat scalars use sparse page projection and a scalar value cache. Nested struct
scalars also read only selected pages. A list or map projection decodes all
pages of the selected collection leaves in each selected row group, so values
crossing page boundaries reconstruct correctly; it materializes full
collections before extracting one index or key and is not cached. Explicit
physical Parquet collection leaf names such as `nums.list.element` return their
low-level wrapped values. The same logical paths work in `readFeatures`
`columns`.

### deck.gl example

A second renderer is hosted at
<https://geoparquet-lod-compare.pages.dev/deck/> and runnable locally at
`http://localhost:8091/demo/deck/index.html?url=/buildings.lod.parquet` after
`npm --prefix js run demo`. `demo/deck/main.js` bundles deck.gl 9.1 with the
GeoArrow deck.gl layers under a `TileLayer`: each tile reads its extent as a
`readFeatures` batch at the tile's zoom, converts it to a GeoArrow table, and
renders through `GeoArrowPolygonLayer` (or the path and scatterplot layers
for lines and points), so coordinates move from Arrow buffers into GPU
attributes without per-feature objects. The tile layer keeps loaded tiles and
their GPU buffers while panning, cancels abandoned loads, and overzooms the
finest overview. Polygon triangulation runs in an earcut worker pool served
from the bundle. Whole features that straddle a tile edge are read by both
tiles, so counts in the status line include those repeats. Outlines are drawn
from zoom 14, where features are larger than the placeholder squares coarse
overviews use. `?view=lon,lat,zoom` opens at a place and `?url=` opens another
file; there is no basemap dependency. For external files use the comparison
server's `/demo/deck/index.html?url=/data/current.parquet` route; the server
needs both `--lod` and `--pmtiles`.
