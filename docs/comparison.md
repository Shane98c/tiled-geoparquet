# Comparison with other formats

Five readers against the archives behind the [hosted demo](hosted-comparison.md),
all derived from the same 53,742,978 Japan building footprints:

- **Tiled GeoParquet**: the [Japan evaluation](japan-evaluation.md) build, read
  through its spatial index.
- **COGP 2.2.2**: a fresh conversion with the released writer
  (`--webmerc-minzoom 10`, otherwise defaults), read with `@cogp/reader` 2.1.4
  the way its MapLibre adapter does.
- **PMTiles**: tippecanoe 2.82 with
  `-zg -Z 0 --drop-smallest-as-needed --extend-zooms-if-still-dropping`.
- **Esri SOP**: Esri's hosted Spatially Optimized Parquet file, read with the
  reference MapLibre reader from its repository, which queries the tile extent
  at the tile's resolution and returns GeoJSON.
- **Baked PMTiles**: [GeoParquet with baked PMTiles](https://github.com/gsueur/geoparquet-pmtiles)
  at commit `b8e4734`, built with its scripts in the order its parcels file was
  built and read with its MapLibre reader. The archive baked in is the PMTiles
  file above cut to the zooms below 14, with zoom 14 tiles of the widest 2% of
  buildings; from zoom 14 its reader builds tiles from the Parquet rows.

Each reader opens once; every tile is then read on a fresh reader, geometry
only where the reader can leave attributes out ([Styled tiles](#styled-tiles)),
over the public internet from the same R2 bucket. Bytes are response
bodies in decimal megabytes; "next tile" is the tile one column east on the
same reader. Elapsed times are not reported because one run from one laptop
is not a CDN benchmark.

| Reader           | Open            |
| ---------------- | --------------- |
| Tiled GeoParquet | 0.02 MB, 1 req  |
| COGP             | 1.50 MB, 3 req  |
| Esri SOP         | 2.62 MB, 3 req  |
| PMTiles          | 0.02 MB, 1 req  |
| Baked PMTiles    | 16.46 MB, 4 req |

Tiled GeoParquet opens with one request for its 16 KiB index root.
Baked PMTiles opens with a 64 KB read of the file's end, which finds the
archive, two requests for the archive's header and metadata, and the Parquet
footer, 16.37 MB over this file's 3,281 row groups, which its page reads at
load for the tiles it builds from rows.

| Tile                  | Tiled GeoParquet                              | COGP                            | Esri SOP                             | PMTiles                                       | Baked PMTiles                                 |
| --------------------- | --------------------------------------------- | ------------------------------- | ------------------------------------ | --------------------------------------------- | --------------------------------------------- |
| z8 metro, Tokyo       | 0.56 MB, 2 req, 93,801 feat.; next 0.02 MB, 1 | 0.12 MB, 8 req, level 0, 1,152  | 26.65 MB, 1,048 req, level 8, 28,000 | 0.50 MB, 2 req, 95,774 feat.; next 0.02 MB, 1 | 0.50 MB, 2 req, 95,774 feat.; next 0.02 MB, 1 |
| z11 city, Tokyo       | 0.26 MB, 2 req, 23,371; next 0.15 MB, 1       | 0.25 MB, 17 req, level 1, 860   | 5.39 MB, 143 req, level 12, 177,679  | 0.31 MB, 2 req, 24,373; next 0.17 MB, 1       | 0.31 MB, 2 req, 24,373; next 0.17 MB, 1       |
| z13 downtown, Shibuya | 0.30 MB, 2 req, 28,630; next 0.23 MB, 1       | 0.68 MB, 42 req, level 3, 2,739 | 2.61 MB, 92 req, level 14, 51,996    | 0.35 MB, 2 req, 28,908; next 0.26 MB, 1       | 0.35 MB, 2 req, 28,908; next 0.26 MB, 1       |
| z13 suburbs, Chiba    | 0.05 MB, 2 req, 4,689; next 0.08 MB, 1        | 0.44 MB, 34 req, level 3, 454   | 1.30 MB, 55 req, level 14, 6,841     | 0.06 MB, 2 req, 4,929; next 0.08 MB, 1        | 0.06 MB, 2 req, 4,929; next 0.08 MB, 1        |
| z15 street, Tokyo     | 0.01 MB, 2 req, 206; next 0.02 MB, 1          | 0.86 MB, 82 req, level 5, 213   | 1.30 MB, 62 req, level 16, 229       | 0.03 MB, 2 req, 1,441; next 0.11 MB, 1        | 0.19 MB, 10 req, 378; next 0.14 MB, 3         |
| z15 street, Osaka     | 0.03 MB, 2 req, 1,261; next 0.03 MB, 1        | 0.80 MB, 80 req, level 5, 1,287 | 1.01 MB, 57 req, level 16, 1,362     | 0.11 MB, 2 req, 6,523; next 0.13 MB, 1        | 0.29 MB, 10 req, 1,593; next 0.29 MB, 7       |

## Reading the table

The five files do not agree on what a tile should contain, so bytes only mean
something next to the feature counts.

- COGP keeps very few features at coarse levels in this conversion: its writer
  assigns each feature to the coarsest level where it is visible and defers
  features until their simplified geometry survives. Reading a fine level means
  reading every coarser level's row groups that touch the view, so requests
  grow with the level count.
- PMTiles stops at z14 and serves the overzoomed parent at z15, which is why
  its street tiles hold more features than the others.
- Esri SOP stores every feature at every level. Its reader skips geometries
  that collapse at the chosen level's resolution and stops at 650,000
  features per query at any zoom, which no measured tile reached. It reads
  many pages per view.
- Baked PMTiles serves the PMTiles file's own tiles through z13, so those rows
  equal the PMTiles column. From z14 it reads the Parquet pages whose boxes
  meet the tile, 512 rows each, after two index reads per row group, and the
  z14 tile of the wide buildings, whose features its street tiles count over
  that whole tile.
- This layout's thinning budgets were calibrated against PMTiles feature
  counts at z8, z11 and z12, as the [Japan evaluation](japan-evaluation.md)
  describes, so the two agree within a few percent where both serve native
  tiles; this layout's counts run lower because its tiles hold boundary
  copies only within two pixels of the edge. It transfers fewer bytes from z11
  up; the remaining byte difference at z8 comes from page layout and geometry
  encoding, not from showing more buildings.

Counts and visual coverage both matter: different thinning, tile buffers, and
overzoom rules can make smaller responses do less work. These single-tile reads
do not establish whole-session or cross-device parity.

## Styled tiles

A PMTiles tile holds the attributes its tiler was given, here `class`,
`height` and `subtype` in every tile, and the baked tiles through z13 are the
same tiles. This layout keeps 17 of the source's columns as attributes, a
column per zoom each, which a tile reads only when its style names the
attribute, as further ranges in the same two request rounds. A map can
therefore style by any of them, or by any other column from the table's rows,
without the file being rebuilt. The same tiles with
`PROPERTIES=class,height,subtype`, which has this layout's tiles hold the
three the PMTiles tiles hold:

| Tile                  | Tiled GeoParquet, geometry only | Tiled GeoParquet, the same three | PMTiles, three attributes       |
| --------------------- | ------------------------------- | -------------------------------- | ------------------------------- |
| z8 metro, Tokyo       | 0.56 MB, 2 req; next 0.02 MB, 1 | 0.63 MB, 5 req; next 0.02 MB, 3  | 0.50 MB, 2 req; next 0.02 MB, 1 |
| z11 city, Tokyo       | 0.26 MB, 2 req; next 0.15 MB, 1 | 0.28 MB, 7 req; next 0.15 MB, 4  | 0.31 MB, 2 req; next 0.17 MB, 1 |
| z13 downtown, Shibuya | 0.30 MB, 2 req; next 0.23 MB, 1 | 0.31 MB, 8 req; next 0.23 MB, 4  | 0.35 MB, 2 req; next 0.26 MB, 1 |
| z13 suburbs, Chiba    | 0.05 MB, 2 req; next 0.08 MB, 1 | 0.06 MB, 8 req; next 0.08 MB, 3  | 0.06 MB, 2 req; next 0.08 MB, 1 |
| z15 street, Tokyo     | 0.01 MB, 2 req; next 0.02 MB, 1 | 0.02 MB, 8 req; next 0.02 MB, 4  | 0.03 MB, 2 req; next 0.11 MB, 1 |
| z15 street, Osaka     | 0.03 MB, 2 req; next 0.03 MB, 1 | 0.04 MB, 8 req; next 0.04 MB, 4  | 0.11 MB, 2 req; next 0.13 MB, 1 |

The three attributes add 0.07 MB at z8 and 0.02 MB or less from z11, in three
to six more requests. With them this layout reads more bytes than PMTiles at
z8 and no more from z11. Styled by one attribute, a zoom 8 tile takes one
more request than its geometry and a tile from zoom 11 two.

## What each format stores

| Format           | Row layout                                      | Which rows at coarse zooms                                         | How a tile is found                                           | Overview geometry                                                                               |
| ---------------- | ----------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Tiled GeoParquet | One Hilbert order; every feature once           | Per-zoom thinning, need not nest; recorded in nullable LOD columns | Spatial directory in the first 16 KiB over tile-aligned pages | Packed varint rows on a quantized grid per LOD, with boundary copies; attribute columns per LOD |
| COGP             | Coarse-to-fine row-group prefixes; feature once | Cumulative: a level adds row groups                                | Row-group prefix, then bbox statistics and page index         | Optional `quantized_geoarrow` nested int32 column                                               |
| Esri SOP         | XZ-code clustering; feature once                | All features at every level                                        | Page index predicate skipping on the cluster code             | Per-level protobuf columns, Douglas-Peucker simplified                                          |
| tylertoo         | `level` column with simplified WKB rows         | Per-level selection, written once per level in duplicating mode    | Level column and bbox                                         | WKB                                                                                             |
| PMTiles          | Pre-cut MVT tiles                               | tippecanoe's per-zoom dropping                                     | Directory lookup                                              | Gzipped MVT per tile: integer coordinates on a 4096 grid, simplified and clipped per zoom       |
| Baked PMTiles    | One Hilbert order, widest features last; once   | The tiler's selection in each overview tile                        | PMTiles directory; from the detail zoom, bbox page index      | Gzipped MVT per overview tile; from the detail zoom, tiles built in the browser from WKB rows   |

The [specification](../spec/SPEC.md#relation-to-other-proposals) discusses the
design differences and where this layout could fold into those proposals.

## Reproducing

Bundle the Esri reference reader and the baked-PMTiles reader for Node once,
then point the harness at the hosted releases:

```sh
cd js && npm ci && npm run build
npx esbuild vendor/esri-sop/index.ts --bundle --platform=node --format=esm --outfile=test/esri-node.mjs
git clone https://github.com/gsueur/geoparquet-pmtiles /tmp/geoparquet-pmtiles
git -C /tmp/geoparquet-pmtiles checkout b8e4734
npm --prefix /tmp/geoparquet-pmtiles install
npx esbuild /tmp/geoparquet-pmtiles/web/baked.js --bundle --platform=node --format=esm --outfile=test/baked-node.mjs
npx esbuild /tmp/geoparquet-pmtiles/web/src/detail-worker.js --bundle --format=esm --outfile=test/baked-worker-node.mjs
R=https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/
BASE=${R}6c3a4f9/ LOD=${R}5503c5a/japan_buildings.parquet \
  COGP=${R}6c3a4f9/japan_buildings.cogp-2.2.2.parquet \
  BAKED=${R}d51954c/japan_buildings.baked.parquet \
  PMTILES=japan_buildings.pmtiles node test/compare-readers.mjs
```

`SKIP=cogp,esri,baked` leaves readers out, and the baked reader's two bundles
are needed only when it runs. `PROPERTIES=class,height,subtype` gives the
[styled tiles](#styled-tiles). The archives, their sizes and checksums are
listed in the [hosted comparison notes](hosted-comparison.md#data-provenance).
