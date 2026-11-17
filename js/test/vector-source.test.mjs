// vectorSource gives a MapLibre source the file's zoom limits and bounds.
//   node test/vector-source.test.mjs [file.lod.parquet plain.parquet]
// The worker is replaced by one answering `open` with metadata: made up, and
// with both files given, also what a reader opens from each.
import assert from 'node:assert/strict'
import { openReader } from '../dist/index.js'
import { fileSource } from '../dist/node.js'
import { sourceLimits } from '../dist/source-limits.js'

const files = new Map()
const opened = []
globalThis.Worker = class {
  addEventListener(type, callback) {
    if (type === 'message') this.reply = callback
  }
  postMessage(request) {
    if (request.type !== 'open') return
    opened.push(request.url)
    const metadata = files.get(request.url)
    const data = metadata
      ? { id: request.id, ok: true, opened: true, metadata }
      : { id: request.id, ok: false, error: `cannot open ${request.url}`, name: 'Error' }
    queueMicrotask(() => this.reply({ data }))
  }
  terminate() {}
}
const { registerProtocol, sourceUrl, tileUrl, vectorSource } = await import('../dist/tiles.js')

// the finest LOD's grid is a quarter of a 512 px tile's pixel at its zoom
const cell = (zoom) => (2 * Math.PI * 6378137) / (512 * 2 ** zoom) / 4
const level = (zoom, placeholder) => ({
  zooms: [zoom],
  geometry_type: 'MultiPolygon',
  scale: [cell(zoom), cell(zoom)],
  offset: [0, 0],
  placeholder,
})
const lod = {
  zooms: [4, 5, 6, 7, 8, 9, 10, 11, 12],
  attributes: [{ name: 'h', type: 'float' }],
  lods: { f4: level(4, 2), f12: level(12, 2) },
}
files.set('lod', { lod, bounds: [-105.05, 39.7, -104.45, 40], columns: ['h'] })
files.set('plain', { lod: undefined, bounds: [-180, -90, 180, 90], columns: [] })
files.set('edge', { lod, bounds: [0, 0, 0, 0], columns: [] })
// a sliver just west of a tile edge, drawn at z14 as a placeholder square across it
const sliver = { ...lod, zooms: [14], lods: { f14: level(14, 2) } }
files.set('sliver', { lod: sliver, bounds: [-0.000005, 40, -0.000004, 40.0002], columns: [] })

const config = { url: 'lod', layer: 'buildings', properties: ['h'] }
const source = await vectorSource(config)
assert.deepEqual(Object.keys(source).sort(), ['bounds', 'maxzoom', 'minzoom', 'tiles', 'type'])
assert.equal(source.type, 'vector')
assert.deepEqual(source.tiles, [tileUrl(config)])
assert.equal(source.minzoom, 0, 'zooms below the coarsest LOD draw it, so the map starts at 0')
assert.equal(source.maxzoom, 12, 'the map overzooms the finest LOD')
const within = (outer, inner, slack) =>
  outer[0] <= inner[0] &&
  outer[1] <= inner[1] &&
  outer[2] >= inner[2] &&
  outer[3] >= inner[3] &&
  outer.every((value, i) => Math.abs(value - inner[i]) <= slack)
// three z12 cells: a placeholder two cells long from the centroid's cell
const reach = (3 * cell(12) * 180) / (Math.PI * 6378137)
assert.ok(within(source.bounds, [-105.05, 39.7, -104.45, 40], reach * 1.001), `${source.bounds}`)
assert.ok(source.bounds[2] - -104.45 > reach * 0.999, 'the bounds hold the finest LOD shapes')
assert.deepEqual(opened, ['lod'], 'the source opens the file in the worker')

assert.equal((await vectorSource(config, { maxzoom: 16 })).maxzoom, 16, 'exact geometry to z16')
assert.equal((await vectorSource(config, { maxzoom: 8 })).maxzoom, 8)
for (const maxzoom of [-1, 2.5, 31, NaN, '12'])
  await assert.rejects(vectorSource(config, { maxzoom }), /maxzoom must be an integer from 0 to 30/)

