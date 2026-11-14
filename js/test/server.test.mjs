// Tiles and TileJSON over HTTP: the request handler, its Node adapter and the
// tiled-geoparquet-serve command.
//   node test/server.test.mjs [file.lod.parquet plain.parquet]
// Routing, statuses, compression and cancellation run against a made-up
// reader; with both files given, tiles are also compared with renderTile's
// from each and the command is started on them.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { gunzipSync } from 'node:zlib'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { openReader, renderTile } from '../dist/index.js'
import { fileSource, nodeListener } from '../dist/node.js'
import { tileHandler } from '../dist/server.js'

// a request or command that never settles fails the run
setTimeout(() => {
  console.error('tile server test timed out')
  process.exit(1)
}, 60000).unref()

const MVT = 'application/vnd.mapbox-vector-tile'
const pointWkb = (lon, lat) => {
  const wkb = new Uint8Array(21)
  const view = new DataView(wkb.buffer)
  view.setUint8(0, 1)
  view.setUint32(1, 1, true)
  view.setFloat64(5, lon, true)
  view.setFloat64(13, lat, true)
  return wkb
}
/** A reader without LODs whose every tile reads `count` points, or what `read` does. */
const plainReader = (count, read) => ({
  lod: undefined,
  finestZoom: -Infinity,
  bounds: [-120, -40, 120, 40],
  readPrimary:
    read ??
    (async (_bbox, names) => ({
      rows: Array.from({ length: count }, (_, i) => i),
      wkb: Array.from({ length: count }, (_, i) => pointWkb(-100 + (i % 200), (i % 60) - 30)),
      columns: Object.fromEntries(names.map((name) => [name, Array(count).fill(`${name}!`)])),
    })),
})
const layersOf = (bytes) => new VectorTile(new PbfReader(new Uint8Array(bytes))).layers
const get = (handler, path, init) => handler(new Request(`http://tiles.test${path}`, init))

// tiles: the bytes renderTile gives, under either extension
const dense = plainReader(600)
const handler = tileHandler(dense, { layer: 'places', properties: ['name'] })
const expected = await renderTile(dense, 0, 0, 0, { layer: 'places', properties: ['name'] })
for (const extension of ['mvt', 'pbf']) {
  const response = await get(handler, `/0/0/0.${extension}`)
  assert.equal(response.status, 200)
  assert.equal(response.headers.get('Content-Type'), MVT)
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*')
  assert.equal(response.headers.get('Content-Encoding'), null, 'identity unless gzip is accepted')
  assert.equal(response.headers.get('Vary'), 'Accept-Encoding')
  const body = await response.arrayBuffer()
  assert.deepEqual(new Uint8Array(body), new Uint8Array(expected))
  const { places } = layersOf(body)
  assert.equal(places.length, 600)
  assert.deepEqual({ ...places.feature(0).properties }, { name: 'name!' })
}

// compression: gzip for a client that accepts it, of a tile large enough to gain
assert.ok(expected.byteLength >= 1024)
for (const [accept, encoded] of [
  ['gzip, deflate, br', true],
  ['br;q=1.0, GZIP;q=0.5', true],
  ['x-gzip', true],
  ['gzip;q=0', false],
  ['br, deflate', false],
  ['identity', false],
]) {
  const response = await get(handler, '/0/0/0.mvt', { headers: { 'Accept-Encoding': accept } })
  assert.equal(response.headers.get('Content-Encoding'), encoded ? 'gzip' : null, accept)
  const body = Buffer.from(await response.arrayBuffer())
  assert.ok(!encoded || body.length < expected.byteLength, 'gzip is smaller')
  assert.deepEqual(new Uint8Array(encoded ? gunzipSync(body) : body), new Uint8Array(expected))
}
const identity = await get(tileHandler(dense, { compress: false }), '/0/0/0.mvt', {
  headers: { 'Accept-Encoding': 'gzip' },
})
assert.equal(identity.headers.get('Content-Encoding'), null, 'compress: false')
assert.equal(identity.headers.get('Vary'), null)

