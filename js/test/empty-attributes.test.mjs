// node test/empty-attributes.test.mjs <file> <cases.json>
// An overview page whose rows hold no value of a column attribute has an
// empty entry in its attribute leaf. A tile styled by the attribute holds
// its value or null on every feature, requests no byte of a page of nulls,
// and, where every overview page it reads is empty of the attribute,
// requests no page of the attribute at all. The cases give the attribute,
// its value at every row, tiles to read, the byte ranges of its pages of
// nulls and of all its pages.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { openReader, renderTile } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!casesPath) {
  console.log('empty-attributes test skipped: no fixture given')
  process.exit(0)
}
const data = await readFile(path)
const { attribute, values, tiles, empty, chunks } = JSON.parse(await readFile(casesPath, 'utf8'))

/** A fresh reader over `data` recording the byte ranges it requests. */
const counted = async () => {
  const ranges = []
  const reader = await openReader({
    byteLength: data.length,
    async slice(a, b = data.length) {
      ranges.push([a, b])
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  })
  return { reader, ranges }
}

/** The bytes of `ranges` inside the disjoint `blocks`. */
const within = (ranges, blocks) =>
  ranges.reduce(
    (sum, [a, b]) =>
      sum + blocks.reduce((n, [s, e]) => n + Math.max(0, Math.min(b, e) - Math.max(a, s)), 0),
    0,
  )

const render = async (reader, [z, x, y], properties) => {
  const buffer = await renderTile(reader, z, x, y, { layer: 'f', properties })
  return new VectorTile(new PbfReader(new Uint8Array(buffer))).layers.f
}

let valued = 0,
  nulls = 0,
  fetching = 0,
  bare = 0
for (const tile of tiles) {
  const [z, x, y] = tile
  const probe = (await counted()).reader
  const [located] = await probe.prepareAttributes(probe.lodForZoom(z), z, x, y, [attribute])
  const hollow = located.pages.every((page) => !page.length)
  const { reader, ranges } = await counted()
  const layer = await render(reader, tile, [attribute])
  if (!layer?.length) continue
  let here = 0
  for (let f = 0; f < layer.length; f++) {
    const feature = layer.feature(f)
    assert.equal(
      feature.properties[attribute],
      values[feature.id] ?? undefined,
      `${tile.join('/')} row ${feature.id}`,
    )
    if (values[feature.id] === null) nulls++
    else here++
  }
  valued += here
  assert.equal(within(ranges, empty), 0, `${tile.join('/')} requests a page of nulls`)
  const fetched = within(ranges, chunks)
  if (fetched) fetching++
  if (hollow) {
    assert.equal(fetched, 0, `${tile.join('/')} requests an attribute page`)
    bare++
  }
}
assert.ok(valued > 0 && nulls > 0, `features carry ${attribute} and nulls`)
assert.ok(fetching > 0, `some tiles fetch ${attribute} pages`)
assert.ok(bare > 0, 'some tiles read only overview pages empty of the attribute')
console.log(
  `empty attribute pages: ${tiles.length} tiles, ${valued} values and ${nulls} nulls of ` +
    `${attribute}, ${fetching} tiles fetching its pages, ${bare} reading only empty ones`,
)
