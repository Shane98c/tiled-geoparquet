# Hosted comparison

The public comparison is at <https://geoparquet-lod-compare.pages.dev/>.
It serves the Tiled GeoParquet build from the
[Japan evaluation](japan-evaluation.md), whose thinning budgets are calibrated
against PMTiles feature counts, beside the matching PMTiles archive. The
[five-reader comparison](comparison.md) is measured on these hosted files and
on the baked-PMTiles file hosted beside them, which has no pane on the page.

The page defaults to Tokyo at zoom 11, geometry styling, and no basemap. Both
readers request data directly from the same R2 public host with no simulated
latency or bandwidth cap. The files use identical immutable cache settings.
The page supports the same view and style controls as the local comparison.
The [deck.gl example](https://geoparquet-lod-compare.pages.dev/deck/) is
deployed beside it and opens the same GeoParquet file through the
renderer-neutral batch API; pass `?url=` to point it at another file.

Use the reader checkboxes and **Apply readers** to choose any combination of
Tiled GeoParquet, PMTiles, released COGP, and Esri SOP. Unselected panes are removed
and do not initialize their readers or request their datasets. The selection is
stored in the URL (`panes=lod,pmtiles,cogp,esri`); the default remains `lod,pmtiles`.
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

Objects live under `geoparquet-lod/releases/<tag>/` in the
`live-classify-data` bucket. The tag is the commit whose recipe produced the
object: `469acd1` built the GeoParquet file with
`benchmarks/write-japan-tippecanoe.py`; `d51954c` built the baked-PMTiles
file with `benchmarks/baked-pmtiles/build.py`; the PMTiles and COGP archives
were converted at `6c3a4f9`, and the Esri SOP file, uploaded at that release,
is Esri's original.

| Dataset       | Release   |         Bytes | MD5                                |
| ------------- | --------- | ------------: | ---------------------------------- |
| GeoParquet    | `469acd1` | 6,418,172,157 | `8d9e9eb990e114e17f02cc5769999005` |
| PMTiles       | `6c3a4f9` | 1,297,122,745 | `0b733b7511dda51102c1b58a4983706b` |
| COGP 2.2.2    | `6c3a4f9` | 9,081,139,765 | `f9a986b09224256534b530ed80dd245a` |
| Esri SOP      | `6c3a4f9` | 6,177,950,661 | `6224bdf08e1915e367baa61ee566f650` |
| Baked PMTiles | `d51954c` | 6,337,852,318 | `08388a4ba5add01de04cf4bcb8cd9477` |

- [GeoParquet](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/469acd1/japan_buildings.parquet)
- [PMTiles](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.pmtiles)
- [Released COGP](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.cogp-2.2.2.parquet)
- [Esri SOP](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.sop.parquet)
- [Baked PMTiles](https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/d51954c/japan_buildings.baked.parquet)
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
map is idle. The table gives medians of five loads from R2 on 2026-11-16, for
the two readers that the page shows by default. A new browser profile has an
empty cache, so the cache-on columns show a first visit through the cache and
not a reopened view. Each
load used a new browser profile with one reader on the page, in headless
Chrome at 1280 by 900 with software rendering, styled by `class`. The Tokyo
views are centred on 139.76, 35.68 and the Shibuya view on 139.7, 35.66.

| View        | Tiled GeoParquet, cache off | PMTiles, cache off | Tiled GeoParquet, cache on | PMTiles, cache on |
| ----------- | --------------------------: | -----------------: | -------------------------: | ----------------: |
| Tokyo z8    |                      1.53 s |             0.72 s |                     1.68 s |            1.25 s |
| Tokyo z11   |                      1.59 s |             1.05 s |                     1.44 s |            2.38 s |
| Shibuya z13 |                      1.46 s |             1.01 s |                     1.55 s |            2.50 s |

With the cache on, PMTiles took 1.3 to 1.5 s longer at z11 and z13 than with
it off. The cache is the only difference between those two columns: it answers
the library's range requests to one URL one after another.

R2 serves these files over HTTP/1.1, and Chrome opens at most six connections
to one host, so readers loaded on the same page share those six. With the
cache off and the two readers side by side in half-width panes, each reader's
first view took 0.07 to 0.19 s longer than the same half-width pane loaded
alone. With the cache off, PMTiles finished first in every view, both alone
and together. To time one reader, load it alone.

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
  --url https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/469acd1/japan_buildings.parquet \
  --pmtiles https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.pmtiles \
  --cogp https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.cogp-2.2.2.parquet \
  --esri https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/6c3a4f9/japan_buildings.sop.parquet \
  --release 469acd1 \
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
of replacing immutable objects.
