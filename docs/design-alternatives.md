# Design alternatives

Layouts and encodings measured in the same role as the current design, on the
Japan building dataset or a Tokyo subset of it. Each was built with the same
writer and read with the same reader as the design it was compared against, so
the numbers isolate one choice. They were measured on the reader revision of
the time and show direction, not current values.

## Coarse prefix versus one order

Storing the rows shown at coarse zooms in leading row groups of their own,
the rest after, gives a reader that knows nothing about the format the same
thing COGP's coarse-to-fine order gives it: QGIS and other sequential readers
draw those first row groups first, so a whole-extent view fills in coarse
before fine. On the Tokyo file a prefix through zoom 10 is 166,545 rows, 3.4%
of the table, in one leading row group. The cost falls on the indexed map
reader, because fine levels then live in both sections and a tile needs a
range from each. Same Tokyo tiles as the table below, cold geometry reads of
the two files of this comparison, recorded under `prefix` in
`benchmarks/standard-overviews/results.json`:

| Tile                      | One order      | Coarse prefix  | Next tile, one order / prefix |
| ------------------------- | -------------- | -------------- | ----------------------------- |
| z8 metro, Tokyo           | 0.53 MB, 2 req | 0.39 MB, 2 req | 0.01 MB, 1 / 0.00 MB, 1       |
| z11 city, Tokyo           | 0.29 MB, 2 req | 0.28 MB, 3 req | 0.16 MB, 1 / 0.16 MB, 2       |
| z13 downtown, Shibuya     | 0.36 MB, 2 req | 0.36 MB, 3 req | 0.25 MB, 1 / 0.25 MB, 2       |
| z15 street, Tokyo station | 0.04 MB, 2 req | 0.06 MB, 4 req | 0.02 MB, 1 / 0.02 MB, 2       |

Feature counts are identical. The prefix saves bytes at the coarse zoom it
serves alone, costs one extra request per tile at city zooms and two at
street zooms, and doubles the requests of every neighbouring tile.

The benefit to readers that do not know the format is measured in QGIS 4.2
(GDAL 3.12), rendering each file headlessly with a parallel render job and
sampling the partial image every 100 ms, as the canvas preview does. Both
files finish in the same time; what differs is how much of the final picture
is on screen early (median of three alternating runs, fraction of the final
painted pixels):

| Extent, file           | 0.5 s | 1 s  | 2 s  | 5 s  | Done   |
| ---------------------- | ----- | ---- | ---- | ---- | ------ |
| Whole Tokyo, one order | 0.02  | 0.04 | 0.09 | 0.23 | 29.9 s |
| Whole Tokyo, prefix    | 0.21  | 0.44 | 0.48 | 0.56 | 29.9 s |
| City, one order        | 0.12  | 0.22 | 0.50 | 1.00 | 5.0 s  |
| City, prefix           | 0.39  | 0.48 | 0.63 | 1.00 | 5.0 s  |

With one order QGIS fills the map region by region along the curve; with the
prefix the coarse selection covers the whole extent within the first
second, because GDAL delivers row groups in file order and the selection is
the first group. The layout keeps one order for now; the prefix is the lever
if sequential readers matter more than a request per tile. The spatial index
lists each LOD's pages in one key order, so a prefix would also need a
directory tree per section. `benchmarks/standard-overviews/write-tokyo.py`
builds the Tokyo file; the prefix file came from the writer at `0a06e75^`,
whose `write` takes `prefix_zoom`, and `qgis-progressive.py` beside the
script reproduces the measurement.

Without the index and without a prefix, a fixed row cap leaves the pages of
a coarse overview, which is sparse, holding few values, and the coarse Tokyo
tile reads twice the bytes. The page writer therefore scales each LOD chunk's
row cap by its density, targeting 2,048 present values per page, up to
262,144 analytical rows.

## Page size and CPU workers

