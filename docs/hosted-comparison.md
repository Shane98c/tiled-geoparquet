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

The counters report response body bytes requested by each reader, including
responses served from the browser HTTP cache; they are not wire-transfer totals.
“Fresh load” clears reader caches by reloading the page, but the browser HTTP
cache can survive. Use a fresh browser session for a first-visit comparison.
“Reset counters” retains reader caches for continued navigation comparisons.

The archives still have different thinning and overzoom rules. The PMTiles
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
  --hide id
wrangler pages deploy /private/tmp/geoparquet-lod-share \
  --project-name geoparquet-lod-compare --branch main
```

The builder supplies public defaults only when query parameters are absent, so
links can override the initial view, style, or datasets. It leaves the local demo
configuration unchanged. Upload new datasets under a new release prefix instead
of replacing immutable objects.
