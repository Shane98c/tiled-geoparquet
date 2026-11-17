# Hosted comparison

The public comparison is at <https://geoparquet-lod-compare.pages.dev/>.
It serves the Tiled GeoParquet build from the
[Japan evaluation](japan-evaluation.md), whose thinning budgets are calibrated
against PMTiles feature counts, beside the matching PMTiles archive. The
[five-reader comparison](comparison.md) is measured on these hosted files and
on the baked-PMTiles file hosted beside them, which has no pane on the page.

The page defaults to Tokyo at zoom 11, coloured by `class`, with no basemap.
Its readers request data directly from the same public S3 bucket with no simulated
latency or bandwidth cap. The files use identical immutable cache settings.
The page supports the same view and style controls as the local comparison.
The [deck.gl example](https://geoparquet-lod-compare.pages.dev/deck/) is
deployed beside it and opens the same GeoParquet file through the
renderer-neutral batch API; pass `?url=` to point it at another file.

Use the reader checkboxes and **Apply readers** to choose any combination of
Tiled GeoParquet, PMTiles, released COGP, and Esri SOP. Unselected panes are removed
and do not initialize their readers or request their datasets. The selection is
stored in the URL (`panes=lod,pmtiles,cogp,esri`, where `lod` is Tiled GeoParquet); the default is `lod,pmtiles`.
Applying a selection reloads the readers while preserving the current view and
style. One, two, or three maps fill the available width; four use a two-column
grid. Hidden panes leave no empty slots. At least one reader must be selected to apply.

All four archives derive from the same 53,742,978 Japan building footprints.
COGP uses the unmodified released `@cogp/maplibre` / `@cogp/reader` 2.1.4 and a
fresh conversion with the released COGP writer 2.2.2 (`cogp-rs-v2.2.2`, commit
`44f29be34d303fbb1febb154413a5aa7aa568a91`). The conversion uses
`--webmerc-minzoom 10` and otherwise the release's defaults.

The baked-PMTiles file of the five-reader comparison was built from the same
source by `benchmarks/baked-pmtiles/build.py`, which runs the scripts of
[gsueur/geoparquet-pmtiles](https://github.com/gsueur/geoparquet-pmtiles) at
commit `b8e4734` and bakes in the PMTiles archive hosted here.

Esri uses its original Japan SOP file and the reference MapLibre reader from
upstream commit `10ff683874db8b2d99c0f03b98354b5ea5f8199c`. Its ten implementation
files match the upstream Git blob hashes; only the dataset-list type is reduced
to the fields used by this demo. The reference reader returns geometry without
source attributes, so this pane stays in geometry styling. Its feature limit
and ready status are shown explicitly; initial timing waits for data rendering.

## Data provenance

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
- [Deployment manifest](https://geoparquet-lod-compare.pages.dev/release.json)

## Interpreting the comparison

The strip under the maps shows how many bytes each reader has requested since
the page loaded, or since the counters were last reset. It counts response bodies, including any served from the
browser's HTTP cache, so these are not wire-transfer totals. All panes follow
the same camera, so the totals cover the same path.

Under each bytes bar is a thinner bar that shows how long that pane took to
draw the view. The time bars share one scale. To the right of each row is the
number of bytes and requests that the last move added on top of what the page
already had, which can be zero.

The Tiled GeoParquet bar is divided by what the bytes are: the file's index,
the shapes drawn on the map, each attribute column that a style has used, and
any other column that the reader looked up in the file's full table. The
hosted page opens coloured by `class`, using the summary of that attribute in
the file's metadata ([Attributes](usage.md#attributes)).

“Fresh load” reloads the page, which clears the readers' caches. “Reset
counters” keeps the caches, so you can compare navigation from that point.

By default the readers' requests skip the browser's HTTP cache. Every load is
then a first visit, and the times reflect only the network and the reader. The
COGP reader is the exception: its released reader always uses the cache.
Ticking “Browser cache” (`cache=on`) sends every reader's requests through the
cache, so reopening a view reads from it. Chrome's cache handles one request
per URL at a time, so range requests that a reader sends to a single URL are
answered one after another. In this mode the Tiled GeoParquet reader puts each
range in the URL ([Requests](usage.md#requests)), so every request has a URL
of its own and none waits on another. The PMTiles library sends all its ranges
to one URL, as it does on any page.

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

The archives have different thinning and overzoom rules. The PMTiles
tiles hold `class`, `height` and `subtype`, the attributes its tiler was
given. The GeoParquet file keeps 17 of its columns as attributes, the
writer's default, those three among them, and its reader styles by any other
column from the table's rows. The page leaves `id` out of its style menu
(`--hide id`): looked up for a zoomed-out view, that column of unique strings
reads hundreds of megabytes, 429 MB for one zoom 8 tile. Full archive sizes include different
capabilities and should not be confused with viewport transfer sizes.

## Rebuilding the hosted page

From the repository root, bundle the reader and build a separate static folder:

```sh
npm --prefix js run demo:bundle
node demo/build-share.mjs \
  --out /private/tmp/geoparquet-lod-share \
  --url https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/5503c5a/japan_buildings.parquet \
  --pmtiles https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.pmtiles \
  --cogp https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.cogp-2.2.2.parquet \
  --esri https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.sop.parquet \
  --release 5503c5a \
  --hide id \
  --style class \
  --domains demo/japan-domains.json
wrangler pages deploy /private/tmp/geoparquet-lod-share \
  --project-name geoparquet-lod-compare --branch main
```

The `--domains` option takes a JSON file of fixed colour scales.
`demo/japan-domains.json` sets the scales for `facade_color` and `roof_color`.
These two columns in the style menu are looked up in the table, so the
GeoParquet file has no summary for them. The script prints scales for the
columns without a summary, so generate the JSON from a local copy of the file
with `uv run python demo/domains.py japan_buildings.parquet > demo/japan-domains.json`.

The builder supplies public defaults only when query parameters are absent, so
links can override the initial view, style, or datasets. It leaves the local demo
configuration unchanged. Upload new datasets under a new release prefix instead
of replacing immutable objects:

```sh
aws s3 cp japan_buildings.parquet \
  s3://carbonplan-share/tiled-geoparquet/releases/<tag>/japan_buildings.parquet \
  --cache-control "public, max-age=31536000, immutable"
```

Upload with `aws s3 cp`, which sends 8 MiB parts. Three of these files
uploaded by rclone in 64 MiB parts answered each request about 0.06 s later
than the same bytes uploaded this way.
