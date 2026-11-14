import assert from 'node:assert/strict'
import { gzipSync } from 'node:zlib'
import { SpatialReader, hilbertIndex } from '../dist/index.js'

// One LOD at zoom 2 with depth-2 keys, so a key is a directory cell. Page
// bytes sit past the nodes and are fetched but not decoded here.
const PAGES = 60000
// Thrift metadata whose schema holds an optional binary `geometry`, and
// another with an optional binary `a` after it
const SCHEMA = 'FQIZLEgGc2NoZW1hFQIAFQwlAhgIZ2VvbWV0cnkAFgAZDAA='
const SCHEMA_A = 'FQIZPEgGc2NoZW1hFQQAFQwlAhgIZ2VvbWV0cnkAFQwlAhgBYQAWABkMAA=='
// and another with optional binaries `a` and `b`
const SCHEMA_AB = 'FQIZTEgGc2NoZW1hFQYAFQwlAhgIZ2VvbWV0cnkAFQwlAhgBYQAVDCUCGAFiABYAGQwA'
const EMPTY_TOP = [1, [], [], [], []]
/** Row directories of `geometry` and `a`, with `top` that of `a`. */
const columnsA = (top = EMPTY_TOP) => ({
  columns: ['geometry', 'a'],
  codecs: ['UNCOMPRESSED', 'UNCOMPRESSED'],
  schema: SCHEMA_A,
  directories: [EMPTY_TOP, top],
})
const reader = (bytes, top, exact = EMPTY_TOP, reads = [], root = {}) =>
  new SpatialReader(
    {
      byteLength: bytes.length,
      slice: async (a, b) => {
        reads.push([a, b])
        return bytes.slice(a, b).buffer
      },
    },
    {
      version: 9,
      file_bytes: bytes.length,
      primary: 'geometry',
      codecs: { f2: 'UNCOMPRESSED' },
      lod: {
        column: 'lod',
        key: { column: 'key', depth: 2 },
        zooms: [2],
        attributes: [],
        lods: { f2: { zooms: [2], geometry_type: 'Point', scale: [1, 1], offset: [0, 0] } },
      },
      directories: { f2: top },
      primary_index: { directory: exact },
      column_index: {
        columns: ['geometry'],
        codecs: ['UNCOMPRESSED'],
        schema: SCHEMA,
        directories: [EMPTY_TOP],
      },
      ...root,
    },
  )
const tile = (r, z, h) => {
  for (let x = 0; x < 2 ** z; x++)
    for (let y = 0; y < 2 ** z; y++)
      if (hilbertIndex(z, x, y) === h) return r.prepareTile(r.lods.f2, z, x, y)
}

/** Gzip JSON nodes stored back to back; `add` returns [offset, length]. */
const store = () => {
  const bytes = new Uint8Array(1 << 16)
  let size = 0
  const put = (packed) => {
    bytes.set(packed, size)
    size += packed.length
    return [size - packed.length, packed.length]
  }
  return {
    bytes,
    size: () => size,
    raw: put,
    add: (node) => put(gzipSync(JSON.stringify(node))),
    /** A level-one node in `cell` whose one entry locates itself. */
    addSelf(cell) {
      let length = 0
      for (let i = 0; i < 10; i++) {
        const packed = gzipSync(JSON.stringify([1, [cell], [0], [size], [length]]))
        if (packed.length === length) return put(packed)
        length = packed.length
      }
      throw new Error('no stable self-reference')
    },
  }
}
const delta = (values, ends = values.map(() => 0)) =>
  values.map((v, i) => v - (i ? values[i - 1] + ends[i - 1] : 0))
/** A node at `level` over one-cell children, each [cell, offset, length]. */
const branch = (level, refs) => [
  level,
  delta(refs.map((r) => r[0])),
  refs.map(() => 0),
  delta(
    refs.map((r) => r[1]),
    refs.map((r) => r[2]),
  ),
  refs.map((r) => r[2]),
]
/** A leaf of one-cell pages, each [cell, offset, length, first row, rows]. */
const leaf = (pages) => [
  ...branch(
    0,
    pages.map((p) => p.slice(0, 3)),
  ),
  delta(
    pages.map((p) => p[3]),
    pages.map((p) => p[4]),
  ),
  pages.map((p) => p[4]),
  pages.map(() => []),
  pages.map(() => 0),
]

{
  const file = store()
  const [offset, length] = file.add(
    leaf([
      [1, PAGES, 10, 0, 3],
      [5, PAGES + 10, 10, 3, 2],
    ]),
  )
  const r = reader(file.bytes, [1, [1], [4], [offset], [length]])
  const whole = await tile(r, 0, 0)
  assert.deepEqual(whole.pages, [
    [1, 1, PAGES, 10, 0, 3, [1, 0], 3],
    [5, 5, PAGES + 10, 10, 3, 2, [5, 0], 2],
  ])
  assert.equal(whole.hostRow, 0)
  assert.deepEqual(
    (await tile(r, 2, 9)).pages.map((p) => p[4]),
    [3],
    'a tile past every page reads the last page for copies',
  )
}

{
  const file = store()
  const [offset, length] = file.add(
    leaf([
      [1, PAGES, 10, 0, 3],
      [5, PAGES + 10, 0, 3, 2],
    ]),
  )
  const r = reader(file.bytes, [1, [1], [4], [offset], [length]])
  await assert.rejects(tile(r, 0, 0), /malformed/, 'a page of length 0 in an LOD leaf')
}

