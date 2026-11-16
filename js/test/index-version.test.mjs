// node test/index-version.test.mjs <unindexed> <indexed> <version>=<file>...
// A file whose spatial index is of another version is refused by every
// entry point, the spatial reader opened or constructed from its root and
// the footer reader, before any value is read; an unindexed file and an
// indexed one of this version open.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { gunzipSync } from 'node:zlib'
import { GeoParquetReader, SpatialReader, openReader, renderTile } from '../dist/index.js'

const [unindexed, indexed, ...others] = process.argv.slice(2)
if (!others.length) throw new Error('index-version.test.mjs needs files of other versions')
const memory = async (path) => {
  const data = await readFile(path)
  return {
    byteLength: data.length,
    slice: async (a, b = data.length) => Uint8Array.from(data.subarray(a, b)).buffer,
  }
}
for (const path of [unindexed, indexed])
  for (const open of [openReader, GeoParquetReader.open]) {
    const reader = await open(await memory(path))
    const z = reader.finestZoom
    await renderTile(reader, z, 0, 0, { layer: 'f', properties: ['height'] })
  }
let refused = 0
for (const other of others) {
  const [version, path] = other.split('=')
  const error = new RegExp(`unsupported spatial index version ${version}`)
  await assert.rejects(openReader(await memory(path)), error, `openReader, version ${version}`)
  await assert.rejects(
    GeoParquetReader.open(await memory(path)),
    error,
    `GeoParquetReader.open, version ${version}`,
  )
  const data = await readFile(path)
  const root = JSON.parse(gunzipSync(data.subarray(16, 16 + data.readUInt32LE(12))))
  const file = await memory(path)
  assert.throws(() => new SpatialReader(file, root), error, `SpatialReader, version ${version}`)
  refused++
}
console.log(`index versions: ${refused} refused, an unindexed file and this version read`)