// A source named by URL is answered by the protocol with what vectorSource gives, less
// its type, which the map's own source object holds.
{
  let scheme, answer
  registerProtocol({
    addProtocol(name, action) {
      scheme = name
      answer = action
    },
  })
  assert.equal(scheme, 'lod')
  const { type, ...described } = source
  assert.equal(type, 'vector')
  const named = await answer({ url: sourceUrl(config), type: 'json' }, new AbortController())
  assert.deepEqual(named, { data: described })
  const deeper = await answer({ url: sourceUrl(config, { maxzoom: 16 }) }, new AbortController())
  assert.equal(deeper.data.maxzoom, 16)
  assert.deepEqual(deeper.data.tiles, [tileUrl(config)])
  await assert.rejects(
    answer({ url: sourceUrl(config, { maxzoom: 99 }) }, new AbortController()),
    /maxzoom must be an integer from 0 to 30/,
  )
  await assert.rejects(
    answer({ url: sourceUrl({ url: 'missing' }) }, new AbortController()),
    /cannot open missing/,
  )
}
const plain = await vectorSource({ url: 'plain' })
assert.equal(plain.minzoom, 0)
assert.equal(plain.maxzoom, 22, 'without LODs every zoom draws exact geometry')
assert.deepEqual(plain.bounds, [-180, -90, 180, 90], 'bounds stay inside the world')
assert.equal((await vectorSource({ url: 'plain' }, { maxzoom: 14 })).maxzoom, 14)

// A point at the origin lies on a tile edge at every zoom: bounds that end on
// the edge have no width, so they reach past it into the tiles on both sides.
const [west, south, east, north] = (await vectorSource({ url: 'edge' })).bounds
assert.ok(west < 0 && south < 0 && east > 0 && north > 0)

assert.ok(
  (await vectorSource({ url: 'sliver' })).bounds[2] > 0,
  'the tile east of the edge holds part of the square',
)
// a collapsed line draws as a segment of its placeholder's whole length, here
// eight z14 cells east of its centroid's cell
const dash = {
  ...lod,
  zooms: [14],
  lods: { l14: { ...level(14, 8), geometry_type: 'MultiLineString' } },
}
files.set('dash', { lod: dash, bounds: [-0.00005, 40, -0.00005, 40], columns: [] })
const segment = (8 * cell(14) * 180) / (Math.PI * 6378137)
assert.ok(
  (await vectorSource({ url: 'dash' })).bounds[2] >= -0.00005 + segment,
  'the bounds hold the whole segment',
)

await assert.rejects(vectorSource({ url: 'missing' }), /cannot open missing/)

// MapLibre's own validation, when its style specification is installed
const spec = await import('@maplibre/maplibre-gl-style-spec').catch(() => undefined)
if (spec) {
  const layers = [{ id: 'fill', type: 'fill', source: 'buildings', 'source-layer': 'buildings' }]
  const errors = (candidate) =>
    spec.validateStyleMin({ version: 8, sources: { buildings: candidate }, layers })
  assert.deepEqual(errors(source), [])
  assert.deepEqual(errors(plain), [])
  assert.equal(errors({ ...source, bounds: [0, 0, 1] }).length, 1, 'the validator checks bounds')
}

const [lodPath, plainPath] = process.argv.slice(2)
if (lodPath) {
  for (const path of [lodPath, plainPath]) {
    const file = await fileSource(path)
    const reader = await openReader(file)
    files.set(path, { lod: reader.lod, bounds: reader.bounds, columns: reader.scalarColumns() })
    await file.close()
  }
  const built = files.get(lodPath)
  const fromLod = await vectorSource({ url: lodPath, layer: 'buildings' })
  assert.equal(fromLod.minzoom, 0)
  assert.equal(fromLod.maxzoom, Math.max(...built.lod.zooms), 'the finest LOD zoom')
  assert.ok(within(fromLod.bounds, built.bounds, 1e-3), `bounds ${fromLod.bounds}`)
  assert.deepEqual(fromLod, {
    type: 'vector',
    tiles: [tileUrl({ url: lodPath, layer: 'buildings' })],
    ...sourceLimits(built),
  })
  const fromPlain = await vectorSource({ url: plainPath })
  assert.equal(files.get(plainPath).lod, undefined)
  assert.equal(fromPlain.maxzoom, 22)
  assert.ok(within(fromPlain.bounds, files.get(plainPath).bounds, 1e-5))
  if (spec)
    assert.deepEqual(spec.validateStyleMin({ version: 8, sources: { fromLod }, layers: [] }), [])
}
console.log(
  `vector source: zoom limits and bounds${lodPath ? ' from built files' : ''}` +
    `${spec ? ', valid MapLibre sources' : ' (style validation skipped: no style specification installed)'}`,
)