{
  const file = store()
  const [offset, length] = file.addSelf(3)
  const r = reader(file.bytes, [2, [3], [0], [offset], [length]])
  await assert.rejects(
    tile(r, 0, 0),
    /disagrees with its parent entry/,
    'a node locating itself is refused at the level it is located for, not descended forever',
  )
}

{
  // Two copies of every node, each locating both copies one level down.
  const file = store()
  const pair = (node) => [file.add(node), file.add(node)]
  let below = pair(leaf([[3, PAGES, 10, 0, 1]]))
  for (let level = 1; level < 24; level++)
    below = pair(
      branch(
        level,
        below.map(([offset, length]) => [3, offset, length]),
      ),
    )
  const r = reader(
    file.bytes,
    branch(
      24,
      below.map(([offset, length]) => [3, offset, length]),
    ),
  )
  await assert.rejects(tile(r, 0, 0), /referenced twice/, 'shared nodes are read once per tile')
}

{
  const file = store()
  const [offset, length] = file.add(leaf([[5, PAGES, 10, 0, 1]]))
  for (const cell of [4, 5]) {
    // parent cells 4..5 or 5..6 over a leaf in cell 5
    const misplaced = reader(file.bytes, [1, [cell], [1], [offset], [length]])
    await assert.rejects(tile(misplaced, 0, 0), /disagrees with its parent entry/, 'cell bounds')
  }
  // Two parents locate the same leaf with different lengths. A tile before
  // the leaf reaches it through the first parent; one after, through the last.
  const parents = [length, length + 1].map((n) => file.add(branch(1, [[5, offset, n]])))
  const r = reader(
    file.bytes,
    branch(
      2,
      parents.map(([o, n]) => [5, o, n]),
    ),
  )
  assert.equal((await tile(r, 2, 3)).pages.length, 1)
  await assert.rejects(tile(r, 2, 9), /disagrees with its parent entry/, 'node length')
}

{
  const file = store()
  // pages in cells 2..6 and 4..8 overlap
  const [offset, length] = file.add([
    0,
    [2, 2],
    [4, 4],
    [PAGES, 0],
    [10, 10],
    [0, 0],
    [1, 1],
    [[], []],
    [0, 0],
  ])
  const r = reader(file.bytes, [1, [2], [6], [offset], [length]])
  await assert.rejects(tile(r, 0, 0), /out of key order/, 'overlapping entries in a leaf')
  assert.throws(
    () => reader(file.bytes, [1, [5, -2], [0, 0], [0, 10], [10, 10]]),
    /out of key order/,
    'a decreasing minimum in a top node',
  )
  for (const [node, error] of [
    [[33, [], [], [], []], /malformed/],
    [[1, [0.5], [0], [0], [1]], /malformed/],
    [[1, ['1'], [0], [0], [1]], /malformed/],
    [[1, [1], [-1], [0], [1]], /malformed/],
    [[0, [], [], [], [], [], [], [], []], /above its leaves/],
  ])
    assert.throws(() => reader(file.bytes, node), error)
}
console.log('directory lookup: self-references, shared nodes, order, bounds, lengths, levels')

// The primary directory: boxes in cells of 2^-12 degrees over pages of WKB.
const cell = (degrees, origin) => Math.floor((degrees + origin) * 4096)
const at = (x, y) => [cell(x, 180), cell(y, 90), cell(x, 180), cell(y, 90)]
const join = (...boxes) => [
  Math.min(...boxes.map((b) => b[0])),
  Math.min(...boxes.map((b) => b[1])),
  Math.max(...boxes.map((b) => b[2])),
  Math.max(...boxes.map((b) => b[3])),
]
const point = (x, y) => {
  const view = new DataView(new ArrayBuffer(21))
  view.setUint8(0, 1)
  view.setUint32(1, 1, true)
  view.setFloat64(5, x, true)
  view.setFloat64(13, y, true)
  return new Uint8Array(view.buffer)
}
const lineString = (points) => {
  const view = new DataView(new ArrayBuffer(9 + 16 * points.length))
  view.setUint8(0, 1)
  view.setUint32(1, 2, true)
  view.setUint32(5, points.length, true)
  points.forEach(([x, y], i) => {
    view.setFloat64(9 + 16 * i, x, true)
    view.setFloat64(17 + 16 * i, y, true)
  })
  return new Uint8Array(view.buffer)
}
const varint = (n) => {
  const out = []
  for (; n >= 128; n = Math.floor(n / 128)) out.push((n % 128) | 128)
  return [...out, n]
}
/** An uncompressed v1 data page of optional binary values, null for a null,
 * PLAIN unless another encoding is given. */
const binaryPage = (values, encoding = 0) => {
  // definition levels as one bit-packed run of groups of eight
  const levels = [...varint(Math.ceil(values.length / 8) * 2 + 1)]
  for (let i = 0; i < values.length; i += 8)
    levels.push(values.slice(i, i + 8).reduce((bits, v, j) => bits | ((v ? 1 : 0) << j), 0))
  const body = [...new Uint8Array(new Uint32Array([levels.length]).buffer), ...levels]
  for (const v of values.filter(Boolean))
    body.push(...new Uint8Array(new Uint32Array([v.length]).buffer), ...v)
  // Thrift i32 fields one id apart: type DATA_PAGE and both sizes, then a
  // nested header of the value count, PLAIN values and RLE levels
  const i32 = (n) => [0x15, ...varint(n * 2)]
  const page = [values.length, encoding, 3, 3].flatMap(i32)
  const header = [...[0, body.length, body.length].flatMap(i32), 0x2c, ...page, 0, 0]
  return Uint8Array.from([...header, ...body])
}
/** The same values as an uncompressed v2 data page, whose header's value,
 * null and row counts may be given. */
