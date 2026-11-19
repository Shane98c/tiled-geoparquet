# tiled-geoparquet

[![CI](https://github.com/Shane98c/tiled-geoparquet/actions/workflows/ci.yml/badge.svg)](https://github.com/Shane98c/tiled-geoparquet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

Tiled GeoParquet is one GeoParquet file for a zoomable map and the original
analytical data. This experimental layout keeps original feature geometries
and source attributes alongside compact, thinned overviews. A spatial page
index lets a browser fetch the relevant Parquet pages without opening the
full footer. Ordinary GeoParquet tools query the original columns; the map
uses a custom reader. That reader also maps any WKB GeoParquet without
overviews straight from its rows, which suits files of a few MB
([plain GeoParquet](js/README.md#plain-geoparquet)).

Format draft 0.2.0. The format can still change. The Python and JavaScript
packages take the format's version, so 0.2.x reads and writes format 0.2.

[Live comparison](https://tiled-geoparquet.pages.dev/) ·
[Format specification](spec/SPEC.md) ·
[Spatial index](spec/SPATIAL-INDEX.md) ·
[Writer guide](docs/usage.md) ·
[Reader guide](js/README.md) ·
[Comparison with other formats](docs/comparison.md) ·
[Contributing](CONTRIBUTING.md)

## demo

See the [demo](https://tiled-geoparquet.pages.dev/), which shows Tiled
GeoParquet and PMTiles built from the same Japan building source. Panes for
two other GeoParquet overview formats, COGP and Esri's Spatially Optimized
Parquet, are optional: select their checkboxes and click **Apply readers**.
Start over Tokyo, zoom out to see the overviews, then zoom into a
neighbourhood. Pan the maps together and compare the request and byte
counters. The style menu lists the source's flat columns except `id`: switch from
geometry to any of them to exercise more than geometry delivery. The browser
reads ranges from the hosted files; nothing needs downloading. See the
[comparison](docs/comparison.md#the-hosted-page) for what the counters
measure and where the files come from. Code for the demo is in the `/demo`
folder.

## background

A map archive makes display fast; an analytical table preserves the data needed
for queries and new styles. This project explores whether one file can do both
with competitive map access. The original geometry appears once in the source
column. Display geometry, per-zoom attribute values, and tile-boundary copies
take extra space, but there is no separate archive of pre-encoded MVT tiles.

![Layout of a Tiled GeoParquet file: a table with source and overview columns, and the byte ranges a map reads for one tile](docs/layout.svg)

The figure draws the file as a table and as bytes. The row count and column
sizes are those of the Japan buildings file used in
[measurements](#measurements); which rows are filled and how wide the byte
segments are is schematic. It shows the simplest read of an overview tile
from a newly opened file, in three numbered steps: the first 16 KB of the
file, which hold the index root and are read once; a leaf of the spatial
directory, the file's own index of its pages; and the tile's pages in the
overview columns. A tile that spans several leaves reads each of them, and a
leaf the reader already holds costs no request. A map style that uses an
attribute kept in the overviews also reads that attribute's entry in the
directory and its pages. DuckDB and PyArrow open the file through its Parquet
footer. The tile read uses neither the footer nor Parquet's page indexes.

A map over the file can style by any column of the table without the file
being rebuilt. The writer keeps each cheap column per zoom, which a styled
tile reads in the same request rounds as its geometry, and the reader fetches
any other column from the table's rows.

The intended use is a static dataset that people need to both explore and query.
If map delivery is the only requirement, PMTiles is the simpler choice. Several
other GeoParquet overview designs exist: COGP, Esri's Spatially Optimized
Parquet, tylertoo's overviews, GeoParquet with baked PMTiles, and the
GeoParquet LOD extension draft (PR 299). The [specification](spec/SPEC.md#relation-to-other-proposals)
describes how this layout differs from each;
[prior work and attribution](THIRD_PARTY.md) lists what it builds on.

## data requirements

Input is GeoParquet with WKB geometry in longitude/latitude (OGC:CRS84 or
EPSG:4326 in x/y order), one geometry family per file. The overviews are
stored in Web Mercator metres.

Host the final file at an immutable URL with HTTP Range support and CORS. A
generic Parquet rewrite loses the spatial access
path; rebuild from the source GeoParquet instead.

## install

Neither package is on a registry yet, so both install from a clone of this
repository. The writer needs Python 3.12+ and uv, and the reader Node 22+.

```sh
uv sync --locked
npm --prefix js ci
npm --prefix js run build
```

To use the reader in an app, install the built package by path
(`npm install ../tiled-geoparquet/js`). Its worker asset must remain
available to your bundler.

## usage

### write a file

Save the following as `build.py` in the repository root and run
`uv run python build.py`. Supply your own `buildings.parquet`. The first
build may download DuckDB's spatial extension.

```python
from tiled_geoparquet import chain, drop_smallest, tiny_polygons, write

if __name__ == '__main__':
    write(
        'buildings.parquet',
        'buildings.lod.parquet',
        thinning=chain(drop_smallest(850_000), tiny_polygons()),
    )
```

`write()` builds the overviews, tile-aligned pages, and the spatial directory
in one call and replaces the output only after the build succeeds; allow
temporary disk space beside the output for its intermediate file. The CLI
does the same with `tiled-geoparquet write source.parquet output.parquet`.

Thinning budgets should be tuned to your data; the build logs each zoom's
[tile sizes](docs/usage.md#tile-sizes) to tune against. Every cheap source
column (numbers, booleans and strings with at most 256 distinct values) is
also kept as an attribute, a column per zoom level, which a tile reads only
when a style names it. `attributes=['height']` keeps only the columns it
names, and `['auto', 'id']` keeps the default columns plus an `id` column,
which the default leaves out, for a map to join on
([Attributes](docs/usage.md#attributes)).

### query the file

The output still contains the original geometries and all source attributes.
For example, with `class` and `height` columns in the source, query the same
file with DuckDB from `uv run python`:

```python
import duckdb

duckdb.sql("""
    SELECT class, count(*) AS buildings, avg(height) AS mean_height
    FROM read_parquet('buildings.lod.parquet')
    GROUP BY class
""").show()
```

The table has one row per source feature, so this counts the original
buildings.

The file has more columns for the map reader, which hold the overview tiles:
`lod` (level of detail), and `lod_attributes` when the file keeps attributes.
`lod` is packed binary geometry, and `lod_attributes` holds attribute values
for each zoom level. Its fields are named like the source columns, but in a
file made by `write()` the values are grouped at the start of each page and
not placed on the rows they describe ([Attributes](docs/usage.md#attributes)). A
query has no use for either column, so name the columns it needs, or drop
them with `SELECT * EXCLUDE (lod, lod_attributes)` in DuckDB (`EXCLUDE (lod)`
for a file without attributes) or a `columns=` list in PyArrow.

### view it locally

With the reader built, run:

```sh
npm --prefix js run demo:compare -- --lod /path/buildings.lod.parquet
```

This serves the comparison page on port 8094 and prints its address. The
page opens on the whole extent of the file and loads MapLibre from a CDN.
`--pmtiles` adds a pane for a PMTiles archive of the same data. A tiny
synthetic fixture for checking installation is available in the
[contributor guide](CONTRIBUTING.md#development).

### add it to a map

In a MapLibre app, after creating the map and loading its style:

```js
import { registerProtocol, sourceUrl } from 'tiled-geoparquet/tiles'

registerProtocol(maplibregl)
map.addSource('buildings', {
  type: 'vector',
  url: sourceUrl({ url, layer: 'buildings', properties: ['height'] }),
})
map.addLayer({
  id: 'buildings',
  type: 'fill',
  source: 'buildings',
  'source-layer': 'buildings',
})
```

MapLibre asks for the source as it is added, which opens the file and gives
the source its bounds and zoom range. The
map overzooms the finest overview past its zoom, as it does a PMTiles
archive; `sourceUrl(config, { maxzoom: 18 })` draws exact geometry from
there to z18, at 230 to 650 KB a cold tile on 155.6 million US buildings
against 30 to 45 KB at the finest overview zoom
([guide](js/README.md#in-a-maplibre-app)).

The tiles are standard Mapbox Vector Tiles, so any map library that lets you
load a tile's bytes yourself can draw them with `fetchTile`; the
[guide](js/README.md#in-other-map-libraries) shows OpenLayers. Clients that
only fetch tile URLs, such as desktop GIS, read them from
[`tiled-geoparquet-serve`](js/README.md#serving-tiles-over-http).

The reader also exposes whole-feature geometry batches with typed coordinate
buffers, plus GeoJSON, GeoArrow IPC, and MVT adapters; the
[deck.gl example](https://tiled-geoparquet.pages.dev/deck/) renders the
hosted Japan file outside MapLibre. See the [writer guide](docs/usage.md) for
custom thinning, attributes and validation, and the
[reader guide](js/README.md) for nested fields and worker configuration.

## measurements

Five readers against the hosted Japan archives, all derived from the same
53,742,978 Overture building footprints: this layout, released COGP 2.2.2,
Esri's Spatially Optimized Parquet with its reference reader, GeoParquet with
baked PMTiles with its reader, and, as the map-delivery reference, PMTiles
written by tippecanoe. The open row is what a reader fetches to start, once
per page load. Each tile row is what a newly opened reader then fetches for
that one tile, from the same public bucket. This layout's tiles are read
with `class`, `height` and `subtype`, the three attributes the PMTiles tiles
hold; the COGP and Esri readers read geometry only. Bytes are decimal.

| Tile                  | Tiled GeoParquet             | COGP 2.2.2                  | Esri SOP                    | Baked PMTiles                | PMTiles                      |
| --------------------- | ---------------------------- | --------------------------- | --------------------------- | ---------------------------- | ---------------------------- |
| open                  | 16 KB, 1 req                 | 1.50 MB, 3 req              | 2.62 MB, 3 req              | 16.46 MB, 4 req              | 16 KB, 1 req                 |
| z8 metro, Tokyo       | 0.63 MB, 5 req, 93,801 feat. | 0.12 MB, 8 req, 1,152 feat. | 26.65 MB, 1,048 req, 28,000 | 0.50 MB, 2 req, 95,774 feat. | 0.50 MB, 2 req, 95,774 feat. |
| z11 city, Tokyo       | 0.28 MB, 7 req, 23,371       | 0.25 MB, 17 req, 860        | 5.39 MB, 143 req, 177,679   | 0.31 MB, 2 req, 24,373       | 0.31 MB, 2 req, 24,373       |
| z13 downtown, Shibuya | 0.31 MB, 8 req, 28,630       | 0.68 MB, 42 req, 2,739      | 2.61 MB, 92 req, 51,996     | 0.35 MB, 2 req, 28,908       | 0.35 MB, 2 req, 28,908       |
| z13 suburbs, Chiba    | 0.06 MB, 8 req, 4,689        | 0.44 MB, 34 req, 454        | 1.30 MB, 55 req, 6,841      | 0.06 MB, 2 req, 4,929        | 0.06 MB, 2 req, 4,929        |
| z15 street, Tokyo     | 0.02 MB, 8 req, 206          | 0.86 MB, 82 req, 213        | 1.30 MB, 62 req, 229        | 0.19 MB, 10 req, 378         | 0.03 MB, 2 req, 1,441        |
| z15 street, Osaka     | 0.04 MB, 8 req, 1,261        | 0.80 MB, 80 req, 1,287      | 1.01 MB, 57 req, 1,362      | 0.29 MB, 10 req, 1,593       | 0.11 MB, 2 req, 6,523        |

The five files do not agree on what a tile should contain, so bytes only mean
something next to the feature counts. COGP's conversion keeps very few
features at coarse levels; Esri's reader returns every feature in the extent,
simplified; PMTiles stops at z14 and serves the overzoomed parent at z15.
Baked PMTiles carries the PMTiles file's tiles through z13 and builds tiles
from the Parquet rows from z14. To build them its page reads the file's
Parquet footer when it loads, which is 16.37 MB of its 16.46 MB open. This layout's
thinning budgets were calibrated against PMTiles feature counts
([the build](docs/comparison.md#the-tiled-geoparquet-build)), so the two agree
within a few percent where both serve native tiles. Its attributes are
columns of their own, which a tile reads in further requests when a style
names them. When its tiles are read geometry only, each one in the table
takes 2 requests, and the z8 tile reads 0.56 MB and the z11 tile 0.26 MB
([styled tiles](docs/comparison.md#styled-tiles)).

To measure whole views as well as single tiles, each of the four readers
the [hosted page](https://tiled-geoparquet.pages.dev/) shows was loaded
alone at Tokyo zoom 11 and moved through seven pans, zooms and jumps
([the path](docs/comparison.md#a-pan-and-zoom-path)). The baked-PMTiles file
has no pane on that page.

|                       | Tiled GeoParquet | COGP 2.2.2                      | Esri SOP            | PMTiles          |
| --------------------- | ---------------- | ------------------------------- | ------------------- | ---------------- |
| First view, Tokyo z11 | 2.22 MB, 9 req   | 2.19 to 2.24 MB, 33 to 35 req   | 18.27 MB, 289 req   | 2.55 MB, 14 req  |
| Seven moves           | 7.58 MB, 36 req  | 7.55 to 7.74 MB, 372 to 388 req | 48.46 MB, 955 req   | 8.51 MB, 51 req  |
| Whole path            | 9.80 MB, 45 req  | 9.74 to 9.96 MB, 405 to 423 req | 66.73 MB, 1,244 req | 11.06 MB, 65 req |

The Tiled GeoParquet pane is coloured by `height`; shapes only, its path
reads 9.59 MB in 29 requests. The PMTiles tiles hold their three attributes
whichever one is shown, and the COGP and Esri panes draw shapes only. As
with single tiles, the panes do not draw the same buildings in a view. The
COGP pane's counts differed between runs, so its cells give the range over
four runs. [The comparison page](docs/comparison.md) has definitions,
provenance, and reproduction steps.

These are reads of one dataset from one network; they show competitive
request counts and transfer sizes, not a general speed ranking.

## tradeoffs

- **Size and construction.** The evaluated Japan file is 6.42 GB, versus the
  4.84 GB analytical source and 1.30 GB PMTiles archive. Those files provide
  different capabilities. Building overviews and indexing is an offline step;
  updates require rebuilding. On a 64 GB laptop the Japan build takes 11
  minutes, and 155.6 million US buildings with nine attributes take 66
  minutes on a 16-core machine with 128 GB of memory, with free disk of
  about twice the output.
- **A custom display encoding and index.** Ordinary tools read original
  geometries and attributes, but cannot interpret the overview payloads, and
  the per-zoom attribute columns among them decode as typed values on rows
  they do not describe, so an analysis has to leave them out. There
  is one implementation, a draft spec, and no independent conformance suite.
  Compatibility has been exercised with PyArrow and DuckDB, not every Parquet
  reader or rewriter. [Design alternatives](docs/design-alternatives.md)
  records what standard encodings cost in the same role.
- **Attribute styles cost requests.** The writer keeps each cheap source
  column as a column per zoom level, which a tile reads only when it names
  the attribute. On 1.75 million Denver buildings, a cold tile styled by
  `height` takes one or two more requests than with geometry only, in the
  same two rounds, and reads 14 to 50% more bytes. Columns not kept this
  way, such as strings with more than 256 distinct values, integer ids and
  nested fields, are read from the source column on demand, at one more
  request round and, at zooms where thinning drops most rows, up to 5.8
  times the bytes.
  [What attribute columns cost](docs/design-alternatives.md#what-attribute-columns-cost)
  has the measurements, and
  [where an attribute sits](docs/design-alternatives.md#where-an-attribute-sits)
  the layouts they were chosen over.
- **Requests go past the browser's HTTP cache.** That cache answers the
  range requests of one URL one at a time, so by default a tile's requests
  skip it, and a view opened again reads from the network where the cache
  would answer from disk. The `rangeParam` option gives each range a URL of
  its own, so the requests run side by side through the cache and a view
  opened again reads from it ([Requests](js/README.md#requests)).
- **Overviews are selective.** Thinning and quantization serve display; they
  are not an exact analytical summary. Whole-feature overview queries preserve
  thinning and use bbox overlap, not exact intersection. Use original geometry
  for exact work. The Japan density tuning is not yet a general policy.
- **Tile margins cost copies.** An overview tile holds a neighbouring
  feature only when it reaches within the file's margin of the tile's edge, 2
  pixels by default, where tippecanoe's default buffer is 10. MapLibre clips
  fill and line layers to each tile, so a line or outline drawn wider than
  twice the margin can be cut where a feature runs along a tile edge. The
  writer's `margin_pixels` widens it, but each copy is a whole feature: for
  146,155 Denver buildings, 16 pixels make 4.7 times the copies of 2, 15%
  more overview bytes and a 4% larger file.
- **Browser work and memory.** Pages must be decoded and converted to MVT, with
  caches and a worker pool. Low-memory devices and prolonged sessions need more
  testing.
- **Coverage and maturity.** Performance work focuses on buildings. Points and
  lines have correctness fixtures, but large overlapping polygons, antimeridian
  behaviour, unthinnable data, and broader CRS needs require further evaluation.
  Full validation checks overview payloads, index consistency and that the
  finest overview shows every row, but does not prove polygon topology or
  source-data equality.

## contributing

Issues, questions and pull requests are welcome. The format is an
experimental draft, so feedback on the design is as useful as code, for
example a dataset where it does poorly or an idea for making the layout
simpler. [CONTRIBUTING.md](CONTRIBUTING.md) has setup and test instructions.

## thanks

This layout builds on other work on GeoParquet overviews and vector tiles.
[COGP](https://github.com/Kanahiro/cloud-optimized-geoparquet) and
[GeoParquet PR 305](https://github.com/opengeospatial/geoparquet/pull/305)
informed the quantized coordinate transform, and the reader uses
`@cogp/reader` and adapts its MVT, clipping and geometry-builder code. The
per-resolution geometry columns and page-index-based spatial access draw on
[GeoParquet PR 299](https://github.com/opengeospatial/geoparquet/pull/299)
and [Esri's Spatially Optimized Parquet](https://github.com/Esri/spatially-optimized-parquet).
[Tippecanoe](https://github.com/felt/tippecanoe) inspired the zoom
selection, feature dropping and tiny-polygon display.
[Prior work and attribution](THIRD_PARTY.md) has the details and the
dependencies. LLMs of several makes aided in the coding and debugging of this
library.

## license

All the code in this repository is
[MIT](https://choosealicense.com/licenses/mit/)-licensed, except the vendored
Esri comparison reader, which is Apache-2.0 and retains its license. We
request that you please provide attribution if reusing any of our digital
content (graphics, logo, articles, etc.). External datasets have their own
terms; see [THIRD_PARTY.md](THIRD_PARTY.md).

## about us

CarbonPlan is a nonprofit organization that uses data and science for climate
action. We aim to improve the transparency and scientific integrity of
climate solutions with open data and tools. Find out more at
[carbonplan.org](https://carbonplan.org/) or get in touch by
[opening an issue](https://github.com/Shane98c/tiled-geoparquet/issues/new)
or [sending us an email](mailto:hello@carbonplan.org).