// an empty tile is a 200 whose bytes are a tile with no features
const empty = await get(tileHandler(plainReader(0)), '/5/3/7.mvt', {
  headers: { 'Accept-Encoding': 'gzip' },
})
assert.equal(empty.status, 200)
assert.equal(empty.headers.get('Content-Type'), MVT)
assert.equal(empty.headers.get('Content-Encoding'), null, 'too small to gzip')
const emptyBytes = await empty.arrayBuffer()
assert.deepEqual(
  new Uint8Array(emptyBytes),
  new Uint8Array(await renderTile(plainReader(0), 5, 3, 7)),
)
assert.ok(emptyBytes.byteLength > 0 && emptyBytes.byteLength < 32)
assert.deepEqual(Object.keys(layersOf(emptyBytes)), [])

// statuses: 400 for a tile address no tile has, 404 for any other path
for (const path of [
  '/0/0/1.mvt',
  '/3/8/0.mvt',
  '/3/0/8.pbf',
  '/31/0/0.mvt',
  '/-1/0/0.mvt',
  '/1.5/0/0.mvt',
  '/01/0/0.mvt',
  '/z/x/y.mvt',
  '/1/1e0/0.mvt',
  '/1/0/99999999999999999999.mvt',
]) {
  const response = await get(handler, path)
  assert.equal(response.status, 400, path)
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*')
}
assert.equal((await get(handler, '/30/1073741823/1073741823.mvt')).status, 200, 'the deepest tile')
for (const path of ['/', '/0/0/0', '/0/0/0.png', '/0/0.mvt', '/a/0/0/0.mvt', '/tiles.json/', '/x'])
  assert.equal((await get(handler, path)).status, 404, path)
const post = await get(handler, '/0/0/0.mvt', { method: 'POST' })
assert.equal(post.status, 405)
assert.equal(post.headers.get('Allow'), 'GET, HEAD, OPTIONS')
const preflight = await get(handler, '/0/0/0.mvt', {
  method: 'OPTIONS',
  headers: { 'Access-Control-Request-Headers': 'authorization' },
})
assert.equal(preflight.status, 204)
assert.equal(preflight.headers.get('Access-Control-Allow-Origin'), '*')
assert.equal(preflight.headers.get('Access-Control-Allow-Headers'), 'authorization')
assert.equal((await get(handler, '/0/0/0.mvt', { method: 'HEAD' })).status, 200)

// TileJSON: absolute tile URLs from the request, zoom limits, bounds, layers
const tileJson = async (handler, path = '/tiles.json') => {
  const response = await get(handler, path)
  assert.equal(response.status, 200)
  assert.equal(response.headers.get('Content-Type'), 'application/json')
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*')
  return response.json()
}
assert.deepEqual(await tileJson(handler), {
  tilejson: '3.0.0',
  tiles: ['http://tiles.test/{z}/{x}/{y}.mvt'],
  minzoom: 0,
  maxzoom: 22,
  bounds: [-120.000001, -40.000001, 120.000001, 40.000001],
  vector_layers: [{ id: 'places', fields: { name: 'Source column' } }],
})
const carrying = {
  ...dense,
  lod: {
    zooms: [2, 3, 4],
    attributes: [
      { name: 'height', type: 'double' },
      { name: 'floors', type: 'int32' },
      { name: 'class', type: 'string', values: ['a', 'b'] },
      { name: 'listed', type: 'bool' },
    ],
    lods: {
      f2: { zooms: [2, 3, 4], geometry_type: 'Point', scale: [1, 1], offset: [0, 0] },
    },
  },
}
const carried = await tileJson(tileHandler(carrying))
assert.equal(carried.maxzoom, 4, 'the finest LOD zoom')
assert.deepEqual(carried.vector_layers, [
  {
    id: 'features',
    fields: { height: 'Number', floors: 'Number', class: 'String', listed: 'Boolean' },
  },
])
const chosen = await tileJson(tileHandler(carrying, { properties: ['height', 'name'], maxzoom: 9 }))
assert.equal(chosen.maxzoom, 9)
assert.deepEqual(chosen.vector_layers[0].fields, { height: 'Number', name: 'Source column' })
assert.deepEqual((await tileJson(tileHandler(carrying, { properties: [] }))).vector_layers, [
  { id: 'features', fields: {} },
])
assert.throws(() => tileHandler(dense, { maxzoom: 31 }), /maxzoom must be an integer/)

