# Attributes: acceptance test

These scripts measure what attributes cost a map. An attribute
(`write(..., attributes=[...])`, by default every cheap column) is stored as a
typed Parquet column per LOD, fetched only when a style uses it. The scripts
build the variants from one source with one thinning recipe and measure them
against each other and against PMTiles. They then print pass or fail against
the thresholds at the top of `verdict.mjs`.

| Variant   | Writer options                                                                                             |
| --------- | ---------------------------------------------------------------------------------------------------------- |
| `columns` | `attributes=ATTRIBUTES`                                                                                    |
| `auto`    | the writer's default, `attributes='auto'`: every numeric, boolean and low-cardinality string source column |
| `none`    | `attributes=()`: no attributes, the geometry-only reference                                                |

For Japan, `ATTRIBUTES` is `height,class`, and the attribute only `auto`
keeps is `subtype`.

## Scripts

- `derive-source.py FILE OUT [--check ORIGINAL]` writes a GeoParquet source
  for the writer from one of this project's files. It drops the covering
  `bbox`, the key column, the `lod` column and any attribute-column struct,
  and their `geo` metadata. `FILE` can be a local path or an http(s) URL. A URL
  is read with range requests one row group at a time. Memory stays near one
  row group, and the output is the only file written. `--check` compares the
  result with an original source: the same columns and types in any order,
  and the same rows as a multiset.
- `build-variants.py SRC OUT_DIR --recipe denver|japan [--attributes height,class] [--variants ...] [--repeat N]`
  writes `OUT_DIR/<variant>.parquet`, each variant's writer log as
  `<variant>.log`, and `builds.json`. `builds.json` holds the build time
  (fastest of `--repeat` runs), the writer options and the compressed bytes
  of every column group: LOD geometry, attribute columns by attribute and by
  LOD, covering, key, source columns, and everything else (footer, page
  indexes, the spatial index root and directories, with the directory and root
  bytes from the log). The `japan` recipe is `write-japan-tippecanoe.py`'s
  options in one `write` call. The `denver` recipe is
  `chain(drop_smallest(850_000), tiny_polygons())` with 8 workers.
  `--explicit-auto` passes `auto` as an explicit list of the cheap columns.
- `single-tiles.mjs --dir OUT_DIR [--pmtiles FILE_OR_URL] [--scenario japan]`
  opens a fresh reader for each named tile, variant and style. It then reads
  the tile (`cold`), and the tile east of it on the same reader (`next`).
  Another fresh reader first reads, in the same style, the tile four columns
  west and four rows south, then the tile (`warm`): a tile in a new area on a
  reader whose directories that style has already used. There are three
  styles: geometry only, an attribute `columns` keeps (`--named`, default
  `height`) and one only `auto` keeps (`--other`, default `subtype`). Each read records requests, rounds, bytes and features. A round
  is one level of requests that had to wait on an earlier one. Every request
  is delayed `--latency` ms (default 20), so rounds count the same on local
  files as over a network. Use 0 for remote URLs. PMTiles is read for each
  tile at its zoom, or at its maximum zoom past that. The script writes
  `single-tiles.json`.
- `session.mjs --dir OUT_DIR [--pmtiles FILE_OR_URL] [--latency 0,100] [--connections 0,6] [--repeat 3]`
  replays the scenario's route without a browser. The route is a list of
  viewports (centre, zoom, size in pixels), some of which switch the style.
  Each viewport becomes the 512-px tiles MapLibre would request, all at once,
  and the step ends when the last tile arrives. For each step the script
  reports requests, bytes, rounds and elapsed time, then the totals, for every
  combination of added latency and per-host connection limit (0 means none).
  Each run is a fresh process, and the run with the median total time is
  kept. The script writes `session.json`.
- `verdict.mjs --dir OUT_DIR` prints pass or fail from whatever results are in
  the directory. The two measurement scripts also print it at the end.
