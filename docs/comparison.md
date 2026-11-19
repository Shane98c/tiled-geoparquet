# Comparison with other formats

Five readers against the archives behind the [hosted page](#the-hosted-page),
all derived from the same 53,742,978 Japan building footprints:

- **Tiled GeoParquet**: [the build described below](#the-tiled-geoparquet-build),
  read through its spatial index.
- **COGP 2.2.2**: a fresh conversion with the released writer
  (`--webmerc-minzoom 10`, otherwise defaults), read with `@cogp/reader` 2.1.4
  the way its MapLibre adapter does.
- **Esri SOP**: Esri's hosted Spatially Optimized Parquet file, read with the
  reference MapLibre reader from its repository, which queries the tile extent
  at the tile's resolution and returns GeoJSON.
- **Baked PMTiles**: [GeoParquet with baked PMTiles](https://github.com/gsueur/geoparquet-pmtiles)
  at commit `b8e4734`, built with its scripts in the order its parcels file was
  built and read with its MapLibre reader. The archive baked in is the PMTiles
  file of the next entry cut to the zooms below 14, with zoom 14 tiles of the
  widest 2% of buildings; from zoom 14 its reader builds tiles from the Parquet
  rows.
- **PMTiles**, the map-delivery reference: tippecanoe 2.82 with
  `-zg -Z 0 --drop-smallest-as-needed --extend-zooms-if-still-dropping`.

The open table is what each reader fetches to start. Every tile is then read
on a newly opened reader, with the open left out of its figures, over the
public internet from the same S3 bucket. The PMTiles tiles hold `class`,
`height` and `subtype`, and the baked file serves those tiles through z13, so
this layout's tiles are read with the same three
([Styled tiles](#styled-tiles)). The COGP and Esri readers read geometry
only. Bytes are response bodies in decimal megabytes; "next tile" is the tile
one column east on the same reader. A COGP or Esri cell also names the level
of its file that the reader read for the tile. Elapsed times are not reported for single
tiles: one run from one laptop is not a CDN benchmark.
[Timed first views and moves](#timed-first-views-and-moves) gives medians of
repeated browser loads for two of the readers.

| Reader           | Open            |
| ---------------- | --------------- |
| Tiled GeoParquet | 0.02 MB, 1 req  |
| COGP             | 1.50 MB, 3 req  |
| Esri SOP         | 2.62 MB, 3 req  |
| Baked PMTiles    | 16.46 MB, 4 req |
| PMTiles          | 0.02 MB, 1 req  |

A reader opens once per page load, whatever is viewed after it. Tiled
GeoParquet opens with one request for its 16 KiB index root. COGP and Esri
SOP open by reading their files' Parquet footers, 1.50 and 2.62 MB. Baked
PMTiles opens with a 64 KB read of the file's end, which finds the archive,
two requests for the archive's header and metadata, and the Parquet footer,
16.37 MB over this file's 3,281 row groups, which its page reads at load for
the tiles it builds from rows. This layout's file has a 1.27 MB footer, which
Parquet tools read and its map reader does not.

| Tile                  | Tiled GeoParquet                              | COGP                            | Esri SOP                             | Baked PMTiles                                 | PMTiles                                       |
| --------------------- | --------------------------------------------- | ------------------------------- | ------------------------------------ | --------------------------------------------- | --------------------------------------------- |
| z8 metro, Tokyo       | 0.63 MB, 5 req, 93,801 feat.; next 0.02 MB, 3 | 0.12 MB, 8 req, level 0, 1,152  | 26.65 MB, 1,048 req, level 8, 28,000 | 0.50 MB, 2 req, 95,774 feat.; next 0.02 MB, 1 | 0.50 MB, 2 req, 95,774 feat.; next 0.02 MB, 1 |
| z11 city, Tokyo       | 0.28 MB, 7 req, 23,371; next 0.15 MB, 4       | 0.25 MB, 17 req, level 1, 860   | 5.39 MB, 143 req, level 12, 177,679  | 0.31 MB, 2 req, 24,373; next 0.17 MB, 1       | 0.31 MB, 2 req, 24,373; next 0.17 MB, 1       |
| z13 downtown, Shibuya | 0.31 MB, 8 req, 28,630; next 0.23 MB, 4       | 0.68 MB, 42 req, level 3, 2,739 | 2.61 MB, 92 req, level 14, 51,996    | 0.35 MB, 2 req, 28,908; next 0.26 MB, 1       | 0.35 MB, 2 req, 28,908; next 0.26 MB, 1       |
| z13 suburbs, Chiba    | 0.06 MB, 8 req, 4,689; next 0.08 MB, 3        | 0.44 MB, 34 req, level 3, 454   | 1.30 MB, 55 req, level 14, 6,841     | 0.06 MB, 2 req, 4,929; next 0.08 MB, 1        | 0.06 MB, 2 req, 4,929; next 0.08 MB, 1        |
| z15 street, Tokyo     | 0.02 MB, 8 req, 206; next 0.02 MB, 4          | 0.86 MB, 82 req, level 5, 213   | 1.30 MB, 62 req, level 16, 229       | 0.19 MB, 10 req, 378; next 0.14 MB, 3         | 0.03 MB, 2 req, 1,441; next 0.11 MB, 1        |
| z15 street, Osaka     | 0.04 MB, 8 req, 1,261; next 0.04 MB, 4        | 0.80 MB, 80 req, level 5, 1,287 | 1.01 MB, 57 req, level 16, 1,362     | 0.29 MB, 10 req, 1,593; next 0.29 MB, 7       | 0.11 MB, 2 req, 6,523; next 0.13 MB, 1        |

## Reading the table

The five files do not agree on what a tile should contain, so bytes only mean
something next to the feature counts.

- COGP keeps very few features at coarse levels in this conversion: its writer
  assigns each feature to the coarsest level where it is visible and defers
  features until their simplified geometry survives. Reading a fine level means
  reading every coarser level's row groups that touch the view, so requests
  grow with the level count.
- Esri SOP stores every feature at every level. Its reader skips geometries
  that collapse at the chosen level's resolution and stops at 650,000
  features per query at any zoom, which no measured tile reached. It reads
  many pages per view.
- Baked PMTiles serves the PMTiles file's own tiles through z13, so those rows
  equal the PMTiles column. From z14 it reads the Parquet pages whose boxes
  meet the tile, 512 rows each, after two index reads per row group. It also
  reads the z14 tile of the wide buildings, and the feature counts of its
  street tiles include every feature of that tile.
- PMTiles stops at z14 and serves the overzoomed parent at z15, which is why
  its street tiles hold more features than the others.
- This layout's thinning budgets were calibrated against PMTiles feature
  counts at z8, z11 and z12 ([the build](#the-tiled-geoparquet-build)), so the
  two agree within a few percent where both serve native
  tiles; this layout's counts run lower because its tiles hold boundary
  copies only within two pixels of the edge. With the same three attributes it
  reads more bytes than PMTiles at z8 and no more from z11, in five to eight
  requests where a PMTiles tile takes two. Of the 0.13 MB difference at z8,
  0.07 MB is the three attribute columns and the rest comes from page layout
  and geometry encoding, not from showing more buildings.

Counts and visual coverage both matter: different thinning, tile buffers, and
overzoom rules can make smaller responses do less work.
[A pan and zoom path](#a-pan-and-zoom-path) measures whole views for the four
readers the hosted page shows.

## Styled tiles

A PMTiles tile holds the attributes its tiler was given, here `class`,
`height` and `subtype` in every tile, and the baked tiles through z13 are the
same tiles. This layout keeps 17 of the source's columns as attributes, a
column per zoom each, which a tile reads only when its style names the
attribute, as further ranges in the same two request rounds. A map can
therefore style by any of them, or by any other column from the table's rows,
without the file being rebuilt. The table above reads this layout's tiles
with the three the PMTiles tiles hold (`PROPERTIES=class,height,subtype`).
This table sets those figures beside the same tiles read geometry only:

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

## The hosted page

The public comparison at <https://tiled-geoparquet.pages.dev/> draws four of
the five files in MapLibre panes side by side; the baked-PMTiles file has no
pane there. It opens with Tiled GeoParquet and PMTiles over Tokyo
at zoom 11, coloured by `class`, with no basemap. The reader checkboxes and
**Apply readers** choose any combination of Tiled GeoParquet, PMTiles,
released COGP and Esri SOP, and a pane that is not selected neither opens its
reader nor requests its file. The selection is in the URL
(`panes=lod,pmtiles,cogp,esri`, where `lod` is Tiled GeoParquet). The readers
request data directly from the public S3 bucket, with no simulated latency or
bandwidth cap, and the files have identical immutable cache settings. The
view and style controls are those of the [local comparison](usage.md#demo).

The strip under the maps shows how many bytes each reader has requested since
the page loaded, or since the counters were last reset. It counts response
bodies, including any served from the browser's HTTP cache, so these are not
wire-transfer totals. All panes follow the same camera, so the totals cover
the same path.

Under each bytes bar is a thinner bar that shows how long that pane took to
draw the view. The time bars share one scale. To the right of each row is the
number of bytes and requests that the last move added on top of what the page
already had, which can be zero.

The Tiled GeoParquet bar is divided by what the bytes are: the file's index,
the shapes drawn on the map, each attribute column that a style has used, and
any other column that the reader looked up in the file's full table. The
page takes the colours for `class` from the summary of that attribute in the
file's metadata ([Attributes](usage.md#attributes)).

**Fresh load** reloads the page, which clears the readers' caches but not the
browser's HTTP cache. **Reset counters** keeps the caches, so you can compare
navigation from that point.

By default the readers' requests skip the browser's HTTP cache. Every load is
then a first visit, and the times reflect only the network and the reader. The
COGP reader is the exception: its released reader always uses the cache.
Ticking **Browser cache** (`cache=on`) sends every reader's requests through the
cache, so reopening a view reads from it. Chrome's cache handles one request
per URL at a time, so range requests that a reader sends to a single URL are
answered one after another. In this mode the Tiled GeoParquet reader puts each
range in the URL ([Requests](usage.md#requests)), so every request has a URL
of its own and none waits on another. The PMTiles library sends all its ranges
to one URL, as it does on any page.

The page leaves `id` out of its style menu (`--hide id`): looked up for a
zoomed-out view, that column of unique strings reads hundreds of megabytes,
429 MB for one zoom 8 tile. The Esri reference reader returns geometry
without source attributes, so its pane stays in geometry styling. The pane
shows that reader's feature limit, and the time it reports for a first view
runs until the data is drawn.

## A pan and zoom path

Each of the four panes of the [hosted page](#the-hosted-page) was loaded
alone at Tokyo zoom 11 (139.76, 35.68) and then moved through seven views: a
pan of half a screen east to 139.98, 35.68, zooms in to 12 and to 13, a jump
to Shibuya (139.7, 35.66) at zoom 13, a zoom in to 14.5, a zoom out to 9 over
Tokyo, and a jump to Osaka (135.5, 34.69) at zoom 12. The figures are the page's counts
of response bodies in headless Chrome at 1280 by 900, each run in a new
browser profile with the cache off. The first view includes the reader's
open. The Tiled GeoParquet, Esri and PMTiles panes read the same bytes and
requests in each of two runs. The COGP pane's counts differed between runs,
so its cells give the range over four runs.

|                       | Tiled GeoParquet | COGP                            | Esri SOP            | PMTiles          |
| --------------------- | ---------------- | ------------------------------- | ------------------- | ---------------- |
| First view, Tokyo z11 | 2.22 MB, 9 req   | 2.19 to 2.24 MB, 33 to 35 req   | 18.27 MB, 289 req   | 2.55 MB, 14 req  |
| Seven moves           | 7.58 MB, 36 req  | 7.55 to 7.74 MB, 372 to 388 req | 48.46 MB, 955 req   | 8.51 MB, 51 req  |
| Whole path            | 9.80 MB, 45 req  | 9.74 to 9.96 MB, 405 to 423 req | 66.73 MB, 1,244 req | 11.06 MB, 65 req |

The Tiled GeoParquet pane is coloured by `height` and so reads that
attribute's columns. Coloured by `class` its path reads 9.83 MB in 49
requests, by `subtype` 9.80 MB in 48, and shapes only 9.59 MB in 29. The
PMTiles tiles hold all three attributes whichever one is shown, and the COGP
and Esri panes draw shapes only. The panes do not draw the same buildings in
a view, as the feature counts of the single tiles show.
[Timed first views and moves](#timed-first-views-and-moves) gives each
move's bytes and time for Tiled GeoParquet and PMTiles. To repeat a run, `https://tiled-geoparquet.pages.dev/?panes=cogp&center=139.76,35.68&zoom=11`
loads one pane, and the strip under the map shows the bytes and requests read
since the page loaded.

## Timed first views and moves

The time for a first view is measured from the request for the file until the
map is idle. The page makes its maps while the Tiled GeoParquet file opens,
and adds each reader's source to a map that is already running. The table
gives medians of nine loads from S3 on 2026-11-17, for the two readers that
the page shows by default. Each load used a new browser profile with one
reader on the page, in headless Chrome at 1280 by 900 with software
rendering, both readers styled by `height` with the same colour stops
(`style=height&stops=height:2.4,4.1,6.9,8.4,17.9`). A new profile has an
empty cache, so the cache-on columns show a first visit through the cache and
not a reopened view. The Tokyo views are centred on 139.76, 35.68 and the
Shibuya view on 139.7, 35.66 (`center=139.76,35.68&zoom=8`). The laptop that
ran them was 60 to 80 ms from the bucket's region by ping.

| View        | Tiled GeoParquet, cache off | PMTiles, cache off | Tiled GeoParquet, cache on | PMTiles, cache on |
| ----------- | --------------------------: | -----------------: | -------------------------: | ----------------: |
| Tokyo z8    |                      1.15 s |             0.89 s |                     1.16 s |            1.31 s |
| Tokyo z11   |                      1.34 s |             1.09 s |                     1.31 s |            2.18 s |
| Shibuya z13 |                      1.43 s |             1.20 s |                     1.49 s |            2.52 s |

With the cache on, PMTiles took 1.1 to 1.3 s longer at z11 and z13 than with
it off. The cache is the only difference between those two columns: it answers
the library's range requests to one URL one after another.

A first view takes each reader three rounds of requests: the head of the
file, a piece of its index or directory, then the data. With the cache off at
zoom 11, coloured by `class`, both readers spent about 0.63 s on the data
round. Tiled GeoParquet took 0.58 s to reach it and PMTiles 0.41 s: the index
round of Tiled GeoParquet is three requests, one of which opened a second
connection, where the PMTiles directory round is one request on a connection
that was already open. After the last byte, the Tiled GeoParquet pane was
idle in 0.11 s and the PMTiles pane in 0.06 s.

After its first view of Tokyo at zoom 11, each reader alone was moved through
the views below with the cache off. Each move is timed from the jump to the
map's next idle, and the table gives medians of seven runs with the bytes and
requests the move added.

| Move                        | Tiled GeoParquet       | PMTiles                 |
| --------------------------- | ---------------------- | ----------------------- |
| Pan half a screen east, z11 | 0.18 s, 0.18 MB, 3 req | 0.16 s, 0.19 MB, 3 req  |
| Zoom in to z12              | 0.55 s, 0.94 MB, 5 req | 0.46 s, 1.04 MB, 7 req  |
| Zoom in to z13              | 0.50 s, 0.61 MB, 5 req | 0.40 s, 0.67 MB, 7 req  |
| Jump to Shibuya, z13        | 0.71 s, 2.85 MB, 9 req | 0.49 s, 3.27 MB, 12 req |
| Zoom in to z14.5            | 0.60 s, 0.56 MB, 5 req | 0.40 s, 0.51 MB, 5 req  |
| Zoom out to z9 over Tokyo   | 0.98 s, 1.00 MB, 5 req | 0.33 s, 1.10 MB, 11 req |
| Jump to Osaka, z12          | 0.84 s, 1.44 MB, 4 req | 0.43 s, 1.73 MB, 6 req  |
| Back to the first view      | 0.33 s, nothing read   | 0.33 s, nothing read    |

A move that needs data takes Tiled GeoParquet two rounds of requests, a piece
of its index and then the data. PMTiles takes one round when it already
holds the directory for that place and two when it has to fetch it. The Tiled
GeoParquet reader also asks for neighbouring pages in one request, and at
this distance from the bucket one large response arrives more slowly than the
same bytes over several connections. In one traced jump to Osaka, the reader's
request of 1.31 MB took 0.9 s to arrive after its first byte, and the six
tile requests of PMTiles, 1.7 MB in all, took 0.3 s after theirs. The zoom
out and the two jumps, where the gap is widest, are the moves with the
largest responses.

S3 serves these files over HTTP/1.1, and Chrome opens at most six connections
to one host, so readers loaded on the same page share those six. With the
cache off and the two readers side by side in half-width panes, the first
view of Tiled GeoParquet took up to 0.3 s longer than the same half-width
pane loaded alone. That of PMTiles took up to 0.25 s less: its requests
used connections the other reader had opened, and opened one or none of
their own against four when it loaded alone. PMTiles finished first in every
view, both alone and together. To time one reader, load it alone.

S3 sent the first byte of a range about 0.1 s after the request for each of
the five files, whatever the file's size. Stores differ in this. Copies of
the five files on Cloudflare R2, timed the same day, answered in about 0.15 s
for the 1.3 GB PMTiles archive and in 0.2 to 0.3 s for the four files of 6 to
9 GB, a difference a reader of one of the larger files would pay in every
round of requests.

## What each format stores

| Format           | Row layout                                      | Which rows at coarse zooms                                         | How a tile is found                                           | Overview geometry                                                                               |
| ---------------- | ----------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Tiled GeoParquet | One Hilbert order; every feature once           | Per-zoom thinning, need not nest; recorded in nullable LOD columns | Spatial directory in the first 16 KiB over tile-aligned pages | Packed varint rows on a quantized grid per LOD, with boundary copies; attribute columns per LOD |
| COGP             | Coarse-to-fine row-group prefixes; feature once | Cumulative: a level adds row groups                                | Row-group prefix, then bbox statistics and page index         | Optional `quantized_geoarrow` nested int32 column                                               |
| Esri SOP         | XZ-code clustering; feature once                | All features at every level                                        | Page index predicate skipping on the cluster code             | Per-level protobuf columns, Douglas-Peucker simplified                                          |
| tylertoo         | `level` column with simplified WKB rows         | Per-level selection, written once per level in duplicating mode    | Level column and bbox                                         | WKB                                                                                             |
| Baked PMTiles    | One Hilbert order, widest features last; once   | The tiler's selection in each overview tile                        | PMTiles directory; from the detail zoom, bbox page index      | Gzipped MVT per overview tile; from the detail zoom, tiles built in the browser from WKB rows   |
| PMTiles          | Pre-cut MVT tiles                               | tippecanoe's per-zoom dropping                                     | Directory lookup                                              | Gzipped MVT per tile: integer coordinates on a 4096 grid, simplified and clipped per zoom       |

The [specification](../spec/SPEC.md#relation-to-other-proposals) discusses the
design differences and where this layout could fold into those proposals.

## The Tiled GeoParquet build

The overview policy applies `drop_smallest` followed by `tiny_polygons`, with
the writer's default attributes kept for styling: 17 of the source's columns,
every flat number and boolean and every string of at most 256 distinct
values. It uses one Hilbert order, full detail from z14 in one LOD serving
z14 and z15, 16 KiB pages for the source columns and 64 KiB overview pages. Past z15
the reader draws the primary geometry; the hosted page's source has `maxzoom`
15, so MapLibre overzooms z15 there. The indexed
file is **6.42 GB**, compared with **4.84 GB** for the analytical source and
**1.30 GB** for PMTiles. These totals represent different capabilities.

Estimated-byte thinning budgets were calibrated against PMTiles feature counts:
1,080,000 at z8, 730,000 at z11, and 750,000 at z12. Other zooms use 1,300,000
through z8 and 850,000 above it. These are Japan-specific construction choices,
not format requirements or general library defaults. They tune area-based
selection rather than reproduce Tippecanoe's compressed-tile feedback algorithm.

### Display density

The density profiles compare matching XYZ tiles of the two archives, with no
properties requested from this layout's reader. Counts include tile-boundary copies, which the two archives draw
differently: a GeoParquet tile carries neighbouring features within two pixels
of its edge, a PMTiles tile within tippecanoe's default buffer of ten.
Calibration uses eight locations across Japan. Validation uses separate tiles
around Hiroshima, Sendai, Kyoto, Naha, Narita and rural Niigata; coarse tiles
shared with calibration are excluded, leaving four validation tiles at z8.

| Sample      | Zoom | GeoParquet features | PMTiles features | Difference |
| ----------- | ---: | ------------------: | ---------------: | ---------: |
| Calibration |    8 |             242,477 |          249,738 |     -2.91% |
| Calibration |   11 |              85,050 |           89,253 |     -4.71% |
| Calibration |   12 |             133,715 |          138,574 |     -3.51% |
| Validation  |    8 |              37,889 |           39,060 |     -3.00% |
| Validation  |   11 |              43,254 |           44,244 |     -2.24% |
| Validation  |   12 |              65,245 |           67,332 |     -3.10% |

Counting only features wholly inside each tile, GeoParquet has 1.4% to 2.2%
more than PMTiles in every row. These aggregate counts do not guarantee
identical feature selection or visual coverage. At z8, Chiba and Sapporo have
about 12% fewer features than PMTiles in all, but 1.2% and 1.4% more inside the
tile. The raw profiles also record summed polygon area and interior polygon
area quantiles; summed area is not raster union coverage.

### Integrity

The build recipe is [`benchmarks/write-japan-tippecanoe.py`](../benchmarks/write-japan-tippecanoe.py).
It takes source, staging-output, and indexed-output paths. It uses private build
phases to check intermediate integrity; the public `write()` API performs the
complete build in one call. See the [usage guide](usage.md) for that workflow.

Source versus writer output: all 53,742,978 rows pass count, XOR and sum checks
of whole-row hashes across all 23 source analytical columns, including IDs and
original geometry. This is a multiset checksum, not an exact equality proof.
Writer output versus indexed output: the schema metadata is equal and every
column of every row compares equal, bbox values included, except the attribute
columns. The indexed file holds a page's attribute values on the page's leading
rows, and all 340,809,878 of them compare equal in order.

Recorded measurements and integrity checks are in
[`benchmarks/dropping/`](../benchmarks/dropping/). `demo-density*.json` profile
the uncalibrated baseline build the budgets were calibrated from against PMTiles
and `calibrated-density*.json` this build, each on the calibration and
held-out tile sets. `ranges.json` holds cold single-tile reads of local
copies at z8, z11 and z12, where `prefix` labels the baseline build, `single`
this build's GeoParquet file and `pmtiles` the archive.

## Files

Objects live under `tiled-geoparquet/releases/<tag>/` in the
`carbonplan-share` S3 bucket, in us-west-2. The tag is the commit whose recipe
produced the object: `5503c5a` built the GeoParquet file with
`benchmarks/write-japan-tippecanoe.py`; `d51954c` built the baked-PMTiles
file with `benchmarks/baked-pmtiles/build.py`; the PMTiles and COGP archives
were converted at `6c3a4f9`, and the Esri SOP file, uploaded at that release,
is Esri's original.

| Dataset       | Release   |         Bytes | MD5                                |
| ------------- | --------- | ------------: | ---------------------------------- |
| GeoParquet    | `5503c5a` | 6,418,176,583 | `c7dc8a0b139fddc8c795404fcc6775b7` |
| PMTiles       | `6c3a4f9` | 1,297,122,745 | `0b733b7511dda51102c1b58a4983706b` |
| COGP 2.2.2    | `6c3a4f9` | 9,081,139,765 | `f9a986b09224256534b530ed80dd245a` |
| Esri SOP      | `6c3a4f9` | 6,177,950,661 | `6224bdf08e1915e367baa61ee566f650` |
| Baked PMTiles | `d51954c` | 6,337,852,318 | `08388a4ba5add01de04cf4bcb8cd9477` |

- [GeoParquet](https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/5503c5a/japan_buildings.parquet)
- [PMTiles](https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.pmtiles)
- [Released COGP](https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.cogp-2.2.2.parquet)
- [Esri SOP](https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.sop.parquet)
- [Baked PMTiles](https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/d51954c/japan_buildings.baked.parquet)
- [Deployment manifest](https://tiled-geoparquet.pages.dev/release.json)

Full archive sizes include different capabilities and should not be confused
with viewport transfer sizes.

- COGP is read with the unmodified released `@cogp/maplibre` and
  `@cogp/reader` 2.1.4. Its file is a fresh conversion with the released
  COGP writer 2.2.2 (`cogp-rs-v2.2.2`, commit
  `44f29be34d303fbb1febb154413a5aa7aa568a91`).
- The baked-PMTiles file was built from the same source by
  `benchmarks/baked-pmtiles/build.py`, which runs the scripts of
  [gsueur/geoparquet-pmtiles](https://github.com/gsueur/geoparquet-pmtiles)
  at commit `b8e4734` and bakes in the PMTiles archive hosted here.
- Esri SOP is Esri's original Japan file, read with the reference MapLibre
  reader from upstream commit `10ff683874db8b2d99c0f03b98354b5ea5f8199c`. Its
  ten implementation files match the upstream Git blob hashes; only the
  dataset-list type is reduced to the fields this demo uses.

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
R=https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/
BASE=${R}6c3a4f9/ LOD=${R}5503c5a/japan_buildings.parquet \
  COGP=${R}6c3a4f9/japan_buildings.cogp-2.2.2.parquet \
  BAKED=${R}d51954c/japan_buildings.baked.parquet \
  PMTILES=japan_buildings.pmtiles PROPERTIES=class,height,subtype \
  node test/compare-readers.mjs
```

`SKIP=cogp,esri,baked` leaves readers out, and the baked reader's two bundles
are needed only when it runs. Without `PROPERTIES` this layout's tiles are
read geometry only, the first column of [styled tiles](#styled-tiles).
[Files](#files) lists the archives, their sizes and checksums, and
[CONTRIBUTING.md](../CONTRIBUTING.md#the-hosted-page) has the steps that
rebuild the hosted page.
