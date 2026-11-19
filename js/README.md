# tiled-geoparquet

[![CI](https://github.com/Shane98c/tiled-geoparquet/actions/workflows/ci.yml/badge.svg)](https://github.com/Shane98c/tiled-geoparquet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

JavaScript reader for Tiled GeoParquet. Reads the Tiled GeoParquet layout and
spatial page index emitted by the writer; generates Mapbox Vector Tiles for
any map library. Experimental, for format draft 0.2.0. See the
[repository README](../README.md), the [specification](../spec/SPEC.md) and
the [writer guide](../docs/usage.md) for format status, construction,
limitations, and measurements.

## install

Node 22 or later. The package is not yet published to npm. Build it from
this directory, where the commands below run, then install the built package
by local path (`npm install ../tiled-geoparquet/js`).

```sh
npm ci
npm run build
```

## usage

Use `tiled-geoparquet` for direct reads and `tiled-geoparquet/tiles` for
worker-backed tiles:

```js
import { openReader, coalescing, httpSource, renderTile } from 'tiled-geoparquet'

const reader = await openReader(coalescing(await httpSource([url])))
const mvt = await renderTile(reader, z, x, y, { properties: ['height'] })
```

`coalescing` merges nearby byte ranges into one request. A tile holds the
attributes and source columns `properties` names. Without it a tile holds
geometry only, in every entry point and the tile server alike.

### in a MapLibre app

```js
import * as maplibregl from 'maplibre-gl'
import { registerProtocol, sourceUrl, prefetch, readRow } from 'tiled-geoparquet/tiles'

const url = 'https://example.com/buildings.lod.parquet'
prefetch(url) // optional: starts opening the file before the map exists
registerProtocol(maplibregl)

map.addSource('buildings', {
  type: 'vector',
  url: sourceUrl({ url, layer: 'buildings', properties: ['risk'] }),
})
map.addLayer({ id: 'fill', type: 'fill', source: 'buildings', 'source-layer': 'buildings' })

// a clicked feature's id is its file row; read any source columns for it
const row = await readRow(url, feature.id, ['name', 'risk'])
```

`sourceUrl(config, { maxzoom })` names the file as a source by a
`tiled-geoparquet://` URL, as a PMTiles archive is named by a URL. MapLibre asks the registered protocol for the
source when it is added, which opens the file and gives the source its bounds
and zoom range, so the page awaits nothing. `await vectorSource(config)` returns
the same source as an object, for a page that needs those limits itself.
A config also takes `pointLayer` for point datasets.

The zoom range:

- `maxzoom` is the finest LOD zoom. Past it the map overzooms that LOD's
  tiles, as it does a PMTiles archive, and requests nothing more.
- A higher `maxzoom`, as in `sourceUrl(config, { maxzoom: 18 })`, draws the
  exact primary geometry from the finest LOD zoom up to it. A cold tile there
  reads 230 to 650 KB in up to five requests on a file of US buildings, where
  a tile at the finest LOD reads 30 to 45 KB in two.
- `minzoom` is 0: below the coarsest LOD zoom a tile draws that LOD over its
  whole area. A `minzoom` on the source's layers stops those reads.

To change what a drawn map styles by, give the source the URL of a config
naming the new columns, as in
`map.getSource('buildings').setUrl(sourceUrl({ ...config, properties: ['height'] }))`,
or `setTiles([tileUrl(...)])` on a source made from `vectorSource`. The
reader fetches those columns for the tiles in view and keeps the geometry it
has decoded. Change the layers' paint once the map is next idle: when a paint
change follows `setUrl` or `setTiles` in the same task, MapLibre 6.18 parses
the tiles it holds again and does not fetch the new ones.

Colour stops can come from the file. `readMetadata(url)` returns each
attribute's summary ([Summaries](../docs/usage.md#summaries)), and a ramp
built on a number's quantiles spreads its colours across the values that most
rows have:

```js
import { readMetadata } from 'tiled-geoparquet/tiles'

const { lod } = await readMetadata(url)
const { quantiles } = lod.attributes.find((a) => a.name === 'height').summary
const stops = [...new Set(quantiles)]
const colours = ['#d8c9b7', '#f0a24d', '#d0421b', '#55215a', '#2b1030']
map.setPaintProperty('fill', 'fill-color', [
  'interpolate',
  ['linear'],
  ['get', 'height'],
  ...stops.flatMap((stop, i) => [stop, colours[i]]),
])
```

Tile properties keep their types: numbers stay numbers and booleans stay
booleans, so style expressions compare numbers with numbers. Other values,
such as strings, dates and nested values, are display strings. MVT has no
null, so a null value leaves its property out of the feature:
`['has', 'risk']` is false there.

### in other map libraries

The tiles are standard Mapbox Vector Tiles on a 512 px Web Mercator grid.
`fetchTile(tile, signal)` returns the bytes of one tile, where `tile` is a
`tileUrl(config)` template with its `{z}`, `{x}` and `{y}` filled in, so any
library that lets you load a tile's bytes yourself can draw the file. In OpenLayers, whose
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

Libraries that fetch tile URLs themselves with no loader hook, and desktop or
native clients, read the tiles [over HTTP](#serving-tiles-over-http).

### hosting

Serve immutable files with HTTP Range support and CORS, which a suitably
configured object store can do. A file with the spatial index opens with one
request for its first 16 KiB, and no tile reads its footer.

The tiles entry point requires the distributed `worker.js` and its imports to
remain available to your bundler. zstd pages decode with WebAssembly,
embedded in the bundle as a `data:` URL. A Content Security Policy must allow
`'wasm-unsafe-eval'` in `script-src` and `data:` in `connect-src`, or pages
decode in JavaScript, about three times slower.

Tiles are decoded and encoded in a worker pool, two workers by default and
one on a device with two cores or fewer. `setWorkerCount(1 | 2 | 3 | 4)` from
the tiles entry point overrides its size before the first call to `prefetch`, `readMetadata`, `registerProtocol`
or `fetchTile`. Four helps dense viewports but costs more memory.

### requests

A tile's requests go out together. Two options of `httpSource`, which
`setSourceOptions({ cache, rangeParam })` sets for the tiles entry point
before a file opens, decide how they meet the browser's HTTP cache:

- `cache`, the cache mode of every request, is `no-store` by default. Chrome's HTTP
  cache has one request of a URL in flight at a time, so range requests to
  one file sent through it are answered in sequence. Past the cache they run
  side by side, and a view opened again reads from the network.
  `cache: 'default'` keeps the cache.
- `rangeParam`, unset by default, names a query parameter that carries each request's
  range, as in `?range=20000-20009`. Every range then has a URL of its own,
  so requests run side by side through the cache and a later visit reads
  from it. On the Japan buildings in Chrome from S3, views opened again took
  0.33 to 0.41 s with it and 0.62 to 1.11 s past the cache. The server has to
  ignore the parameter, as S3 and R2 do, and a signed URL does not survive
  one. Use it for a file that never changes at its URL.

### serving tiles over HTTP

`tiled-geoparquet-serve` serves one file, a local path or an http(s) URL, as
tiles any client can fetch. Where the package is installed the command is
`npx tiled-geoparquet-serve`; from this directory it is
`node dist/serve-cli.js`.

```sh
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

`/{z}/{x}/{y}.mvt` (or `.pbf`) is the tile `renderTile` gives, and
`/tiles.json` is TileJSON 3.0.0. A tile with no features is a 200 holding an
empty layer. Tiles are gzipped for clients that accept gzip, and every
response carries `Access-Control-Allow-Origin: *`. The command reads a local
file as it was at startup, so restart it after rebuilding the file.

The same service is `tileHandler(reader, options)` from
`tiled-geoparquet/server`, a function from a `Request` to a `Response` that
imports nothing from Node, so it is also the fetch handler of `Bun.serve` or
`Deno.serve`:

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
path it is mounted under, `publicUrl`, and `compress: false` where a proxy or
CDN in front compresses.

Cloudflare Workers refuses one request the I/O objects of another, so a
Worker opens the reader inside each request, at one more 16 KiB range request
per tile. It passes `cache: 'default'` so that the request names no cache
mode, which a Worker with a compatibility date before 2024-11-11 refuses:

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

### reader behaviour

The reader caches up to 16 MiB of compressed data pages and 128 nodes of each
directory kind, and retains up to 64 decoded tiles within an estimated
32 MiB. The sizes are fixed. A style change on a loaded view reuses decoded
geometry and fetches only the new attribute. Neighbouring tiles share pending
fetches, and work whose last consumer leaves is cancelled.

### plain GeoParquet

The reader also maps a WKB GeoParquet file without LODs, in OGC:CRS84 or
EPSG:4326, through the same entry points. Every zoom then draws the exact
primary geometry of the rows that meet the tile. Rows are chosen by the
file's bbox covering, or, in a file with none, by Parquet's geospatial
statistics and each row's WKB bounds.

That suits files of a few MB. 31,000 IBTrACS track segments in one row group
are 452 KB, and a session's tiles read 409 KB in 11 requests.

### whole-feature reads

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

`readFeatures` returns a renderer-neutral batch with typed coordinate
buffers, hierarchy offsets, source row IDs and attributes.

- Overview queries return bbox-overlapping, deduplicated features and retain
  LOD thinning. They do not perform exact intersection tests. Coordinates
  stay quantized `Int32Array` in EPSG:3857 metres, and `batchInDegrees`
  converts a batch to longitude/latitude.
- Primary queries return the original WKB as `Float64Array` in
  longitude/latitude, with the source attributes.
- A query enumerates the intersecting tiles, up to 256 by default; see the
  `maxTiles` option.
- `batchToGeoJSON` is intended for small results. `batchToGeoArrow` produces
  an Arrow IPC stream, in which nested and mixed attributes become display
  strings. `batchToMvt` clips the batch to one tile with the encoder
  `renderTile` uses.

### rows and nested columns

`readRow(url, row, columns)` reads source columns for one row, and
`reader.readColumns(rowIds, columns)` for many, in the order asked. A tile
feature's id is its row. Both return a 64-bit integer as a BigInt.

`columns` accepts struct paths (`details.height`), list indexes
(`floors[0].name`) and map keys (`tags.name`), in `readFeatures` too. Missing
map keys, out-of-range list indexes and null ancestors return `null`.

## demo

```sh
npm run demo:compare -- --lod /path/buildings.lod.parquet
```

That bundles the reader and serves `demo/compare.html` at
`http://127.0.0.1:8094`, opened on the whole extent of the file. `--center
lon,lat` and `--zoom` set another view. `--pmtiles /path/buildings.pmtiles`
adds a pane that reads a PMTiles archive of the same data, and `--cogp` and
`--esri` add the other readers.

The page is the one hosted at <https://tiled-geoparquet.pages.dev/>, and
[the comparison](../docs/comparison.md#the-hosted-page) explains its
counters. Its query parameters:

- `style=class` sets the style that the page opens with.
- `hide=id` leaves a column out of the style menu.
- `stops=name:0,10,40` sets the colour stops for a number.
- `basemap=1` adds a basemap.

A deck.gl example is hosted at <https://tiled-geoparquet.pages.dev/deck/> and
runs locally at `http://127.0.0.1:8094/demo/deck/index.html` after
`npm run demo:compare`, where it opens the `--lod` file. It feeds `batchToGeoArrow` output to the GeoArrow deck.gl
layers.

## license

All the code in this package is
[MIT](https://choosealicense.com/licenses/mit/)-licensed, but we request that
you please provide attribution if reusing any of our digital content
(graphics, logo, articles, etc.). The MVT encoder and geometry builder
include code adapted from `@cogp/reader` with its MIT copyright notice
retained in source and compiled output; see `src/COGP-LICENSE.txt`. The
optional vendored Esri comparison reader is not included in this package.

## about us

CarbonPlan is a nonprofit organization that uses data and science for climate
action. We aim to improve the transparency and scientific integrity of
climate solutions with open data and tools. Find out more at
[carbonplan.org](https://carbonplan.org/) or get in touch by
[opening an issue](https://github.com/Shane98c/tiled-geoparquet/issues/new)
or [sending us an email](mailto:hello@carbonplan.org).
