# Tiled GeoParquet JavaScript reader

Experimental draft 0.1.0. Reads the Tiled GeoParquet layout and spatial page
index emitted by the writer; generates Mapbox Vector Tiles for any map library. Not yet published to
npm. See the repository root README, `spec/`, and `docs/usage.md` for format
status, construction, limitations, and measurements.

Build from this directory with `npm ci && npm run build`. Install the built
package by local path. Use `tiled-geoparquet` for direct reads and
`tiled-geoparquet/tiles` for worker-backed tiles:

```js
import { openReader, coalescing, httpSource, renderTile } from 'tiled-geoparquet'

const reader = await openReader(coalescing(await httpSource([url])))
const mvt = await renderTile(reader, z, x, y, { properties: ['height'] })
```

A tile holds the attributes `properties` names: packed attributes, column
attributes or source columns. Without it, in `renderTile`, `vectorSource`,
`tileUrl` and the tile server alike, a tile holds the packed attributes only.

`httpSource(urls, { cache, connections })` sends a tile's requests past the
browser's HTTP cache, which answers the range requests of one URL one at a
time, and opens six connections to the host as the file opens;
`setSourceOptions` from the tiles entry point sets the same two. A server
that opens the file for each request passes `cache: 'default'`. See
[Requests](../docs/usage.md#requests).

Serve immutable files with HTTP Range support and CORS exposing `Content-Range`.
The tiles entry point requires the distributed `worker.js` and its imports to
remain available to your bundler. Other dependencies remain external in `dist`.

In a map, `vectorSource` from the tiles entry point is a MapLibre vector
source with the file's bounds and zoom limits. Its `maxzoom` is the finest
LOD zoom, which the map overzooms; `{ maxzoom }` as a second argument raises
it to draw exact geometry further in, and a file without LODs gets 22:

```js
import { registerProtocol, vectorSource } from 'tiled-geoparquet/tiles'

registerProtocol(maplibregl)
map.addSource('buildings', await vectorSource({ url, layer: 'buildings', properties: ['height'] }))
```

For clients that fetch tile URLs themselves, the `tiled-geoparquet-serve`
command serves a local path or an http(s) URL on loopback, as
`/{z}/{x}/{y}.mvt` and TileJSON at `/tiles.json`:

```sh
npx tiled-geoparquet-serve buildings.lod.parquet --port 8080 --layer buildings --properties height,class
```

`--host`, `--maxzoom` and `--public-url` are its other options; from this
directory it is `node dist/serve-cli.js`. The same service is
`tileHandler(reader, options)` from `tiled-geoparquet/server`, a function
from a web-standard `Request` to a `Response` with no Node imports.
`tiled-geoparquet/node` has `fileSource(path)`, a local file as a byte source,
and `nodeListener(handler)` for `node:http`:

```js
import { createServer } from 'node:http'
import { openReader } from 'tiled-geoparquet'
import { fileSource, nodeListener } from 'tiled-geoparquet/node'
import { tileHandler } from 'tiled-geoparquet/server'

const reader = await openReader(await fileSource('buildings.lod.parquet'))
createServer(nodeListener(tileHandler(reader, { layer: 'buildings' }))).listen(8080, '127.0.0.1')
```

Tiles are gzipped for clients that accept it, an empty tile is a 200 with an
empty layer, and a Cloudflare Worker must open the reader inside each
request; [usage documentation](../docs/usage.md#serving-tiles-over-http) has
the routes, statuses and options.

Project code is MIT; the MVT encoder and geometry builder include code adapted
from `@cogp/reader` with its MIT copyright notice retained in source and
compiled output. See
`src/COGP-LICENSE.txt`. The optional vendored Esri comparison reader is not
included in this package.

Use `readFeatures(reader, { bbox, resolution, columns, geometry })` for a
renderer-neutral batch with typed coordinate buffers, hierarchy offsets, source
row IDs and attributes. `batchToGeoJSON`, `batchToGeoArrow` (Arrow IPC), and
`batchToMvt` consume it. GeoArrow conversion uses the `@cogp/reader` dependency; nested or
mixed attributes become display strings. GeoJSON is intended for small results.
The deck.gl example is `demo/deck/index.html` after `npm run demo:bundle`,
served with Range support; it feeds `batchToGeoArrow` output to the GeoArrow
deck.gl layers.

`readColumns` and query `columns` accept struct paths (`details.height`), list
indexes (`floors[0].name`), and map keys (`tags.name`). Null/missing values become
`null`. Struct scalars project only selected leaf pages; collections reconstruct
all selected row-group pages before extracting elements. Flat scalars use the
sparse page projection. See [usage documentation](../docs/usage.md) for
path ambiguity, collection representation, validation scope and adapter limits.
