// node test/coalesced-leaves.test.mjs <file> <same source without attributes> <cases.json>
// A reader styling by no attribute, its reads merged by the default
// `coalescing`, makes as many requests and fetches as many bytes for
// concurrent tiles from a file with attributes as from the same
// source written without them, reads directory leaves and LOD pages of
// both, and renders the same features. The cases give the tiles and, per
// file, the byte ranges of its directory leaves and of its LOD pages.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { coalescing, openReader, renderTile } from '../dist/index.js'

const [withPath, withoutPath, casesPath] = process.argv.slice(2)
if (!casesPath) {
  console.log('coalesced-leaves test skipped: no fixture given')
  process.exit(0)
}
const { tiles, files } = JSON.parse(await readFile(casesPath, 'utf8'))

/** Whether any of the sorted, disjoint `ranges` meets [a, b). */
const meets = (ranges, a, b) => {
  let lo = 0,
    hi = ranges.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (ranges[mid][1] <= a) lo = mid + 1
    else hi = mid
  }
  return lo < ranges.length && ranges[lo][0] < b
}

/** Requests and bytes of a cold geometry-only read of every tile at once,
 * the features of each tile, and how many requests meet a directory leaf
 * and an LOD page of the file. */
const measure = async (path, { leaves, pages }) => {
  const data = await readFile(path)
  const net = { requests: 0, bytes: 0 }
  const ranges = []
  const reader = await openReader(
    coalescing({
      byteLength: data.length,
      async slice(a, b = data.length) {
        net.requests++
        net.bytes += b - a
        ranges.push([a, b])
        await new Promise((resolve) => setTimeout(resolve, 2))
        return Uint8Array.from(data.subarray(a, b)).buffer
      },
    }),
  )
  const features = await Promise.all(
    tiles.map(async ([z, x, y]) => {
      const buffer = await renderTile(reader, z, x, y, { layer: 'f', properties: [] })
      return new VectorTile(new PbfReader(new Uint8Array(buffer))).layers.f?.length ?? 0
    }),
  )
  return {
    net,
    features,
    leafReads: ranges.filter(([a, b]) => meets(leaves, a, b)).length,
    pageReads: ranges.filter(([a, b]) => meets(pages, a, b)).length,
  }
}

const styled = await measure(withPath, files[0])
const plain = await measure(withoutPath, files[1])
console.log(
  `coalesced leaves: ${tiles.length} tiles, with attributes ${styled.net.requests} ` +
    `requests ${styled.net.bytes} bytes, without ${plain.net.requests} requests ` +
    `${plain.net.bytes} bytes`,
)
for (const measured of [styled, plain]) {
  assert.ok(measured.leafReads > 0, 'the tiles read directory leaves')
  assert.ok(measured.pageReads > 0, 'the tiles read LOD pages')
  assert.ok(
    measured.features.filter(Boolean).length > tiles.length / 2,
    `most tiles hold features (${measured.features})`,
  )
}
assert.deepEqual(styled.features, plain.features)
assert.deepEqual(styled.net, plain.net)