// mounted under a path, and behind a proxy
const mounted = tileHandler(dense, { base: '/data/places/' })
assert.deepEqual((await tileJson(mounted, '/data/places/tiles.json')).tiles, [
  'http://tiles.test/data/places/{z}/{x}/{y}.mvt',
])
assert.equal((await get(mounted, '/data/places/0/0/0.mvt')).status, 200)
for (const path of ['/0/0/0.mvt', '/tiles.json', '/data/places', '/data/placesx/0/0/0.mvt'])
  assert.equal((await get(mounted, path)).status, 404, path)
const proxied = tileHandler(dense, { publicUrl: 'https://maps.example/tiles/' })
assert.deepEqual((await tileJson(proxied)).tiles, ['https://maps.example/tiles/{z}/{x}/{y}.mvt'])

// a failed read is a 500 that says nothing of the file, and is logged
const logged = []
const consoleError = console.error
console.error = (...args) => logged.push(args)
try {
  const failing = plainReader(0, async () => {
    throw new Error('range request failed: 503 from https://bucket.example/secret.parquet')
  })
  const response = await get(tileHandler(failing), '/0/0/0.mvt')
  assert.equal(response.status, 500)
  assert.equal(await response.text(), 'Tile read failed')
  assert.match(logged[0][0].message, /range request failed/)
} finally {
  console.error = consoleError
}

// an aborted request: the read sees the abort, and the handler still settles
let reading
const waiting = () => {
  let started
  const begun = new Promise((resolve) => (started = resolve))
  const reader = plainReader(0, (_bbox, _names, signal) => {
    reading = signal
    started()
    return new Promise((_, reject) =>
      signal.addEventListener('abort', () => reject(signal.reason), { once: true }),
    )
  })
  return { reader, begun }
}
{
  const { reader, begun } = waiting()
  const controller = new AbortController()
  const pending = get(tileHandler(reader), '/2/1/1.mvt', { signal: controller.signal })
  await begun
  assert.equal(reading.aborted, false)
  controller.abort()
  const response = await pending
  assert.equal(reading.aborted, true, "the read is given the request's signal")
  assert.equal(response.status, 499)
  assert.equal(logged.length, 1, 'an abort is not logged as a failure')
}

// the Node adapter: methods, lengths, and a client that disconnects
const listen = async (handler) => {
  const server = createServer(nodeListener(handler))
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  return { server, origin: `http://127.0.0.1:${server.address().port}` }
}
{
  const { server, origin } = await listen(handler)
  const response = await fetch(`${origin}/0/0/0.mvt`)
  assert.equal(response.status, 200)
  assert.equal(response.headers.get('content-type'), MVT)
  assert.equal(response.headers.get('content-encoding'), 'gzip', 'fetch accepts gzip')
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), new Uint8Array(expected))
  const plain = await fetch(`${origin}/0/0/0.mvt`, { headers: { 'Accept-Encoding': 'identity' } })
  assert.equal(plain.headers.get('content-length'), String(expected.byteLength))
  const head = await fetch(`${origin}/0/0/0.mvt`, {
    method: 'HEAD',
    headers: { 'Accept-Encoding': 'identity' },
  })
  assert.equal(head.status, 200)
  assert.equal(head.headers.get('content-length'), String(expected.byteLength))
  assert.equal((await head.arrayBuffer()).byteLength, 0)
  assert.deepEqual((await (await fetch(`${origin}/tiles.json`)).json()).tiles, [
    `${origin}/{z}/{x}/{y}.mvt`,
  ])
  assert.equal((await fetch(`${origin}/9/0/999.mvt`)).status, 400)
  assert.equal((await fetch(`${origin}/nothing`)).status, 404)
  assert.equal((await fetch(`${origin}/0/0/0.mvt`, { method: 'DELETE' })).status, 405)
  server.closeAllConnections()
  server.close()
}
{
  const { reader, begun } = waiting()
  const { server, origin } = await listen(tileHandler(reader))
  const controller = new AbortController()
  const request = fetch(`${origin}/2/1/1.mvt`, { signal: controller.signal })
  await begun
  controller.abort()
  await assert.rejects(request, { name: 'AbortError' })
  if (!reading.aborted) await once(reading, 'abort', { signal: AbortSignal.timeout(5000) })
  assert.equal(reading.aborted, true, 'a disconnect aborts the read')
  server.closeAllConnections()
  server.close()
}
{
  // a response whose body fails is answered with 500, and the server keeps serving
  const failing = new ReadableStream({ start: (controller) => controller.error(new Error('lost')) })
  const logged = console.error
  console.error = () => {}
  const { server, origin } = await listen(async (request) =>
    new URL(request.url).pathname === '/broken' ? new Response(failing) : new Response('fine'),
  )
  assert.equal((await fetch(`${origin}/broken`)).status, 500)
  assert.equal(await (await fetch(`${origin}/whole`)).text(), 'fine')
  console.error = logged
  server.closeAllConnections()
  server.close()
}

