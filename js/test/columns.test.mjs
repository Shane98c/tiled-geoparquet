// node test/columns.test.mjs <packed file> <columns file> <cases json>
// A file keeping attributes as columns renders the tiles a file packing them
// renders, through the spatial reader, the footer reader and the worker pool,
// and a style asking for a column attribute fetches only that attribute.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { GeoParquetReader, openReader, readFeatures, renderTile } from '../dist/index.js'
import { decodePreparedTile } from '../dist/tile.js'
import { decodeAttributes, decodeTile } from '../dist/spatial.js'
import { attributeFields } from '../dist/decode.js'
import { workerClient } from './worker-client.mjs'

const [packedPath, columnsPath, casesPath] = process.argv.slice(2)
if (!casesPath) throw new Error('columns.test.mjs needs a packed file, a columns file and cases')
const { tiles, styles, query } = JSON.parse(await readFile(casesPath, 'utf8'))

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
const features = (buffer) => {
  const tile = new VectorTile(new PbfReader(new Uint8Array(buffer)))
  const out = []
  for (const [name, layer] of Object.entries(tile.layers))
    for (let i = 0; i < layer.length; i++) {
      const f = layer.feature(i)
      const properties = Object.entries(f.properties).sort()
      out.push(JSON.stringify([name, f.id, f.type, properties, f.loadGeometry()]))
    }
  return out.sort()
}

const packed = await openReader(await memory(packedPath))
const columns = await openReader(await memory(columnsPath))
const footer = await GeoParquetReader.open(await memory(columnsPath))
assert.ok(columns.columnAttributes.length, 'the file keeps attributes as columns')
const clients = [await workerClient(columnsPath, 1), await workerClient(columnsPath, 2)]

let compared = 0
for (const [z, x, y] of tiles) {
  for (const properties of styles) {
    const expected = features(await renderTile(packed, z, x, y, { layer: 'f', properties }))
    const got = {
      spatial: features(await renderTile(columns, z, x, y, { layer: 'f', properties })),
    }
    if (z <= columns.finestZoom)
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
    for (const [path, found] of Object.entries(got)) {
      assert.deepEqual(found, expected, `${path} z${z}/${x}/${y} ${JSON.stringify(properties)}`)
      compared++
    }
  }
}

// whole-feature overview queries return column attributes as packed ones
for (const reader of [columns, footer]) {
  const a = await readFeatures(packed, query)
  const b = await readFeatures(reader, query)
  assert.deepEqual(b.rowIndex, a.rowIndex)
  assert.deepEqual(b.columns, a.columns)
}

// A tile decoded without a column attribute's pages holds no array for it,
// only the values its boundary copies pack.
let copiesSeen = 0
for (const [z, x, y] of tiles.filter(([z]) => z <= columns.finestZoom)) {
  const lod = columns.lodForZoom(z)
  const decoded = decodePreparedTile(await columns.prepareTile(lod, z, x, y))
  const copies = decoded.own.reduce((n, mark) => n + (mark ? 0 : 1), 0)
  copiesSeen += copies
  assert.deepEqual(decoded.pending, columns.columnAttributes)
  for (const name of columns.columnAttributes) {
    assert.ok(!Object.hasOwn(decoded.columns, name), `${name} has no array before it is read`)
    assert.equal(decoded.copies[name].length, copies)
  }
  const whole = decodePreparedTile(
    await columns.prepareTile(lod, z, x, y, undefined, [], columns.columnAttributes),
  )
  assert.deepEqual([whole.pending, whole.own, whole.copies], [undefined, undefined, undefined])
  for (const name of columns.columnAttributes)
    assert.deepEqual(
      whole.columns[name].filter((_, i) => !decoded.own[i]),
      decoded.copies[name],
    )
}

// Every attribute page a tile fetched is checked against the rows its LOD
// page shows, one no row of the tile takes a value from among them.
{
  const name = columns.columnAttributes[0]
  const fields = attributeFields(columns.lod.attributes)
  let checked = 0
  for (const [z, x, y] of tiles.filter(([z]) => z <= columns.finestZoom)) {
    const lod = columns.lodForZoom(z)
    const { shown } = decodeTile(await columns.prepareTile(lod, z, x, y), [])
    const attributes = await columns.prepareAttributes(lod, z, x, y, [name])
    if (!attributes[0].pages.some((page) => page.length)) continue
    const task = { rows: [], ranks: [], fields, attributes }
    assert.deepEqual(decodeAttributes({ ...task, shown }), { [name]: [] })
    assert.throws(
      () => decodeAttributes({ ...task, shown: shown.map((value, k) => (k % 2 ? 0 : value)) }),
      new RegExp(`${name} holds a value past the rows its page shows`),
      `z${z}/${x}/${y}`,
    )
    checked++
  }
  assert.ok(checked > 0, 'attribute pages checked with no row read from them')
}

