// Shapes just short of a tile's west edge reach the tile when the file's margin
// covers the gap, drawn unclipped out to the margin, from the LODs and past the
// finest zoom alike; files of another format version or without a margin are refused.
//   node test/margin.test.mjs file.parquet cases.json [refused.parquet ...]
// cases.json: [{ z, x, y, id, present, reach }], reach in MVT extent units
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { GeoParquetReader, coalescing, openReader, renderTile } from '../dist/index.js'
import { isCompatibleVersion } from '../dist/reader.js'

const [path, casesPath, ...refused] = process.argv.slice(2)
if (!path) {
  console.log('margin test skipped: no fixture given')
  process.exit(0)
}
const open = async (path) => {
  const data = await readFile(path)
  return coalescing({
    byteLength: data.length,
    slice: async (a, b) => Uint8Array.from(data.subarray(a, b)).buffer,
  })
}
const readers = [await openReader(await open(path)), await GeoParquetReader.open(await open(path))]
assert.notEqual(readers[0].constructor, readers[1].constructor, 'both readers are tested')
const cases = JSON.parse(await readFile(casesPath, 'utf8'))
for (const reader of readers) {
  for (const { z, x, y, id, present, reach } of cases) {
    const layer = new VectorTile(new PbfReader(new Uint8Array(await renderTile(reader, z, x, y))))
      .layers.features
    let feature
    for (let i = 0; i < (layer?.length ?? 0); i++)
      if (layer.feature(i).id === id) feature = layer.feature(i)
    const where = `row ${id} in ${z}/${x}/${y} from ${reader.constructor.name}`
    if (!present) {
      assert.equal(feature, undefined, `${where} lies past the margin`)
      continue
    }
    assert.ok(feature, `${where} lies within the margin`)
    const xs = feature
      .loadGeometry()
      .flat()
      .map((p) => p.x)
    assert.ok(Math.min(...xs) <= -reach, `${where} reaches ${Math.min(...xs)}, short of ${-reach}`)
  }
}
assert.equal(isCompatibleVersion('0.1.0'), false)
for (const file of refused) {
  await assert.rejects(async () => openReader(await open(file)), /lod_columns/, file)
}
console.log(`margin tests passed, ${cases.length} cases, ${refused.length} files refused`)
