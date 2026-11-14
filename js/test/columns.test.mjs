// node test/columns.test.mjs <file> <cases json>
// A file's attributes render with the values the cases expect, through the
// spatial reader, the footer reader and the worker pool, which render the
// same tiles, and a style asking for an attribute fetches only that attribute.
// The cases give each zoom's expected values by row: {number: 'NaN'} stands
// for a value JSON cannot hold.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { GeoParquetReader, openReader, readFeatures, renderTile } from '../dist/index.js'
import { decodePreparedTile } from '../dist/tile.js'
import { decodeAttributes, decodeTile } from '../dist/spatial.js'
import { attributeFields } from '../dist/decode.js'
import { workerClient } from './worker-client.mjs'

const [path, casesPath] = process.argv.slice(2)
if (!casesPath) throw new Error('columns.test.mjs needs a file and cases')
const { tiles, styles, query, expected } = JSON.parse(await readFile(casesPath, 'utf8'))

const memory = async (path) => {
  const data = await readFile(path)
  const reads = []
  return {
    reads,
    byteLength: data.length,
    async slice(a, b = data.length) {
      reads.push([a, b])
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  }
}
const decoded = (buffer) => {
  const tile = new VectorTile(new PbfReader(new Uint8Array(buffer)))
  const out = []
  for (const [name, layer] of Object.entries(tile.layers))
    for (let i = 0; i < layer.length; i++) {
      const f = layer.feature(i)
      out.push({ layer: name, id: f.id, type: f.type, properties: f.properties, f })
    }
  return out
}
const features = (buffer) =>
  decoded(buffer)
    .map(({ layer, id, type, properties, f }) =>
      JSON.stringify([layer, id, type, Object.entries(properties).sort(), f.loadGeometry()]),
    )
    .sort()

const reader = await openReader(await memory(path))
const footer = await GeoParquetReader.open(await memory(path))
const names = reader.attributeNames
assert.ok(names.length, 'the file has attributes')
const clients = [await workerClient(path, 1), await workerClient(path, 2)]

// expected values by zoom, attribute and row, as numbers and as an MVT holds them
const number = (value) => (value && typeof value === 'object' ? Number(value.number) : value)
const values = Object.fromEntries(
  Object.entries(expected).map(([zoom, byName]) => [
    zoom,
    Object.fromEntries(
      Object.entries(byName).map(([name, rows]) => [
        name,
        new Map(Object.entries(rows).map(([row, value]) => [Number(row), number(value)])),
      ]),
    ),
  ]),
)
const inMvt = (value) =>
  typeof value === 'number' && !Number.isFinite(value) ? String(value) : value
const expectedAt = (z) => values[Math.min(z, reader.finestZoom)]

let compared = 0,
  checked = 0
for (const [z, x, y] of tiles) {
  const geometry = features(await renderTile(reader, z, x, y, { layer: 'f', properties: [] }))
  for (const properties of styles) {
    const spatial = await renderTile(reader, z, x, y, { layer: 'f', properties })
    const got = {}
    if (z <= reader.finestZoom)
      got.footer = features(await renderTile(footer, z, x, y, { layer: 'f', properties }))
    for (const [i, client] of clients.entries()) {
      const reply = await client.call({
        type: 'tile',
        config: { url: 'file', layer: 'f', properties },
        z,
        x,
        y,
      })
      got[`workers ${i + 1}`] = features(reply.data)
    }
    const where = `z${z}/${x}/${y} ${JSON.stringify(properties)}`
    for (const [label, found] of Object.entries(got)) {
      assert.deepEqual(found, features(spatial), `${label} ${where}`)
      compared++
    }
    const shown = expectedAt(z)
    const drawn = []
    for (const { layer, id, type, properties: held, f } of decoded(spatial)) {
      const wanted = {}
      for (const name of properties) {
        assert.ok(shown[name].has(id), `row ${id} is shown at ${where}`)
        const value = inMvt(shown[name].get(id))
        if (value !== null) wanted[name] = value
      }
      assert.deepEqual({ ...held }, wanted, `row ${id} ${where}`)
      if (properties.length) checked++
      drawn.push(JSON.stringify([layer, id, type, [], f.loadGeometry()]))
    }
    assert.deepEqual(drawn.sort(), geometry, `a style leaves the geometry as it is, ${where}`)
  }
}
assert.ok(checked > 0, 'features with attributes checked')

// whole-feature overview queries return the expected values
{
  const target = Math.ceil(Math.log2(360 / (reader.lod.tile_size * query.resolution)))
  const zoom = [...reader.lod.zooms].sort((a, b) => a - b).find((z) => z >= target)
  const shown = expectedAt(zoom)
  const a = await readFeatures(reader, query)
  const b = await readFeatures(footer, query)
  assert.deepEqual(b.rowIndex, a.rowIndex)
  assert.deepEqual(b.columns, a.columns)
  assert.ok(a.rowIndex.length, 'the query reads features')
  for (const name of query.columns)
    assert.deepEqual(
      a.columns[name],
      a.rowIndex.map((row) => shown[name].get(row)),
      `query ${name}`,
    )
}

// A tile decoded without an attribute's pages holds no array for it, only
// the values its boundary copies pack.
let copiesSeen = 0
for (const [z, x, y] of tiles.filter(([z]) => z <= reader.finestZoom)) {
  const lod = reader.lodForZoom(z)
  const tile = decodePreparedTile(await reader.prepareTile(lod, z, x, y))
  const copies = tile.own.reduce((n, mark) => n + (mark ? 0 : 1), 0)
  copiesSeen += copies
  assert.deepEqual(tile.pending, names)
  for (const name of names) {
    assert.ok(!Object.hasOwn(tile.columns, name), `${name} has no array before it is read`)
    assert.equal(tile.copies[name].length, copies)
  }
  const whole = decodePreparedTile(await reader.prepareTile(lod, z, x, y, undefined, [], names))
  assert.deepEqual([whole.pending, whole.own, whole.copies], [undefined, undefined, undefined])
  const shown = expectedAt(z)
  for (const name of names) {
    assert.deepEqual(
      whole.columns[name].filter((_, i) => !tile.own[i]),
      tile.copies[name],
    )
    assert.deepEqual(
      whole.columns[name],
      whole.rows.map((row) => shown[name].get(row)),
      `${name} z${z}/${x}/${y}`,
    )
  }
}

// Every attribute page a tile fetched is checked against the rows its LOD
// page shows, one no row of the tile takes a value from among them.
{
  const name = names[0]
  const fields = attributeFields(reader.lod.attributes)
  let pages = 0
  for (const [z, x, y] of tiles.filter(([z]) => z <= reader.finestZoom)) {
    const lod = reader.lodForZoom(z)
    const { shown } = decodeTile(await reader.prepareTile(lod, z, x, y), [])
    const attributes = await reader.prepareAttributes(lod, z, x, y, [name])
    if (!attributes[0].pages.some((page) => page.length)) continue
    const task = { rows: [], ranks: [], fields, attributes }
    assert.deepEqual(decodeAttributes({ ...task, shown }), { [name]: [] })
    assert.throws(
      () => decodeAttributes({ ...task, shown: shown.map((value, k) => (k % 2 ? 0 : value)) }),
      new RegExp(`${name} holds a value past the rows its page shows`),
      `z${z}/${x}/${y}`,
    )
    pages++
  }
  assert.ok(pages > 0, 'attribute pages checked with no row read from them')
}

// A style change on a decoded tile fetches only the new attribute's pages and
// its leaf; asking again fetches nothing.
const [z, x, y] = tiles.find(([z]) => z <= reader.finestZoom && z >= 12) ?? tiles[0]
const [first, second] = names
const client = await workerClient(path, 2)
const stats = async () => (await client.call({ type: 'stats' })).stats.file ?? { bytes: 0 }
const tile = (properties) =>
  client.call({ type: 'tile', config: { url: 'file', layer: 'f', properties }, z, x, y })
const rendered = async (properties) =>
  features(await renderTile(reader, z, x, y, { layer: 'f', properties }))
await client.call({ type: 'open', url: 'file' })
const opened = await stats()
await tile([])
const plain = await stats()
{
  const { helpers, helperJobs } = (await client.call({ type: 'stats' })).network
  assert.equal(helpers, 2, 'two workers start two CPU helpers')
  assert.ok(helperJobs > 0, 'the helpers decode the tile')
}
// a tile naming no properties holds geometry only and reads no attribute
const unnamed = await client.call({ type: 'tile', config: { url: 'file', layer: 'f' }, z, x, y })
assert.equal((await stats()).bytes, plain.bytes, 'a tile naming no properties reads no attribute')
assert.deepEqual(features(unnamed.data), await rendered([]))
assert.deepEqual(features(await renderTile(reader, z, x, y, { layer: 'f' })), await rendered([]))
assert.ok(
  decoded(unnamed.data).every((f) => !Object.keys(f.properties).length),
  'no properties',
)
const styled = await tile([first])
const restyled = await stats()
await tile([first])
assert.equal((await stats()).bytes, restyled.bytes, 'a decoded style is not fetched again')
assert.ok(restyled.requests - plain.requests <= 2, 'one attribute leaf and its pages')
// a shape outweighs one attribute of it; a point packs to a few bytes
if (reader.lodForZoom(z).geometry_type !== 'Point')
  assert.ok(
    restyled.bytes - plain.bytes < plain.bytes - opened.bytes,
    'the style fetches less than the tile did',
  )
assert.deepEqual(features(styled.data), await rendered([first]))
// a cancelled style leaves the decoded tile usable for the next request
if (second) {
  const cancelled = tile([first, second])
  client.cancel(cancelled.id)
  await cancelled.catch((error) => assert.equal(error.name, 'AbortError'))
  const both = await tile([first, second])
  assert.deepEqual(features(both.data), await rendered([first, second]))
}
// requests arriving together for a tile's last pending attribute all get it
{
  const together = await workerClient(path, 2)
  const ask = (properties) =>
    together.call({ type: 'tile', config: { url: 'file', layer: 'f', properties }, z, x, y })
  const last = names.at(-1)
  await ask(names.filter((name) => name !== last))
  const wanted = await rendered([last])
  for (const reply of await Promise.all([1, 2, 3, 4].map(() => ask([last]))))
    assert.deepEqual(features(reply.data), wanted)
  together.close()
}
// a reader's first tile, naming no properties, reads what a geometry-only one does
{
  const requested = async (config) => {
    const fresh = await workerClient(path, 2)
    const reply = await fresh.call({ type: 'tile', config, z, x, y })
    const { bytes } = (await fresh.call({ type: 'stats' })).stats.file
    fresh.close()
    return [bytes, features(reply.data)]
  }
  const [bytes, found] = await requested({ url: 'file', layer: 'f' })
  const [geometryBytes] = await requested({ url: 'file', layer: 'f', properties: [] })
  assert.equal(bytes, geometryBytes, 'a first tile naming no properties reads no attribute')
  assert.deepEqual(found, await rendered([]))
}
client.close()
for (const c of clients) c.close()
console.log(
  `attributes: ${compared} renders agree, ${checked} styled renders hold the expected values, ` +
    `${copiesSeen} boundary copies checked`,
)
