# Tiled GeoParquet

**One GeoParquet file for a zoomable map and the original analytical data.**

This experimental layout keeps original feature geometries and source attributes
alongside compact, thinned overviews. A spatial page index lets a browser
fetch the relevant Parquet pages without opening the full footer. Ordinary
GeoParquet tools query the original columns; the map uses a custom reader.
That reader also maps any WKB GeoParquet without overviews straight from its
rows, which suits files of a few MB ([plain GeoParquet](docs/usage.md#plain-geoparquet)).

Format draft 0.2.0. The format can still change, and the Python and JavaScript
packages are not published to registries yet.

[Live comparison](https://geoparquet-lod-compare.pages.dev/) ·
[Format specification](spec/SPEC.md) ·
[Spatial index](spec/SPATIAL-INDEX.md) ·
[Writer and reader guide](docs/usage.md) ·
[Comparison with other formats](docs/comparison.md) ·
[Contributing](CONTRIBUTING.md)

## Why this exists

A map archive makes display fast; an analytical table preserves the data needed
for queries and new styles. This project explores whether one file can do both
with competitive map access. The original geometry appears once in the source
column. Display geometry, per-zoom attribute values, and tile-boundary copies
take extra space, but there is no separate archive of pre-encoded MVT tiles.

The intended use is a static dataset that people need to both explore and query.
If map delivery is the only requirement, PMTiles is the simpler choice. Several
other GeoParquet overview designs exist: COGP, Esri's Spatially Optimized
Parquet, tylertoo's overviews, and the GeoParquet LOD extension draft
(PR 299). The [specification](spec/SPEC.md#relation-to-other-proposals)
describes how this layout differs from each;
[prior work and attribution](THIRD_PARTY.md) lists what it builds on.

## Try it

The [hosted demo](https://geoparquet-lod-compare.pages.dev/) shows Tiled
GeoParquet and PMTiles built from the same Japan building source. COGP and Esri panes
are optional: select their checkboxes and click **Apply readers**. Start over
Tokyo, zoom out to see the overviews, then zoom into a neighbourhood. Pan the
maps together and compare the request and byte counters. Switch from geometry
styling to an attribute style to exercise more than geometry delivery. The
browser reads ranges from the hosted files; nothing needs downloading. See the
[hosted comparison notes](docs/hosted-comparison.md) for what the counters
measure and where the files come from.

## Build your own file

Input is GeoParquet with WKB geometry in longitude/latitude (OGC:CRS84 or
EPSG:4326 in x/y order), one geometry family per file. The overviews are
stored in Web Mercator metres. Thinning budgets should be tuned to your data;
the build logs each zoom's [tile sizes](docs/usage.md#tile-sizes) to tune against.

With Python 3.12+ and uv installed, run `uv sync --locked` from a clone.
Save the following as `build.py` and run `uv run python build.py`. Supply your
own `buildings.parquet`. Every cheap source column (numbers, booleans and
strings with at most 256 distinct values) is also kept as an attribute, a
column per zoom level, which a tile reads only when a style names it;
`attributes=['height']` keeps only the columns it names
([Attributes](docs/usage.md#attributes)). The first build may download
DuckDB's spatial extension.

```python
from tiled_geoparquet import chain, drop_smallest, tiny_polygons, write

if __name__ == '__main__':
    write(
        'buildings.parquet',
        'buildings.lod.parquet',
        thinning=chain(drop_smallest(850_000), tiny_polygons()),
    )
```

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

This query counts source features, not the thinned overviews.

To view your output locally, install Node 22+ and run:

```sh
npm --prefix js ci
npm --prefix js run demo
```

The demo opens `buildings.lod.parquet` from the repository root. Click a
building to inspect its source row. It loads MapLibre from a CDN. A tiny
synthetic fixture for checking installation is available in the
[contributor guide](CONTRIBUTING.md#development).

Host the final file at an immutable URL with HTTP Range support and CORS
exposing `Content-Range`. `write()` builds the overviews, tile-aligned pages,
and the spatial directory in one call and replaces the output only after the
build succeeds; allow temporary disk space beside the output for its
intermediate file. The CLI does the same with
`tiled-geoparquet write source.parquet output.parquet`. A generic Parquet rewrite
loses the spatial access path; rebuild from the source GeoParquet instead.

Neither package is on a registry yet: run `npm --prefix js run build` and
install the JS package by path (`npm install ../tiled-geoparquet/js`). Its
worker asset must remain available to your bundler. The tiles are standard
Mapbox Vector Tiles, so any map library that lets you load a tile's bytes
yourself can draw them with `fetchTile`; the
[guide](docs/usage.md#in-other-map-libraries) shows OpenLayers. Clients that
only fetch tile URLs, such as desktop GIS, read them from
[`tiled-geoparquet-serve`](docs/usage.md#serving-tiles-over-http). In a
MapLibre app, after creating the map and loading its style:

```js
import { registerProtocol, vectorSource } from 'tiled-geoparquet/tiles'

registerProtocol(maplibregl)
map.addSource('buildings', await vectorSource({ url, layer: 'buildings', properties: ['height'] }))
map.addLayer({
  id: 'buildings',
  type: 'fill',
  source: 'buildings',
  'source-layer': 'buildings',
})
```

`vectorSource` reads the file's bounds and zoom range into the source. The
map overzooms the finest overview past its zoom, as it does a PMTiles
archive; `vectorSource(config, { maxzoom: 18 })` draws exact geometry from
there to z18, at 230 to 650 KB a cold tile on 155.6 million US buildings
against 30 to 45 KB at the finest overview zoom
([guide](docs/usage.md#in-a-maplibre-app)).

The reader also exposes whole-feature geometry batches with typed coordinate
buffers, plus GeoJSON, GeoArrow IPC, and MVT adapters; the
[deck.gl example](https://geoparquet-lod-compare.pages.dev/deck/) renders the
hosted Japan file outside MapLibre. See the [guide](docs/usage.md) for source
attributes, nested fields, custom thinning, validation, and worker
configuration.

## What we have measured

Four readers against the hosted Japan archives, all derived from the same
53,742,978 Overture building footprints: this layout, released COGP 2.2.2,
Esri's Spatially Optimized Parquet with its reference reader, and, as the
map-delivery reference, PMTiles written by tippecanoe. Each cell is a cold
read on a fresh reader after one open, geometry only, served from the same
public bucket. Bytes are decimal.

| Tile                  | Tiled GeoParquet             | COGP 2.2.2                  | Esri SOP                    | PMTiles                      |
| --------------------- | ---------------------------- | --------------------------- | --------------------------- | ---------------------------- |
| open                  | 16 KB, 1 req                 | 1.50 MB, 3 req              | 2.62 MB, 3 req              | 16 KB, 1 req                 |
| z8 metro, Tokyo       | 0.62 MB, 2 req, 93,801 feat. | 0.12 MB, 8 req, 1,152 feat. | 26.65 MB, 1,048 req, 28,000 | 0.50 MB, 2 req, 95,774 feat. |
| z11 city, Tokyo       | 0.27 MB, 2 req, 23,371       | 0.25 MB, 17 req, 860        | 5.39 MB, 143 req, 177,679   | 0.31 MB, 2 req, 24,373       |
| z13 downtown, Shibuya | 0.31 MB, 2 req, 28,630       | 0.68 MB, 42 req, 2,739      | 2.61 MB, 92 req, 51,996     | 0.35 MB, 2 req, 28,908       |
| z13 suburbs, Chiba    | 0.05 MB, 2 req, 4,689        | 0.44 MB, 34 req, 454        | 1.30 MB, 55 req, 6,841      | 0.06 MB, 2 req, 4,929        |
| z15 street, Tokyo     | 0.01 MB, 2 req, 206          | 0.86 MB, 82 req, 213        | 1.30 MB, 62 req, 229        | 0.03 MB, 2 req, 1,441        |
| z15 street, Osaka     | 0.03 MB, 2 req, 1,261        | 0.80 MB, 80 req, 1,287      | 1.01 MB, 57 req, 1,362      | 0.11 MB, 2 req, 6,523        |

The four files do not agree on what a tile should contain, so bytes only mean
something next to the feature counts. COGP's conversion keeps very few features
at coarse levels; PMTiles stops at z14 and serves the overzoomed parent at z15;
Esri's reader returns every feature in the extent, simplified. This layout's
thinning budgets were calibrated against PMTiles feature counts, as the
[Japan evaluation](docs/japan-evaluation.md) describes, so the two agree within
a few percent where both serve native tiles. [The comparison page](docs/comparison.md)
has definitions, provenance, and reproduction steps.

These are single-tile reads on one dataset from one network; they show
competitive request counts and transfer sizes, not a general speed ranking.

## Main tradeoffs

- **Size and construction.** The evaluated Japan file is 6.32 GB, versus the
  4.84 GB analytical source and 1.30 GB PMTiles archive. Those files provide
  different capabilities. Building overviews and indexing is an offline step;
  updates require rebuilding. On a 64 GB laptop the Japan build takes 10
  minutes, and 155.6 million US buildings with nine attributes take 66
  minutes on a 16-core machine with 128 GB of memory, with free disk of
  about twice the output.
- **A custom display encoding and index.** Ordinary tools read original
  geometries and attributes, but cannot interpret the overview payloads. There
  is one implementation, a draft spec, and no independent conformance suite.
  Compatibility has been exercised with PyArrow and DuckDB, not every Parquet
  reader or rewriter. [Design alternatives](docs/design-alternatives.md)
  records what standard encodings cost in the same role.
- **Attribute styles cost requests.** The writer keeps each cheap source
  column as a column per zoom level, which a tile reads only when it names
  the attribute. On 1.75 million Denver buildings, a cold tile styled by
  `height` takes one or two more requests than with geometry only, in the
  same two rounds, and reads 14 to 50% more bytes; the next tile takes one
  more request. On 155.6 million US buildings read from S3 in Chrome, a first
  view styled by two half-float attributes took 3 to 15 requests in two
  rounds and 0.75 to 1.06 s, against 2 to 5 requests and 0.70 to 1.00 s with
  geometry only. That is with a tile's requests sent past the
  browser's HTTP cache, which answers the range requests of one URL one at a
  time, and six connections opened as the file opens, the reader's defaults
  ([Requests](docs/usage.md#requests)); a view opened again then reads from
  the network where the cache would answer from disk. Switching drawn tiles
  to an attribute fetches its pages, never their geometry: six zoom 13 tiles
  took 4 requests, 154 KB and 0.26 s at 100 ms per request. Boundary copies
  pack every attribute, so a geometry-only tile reads their values too, up
  to 1.3% more bytes with two attributes and 12% more with eight. Columns
  not kept this way, such as strings with more than 256 distinct values,
  integer ids and nested fields, are read from the source column on demand,
  at one more request round and, at zooms where thinning drops most rows, up
  to 5.8 times the bytes; nested fields read the footer first.
  [Design alternatives](docs/design-alternatives.md#where-an-attribute-sits)
  records packing attributes into every overview row instead.
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
  Full validation checks overview payloads and index consistency, but does not
  prove polygon topology, source-data equality, or finest-LOD completeness.

## Help evaluate the idea

Useful contributions are independent reader implementations, reproducible
comparisons on other datasets and devices, adversarial geometry fixtures, and
ways to simplify the layout or integrate its access ideas into the other
overview proposals. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests,
and what to include in performance reports. The [logical format](spec/SPEC.md)
and [physical index profile](spec/SPATIAL-INDEX.md) are separate design
surfaces for discussion.

## License

Project code is MIT. The vendored Esri comparison reader is Apache-2.0 and
retains its license. External datasets have their own terms; see
[THIRD_PARTY.md](THIRD_PARTY.md).