const [lodPath, plainPath] = process.argv.slice(2)
if (!lodPath) {
  console.log('tile server: handler and Node adapter passed; no files given, command not started')
  process.exit(0)
}

// a local file as a byte source
const file = await fileSource(lodPath)
assert.equal(new TextDecoder().decode(await file.slice(0, 4)), 'PAR1')
assert.equal(new TextDecoder().decode(await file.slice(file.byteLength - 4)), 'PAR1')
assert.equal((await file.slice(100, 100)).byteLength, 0)
assert.equal((await file.slice(8, 4104)).byteLength, 4096)
await assert.rejects(file.slice(0, file.byteLength + 1), /invalid byte range/)
await assert.rejects(file.slice(-1, 4), /invalid byte range/)
await assert.rejects(file.slice(0, 4, AbortSignal.abort()), { name: 'AbortError' })

const tileAt = (lon, lat, z) => {
  const n = 2 ** z
  const phi = (lat * Math.PI) / 180
  return [
    z,
    Math.floor(((lon + 180) / 360) * n),
    Math.floor(((1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2) * n),
  ]
}
const reader = await openReader(file)
const finest = Math.max(...reader.lod.zooms)
const carriedNames = reader.lod.attributes.map((a) => a.name)
const [west, south, east, north] = reader.bounds
const centre = [(west + east) / 2, (south + north) / 2]
const coarsest = Math.min(...reader.lod.zooms)
assert.ok(coarsest > 0, 'the file leaves zooms below its coarsest LOD')
const options = { layer: 'buildings', properties: [...carriedNames, 'name'] }
const built = tileHandler(reader, options)
let features = 0
// LOD tiles at every zoom from 0, then exact geometry two zooms past the finest
for (let z = 0; z <= finest + 2; z++) {
  const [, x, y] = tileAt(...centre, z)
  const response = await get(built, `/${z}/${x}/${y}.mvt`)
  assert.equal(response.status, 200)
  const body = await response.arrayBuffer()
  const rendered = await renderTile(reader, z, x, y, options)
  assert.deepEqual(new Uint8Array(body), new Uint8Array(rendered), `tile ${z}/${x}/${y}`)
  const layer = layersOf(body).buildings
  // one zoom below the coarsest LOD the tile draws that LOD's features
  if (z >= coarsest - 1 && z <= finest) assert.ok(layer?.length, `features at z${z}`)
  for (let i = 0; i < (layer?.length ?? 0); i++) {
    const { id, properties } = layer.feature(i)
    assert.match(properties.name, /^b\d+$/)
    if (i < 3)
      assert.deepEqual(
        { name: properties.name },
        await reader.readRow(id, ['name']),
        'a source column joins by row',
      )
    features++
  }
}
assert.ok(features > 0)
const described = await tileJson(built)
assert.equal(described.minzoom, 0)
assert.equal(described.maxzoom, finest)
assert.ok(described.bounds[0] <= west && described.bounds[2] >= east)
assert.ok(described.bounds[1] <= south && described.bounds[3] >= north)
assert.deepEqual(described.vector_layers, [
  {
    id: 'buildings',
    fields: {
      ...Object.fromEntries(
        reader.lod.attributes.map(({ name, type }) => [
          name,
          type === 'string' ? 'String' : 'Number',
        ]),
      ),
      name: 'Source column',
    },
  },
])

// a read cancelled part way leaves the reader able to serve the same tile
{
  const [z, x, y] = tileAt(...centre, finest)
  const signals = []
  const slow = await openReader({
    byteLength: file.byteLength,
    slice: async (start, end, signal) => {
      signals.push(signal)
      await new Promise((resolve) => setTimeout(resolve, 20))
      signal?.throwIfAborted()
      return file.slice(start, end, signal)
    },
  })
  signals.length = 0
  const cancellable = tileHandler(slow, options)
  const controller = new AbortController()
  const pending = get(cancellable, `/${z}/${x}/${y}.mvt`, { signal: controller.signal })
  setTimeout(() => controller.abort(), 5)
  assert.equal((await pending).status, 499)
  assert.ok(signals.length > 0, 'a read of the file was under way')
  assert.ok(
    signals.every((signal) => signal?.aborted),
    'and was cancelled',
  )
  const again = await get(cancellable, `/${z}/${x}/${y}.mvt`)
  assert.equal(again.status, 200)
  assert.deepEqual(
    new Uint8Array(await again.arrayBuffer()),
    new Uint8Array(await renderTile(reader, z, x, y, options)),
  )
}

// a plain GeoParquet file: exact geometry at every zoom, properties on request
const plainFile = await fileSource(plainPath)
const plain = await openReader(plainFile)
assert.equal(plain.lod, undefined)
const bare = tileHandler(plain)
const plainJson = await tileJson(bare)
assert.equal(plainJson.maxzoom, 22)
assert.deepEqual(plainJson.vector_layers, [{ id: 'features', fields: {} }])
for (const z of [0, 12, 16]) {
  const [, x, y] = tileAt(...centre, z)
  const body = await (await get(bare, `/${z}/${x}/${y}.pbf`)).arrayBuffer()
  assert.deepEqual(new Uint8Array(body), new Uint8Array(await renderTile(plain, z, x, y)))
  if (z === 12) assert.ok(layersOf(body).features.length > 0)
}

// the command: start on a free port, read TileJSON and a tile, stop
const cli = fileURLToPath(new URL('../dist/serve-cli.js', import.meta.url))
const run = async (args, use) => {
  const child = spawn(process.execPath, [cli, ...args], { stdio: ['ignore', 'pipe', 'pipe'] })
  const timer = setTimeout(() => child.kill('SIGKILL'), 30000)
  let stderr = ''
  child.stderr.on('data', (chunk) => (stderr += chunk))
  const exited = once(child, 'exit')
  try {
    if (use) {
      let origin
      for await (const line of createInterface({ input: child.stdout })) {
        origin = /TileJSON\s+(http:\S+)\/tiles\.json$/.exec(line)?.[1]
        if (origin) break
      }
      assert.ok(origin, `the command prints its TileJSON URL (stderr: ${stderr})`)
      await use(origin)
      child.kill('SIGTERM')
    }
    const [code, signal] = await exited
    return { code, signal, stderr }
  } finally {
    clearTimeout(timer)
    child.kill('SIGKILL')
  }
}
const served = await run(
  [lodPath, '--port', '0', '--layer', 'buildings', '--properties', options.properties.join(',')],
  async (origin) => {
    assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/, 'loopback by default')
    const response = await fetch(`${origin}/tiles.json`)
    assert.equal(response.status, 200)
    const json = await response.json()
    assert.deepEqual(json, { ...described, tiles: [`${origin}/{z}/{x}/{y}.mvt`] })
    const [z, x, y] = tileAt(...centre, finest)
    const url = json.tiles[0].replace('{z}', z).replace('{x}', x).replace('{y}', y)
    const tile = await fetch(url)
    assert.equal(tile.status, 200)
    assert.equal(tile.headers.get('content-type'), MVT)
    assert.equal(tile.headers.get('access-control-allow-origin'), '*')
    const body = await tile.arrayBuffer()
    assert.deepEqual(
      new Uint8Array(body),
      new Uint8Array(await renderTile(reader, z, x, y, options)),
    )
    assert.ok(layersOf(body).buildings.length > 0)
    assert.equal((await fetch(`${origin}/${z}/${x}/${2 ** z}.mvt`)).status, 400)
    assert.equal((await fetch(`${origin}/missing`)).status, 404)
  },
)
assert.deepEqual([served.code, served.signal], [0, null], `a clean stop (${served.stderr})`)
const plainServed = await run(
  [plainPath, '--port', '0', '--maxzoom', '16', '--public-url', 'https://maps.example/b/'],
  async (origin) => {
    const json = await (await fetch(`${origin}/tiles.json`)).json()
    assert.deepEqual(json.tiles, ['https://maps.example/b/{z}/{x}/{y}.mvt'])
    assert.equal(json.maxzoom, 16)
    const [z, x, y] = tileAt(...centre, 12)
    const body = await (await fetch(`${origin}/${z}/${x}/${y}.pbf`)).arrayBuffer()
    assert.deepEqual(new Uint8Array(body), new Uint8Array(await renderTile(plain, z, x, y)))
  },
)
assert.equal(plainServed.code, 0)
// an http(s) URL is read by range requests
{
  const requested = []
  const origin = createServer(async (request, response) => {
    const [, first, last] = /^bytes=(\d+)-(\d+)$/.exec(request.headers.range ?? '') ?? []
    if (first === undefined) return response.writeHead(416).end()
    requested.push(request.headers.range)
    const end = Math.min(Number(last) + 1, file.byteLength)
    const body = Buffer.from(await file.slice(Number(first), end))
    response.writeHead(206, {
      'Content-Range': `bytes ${first}-${end - 1}/${file.byteLength}`,
      'Content-Length': body.length,
    })
    response.end(body)
  })
  origin.listen(0, '127.0.0.1')
  await once(origin, 'listening')
  const url = `http://127.0.0.1:${origin.address().port}/buildings.lod.parquet`
  const remote = await run([url, '--port', '0', '--layer', 'buildings'], async (served) => {
    // the one-byte requests beside the opening read open connections
    const reads = () => requested.filter((range) => range !== 'bytes=0-0')
    assert.deepEqual(reads(), ['bytes=0-16383'], 'an indexed file opens in one read')
    const [z, x, y] = tileAt(...centre, finest)
    const body = await (await fetch(`${served}/${z}/${x}/${y}.mvt`)).arrayBuffer()
    assert.deepEqual(
      new Uint8Array(body),
      new Uint8Array(await renderTile(reader, z, x, y, { layer: 'buildings' })),
    )
    assert.ok(reads().length > 1, 'the tile is read from the URL')
  })
  assert.equal(remote.code, 0, remote.stderr)
  origin.closeAllConnections()
  origin.close()
}
const missing = await run([`${lodPath}.missing`, '--port', '0'])
assert.equal(missing.code, 1)
assert.match(missing.stderr, /tiled-geoparquet-serve: cannot serve .*ENOENT/)
const usage = await run([])
assert.equal(usage.code, 2)
assert.match(usage.stderr, /expected one path or URL[\s\S]*Usage: tiled-geoparquet-serve/)
assert.equal((await run([lodPath, '--port', '70000'])).code, 2)
const deep = await run([lodPath, '--maxzoom', 'deep'])
assert.equal(deep.code, 2)
assert.match(deep.stderr, /invalid maxzoom deep/)

await file.close()
await plainFile.close()
console.log(`tile server: handler, Node adapter and command passed, ${features} features compared`)