const binaryPageV2 = (
  values,
  { count = values.length, nulls = values.filter((v) => !v).length, rows = values.length } = {},
) => {
  // v2 definition levels have no length prefix: the header gives it
  const levels = [...varint(Math.ceil(values.length / 8) * 2 + 1)]
  for (let i = 0; i < values.length; i += 8)
    levels.push(values.slice(i, i + 8).reduce((bits, v, j) => bits | ((v ? 1 : 0) << j), 0))
  const body = [...levels]
  for (const v of values.filter(Boolean))
    body.push(...new Uint8Array(new Uint32Array([v.length]).buffer), ...v)
  const i32 = (n) => [0x15, ...varint(n < 0 ? -2 * n - 1 : 2 * n)]
  // type DATA_PAGE_V2 and both sizes, then field 8: the counts, PLAIN and
  // the levels' byte lengths
  const page = [count, nulls, rows, 0, levels.length, 0].flatMap(i32)
  const header = [...[3, body.length, body.length].flatMap(i32), 0x5c, ...page, 0, 0]
  return Uint8Array.from([...header, ...body])
}
/** A primary leaf over pages {offset, length, first, runs: [{box, count}]}:
 * an entry per run, each but a page's first of length 0. */
const primaryLeaf = (pages) => {
  const runs = pages.flatMap((page) => {
    let first = page.first
    return page.runs.map((run, i) => {
      const entry = { ...run, first, offset: page.offset, length: i ? 0 : page.length }
      first += run.count
      return entry
    })
  })
  const ends = runs.map((r) => (r.length ? r.offset + r.length : null))
  let end = 0
  const gaps = runs.map((r, i) => {
    const gap = r.length ? r.offset - end : 0
    end = ends[i] ?? end
    return gap
  })
  return [
    0,
    delta(runs.map((r) => r.box[0])),
    delta(runs.map((r) => r.box[1])),
    runs.map((r) => r.box[2] - r.box[0]),
    runs.map((r) => r.box[3] - r.box[1]),
    gaps,
    runs.map((r) => r.length),
    delta(
      runs.map((r) => r.first),
      runs.map((r) => r.count),
    ),
    runs.map((r) => r.count),
  ]
}
/** A level-one node over leaves {low, high, offset, length} spanning cells low..high. */
const cellBranch = (level, children) => [
  level,
  delta(children.map((c) => c.low)),
  children.map((c) => c.high - c.low),
  delta(
    children.map((c) => c.offset),
    children.map((c) => c.length),
  ),
  children.map((c) => c.length),
]

{
  const file = store()
  const put = (bytes) => {
    const raw = binaryPage(bytes)
    const [offset, length] = [file.size(), raw.length]
    file.raw(raw)
    return { offset, length }
  }
  const pageA = {
    ...put([point(10, 10), point(10.5, 10.5)]),
    first: 0,
    runs: [{ box: join(at(10, 10), at(10.5, 10.5)), count: 2 }],
  }
  // page B's rows lie in two distant places, a run for each
  const pageB = {
    ...put([point(50, 50), null, point(-20, -20)]),
    first: 2,
    runs: [
      { box: at(50, 50), count: 2 },
      { box: at(-20, -20), count: 1 },
    ],
  }
  const leaf = primaryLeaf([pageA, pageB])
  // a top node over one leaf of every cell, located as [offset, length]
  const whole = ([offset, length]) => cellBranch(1, [{ low: 0, high: 15, offset, length }])
  const reads = []
  const r = reader(file.bytes, [1, [], [], [], []], whole(file.add(leaf)), reads)
  const rows = async (box) => (await r.readPrimary(box)).rows
  assert.deepEqual(await rows([9, 9, 11, 11]), [0, 1])
  assert.deepEqual(await rows([49, 49, 51, 51]), [2])
  assert.deepEqual(await rows([-180, -85, 180, 85]), [0, 1, 2, 4])
  reads.length = 0
  assert.deepEqual(await rows([0, 0, 1, 1]), [], 'between runs')
  assert.deepEqual(reads, [], 'a query between a page’s runs leaves the page unread')
  assert.deepEqual(await rows([100, 0, 101, 1]), [])

  // two leaves: cells 0..7 hold tile 1/2 at zoom 2 (cell 7) and cells 8..15
  // tile 2/1 (cell 13), each listing the pages whose runs reach it
  const west = file.add(primaryLeaf([pageB]))
  const east = file.add(primaryLeaf([pageA, pageB]))
  const top = cellBranch(1, [
    { low: 0, high: 7, offset: west[0], length: west[1] },
    { low: 8, high: 15, offset: east[0], length: east[1] },
  ])
  const routed = []
  const two = reader(file.bytes, [1, [], [], [], []], top, routed)
  assert.deepEqual((await two.readPrimary([9, 9, 11, 11])).rows, [0, 1])
  assert.deepEqual(routed[0], [east[0], east[0] + east[1]], 'a query reads its tile’s leaf')
  assert.ok(!routed.some(([a]) => a === west[0]), 'and no other')
  routed.length = 0
  assert.deepEqual((await two.readPrimary([-21, -21, -19, -19])).rows, [4])
  assert.ok(!routed.some(([a]) => a === east[0]), 'the other leaf alone serves its tile')
  assert.deepEqual((await two.readPrimary([-180, -85, 180, 85])).rows, [0, 1, 2, 4])

  // a branch's cells must be its children's
  const ok = reader(file.bytes, [1, [], [], [], []], whole(east))
  assert.deepEqual((await ok.readPrimary([9, 9, 11, 11])).rows, [0, 1])
  const node = file.add(cellBranch(1, [{ low: 8, high: 15, offset: east[0], length: east[1] }]))
  const above = (low) => cellBranch(2, [{ low, high: 15, offset: node[0], length: node[1] }])
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], above(0)).readPrimary([9, 9, 11, 11]),
    /disagrees with its parent entry/,
    'a parent spanning cells its child does not',
  )
  assert.deepEqual(
    (await reader(file.bytes, [1, [], [], [], []], above(8)).readPrimary([9, 9, 11, 11])).rows,
    [0, 1],
  )
  await assert.rejects(
    reader(
      file.bytes,
      [1, [], [], [], []],
      cellBranch(2, [{ low: 8, high: 15, offset: east[0], length: east[1] }]),
    ).readPrimary([9, 9, 11, 11]),
    /malformed spatial directory node/,
    'a leaf located as a level-one node',
  )

  for (const [bad, why] of [
    [leaf.slice(0, 8), 'a leaf without counts'],
    [leaf.with(3, [-1, 0, 0]), 'a negative width'],
    [leaf.with(6, [0, ...leaf[6].slice(1)]), 'a first entry in the previous entry’s page'],
    [leaf.with(5, [0, 1, 0]).with(6, [leaf[6][0], 0, 0]), 'a later page of length 0'],
    [leaf.with(5, [...leaf[5].slice(0, 2), 1]), 'a later run with a byte gap'],
    [leaf.with(7, [...leaf[7].slice(0, 2), 1]), 'a later run with a row gap'],
    [[...leaf, [[[], [], [], []]]], 'a column past the counts'],
    [leaf.with(1, [0.5, 0, 0]), 'a fractional cell'],
  ])
    await assert.rejects(
      reader(file.bytes, [1, [], [], [], []], whole(file.add(bad))).readPrimary([-9, -9, 9, 9]),
      /malformed/,
      why,
    )
  for (const [top, error] of [
    [leaf, /malformed spatial directory node/],
    [[0, [], [], [], [], [], [], [], []], /must start above its leaves/],
  ])
    assert.throws(() => reader(file.bytes, [1, [], [], [], []], top), error, 'a leaf as the top')
}

