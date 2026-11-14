// node test/attribute-rounds.test.mjs <file> <cases.json>
// A tile styled by an attribute takes as many request rounds as the
// same tile with geometry only, cold and next on the same reader, and holds
// the attribute's values; a reader styling by no attribute requests
// no byte of an attribute slot. Through the default `coalescing`, a
// geometry-only view under one level-one node fetches no slot byte and takes
// the requests and bytes of the same source written without
// attributes, and a view over the leaves of two level-one nodes, both held,
// takes one more request or fetches the slot block between them as filler. The cases
// give the attribute, pairs of tiles read one after the other, the byte
// ranges of every slot block, the attribute's value at every row, a file
// without attributes whose trees branch, the view under one
// level-one node, and the view over two with, per file of the same source
// and branching, its slot blocks, the block between the two nodes' leaves
// and which cost it takes. The first of those files holds every
// attribute, and the view under one level-one node reads from it.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { coalescing, openReader, renderTile } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!casesPath) {
  console.log('attribute-rounds test skipped: no fixture given')
  process.exit(0)
}
const data = await readFile(path)
const { attribute, pairs, slots, values, plain, within, across } = JSON.parse(
  await readFile(casesPath, 'utf8'),
)

/** A byte source over `data` whose requests each wait `latency` ms. A
 * request's round is one more than the deepest completed when it was
 * issued, so the deepest round is the number of rounds since `reset()`. */
const counting = (latency = 5, bytes = data) => {
  const net = { requests: 0, rounds: 0, done: 0, ranges: [] }
  return {
    net,
    reset() {
      Object.assign(net, { requests: 0, rounds: 0, done: 0, ranges: [] })
    },
    source: {
      byteLength: bytes.length,
      async slice(a, b = bytes.length) {
        const round = net.done + 1
        net.requests++
        net.rounds = Math.max(net.rounds, round)
        net.ranges.push([a, b])
        await new Promise((resolve) => setTimeout(resolve, latency))
        net.done = Math.max(net.done, round)
        return Uint8Array.from(bytes.subarray(a, b)).buffer
      },
    },
  }
}

const render = async (reader, [z, x, y], properties) => {
  const buffer = await renderTile(reader, z, x, y, { layer: 'f', properties })
  return new VectorTile(new PbfReader(new Uint8Array(buffer))).layers.f
}

/** Rounds and requests of each tile of `tiles`, read in turn by one fresh
 * reader styling by `properties`. */
const measure = async (tiles, properties) => {
  const counter = counting()
  const reader = await openReader(coalescing(counter.source, { windowMs: 1 }))
  const out = []
  for (const tile of tiles) {
    counter.reset()
    const layer = await render(reader, tile, properties)
    out.push({ rounds: counter.net.rounds, requests: counter.net.requests, layer })
  }
  return out
}

let valued = 0
for (const tiles of pairs) {
  const plain = await measure(tiles, [])
  const styled = await measure(tiles, [attribute])
  console.log(
    `${tiles.map((t) => t.join('/')).join(' then ')}: geometry ` +
      plain.map((m) => `${m.rounds} rounds ${m.requests} requests`).join(', ') +
      `; styled by ${attribute} ` +
      styled.map((m) => `${m.rounds} rounds ${m.requests} requests`).join(', '),
  )
  for (let i = 0; i < tiles.length; i++) {
    assert.ok(plain[i].rounds > 0)
    assert.equal(styled[i].rounds, plain[i].rounds, `${tiles[i].join('/')} rounds`)
    const layer = styled[i].layer
    assert.ok(layer?.length, `${tiles[i].join('/')} holds features`)
    for (let f = 0; f < layer.length; f++) {
      const feature = layer.feature(f)
      assert.equal(
        feature.properties[attribute],
        values[feature.id] ?? undefined,
        `${tiles[i].join('/')} row ${feature.id}`,
      )
      if (values[feature.id] !== null) valued++
    }
  }
}
assert.ok(valued > 0, `features hold ${attribute}`)

/** The bytes of `ranges` inside the disjoint `blocks`. */
const slotBytes = (ranges, blocks) =>
  ranges.reduce(
    (sum, [a, b]) =>
      sum + blocks.reduce((n, [s, e]) => n + Math.max(0, Math.min(b, e) - Math.max(a, s)), 0),
    0,
  )

// a reader styling by no attribute requests no slot byte
const counter = counting(0)
const reader = await openReader(counter.source)
for (const tile of pairs.flat()) await render(reader, tile, [])
assert.ok(counter.net.ranges.length > pairs.length)
assert.equal(slotBytes(counter.net.ranges, slots), 0, 'geometry-only requests read a slot')

/** Requests of a geometry-only read of `tiles` at once from the file at
 * `path` through the default coalescing, by a reader that has read `warm`,
 * the bytes of those meeting a directory node of `nodes` that lie in no
 * node, their ranges, and the tiles' features. */
const view = async ({ path, nodes }, tiles, warm = []) => {
  const counter = counting(2, await readFile(path))
  const reader = await openReader(coalescing(counter.source))
  for (const tile of warm) await render(reader, tile, [])
  counter.reset()
  const layers = await Promise.all(tiles.map((tile) => render(reader, tile, [])))
  const { requests, ranges } = counter.net
  const reading = ranges.filter((range) => slotBytes([range], nodes))
  return {
    requests,
    nodes: reading.length,
    filler: reading.reduce((sum, [a, b]) => sum + b - a, 0) - slotBytes(reading, nodes),
    ranges,
    features: layers.map((layer) => layer?.length ?? 0),
  }
}

const [branched] = across.files
const one = await view(branched, within)
const oneWithout = await view(plain, within)
assert.ok(one.features.every(Boolean), 'the view under one level-one node holds features')
assert.deepEqual(one.features, oneWithout.features)
assert.equal(slotBytes(one.ranges, branched.slots), 0, 'a view under one node reads a slot')
assert.ok(one.nodes > 0, 'the view under one level-one node reads directory nodes')
assert.deepEqual(
  [one.requests, one.nodes, one.filler],
  [oneWithout.requests, oneWithout.nodes, oneWithout.filler],
)
console.log(
  `attribute rounds: ${pairs.length} tile pairs, ${valued} values of ${attribute}, ` +
    `${counter.net.ranges.length} geometry-only requests and a coalesced view of ` +
    `${within.length} tiles under one level-one node request no slot byte`,
)

// the level-one nodes over both leaves held, the leaves load in one round
const twoWithout = await view(plain, across.tiles, across.warm)
assert.ok(twoWithout.features.every(Boolean), 'the view over two level-one nodes holds features')
for (const file of across.files) {
  const { slots: blocks, block, cost } = file
  const two = await view(file, across.tiles, across.warm)
  assert.deepEqual(two.features, twoWithout.features)
  const fetched = slotBytes(two.ranges, blocks)
  assert.equal(fetched, slotBytes(two.ranges, [block]), 'a slot byte outside the block between')
  assert.equal(two.filler, twoWithout.filler + fetched)
  if (cost === 'request') {
    assert.equal(fetched, 0)
    assert.equal(two.requests, twoWithout.requests + 1)
    assert.equal(two.nodes, twoWithout.nodes + 1)
  } else {
    assert.equal(fetched, block[1] - block[0])
    assert.equal(two.requests, twoWithout.requests)
    assert.equal(two.nodes, twoWithout.nodes)
  }
  console.log(
    `a view over two level-one nodes, ${cost}: ${two.requests} requests against ` +
      `${twoWithout.requests}, ${fetched} slot bytes`,
  )
}