With tile-aligned pages, 64 KiB uncompressed overview pages against 16 KiB cut
the cold Tokyo city viewport from 256 to 209 ms with two CPU workers, or 195 ms
with four, and a cached repeat from 125 to 69 ms. Two workers are the default
because a third and fourth each cost a decoder and projection cache in memory;
devices reporting at most two cores render serially.

## Where an attribute sits

An attribute a renderer styles with can sit in three places beside the
overview geometry. All three were measured, and the writer keeps it in
attribute columns.

**Attribute columns** ([Attributes](usage.md#attributes)). Every LOD holds
its shown rows' values in a nullable typed column with pages cut where its
overview pages are, each page's values on its leading rows, but one page of
nulls for each run of overview pages without a value; boundary copies pack
the value; and after the leaves of each level-one directory node, a block of
fixed-size slots, one per attribute and leaf, holds the attribute leaves
listing the length of the attribute page beside each overview page, 0
beside one without a value, which a reader locates from the level-one node
and fetches with the leaf. A tile that names no attribute requests no part
of this.

**Packed into every overview row.** Each overview row's payload holds the
attribute, so a tile reads it in the pages it reads anyway. Every tile pays
for it whether styled or not, and the LODs from the complete zoom up repeat
the source column. Against attribute columns:

- On 1,753,860 Denver buildings, a cold tile that styles by nothing is 7 to
  39% lighter without `height` packed. A tile styled by `height` from its
  attribute column takes the same rounds with one more request cold at zooms
  8 to 13, two more at zooms 14 and 15 and one more next, and reads 3 to 9%
  fewer bytes than packed at zooms 8 to 13 and 1 to 2% more at zooms 14 and
  15, except a small zoom 15 tile, where the first styled tile under an
  overview leaf reads an attribute slot of about 2 KB, 14.7 KB against
  12.6 KB; from zoom 11 the next tile reads 3 to 5% less.
- Switching the styled attribute fetches only that attribute's pages, never
  the geometry: restyling six drawn zoom 13 Denver tiles by `height` took 4
  requests, 154 KB and 0.26 s at 100 ms per request, where the packed
  attribute restyled from the tiles already held in 25 ms.
- On 155,597,622 US buildings read from S3 in Chrome, a first 1280 by 800
  view styled by two half-float attributes took 3 to 15 requests against 2
  to 5 with both packed, in the same two rounds, read from 8% fewer bytes to
  4% more, and took 0.73 to 1.12 s against 0.73 to 1.05 s, within 11% either
  way at each view. Styled by one attribute it took within 10% of the time of
  the file packing two, on a fast link and with 300 ms added to every request
  alike; styled by two, as long on a fast link and from 6% less to 24% more
  with 300 ms added; styled by four, up to 42% more than by two packed. The
  file with eight half-float attribute columns is 24.4 GB against 21.4 GB
  with the two packed and no columns.

Packing stayed ahead where tiles already held are restyled, which it does
with no request, and for a map styled by several attributes from its first
view whose readers are on slow links. It made every tile pay for every packed
attribute, so the writer keeps attributes in columns alone.

**A LIST column per attribute.** Beside a geometry-only payload, each overview
row holds a list with a slot for its own value and one per boundary copy it
hosts. On the 4.9-million-row Tokyo subset this saved modest bytes for
geometry-only views but made styled views slower: the companion pages added
requests and decode work that outweighed the saving, and every copy's value
sits in its host's list as well.

Choices within the column layout were measured on the same buildings:

- A value on its own row, where a file without the spatial index holds it,
  has each page spend definition levels on every gap between the rows its
  zoom shows. Held that way the attribute columns of zooms 4 to 7, a number
  and a string of few values, take twice the bytes they take leading their
  pages, those of zooms 8 to 11 about one and a half times, and those of
  zooms showing every row the same.
- A separate gzip directory listing the byte range of every attribute leaf,
  read the first time a style uses an attribute, would keep the slots'
  padding out of the file but grows with attributes times leaves. On 53.7
  million Japan buildings with 15 attributes it would take 12.1 KB
  beside a 7.3 KB root, too much for the first 16 KiB, so the first tile
  styled by an attribute would take three request rounds where a
  geometry-only tile takes two.
- Slots placed right after each leaf would be found from the leaf's own
  entry, but would sit between consecutive leaves, so a geometry-only view
  reaching several leaves would either split what is otherwise one request
  or fetch the slots between them as filler. On 30,000 points with 20
  attributes and 64 pages per leaf, 48 concurrent zoom 16 tiles read through
  the reader's coalescing would take 44 requests and 47.3 KB, against 28
  requests and 23.6 KB for the same source without attributes. With a
  slot block after each level-one node's leaves, both take the same
  requests and bytes.

- Listing the byte ranges of the attribute leaves in the level-one directory
  nodes, beside the leaves they locate, loads both in one round with no
  separate directory,
  but makes every node above the leaves grow by two numbers per attribute per
  entry. On 53.7 million Japan buildings with 17 attributes that pushed the
  level-one nodes out of the root, and every cold geometry-only zoom 15 tile
  read three requests in three rounds, 15.4 KB, against two in two, 10.7 KB,
  without the attributes.
- Listing an LOD leaf's attribute pages inside the leaf instead of in a leaf
  per attribute beside it makes every tile read every attribute's lists: with
  eight attributes a cold geometry-only tile reads 21.0 KB against 12.7 KB at
  zoom 15 and 133 KB against 117 KB at zoom 13.
- At the LODs showing every row, reading the source column, its pages cut
  where the finest LOD's tiles meet and holding 8 KiB of values each, instead
  of a column of the LOD's own makes a smaller file, 248.5 MB against 254.2 MB
  on the Denver buildings, but each attribute leaf there lists the source
  pages with their rows, 3 to 7 KB, and a cold zoom 15 tile styled by
  `height` reads 18.3 KB against 14.7 KB with a column of the LOD's own.
- An attribute page beside every overview page, a page of nulls included,
  would keep every attribute leaf entry a byte range, but on 53.7 million
  Japan buildings with 17 attributes it would make their columns
  269 MB against 157 MB: the ten valued on fewer than 0.2% of rows would
  take about 9 MB each, 92 MB together against about 4 MB, mostly page
  headers over almost no values, and a tile styled by one would request
  pages holding nothing.
- Boundary copies packing the attributes keeps a styled tile at two
  rounds, since a copy's value is in the tile's own pages, but a geometry-only
  tile reads those values too: with eight attributes, 5 to 12% more than with
  none at zooms 13 to 15. Moving them to a column of the
  copies' values beside each LOD would make that cost a styled tile's
  instead, at one more request.

## Shared detail levels

Every complete LOD stores every feature again, so the writer pairs complete
zooms: one LOD on the finer zoom's grid serves both. The Japan file serves z14
and z15 from one z15-grid LOD and stops there. Past it the reader draws the
primary geometry, and a map source whose `maxzoom` is 15 overzooms z15, as the
hosted demo does.
That grid has four cells per z15 pixel, so two per pixel at z16 and one at
z17. A z16 LOD would add 1.11 GB, every feature again, for those views. Serving all complete zooms from one payload suits small files, and
the writer does so below `single_lod_bytes`; larger files pair zooms because a
cold read at the coarser zoom would read the finest grid's geometry.

## Standard geometry encodings

The packed rows are a private byte format. On a matched 542,728-feature sample
of already-quantized Japan overviews, the same geometry as standard GeoArrow
float64 multipolygons takes **2.16×** the compressed bytes and WKB **2.60×**,
and each takes about **2.1×** the decoding CPU; MVT encoding costs the same.
Quantized nested int32 coordinates with Parquet delta encoding, the shape COGP's
`quantized_geoarrow` uses, approach the packed size at dense levels. WKB has a
concrete benefit the others lack: DuckDB spatial recognizes it and runs spatial
SQL on the overviews without a custom decoder.

## Standard overview table with tile-sized row groups

The closest standard-Parquet equivalent of the packed rows, built from the
same Tokyo file so the two hold identical overviews: one table with a row per
shown feature per level and a row per boundary copy tagged with the tile it
serves, columns `level, row, tile, geometry, height, class, key`, geometry as
nested int32 x/y on the level's grid (COGP's `quantized_geoarrow` shape), in
Hilbert order with row groups cut at the level's tile boundaries and at 4,096
rows. A tile then reads whole row groups, each one contiguous byte range, and
a directory over the row groups, in the form of the spatial index's directory
nodes, replaces the 11 MB footer. The reader's coalescing policy is applied to
both; the packed numbers are the real reader on the local file, the standard
numbers a simulation from the footer with membership checked equal to the
packed reader on every tile.
`benchmarks/standard-overviews/` holds the Tokyo build, the converter, the
simulator, and the results.

4,881,955 Tokyo buildings, zooms 8 to 16, cold geometry-only reads after a
16 KiB open, then the tile one column east on the same reader:

| Tile                      | Packed cold                   | Standard cold  | Packed next    | Standard next  |
| ------------------------- | ----------------------------- | -------------- | -------------- | -------------- |
| z8 metro, Tokyo           | 0.47 MB, 2 req, 102,414 feat. | 0.84 MB, 2 req | 6 KB, 1 req    | 8 KB, 1 req    |
| z11 city, Tokyo           | 0.25 MB, 2 req, 23,371 feat.  | 0.41 MB, 2 req | 0.14 MB, 1 req | 0.24 MB, 1 req |
| z12 city, Tokyo           | 0.30 MB, 2 req, 28,334 feat.  | 0.50 MB, 2 req | 0.28 MB, 1 req | 0.47 MB, 1 req |
| z13 downtown, Shibuya     | 0.29 MB, 2 req, 28,630 feat.  | 0.49 MB, 2 req | 0.22 MB, 1 req | 0.38 MB, 1 req |
| z13 suburbs, Kawasaki     | 0.21 MB, 2 req, 19,336 feat.  | 0.35 MB, 2 req | 0.30 MB, 1 req | 0.52 MB, 1 req |
| z15 street, Tokyo station | 10 KB, 2 req, 206 feat.       | 15 KB, 2 req   | 20 KB, 1 req   | 29 KB, 1 req   |
| z15 street, Shinjuku      | 20 KB, 2 req, 1,031 feat.     | 29 KB, 2 req   | 20 KB, 1 req   | 31 KB, 1 req   |

Geometry-only request counts are identical. On a cold read the standard
table costs 66 to 81% more bytes from z8 to z13 and 41 to 47% more at street
zooms; on the next tile it costs 42 to 73% more; and it stores 333 MB against
187 MB for the packed overview and attribute columns.

A styled read of `height` and `class` fetches each attribute's pages as a
range of its own, in the same two rounds as the geometry. On a cold read it
adds 4 to 9% to the packed bytes through z13 and 4 to 5 KB at street zooms,
and takes 3 or 4 requests, or 6 at street zooms, where each attribute's
directory leaf is a range too; the next tile takes up to 3. The standard
table stays at 2 requests and 1, and adds at most 5%, apart from its z8 next
tile at 14%: a row group's attribute chunks sit beside its geometry, inside
the range the group is read as. Its cold styled reads cost 59 to 66% more
bytes than the packed ones through z13 and 7 to 25% more at street zooms.

The table is ordinary Parquet; DuckDB queries it and reads the nested
coordinates directly, though no tool treats them as geometry without the
level's scale and offset. Not measured: decode CPU, which the
standard-geometry ablation above puts at about twice the packed rows, and
larger datasets, where the 15,219 row groups here become hundreds of thousands
and the directory must hold them.

## Modified COGP reader

Patching a COGP checkout to read packed overview pages through a spatial
directory, while keeping COGP's cumulative level semantics, removed most of its
per-tile requests. It did not reach this layout's transfer sizes because the
cumulative prefixes still read every coarser level. It suggests the page-level
access path can be added to COGP as an optional extension.