{
  const file = store()
  // a level-one node whose one entry locates itself, as a leaf
  let length = 0,
    self
  for (let i = 0; i < 10; i++) {
    const offset = file.size()
    const packed = gzipSync(JSON.stringify(cellBranch(1, [{ low: 0, high: 15, offset, length }])))
    if (packed.length === length) {
      file.raw(packed)
      self = [offset, length]
      break
    }
    length = packed.length
  }
  const top = cellBranch(2, [{ low: 0, high: 15, offset: self[0], length: self[1] }])
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], top).readPrimary([4, 4, 6, 6]),
    /malformed primary directory leaf/,
    'a node locating itself is refused at the level it is located for',
  )
}

{
  // two copies of every node, each locating both copies one level down
  const file = store()
  const pair = (node) => [file.add(node), file.add(node)]
  let below = pair(
    primaryLeaf([{ offset: PAGES, length: 10, first: 0, runs: [{ box: at(5, 5), count: 1 }] }]),
  )
  // every entry spans one cell, so a node's two entries can locate both copies
  const refs = (nodes) => nodes.map(([offset, length]) => ({ low: 5, high: 5, offset, length }))
  for (let level = 1; level < 24; level++) below = pair(cellBranch(level, refs(below)))
  const top = cellBranch(24, refs(below))
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], top).readPrimary([-180, -85, 180, 85]),
    /referenced twice/,
    'shared nodes are read once per query',
  )
}
console.log('primary directory: routing, runs, cells, levels, self-references, shared nodes')

{
  // A byte range past the end of the file is refused before any request: an
  // overview node or page, a primary node or geometry page, or a row
  // directory node or the page it locates.
  const file = store()
  const past = file.bytes.length + 1024
  const inside = file.raw(binaryPage([point(5, 5)]))
  const page = (offset, length) => ({
    offset,
    length,
    first: 0,
    runs: [{ box: at(5, 5), count: 1 }],
  })
  /** A top node over one leaf of `pages`. */
  const exact = (pages) => {
    const [offset, length] = file.add(primaryLeaf(pages))
    return cellBranch(1, [{ low: 0, high: 15, offset, length }])
  }
  const lodLeaf = file.add(leaf([[1, past, 10, 0, 3]]))
  const reads = []
  const rows = (r, attributes = []) => r.readPrimary([4, 4, 6, 6], attributes)
  for (const [what, r, read] of [
    ['an overview node', reader(file.bytes, [1, [1], [0], [past], [99]], EMPTY_TOP, reads)],
    [
      'an overview page',
      reader(file.bytes, [1, [1], [0], ...lodLeaf.map((v) => [v])], EMPTY_TOP, reads),
    ],
    [
      'a primary node',
      reader(
        file.bytes,
        EMPTY_TOP,
        cellBranch(1, [{ low: 0, high: 15, offset: past, length: 99 }]),
        reads,
      ),
      rows,
    ],
    ['a geometry page', reader(file.bytes, EMPTY_TOP, exact([page(past, 10)]), reads), rows],
    [
      'a row directory node',
      reader(file.bytes, EMPTY_TOP, EMPTY_TOP, reads, {
        column_index: columnsA([1, [0], [0], [past], [99]]),
      }),
      (r) => r.readRow(0, ['a']),
    ],
    [
      'a page a row directory locates',
      reader(file.bytes, EMPTY_TOP, EMPTY_TOP, reads, {
        column_index: columnsA([
          1,
          [0],
          [0],
          ...file.add([0, [past], [10], [0], [1]]).map((v) => [v]),
        ]),
      }),
      (r) => r.readRow(0, ['a']),
    ],
  ])
    await assert.rejects((read ?? ((r) => tile(r, 0, 0)))(r), /outside the file/, what)
  assert.ok(
    reads.every(([, end]) => end <= file.bytes.length),
    'no request reaches past the file',
  )
}
console.log('byte ranges: nodes and pages past the file are refused unread')