- `check-session.mjs --file label=path --step N` checks one step of the session
  model against independent measurements, as described in
  [Session check](#session-check).
- `lib.mjs` is shared code: the counting byte source, tile arithmetic and MVT
  feature counts. `scenarios/` holds the named tiles and routes for Japan and
  Denver; `--scenario` also takes a path to a JSON file.

The Node scripts import the built reader from `js/dist` and the `pmtiles`
package from `js/node_modules`. A local path and an http(s) URL go through the
same reader code: a local file is served from disk behind a stand-in `fetch`.

### Thresholds

At the top of `verdict.mjs`. Every threshold is on read time: requests,
rounds and bytes. "Within x" means at most x heavier or slower, so a lighter
or faster result always passes.

- `auto` against `none`, geometry-only tiles, cold and next, every zoom: the
  same requests and rounds, bytes within 2%.
- `auto` against `columns`, tiles styled by the attribute both keep, cold
  and next, every zoom: the same requests and rounds, bytes within 2%.
- `auto`, tiles styled by the attribute both keep against geometry-only
  tiles, cold and next, every zoom: the same rounds.
- `auto`, the attribute only it keeps, warm: at most 2 rounds.
- Session at 100 ms per request, at every connection limit the session ran:
  `auto` no slower than `columns` in total, with every step reported.

File size and build time are reported, not checked. Dense zooms are the zooms
at which `columns.log` shows every row (the "rows shown at each zoom" line). If
that line is missing, the scenario's `dense_from` is used.

## Session model

A Tiled GeoParquet file is served by the reader's own worker module
(`js/dist/worker.js`), loaded into the script's process with one CPU worker.
Its readers, its cache of decoded tiles and its handling of attributes and
source columns are therefore the map's own. PMTiles is read with the
`pmtiles` package, the way its MapLibre protocol reads it. On the map side:

- The tiles requested are those whose extent meets the viewport, at the view
  zoom rounded down and held to the source's zoom range, so past `maxzoom` the
  map overzooms. Tiles outside the source's bounds are skipped.
- MapLibre's tile cache per source is modelled at its default size, five
  viewports' worth of tiles. A style switch on a Tiled GeoParquet source
  changes its tile URL (`setTiles`, as `demo/compare.html` does), which
  empties that cache. On PMTiles a style switch only repaints the tiles
  already held.
- The added latency delays every request in the byte source. The connection
  limit queues requests per host past N in flight.

Left out, any of which could move a close verdict:

- **CPU.** One thread does the reader's decoding and MVT encoding. The browser
  spreads that work over up to two helper workers, so these runs overstate
  CPU's share of elapsed time. MapLibre's own parsing, layout and drawing are
  not counted for either format, and both formats' tiles go through them. The
  0 ms runs show roughly how much of each total is CPU.
- **Animation and cancellation.** Each viewport is a jump (`jumpTo`), and each
  step waits for every tile before the next begins. A pan or zoom animation
  asks for tiles at intermediate views and cancels the ones it leaves behind.
  MapLibre also prefetches lower zooms during zoom animations. None of this
  happens here.
- **Network.** Latency is the same fixed delay for every request, with no
  bandwidth limit, no variation, no TCP or TLS setup, and no HTTP cache
  between visits. A per-host limit of 6 stands for HTTP/1.1. Leave it at 0
  for HTTP/2, which multiplexes.
- **Placeholder tiles.** MapLibre draws parent or child tiles it already holds
  while loading. They change what the user sees and add no requests.

## Session check

`check-session.mjs` runs one step of the route alone on a fresh reader. It
then runs each of the step's tiles alone, each on a fresh reader in its own
process, and compares the two from the byte ranges they requested. The step's
bytes should equal the tiles' bytes summed, less the bytes that two or more
tiles fetched (what shared caches save), plus any gaps coalescing read between
wanted ranges. Three more conditions must hold: no byte is fetched twice in
the step, no byte any tile needed is missing from it, and every request in the
step starts and ends on a wanted byte. The script also runs the step at 0 ms
and at `--latency` ms. The difference should be about one latency per round,
not one per request. On the Denver build, step 2 (`columns`, geometry, 9
tiles) and step 10 (`columns`, the attribute only `auto` keeps, read from its
source column, 12 tiles) both pass. For step 10, 78 requests and 1.68 MB
summed over the tiles became 12 requests and 1.49 MB in the step, and 100 ms
per request added 105 ms per round over 3 rounds.

## Japan run

The Japan run needs a Linux VM with 16 or more vCPUs, 64 GB of memory and
150 GB of free disk, with `uv` and Node 22 installed. The Japan recipe runs 12
workers with a 40 GB DuckDB limit; the Japan build takes 10 minutes on a
64 GB laptop. From a checkout of the commit under test:

```sh
uv sync --locked
npm --prefix js ci
npm --prefix js run build
mkdir -p ~/attr-bench
R2=https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases
uv run python benchmarks/attribute-columns/derive-source.py $R2/5503c5a/japan_buildings.parquet ~/attr-bench/source.parquet
curl -fL -o ~/attr-bench/japan_buildings.pmtiles $R2/6c3a4f9/japan_buildings.pmtiles
uv run python benchmarks/attribute-columns/build-variants.py ~/attr-bench/source.parquet ~/attr-bench --recipe japan --attributes height,class
node benchmarks/attribute-columns/single-tiles.mjs --dir ~/attr-bench --pmtiles ~/attr-bench/japan_buildings.pmtiles --scenario japan --named height --other subtype
node benchmarks/attribute-columns/session.mjs --dir ~/attr-bench --pmtiles ~/attr-bench/japan_buildings.pmtiles --scenario japan --named height --other subtype
node benchmarks/attribute-columns/check-session.mjs --file columns=$HOME/attr-bench/columns.parquet --scenario japan --named height --other subtype --step 5
node benchmarks/attribute-columns/verdict.mjs --dir ~/attr-bench --scenario japan
```

`derive-source.py` streams the 6.42 GB hosted file from R2. It reads only the
source columns, about 4.3 GB, and keeps one of the 52 row groups in memory at
a time. Every source column is in the hosted file, so the derived source
has the same rows and columns as the source of the hosted build. The PMTiles archive (1.30 GB)
is downloaded so both formats are read from the same disk with the same added
latency. Pointing `--pmtiles` at the URL instead measures it over the real
network.

What to expect:

- **Disk.** About 4.5 GB for the source, 6 to 7 GB for each of `columns`
  and `none`, more for `auto`, whose every LOD holds a column of each kept
  attribute, and 1.3 GB for PMTiles: roughly 27 GB kept. A build needs free
  disk of about twice its output while it runs.
- **Memory.** Up to the 40 GB DuckDB limit in the main process, plus about
  1 GB per worker. `--workers` and `--memory-limit` override the recipe on a
  smaller machine; build every variant with the same settings so the
  reported build times compare like with like.
- **Time.** About 5 to 10 minutes to derive the source, depending on the link
  to R2. The three builds take about 10 minutes each. `auto` keeps every
  cheap column, about 20 for Japan, and may take longer. Single tiles take a
  few minutes. The session runs 48 replays (4 files, 2 latencies, 2
  connection limits, 3 repeats), a few minutes for Denver; Japan's tiles are
  heavier, so expect several times that. `--repeat 1` cuts it to a third.

## Denver

The scripts also run on 1,753,860 Denver building footprints (id, height,
class). With no `subtype`, `columns` keeps only `height`, and `class` is the
attribute only `auto` keeps:

```sh
uv run python benchmarks/attribute-columns/build-variants.py denver.source.parquet BENCH --recipe denver --attributes height
node benchmarks/attribute-columns/single-tiles.mjs --dir BENCH --pmtiles denver.pmtiles --scenario denver --named height --other class
node benchmarks/attribute-columns/session.mjs --dir BENCH --pmtiles denver.pmtiles --scenario denver --named height --other class
```

The Denver PMTiles archive was made with tippecanoe 2.82 and the options of
the Japan archive (`-zg -Z 0 --drop-smallest-as-needed --extend-zooms-if-still-dropping`).
It was not calibrated against the thinning, so its feature counts are much
lower than the variants'.
