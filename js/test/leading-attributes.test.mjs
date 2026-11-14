// node test/leading-attributes.test.mjs <file> <cases json>
// A file whose attribute pages hold values past the rows their overview
// pages show is refused where a tile reads such a page, not rendered with
// the values of other rows: by the spatial reader, by the footer reader, and
// when a tile already decoded is styled by the attribute.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { GeoParquetReader, openReader, renderTile } from '../dist/index.js'
import { workerClient } from './worker-client.mjs'

const [path, casesPath] = process.argv.slice(2)
if (!casesPath) throw new Error('leading-attributes.test.mjs needs a file and cases')
const { tiles, attributes } = JSON.parse(await readFile(casesPath, 'utf8'))
const data = await readFile(path)
const memory = () => ({
  byteLength: data.length,
  slice: async (a, b = data.length) => Uint8Array.from(data.subarray(a, b)).buffer,
})
const client = await workerClient(path, 1)
const readers = {
  spatial: await openReader(memory()),
  footer: await GeoParquetReader.open(memory()),
}
const past = (name) => new RegExp(`${name} holds a value past the rows its page shows`)

const refused = Object.fromEntries(
  [...Object.keys(readers), 'restyle'].map((kind) => [
    kind,
    Object.fromEntries(attributes.map((name) => [name, 0])),
  ]),
)
let rendered = 0
for (const [z, x, y] of tiles)
  for (const name of attributes) {
    const outcomes = {}
    for (const [kind, reader] of Object.entries(readers)) {
      if (z > reader.finestZoom) continue
      try {
        await renderTile(reader, z, x, y, { layer: 'f', properties: [name] })
        outcomes[kind] = 'rendered'
        rendered++
      } catch (error) {
        assert.match(error.message, past(name), `${kind} z${z}/${x}/${y}`)
        outcomes[kind] = 'refused'
        refused[kind][name]++
      }
    }
    // a tile decoded for its geometry, then styled by the attribute
    const tile = (properties) =>
      client.call({ type: 'tile', config: { url: 'file', layer: 'f', properties }, z, x, y })
    await tile([])
    try {
      await tile([name])
      outcomes.restyle = 'rendered'
    } catch (error) {
      assert.match(error.message, past(name), `restyle z${z}/${x}/${y}`)
      outcomes.restyle = 'refused'
      refused.restyle[name]++
    }
    if ('spatial' in outcomes)
      assert.equal(outcomes.restyle, outcomes.spatial, `restyle as fresh, z${z}/${x}/${y} ${name}`)
  }
for (const [kind, counts] of Object.entries(refused))
  for (const [name, count] of Object.entries(counts))
    assert.ok(count > 0, `${kind} refuses a tile over a misplaced ${name} page`)
await client.close()
console.log(`leading attributes: ${JSON.stringify(refused)} refused, ${rendered} rendered`)