{
  // Before any row is visited, a page's header must count the rows its runs,
  // or the leaf for an attribute page, give it, and not be dictionary encoded.
  const file = store()
  const plain = file.raw(binaryPage([point(5, 5), point(5, 5)]))
  const named = file.raw(binaryPage([new Uint8Array([97]), new Uint8Array([98])]))
  // the attribute leaf of `a` in the slot beside the primary leaf
  const size = 128
  const read = (count, attributeRows, geometry = plain) => {
    const page = { offset: geometry[0], length: geometry[1], first: 0 }
    const [offset, length] = file.add(primaryLeaf([{ ...page, runs: [{ box: at(5, 5), count }] }]))
    const held = gzipSync(JSON.stringify([0, [named[0]], [named[1]], [0], [attributeRows]]))
    const slot = new Uint8Array(size)
    const view = new DataView(slot.buffer)
    view.setUint32(0, held.length, true)
    view.setBigUint64(8, BigInt(offset), true)
    slot.set(held, 16)
    file.raw(slot)
    return reader(
      file.bytes,
      EMPTY_TOP,
      cellBranch(1, [{ low: 0, high: 15, offset, length }]),
      [],
      {
        lod: {
          column: 'lod',
          key: { column: 'key', depth: 2 },
          zooms: [2],
          attributes: [{ name: 'a', type: 'string' }],
          attribute_columns: { column: 'lod_attributes', lods: ['f2'] },
          lods: { f2: { zooms: [2], geometry_type: 'Point', scale: [1, 1], offset: [0, 0] } },
        },
        attribute_codec: 'UNCOMPRESSED',
        attribute_slots: [size, size],
        column_index: columnsA(),
      },
    ).readPrimary([4, 4, 6, 6], ['a'])
  }
  const exact = await read(2, 2)
  assert.deepEqual(exact.rows, [0, 1])
  assert.deepEqual(
    exact.columns.a.map((v) => [...v]),
    [[97], [98]],
  )
  for (const [count, attributeRows, why] of [
    [500_000_000, 2, 'a run far past its page’s rows'],
    [1, 2, 'a run short of its page’s rows'],
    [2, 3, 'an attribute page past its header’s rows'],
  ])
    await assert.rejects(read(count, attributeRows), /page header disagrees/, why)
  const dictionary = file.raw(binaryPage([point(5, 5), point(5, 5)], 8))
  await assert.rejects(read(2, 2, dictionary), /page header disagrees/, 'a dictionary page')

  // A v2 header counts values, nulls and rows apart, and hyparquet sizes a
  // page's arrays by its values: those of a flat column's page are its rows.
  const rows = (geometry) => {
    const leaf = primaryLeaf([
      { offset: geometry[0], length: geometry[1], first: 0, runs: [{ box: at(5, 5), count: 3 }] },
    ])
    const [offset, length] = file.add(leaf)
    const top = cellBranch(1, [{ low: 0, high: 15, offset, length }])
    return reader(file.bytes, EMPTY_TOP, top).readPrimary([4, 4, 6, 6])
  }
  const values = [point(5, 5), null, point(5, 5)]
  assert.deepEqual((await rows(file.raw(binaryPageV2(values)))).rows, [0, 2])
  for (const [counts, why] of [
    [{ count: 500_000_000 }, 'values far past its rows'],
    [{ rows: 2 }, 'rows short of the directory’s'],
    [{ count: 4, rows: 4 }, 'values and rows past the directory’s'],
    [{ nulls: -1 }, 'negative nulls'],
    [{ nulls: 4 }, 'more nulls than values'],
  ])
    await assert.rejects(rows(file.raw(binaryPageV2(values, counts))), /page header disagrees/, why)
}
console.log('page headers: counts and encoding agree with the directory before rows are read')