// A style change on a decoded tile fetches only the new attribute's pages and
// its leaf; asking again fetches nothing.
const [z, x, y] = tiles.find(([z]) => z <= columns.finestZoom && z >= 12) ?? tiles[0]
const [first, second] = columns.columnAttributes
const client = await workerClient(columnsPath, 2)
const stats = async () => (await client.call({ type: 'stats' })).stats.file ?? { bytes: 0 }
const tile = (properties) =>
  client.call({ type: 'tile', config: { url: 'file', layer: 'f', properties }, z, x, y })
await client.call({ type: 'open', url: 'file' })
const opened = await stats()
await tile([])
const plain = await stats()
{
  const { helpers, helperJobs } = (await client.call({ type: 'stats' })).network
  assert.equal(helpers, 2, 'two workers start two CPU helpers')
  assert.ok(helperJobs > 0, 'the helpers decode the tile')
}
// a tile naming no properties holds the packed attributes and reads no column
const packedNames = columns.lod.attributes.filter((a) => a.storage !== 'column').map((a) => a.name)
const unnamed = await client.call({ type: 'tile', config: { url: 'file', layer: 'f' }, z, x, y })
assert.equal((await stats()).bytes, plain.bytes, 'a tile naming no properties reads no column')
assert.deepEqual(
  features(unnamed.data),
  features(await renderTile(columns, z, x, y, { layer: 'f', properties: packedNames })),
)
assert.deepEqual(
  features(await renderTile(columns, z, x, y, { layer: 'f' })),
  features(unnamed.data),
)
const styled = await tile([first])
const restyled = await stats()
await tile([first])
assert.equal((await stats()).bytes, restyled.bytes, 'a decoded style is not fetched again')
assert.ok(restyled.requests - plain.requests <= 2, 'one attribute leaf and its pages')
// a shape outweighs one attribute of it; a point packs to a few bytes
if (columns.lodForZoom(z).geometry_type !== 'Point')
  assert.ok(
    restyled.bytes - plain.bytes < plain.bytes - opened.bytes,
    'the style fetches less than the tile did',
  )
assert.deepEqual(
  features(styled.data),
  features(await renderTile(packed, z, x, y, { layer: 'f', properties: [first] })),
)
// a cancelled style leaves the decoded tile usable for the next request
if (second) {
  const cancelled = tile([first, second])
  client.cancel(cancelled.id)
  await cancelled.catch((error) => assert.equal(error.name, 'AbortError'))
  const both = await tile([first, second])
  assert.deepEqual(
    features(both.data),
    features(await renderTile(packed, z, x, y, { layer: 'f', properties: [first, second] })),
  )
}
// requests arriving together for a tile's last pending attribute all get it
{
  const together = await workerClient(columnsPath, 2)
  const ask = (properties) =>
    together.call({ type: 'tile', config: { url: 'file', layer: 'f', properties }, z, x, y })
  const last = columns.columnAttributes.at(-1)
  await ask(columns.columnAttributes.filter((name) => name !== last))
  const expected = features(await renderTile(packed, z, x, y, { layer: 'f', properties: [last] }))
  for (const reply of await Promise.all([1, 2, 3, 4].map(() => ask([last]))))
    assert.deepEqual(features(reply.data), expected)
  together.close()
}
// a reader's first tile, naming no properties, reads what a geometry-only one does
{
  const requested = async (config) => {
    const fresh = await workerClient(columnsPath, 2)
    const reply = await fresh.call({ type: 'tile', config, z, x, y })
    const { bytes } = (await fresh.call({ type: 'stats' })).stats.file
    fresh.close()
    return [bytes, features(reply.data)]
  }
  const [bytes, found] = await requested({ url: 'file', layer: 'f' })
  const [geometryBytes] = await requested({ url: 'file', layer: 'f', properties: [] })
  assert.equal(bytes, geometryBytes, 'a first tile naming no properties reads no column')
  assert.deepEqual(
    found,
    features(await renderTile(packed, z, x, y, { layer: 'f', properties: packedNames })),
  )
}
client.close()
for (const c of clients) c.close()
console.log(
  `column attributes: ${compared} tiles render as packed, ${copiesSeen} boundary copies checked`,
)
