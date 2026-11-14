// node test/many-leaves.test.mjs <file> <cases.json>
// A tile reaching more directory leaves than the reader keeps still reads an
// attribute: the leaves it reaches first are evicted and loaded again.
// The cases give two attributes, each one's value at every row, and
// the byte offset of each leaf of the LOD serving zoom 0 with a tile under
// it reaching no other leaf. Whether the reader first reads that zoom 0
// tile with geometry only or styled by the first attribute, a style by the
// second then loads its leaves beside every leaf the reader holds, so a
// tile under any of them takes at most one request round, none when its
// overview pages hold no value of the attribute, and every styled tile
// holds the attribute's values.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { GeoParquetReader, openReader, renderTile } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!casesPath) {
  console.log('many-leaves test skipped: no fixture given')
  process.exit(0)
}
const data = await readFile(path)
const { attributes, values, held: leaves } = JSON.parse(await readFile(casesPath, 'utf8'))

/** A reader over `data` and its reads: the offsets requested, and the
 * deepest request round since `reset()`, a request's round being one more
 * than the deepest completed when it was issued. */
const counted = async () => {
  const net = { offsets: new Set(), rounds: 0, done: 0 }
  const reader = await openReader({
    byteLength: data.length,
    async slice(a, b = data.length) {
      const round = net.done + 1
      net.offsets.add(a)
      net.rounds = Math.max(net.rounds, round)
      await new Promise((resolve) => setTimeout(resolve, 1))
      net.done = Math.max(net.done, round)
      return Uint8Array.from(data.subarray(a, b)).buffer
    },
  })
  return { reader, net, reset: () => Object.assign(net, { rounds: 0, done: 0 }) }
}

const render = async (reader, [z, x, y], properties) =>
  new VectorTile(
    new PbfReader(new Uint8Array(await renderTile(reader, z, x, y, { layer: 'f', properties }))),
  ).layers.f

/** The number of features of `layer` holding a value of `attribute`, after
 * checking each one's value against the row's. */
const checked = (layer, attribute, tile) => {
  let valued = 0
  for (let i = 0; i < (layer?.length ?? 0); i++) {
    const feature = layer.feature(i)
    assert.equal(
      feature.properties[attribute],
      values[attribute][feature.id] ?? undefined,
      `${tile.join('/')} row ${feature.id} ${attribute}`,
    )
    if (values[attribute][feature.id] !== null) valued++
  }
  return valued
}

/** The rows of `layer`'s features, in order. */
const rows = (layer) => {
  const found = []
  for (let i = 0; i < (layer?.length ?? 0); i++) found.push(layer.feature(i).id)
  return found.sort((a, b) => a - b)
}
// the same tiles read through the footer, with no spatial directory
const footer = await GeoParquetReader.open({
  byteLength: data.length,
  slice: async (a, b = data.length) => Uint8Array.from(data.subarray(a, b)).buffer,
})

const [first, second] = attributes
for (const lead of [[], [first]]) {
  const { reader, net, reset } = await counted()
  const opened = net.offsets.size
  const top = await render(reader, [0, 0, 0], lead)
  assert.ok(top.length > 0, 'the zoom 0 tile holds features')
  assert.ok(net.offsets.size - opened > 300, `the tile reaches many leaves (${net.offsets.size})`)
  if (lead.length) assert.ok(checked(top, first, [0, 0, 0]) > 0, `features hold ${first}`)
  // the leaves the reader holds, which its directory cache bounds
  const held = leaves.filter(([offset]) => reader.directories.has(offset))
  assert.ok(held.length > 100 && held.length < leaves.length, `${held.length} leaves held`)
  assert.ok(
    held.every(([offset]) => reader.directories.get(offset).place),
    'every leaf held knows where its slots lie',
  )
  await render(reader, held[0][1], [second])
  let valued = 0,
    fetching = 0
  for (const [, tile] of held.slice(1)) {
    reset()
    const layer = await render(reader, tile, [second])
    assert.ok(net.rounds <= 1, `${tile.join('/')} under a held leaf`)
    assert.ok(layer?.length, `${tile.join('/')} holds features`)
    assert.deepEqual(
      rows(layer),
      rows(await render(footer, tile, [second])),
      `${tile.join('/')} holds the features the footer reader finds`,
    )
    fetching += net.rounds
    valued += checked(layer, second, tile)
  }
  assert.ok(valued > 0, `tiles under held leaves hold ${second}`)
  assert.ok(fetching > 0, `tiles under held leaves fetch ${second} pages`)
  const styled = await render(reader, [0, 0, 0], [second])
  assert.equal(styled.length, top.length)
  checked(styled, second, [0, 0, 0])
  console.log(
    `many leaves, read ${lead.length ? `styled by ${first}` : 'with geometry only'} first: ` +
      `${top.length} features, then a tile under each of ${held.length - 1} held leaves ` +
      `styled by ${second} in at most one round`,
  )
}