{
  // An indexed column's rows come from its row directory: the node a parent
  // entry locates holds the rows and level that entry gives it, and a page's
  // header the rows its leaf entry gives it.
  const file = store()
  const named = file.raw(binaryPage([new Uint8Array([97]), new Uint8Array([98])]))
  const read = (pageRows, last = 1, rows = [1, 0, 1], level = 1) => {
    const leaf = file.add([0, [named[0]], [named[1]], [0], [pageRows]])
    const top = [level, [0], [last], ...leaf.map((v) => [v])]
    const r = reader(file.bytes, EMPTY_TOP, EMPTY_TOP, [], { column_index: columnsA(top) })
    return r.readColumns(rows, ['a'])
  }
  assert.deepEqual(
    (await read(2)).a.map((v) => [...v]),
    [[98], [97], [98]],
    'values in the order of the rows asked for',
  )
  for (const [args, error, why] of [
    [[2, 2], /disagrees with its parent entry/, 'a leaf short of its entry’s rows'],
    [[3, 2], /page header disagrees/, 'a page short of its entry’s rows'],
    [[2, 1, [2]], /coverage is incomplete/, 'a row past the column'],
    [[2, 1, [0], 2], /malformed spatial directory node/, 'a leaf where a branch belongs'],
  ])
    await assert.rejects(read(...args), error, why)
  const empty = file.add([0, [named[0]], [0], [0], [2]])
  await assert.rejects(
    reader(file.bytes, EMPTY_TOP, EMPTY_TOP, [], {
      column_index: columnsA([1, [0], [1], [empty[0]], [empty[1]]]),
    }).readColumns([1], ['a']),
    /malformed/,
    'a page of length 0 in a row directory leaf',
  )
  const [offset, length] = file.add([0, [named[0]], [named[1]], [0], [2]])
  const overlapping = [1, [0, 1], [1, 1], [offset, 0], [length, length]]
  await assert.rejects(
    reader(file.bytes, EMPTY_TOP, EMPTY_TOP, [], {
      column_index: columnsA(overlapping),
    }).readColumns([1], ['a']),
    /do not follow on/,
    'entries sharing a row',
  )
  assert.throws(
    () =>
      reader(file.bytes, EMPTY_TOP, EMPTY_TOP, [], { column_index: columnsA([0, [], [], [], []]) }),
    /malformed spatial directory node/,
    'a leaf as the top',
  )
  // two one-row pages with a row between them: row 1 has no page, so no value
  const one = file.raw(binaryPage([new Uint8Array([97])]))
  const two = file.raw(binaryPage([new Uint8Array([98])]))
  const gapped = file.add([0, [one[0], two[0] - one[0] - one[1]], [one[1], two[1]], [0, 1], [1, 1]])
  await assert.rejects(
    reader(file.bytes, EMPTY_TOP, EMPTY_TOP, [], {
      column_index: columnsA([1, [0], [2], ...gapped.map((v) => [v])]),
    }).readColumns([1], ['a']),
    /pages do not follow on/,
    'a row between two pages',
  )
  const late = file.add([0, [named[0]], [named[1]], [1], [2]])
  await assert.rejects(
    reader(file.bytes, EMPTY_TOP, EMPTY_TOP, [], {
      column_index: columnsA([1, [1], [1], ...late.map((v) => [v])]),
    }).readColumns([1], ['a']),
    /entries do not follow on/,
    'a tree that starts past row 0',
  )
}
console.log('row directories: rows, levels and page headers agree before values are read')

{
  // A NaN x or y leaves that ordinate alone out of a row's bbox, as the
  // covering's bounds do: the first line reaches latitude 50 but not
  // longitude 12. A geometry with an infinite coordinate, or with no x but
  // NaN, has no finite bounds and so a null covering, and is never kept.
  const file = store()
  const geometry = file.raw(
    binaryPage([
      lineString([
        [10, 10],
        [NaN, 50],
        [11, 11],
      ]),
      lineString([
        [10.5, 40],
        [Infinity, 40],
      ]),
      lineString([
        [NaN, 40],
        [NaN, 41],
      ]),
    ]),
  )
  const page = { offset: geometry[0], length: geometry[1], first: 0 }
  const leaf = file.add(
    primaryLeaf([{ ...page, runs: [{ box: join(at(10, 10), at(11, 50)), count: 3 }] }]),
  )
  const top = cellBranch(1, [{ low: 0, high: 15, offset: leaf[0], length: leaf[1] }])
  const r = reader(file.bytes, EMPTY_TOP, top)
  assert.deepEqual((await r.readPrimary([10.4, 39.9, 10.6, 40.1])).rows, [0])
  assert.deepEqual((await r.readPrimary([-180, -85, 180, 85])).rows, [0])
  assert.deepEqual((await r.readPrimary([11.5, 30, 12, 60])).rows, [])
}
console.log('exact bboxes: NaN ordinates left out one axis at a time, no finite bounds no row')

// Attributes: the leaves under a level-one node lie back to back,
// followed by its slot block: per attribute in metadata order, a slot
// per leaf in leaf order, of the tree's size, framed with the attribute's
// position and the leaf's offset and holding an attribute leaf listing an
// attribute page beside each page of the leaf. A file with such attributes
// names their codec, slot sizes and indexed source columns, and holds them
// at every LOD.
{
  const lod = {
    column: 'lod',
    key: { column: 'key', depth: 2 },
    zooms: [1, 2],
    attributes: [
      { name: 'a', type: 'double' },
      { name: 'b', type: 'double' },
    ],
    attribute_columns: { column: 'lod_attributes', lods: ['f1', 'f2'] },
    lods: {
      f1: { zooms: [1], geometry_type: 'Point', scale: [1, 1], offset: [0, 0] },
      f2: { zooms: [2], geometry_type: 'Point', scale: [1, 1], offset: [0, 0] },
    },
  }
  // f1's, f2's and the primary directory's slot sizes
  const SLOTS = [64, 96, 80]
  const columns = {
    lod,
    codecs: { f1: 'UNCOMPRESSED', f2: 'UNCOMPRESSED' },
    attribute_codec: 'UNCOMPRESSED',
    column_index: {
      columns: ['geometry', 'a', 'b'],
      codecs: ['UNCOMPRESSED', 'UNCOMPRESSED', 'UNCOMPRESSED'],
      schema: SCHEMA_AB,
      directories: [EMPTY_TOP, EMPTY_TOP, EMPTY_TOP],
    },
    attribute_slots: SLOTS,
  }
  /** A slot of `size` bytes framing `node` for the attribute at
   * `attribute` beside the leaf at byte `leaf`, its length prefix `length`
   * when given. */
  const slot = (node, attribute, leaf, size, length) => {
    const packed = gzipSync(JSON.stringify(node))
    const out = new Uint8Array(size)
    const view = new DataView(out.buffer)
    view.setUint32(0, length ?? packed.length, true)
    view.setUint32(4, attribute, true)
    view.setBigUint64(8, BigInt(leaf), true)
    out.set(packed, 16)
    return out
  }
  const f2Leaves = [leaf([[1, PAGES, 10, 0, 3]]), leaf([[5, PAGES + 10, 10, 3, 2]])]
  /** f2's attribute leaves, per attribute and leaf. */
  const nodes = [
    [
      [0, [0], [7]],
      [0, [7], [9]],
    ],
    [
      [0, [100], [5]],
      [0, [105], [6]],
    ],
  ]
  /** A file of an f1 leaf and its slots of `f1Size` bytes, `gap` zero
   * bytes, then f2's two leaves under its top node and their slot block,
   * then `tail` zero bytes. Each f2 slot is as `change` returns it from the
   * slot of attribute `a` beside leaf `j`, given the block and f2's leaves'
   * byte ranges, `mix` may then change f1's slots and f2's block in place,
   * and `sizes` are the root's slot sizes. Returns the reader and the byte
   * ranges of the two slot blocks. */
  const styled = ({
    change = (s) => s,
    sizes = SLOTS,
    reads = [],
    f1Size = SLOTS[0],
    gap = 0,
    tail = 0,
    mix = () => {},
  } = {}) => {
    const file = store()
    file.raw(new Uint8Array(16384))
    const f1 = file.add(leaf([[0, PAGES + 20, 10, 5, 1]]))
    const f1Block = file.size()
    const f1Slots = [
      slot([0, [300], [4]], 0, f1[0], f1Size),
      slot([0, [400], [8]], 1, f1[0], f1Size),
    ]
    const after = f1Block + 2 * f1Size + gap
    let at = after
    const f2 = f2Leaves.map((node) => {
      const range = [at, gzipSync(JSON.stringify(node)).length]
      at += range[1]
      return range
    })
    const f2Block = at
    const block = nodes.flatMap((leaves, a) =>
      leaves.map((node, j) => slot(node, a, f2[j][0], SLOTS[1])),
    )
    const changed = block.map((bytes, i) => change(bytes, Math.floor(i / 2), i % 2, block, f2))
    mix(f1Slots, changed)
    for (const bytes of f1Slots) file.raw(bytes)
    file.raw(new Uint8Array(gap))
    for (const node of f2Leaves) file.add(node)
    assert.equal(file.size(), f2Block)
    for (const bytes of changed) file.raw(bytes)
    file.raw(new Uint8Array(tail))
    const r = reader(file.bytes, EMPTY_TOP, EMPTY_TOP, reads, {
      ...columns,
      attribute_slots: sizes,
      directories: {
        f1: branch(1, [[0, ...f1]]),
        f2: branch(1, [
          [1, ...f2[0]],
          [5, ...f2[1]],
        ]),
      },
    })
    return {
      r,
      blocks: [
        [f1Block, f1Block + 2 * f1Size],
        [f2Block, f2Block + 4 * SLOTS[1]],
      ],
    }
  }
  const prepare = (r, attributes, name = 'f2', z = 0) =>
    r.prepareTile(r.lods[name], z, 0, 0, undefined, [], attributes)
  const { r, blocks } = styled()
  assert.deepEqual((await prepare(r, ['a'])).attributes[0].pages, [
    { offset: 0, length: 7, first: 0, count: 3 },
    { offset: 7, length: 9, first: 3, count: 2 },
  ])
  assert.deepEqual(
    (await prepare(styled().r, ['b'])).attributes[0].pages,
    [
      { offset: 100, length: 5, first: 0, count: 3 },
      { offset: 105, length: 6, first: 3, count: 2 },
    ],
    'the second attribute reads the slots after those of the first',
  )
  assert.deepEqual(
    (await prepare(styled().r, ['b'], 'f1')).attributes[0].pages,
    [{ offset: 400, length: 8, first: 5, count: 1 }],
    "each tree's slots are of its own size",
  )
  // a tile styled by no attribute reads no slot byte
  const reads = []
  await prepare(styled({ reads }).r, [])
  assert.ok(reads.length)
  assert.ok(reads.every(([a, b]) => blocks.every(([start, end]) => b <= start || a >= end)))
  // a tile styled by `b` reads its slots, and not those of `a`
  const styledReads = []
  await prepare(styled({ reads: styledReads }).r, ['b'])
  const [start] = blocks[1]
  for (const j of [0, 1])
    assert.ok(
      styledReads.some(
        ([a, b]) => a === start + (2 + j) * SLOTS[1] && b === start + (3 + j) * SLOTS[1],
      ),
    )
  assert.ok(styledReads.every(([a, b]) => b <= start || a >= start + 2 * SLOTS[1]))
  const first = (make) => (bytes, a, j, block, f2) => (a || j ? bytes : make(bytes, f2))
  const own = (node, f2, length) => slot(node, 0, f2[0][0], SLOTS[1], length)
  for (const [options, message] of [
    [{ change: first((_, f2) => own([0, [], []], f2)) }, /malformed byte ranges|disagrees/],
    [
      { change: first((_, f2) => own([0, [0, 7], [7, 9]], f2)) },
      /a leaf disagrees with the f2 leaf/,
    ],
    [{ change: first((_, f2) => own(nodes[0][0], f2, SLOTS[1] - 15)) }, /overruns its slot/],
    [{ change: first((_, f2) => own(nodes[0][0], f2, 0)) }, /overruns its slot/],
    [{ change: first((_, f2) => own([0, [10], [-8]], f2)) }, /malformed byte ranges/],
    [{ change: first((_, f2) => own([0, [0]], f2)) }, /malformed attribute leaf/],
    [{ change: first((_, f2) => own([0, [10 ** 7], [7]], f2)) }, /outside the file/],
    [
      { change: first((_, f2) => slot(nodes[0][0], 1, f2[0][0], SLOTS[1])) },
      /holds another attribute/,
    ],
    [{ change: first((_, f2) => slot(nodes[0][0], 0, f2[1][0], SLOTS[1])) }, /beside another leaf/],
    // a slot size doubled in the root
    [{ sizes: [SLOTS[0], 2 * SLOTS[1], SLOTS[2]] }, /padding is not zero/],
    // the slots of `a` beside the two leaves swapped
    [{ change: (bytes, a, j, block) => (a ? bytes : block[1 - j]) }, /beside another leaf/],
    [
      {
        change: first((bytes) => {
          const out = bytes.slice()
          out[out.length - 1] = 1
          return out
        }),
      },
      /padding is not zero/,
    ],
  ])
    await assert.rejects(prepare(styled(options).r, ['a']), message)
  // tree 0's slot size at f2 misplaces its slots
  await assert.rejects(
    prepare(styled({ sizes: [SLOTS[0], SLOTS[0], SLOTS[2]] }).r, ['b']),
    /beside another leaf|another attribute|padding is not zero|overruns/,
  )
  // the slots of `a` beside f1's leaf and f2's first leaf, of one size,
  // swapped between the trees
  const swapped = {
    f1Size: SLOTS[1],
    sizes: [SLOTS[1], SLOTS[1], SLOTS[2]],
    mix: (f1Slots, block) => ([f1Slots[0], block[0]] = [block[0], f1Slots[0]]),
  }
  await prepare(styled({ ...swapped, mix: () => {} }).r, ['a'], 'f1')
  await assert.rejects(prepare(styled(swapped).r, ['a']), /beside another leaf/)
  await assert.rejects(prepare(styled(swapped).r, ['a'], 'f1'), /beside another leaf/)
  // f1's slot size wrong by a factor that puts its slot of `b` at f2's slot
  // of `b` beside f2's second leaf, a whole slot with zeros after it
  const leaves = blocks[1][0] - blocks[0][1]
  const factor = Math.ceil((2 * SLOTS[0] + leaves + 3 * SLOTS[1]) / SLOTS[0])
  const wrong = factor * SLOTS[0]
  assert.ok(factor > 1)
  await assert.rejects(
    prepare(
      styled({
        sizes: [wrong, SLOTS[1], SLOTS[2]],
        gap: wrong - (2 * SLOTS[0] + leaves + 3 * SLOTS[1]),
        tail: wrong - SLOTS[1],
      }).r,
      ['b'],
      'f1',
    ),
    /beside another leaf/,
  )
  const bytes = new Uint8Array(PAGES)
  const top = [1, [0], [0], [100], [10]]
  const tops = { directories: { f1: top, f2: top } }
  assert.ok(reader(bytes, top, EMPTY_TOP, [], { ...columns, ...tops }))
  const bare = { ...lod, attributes: [] }
  delete bare.attribute_columns
  for (const [root, message] of [
    [{ attribute_codec: undefined }, /names no attribute codec/],
    [{ column_index: undefined }, /malformed row directories/],
    [
      {
        column_index: {
          columns: ['geometry'],
          codecs: ['UNCOMPRESSED'],
          schema: SCHEMA,
          directories: [EMPTY_TOP],
        },
      },
      /no indexed source column/,
    ],
    [
      { lod: { ...lod, attribute_columns: { column: 'lod_attributes', lods: ['f2'] } } },
      /attribute columns at every LOD/,
    ],
    [{ attribute_slots: undefined }, /malformed attribute slot sizes/],
    [{ attribute_slots: SLOTS.slice(1) }, /malformed attribute slot sizes/],
    [{ attribute_slots: [SLOTS[0], 15, SLOTS[2]] }, /malformed attribute slot sizes/],
    [{ attribute_slots: [SLOTS[0], 8.5, SLOTS[2]] }, /malformed attribute slot sizes/],
    [{ primary_index: undefined }, /malformed primary directory/],
    [{ lod: bare }, /attribute slots without attributes/],
  ])
    assert.throws(
      () => reader(bytes, top, EMPTY_TOP, [], { ...columns, ...tops, ...root }),
      message,
    )
  assert.throws(
    () =>
      reader(bytes, top, EMPTY_TOP, [], {
        ...columns,
        directories: { f1: top, f2: [...top, [[[200], [5]]]] },
      }),
    /malformed spatial directory node/,
  )
}
console.log('attributes: attribute slots, leaves, codec and source columns checked')
