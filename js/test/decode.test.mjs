import assert from 'node:assert/strict'
import { GeometryBuilder } from '../dist/index.js'
import { decodeSelectedRow, showsRow } from '../dist/decode.js'
import { columnar, nested } from './columnar.mjs'

// a MultiPolygon, a MultiLineString and a Point in grid units, copied feature by feature
const mp = [
  [
    [
      [0, 0],
      [3, 0],
      [3, 3],
      [0, 0],
    ],
  ],
  [
    [
      [10, 10],
      [12, 10],
      [10, 12],
      [10, 10],
    ],
  ],
]
const line = [
  [
    [0, 0],
    [1, 1],
  ],
]
const sources = [columnar('MultiPolygon', [mp]), columnar('MultiLineString', [line])]
const b = new GeometryBuilder()
b.append(sources[0], 0)
b.append(sources[1], 0)
b.append(columnar('Point', [[7, 8]]), 0)
const g = b.finish([1, 1], [0, 0])
assert.equal(g.length, 3)
assert.deepEqual(Array.from(g.types), [6, 5, 1])
assert.deepEqual(Array.from(g.geometryOffsets), [0, 2, 3, 4])
assert.deepEqual(Array.from(g.polygonOffsets), [0, 1, 2, 3, 4])
assert.deepEqual(Array.from(g.ringOffsets), [0, 4, 8, 10, 11])
assert.deepEqual(Array.from(g.x), [0, 3, 3, 0, 10, 12, 10, 10, 0, 1, 7])
assert.deepEqual(Array.from(g.y), [0, 0, 3, 0, 10, 10, 12, 10, 0, 1, 8])
assert.deepEqual(nested(g, 0, 'MultiPolygon'), mp)
assert.deepEqual(nested(g, 1, 'MultiLineString'), line)
assert.deepEqual(nested(g, 2, 'Point'), [7, 8])

// a packed row: one square at (5, 5) of side 2, which holds no attributes of its own,
// hosting one copy for tile zoom 12 / 7 of row 9: a unit square at (1, 1), h=4, name=null
const zz = (v) => (v < 0 ? -2 * v - 1 : 2 * v)
const varint = (v) => {
  const out = []
  while (v >= 128) {
    out.push((v % 128) + 128)
    v = Math.floor(v / 128)
  }
  out.push(v)
  return out
}
// a polygon ring is stored without its closing vertex, which repeats the first
const ringBytes = (pts) => {
  const out = varint(pts.length - 1)
  let x = 0
  let y = 0
  for (const [px, py] of pts.slice(0, -1)) {
    out.push(...varint(zz(px - x)), ...varint(zz(py - y)))
    x = px
    y = py
  }
  return out
}
const square = (x, y, s) => [
  [x, y],
  [x + s, y],
  [x + s, y + s],
  [x, y + s],
  [x, y],
]
const bytes = Uint8Array.from([
  ...varint(1),
  ...varint(1),
  ...ringBytes(square(5, 5, 2)), // own geometry: one polygon, one ring
  ...varint(1), // one copy
  ...varint(12 * 2 ** 48 + 7),
  ...varint(9),
  ...varint(1),
  ...varint(1),
  ...ringBytes(square(1, 1, 1)),
  ...varint(0b01),
  ...varint(zz(4)), // copy attrs: only h present
])
const fields = [
  { name: 'h', kind: 'int' },
  { name: 'name', kind: 'string' },
]
const decode = (own, tile, withAttributes = true) => {
  const builder = new GeometryBuilder()
  const features = []
  const add = (id, attrs, slot) => features.push({ id, attrs, slot })
  decodeSelectedRow(bytes, 'MultiPolygon', fields, builder, own, 4, tile, add, withAttributes)
  const geometry = builder.finish([1, 1], [0, 0])
  return features.map((f, i) => ({ ...f, geometry: nested(geometry, i, 'MultiPolygon') }))
}
const copyTile = 12 * 2 ** 48 + 7
assert.deepEqual(decode(true, copyTile), [
  { id: 4, attrs: [null, null], slot: 0, geometry: [[square(5, 5, 2)]] },
  { id: 9, attrs: [4, null], slot: 1, geometry: [[square(1, 1, 1)]] },
])
assert.deepEqual(decode(true, copyTile + 1), [
  { id: 4, attrs: [null, null], slot: 0, geometry: [[square(5, 5, 2)]] },
])
assert.deepEqual(decode(false, copyTile), [
  { id: 9, attrs: [4, null], slot: 1, geometry: [[square(1, 1, 1)]] },
])
assert.deepEqual(decode(false, copyTile + 1), [])
assert.deepEqual(
  decode(true, copyTile, false).map((f) => [f.id, f.attrs, f.geometry]),
  [
    [4, [], [[square(5, 5, 2)]]],
    [9, [], [[square(1, 1, 1)]]],
  ],
  'geometry without attributes',
)
// a hosted copy's strings trail the row's varints
const named = Uint8Array.from([
  ...bytes.subarray(0, bytes.length - 2),
  ...varint(0b11),
  ...varint(zz(4)),
  ...varint(3),
  ...new TextEncoder().encode('xyz'),
])
const copy = new GeometryBuilder()
const copies = []
decodeSelectedRow(named, 'MultiPolygon', fields, copy, false, 4, copyTile, (...f) => copies.push(f))
assert.deepEqual(copies, [[9, [4, 'xyz'], 1]])
console.log('decode tests passed')

/** The attributes of a copy packing `attrs`, hosted by a point at the origin. */
const copied = (attrs, fields) => {
  const row = Uint8Array.from([0, 0, 1, 0, 1, 0, 0, ...attrs])
  const found = []
  decodeSelectedRow(row, 'Point', fields, new GeometryBuilder(), false, 0, 0, (_, values) =>
    found.push(values),
  )
  assert.equal(found.length, 1)
  return found[0]
}

// the stored key: a tile is one interval, its parent the union of its four children
import { hilbertIndex, tileInterval, cellInterval } from '../dist/index.js'
assert.deepEqual(
  [0, 1, 2, 3].map((i) => hilbertIndex(1, i & 1, i >> 1)),
  [0, 3, 1, 2],
)
const child = (x, y) => cellInterval(16, 12, x, y)
const parent = cellInterval(16, 11, 7, 5)
const kids = [child(14, 10), child(15, 10), child(14, 11), child(15, 11)].sort(
  (a, b) => a[0] - b[0],
)
assert.deepEqual([kids[0][0], kids[3][1]], parent)
assert.ok(
  kids.every((k, i) => i === 0 || k[0] === kids[i - 1][1]),
  'children are contiguous',
)
assert.deepEqual(tileInterval(16, 11, 7, 5), parent)
assert.deepEqual(
  tileInterval(16, 18, 100, 100),
  cellInterval(16, 16, 25, 25),
  'beyond the depth, the depth cell',
)
console.log('key tests passed')

// Float64 packing stores raw bits, including low mantissa bits above Number’s
// integer precision.
for (const value of [
  0.1,
  -13.37,
  Number.MIN_VALUE,
  Number.MAX_VALUE,
  -0,
  Infinity,
  -Infinity,
  NaN,
]) {
  const view = new DataView(new ArrayBuffer(8))
  view.setFloat64(0, value, true)
  let bits = view.getBigUint64(0, true)
  const encoded = []
  do {
    const byte = Number(bits & 127n)
    bits >>= 7n
    encoded.push(byte | (bits ? 128 : 0))
  } while (bits)
  assert.ok(Object.is(copied([1, ...encoded], [{ name: 'value', kind: 'float64' }])[0], value))
}

// Float16 packing stores the half-precision bit pattern; values from numpy.
for (const [bits, value] of [
  [0x0000, 0],
  [0x8000, -0],
  [0x0001, 5.960464477539063e-8],
  [0x03ff, 6.097555160522461e-5],
  [0x0400, 6.103515625e-5],
  [0x3555, 0.333251953125],
  [0x3bff, 0.99951171875],
  [0x3c00, 1],
  [0x3fff, 1.9990234375],
  [0x4000, 2],
  [0xc000, -2],
  [0x7bff, 65504],
  [0xfbff, -65504],
  [0x7c00, Infinity],
  [0xfc00, -Infinity],
  [0x7e00, NaN],
  [0x7c01, NaN],
]) {
  const fields = [{ name: 'value', kind: 'float16' }]
  assert.ok(Object.is(copied([1, ...varint(bits)], fields)[0], value), `0x${bits.toString(16)}`)
}

// an integer attribute is the number nearest its 64-bit value, with its sign
const varint64 = (v) => {
  const out = []
  while (v >= 128n) {
    out.push(Number(v % 128n) + 128)
    v /= 128n
  }
  out.push(Number(v))
  return out
}
const zz64 = (v) => (v < 0n ? -2n * v - 1n : 2n * v)
for (const value of [
  0n,
  -1n,
  2n ** 31n,
  -(2n ** 31n) - 1n,
  2n ** 52n + 1n,
  -(2n ** 52n) - 1n,
  2n ** 53n,
  -(2n ** 53n),
  2n ** 53n + 1n,
  -(2n ** 53n) - 3n,
  2n ** 63n - 1n,
  -(2n ** 63n),
]) {
  const fields = [{ name: 'value', kind: 'int' }]
  assert.equal(copied([1, ...varint64(zz64(value))], fields)[0], Number(value), `${value}`)
}

// the presence bitmap is exact for all 64 attributes it can hold
{
  const fields = Array.from({ length: 64 }, (_, i) => ({ name: `a${i}`, kind: 'int' }))
  for (const present of [[0], [31], [32], [52], [53], [63], [0, 31, 32, 53, 63]]) {
    const bitmap = present.reduce((bits, i) => bits | (1n << BigInt(i)), 0n)
    const attrs = [...varint64(bitmap), ...present.flatMap((i) => varint(zz(i + 1)))]
    const expected = fields.map((_, i) => (present.includes(i) ? i + 1 : null))
    assert.deepEqual(copied(attrs, fields), expected, `attributes ${present}`)
    const row = Uint8Array.from([0, 0, 1, 0, 1, 0, 0, ...attrs])
    const seen = []
    decodeSelectedRow(row, 'Point', fields, new GeometryBuilder(), true, 0, 0, (_, attrs) =>
      seen.push(attrs),
    )
    assert.deepEqual(seen, [fields.map(() => null), expected], `selected attributes ${present}`)
  }
}

// A row its LOD shows holds a geometry of its own: a shape row whose count
// of parts, a varint of however many bytes, is not zero. The count decides
// both whether the row is drawn and whether it takes a place among the rows
// its page shows.
{
  const triangle = [1, 3, 0, 0, 4, 0, 4, 4]
  for (const [count, shown] of [
    [[0], false],
    [[0x80, 0x00], false],
    [[0x80, 0x80, 0x00], false],
    [[1], true],
    [[0x81, 0x00], true],
    [[0x81, 0x80, 0x00], true],
  ]) {
    const row = Uint8Array.from([...count, ...(shown ? triangle : []), 0])
    assert.equal(showsRow(row, 'MultiPolygon'), shown, `count ${count}`)
    const drawn = []
    decodeSelectedRow(row, 'MultiPolygon', [], new GeometryBuilder(), true, 7, 0, (id) =>
      drawn.push(id),
    )
    assert.deepEqual(drawn, shown ? [7] : [], `a row of count ${count} is drawn when shown`)
  }
  assert.equal(showsRow(Uint8Array.of(0x80, 0x01, 2, 0, 0, 2, 2), 'MultiLineString'), true)
  assert.equal(showsRow(Uint8Array.of(0, 0, 0), 'MultiLineString'), false)
  assert.equal(showsRow(new Uint8Array(0), 'MultiPolygon'), false)
  assert.equal(showsRow(Uint8Array.of(0x80), 'MultiPolygon'), false)
  assert.equal(showsRow(Uint8Array.of(0, 0, 0, 0), 'Point'), true, 'every point row is shown')
}

// A row is its geometry, its copies and their strings. A row with bytes that
// are none of those, such as an attribute section of its own, is refused, not
// read as copies the row does not host.
{
  const add = () => {}
  const decode = (bytes, fields, withAttributes) =>
    decodeSelectedRow(
      Uint8Array.from(bytes),
      'Point',
      fields,
      new GeometryBuilder(),
      true,
      42,
      0,
      add,
      withAttributes,
    )
  const fields = [{ name: 'value', kind: 'int' }]
  for (const withAttributes of [true, false]) {
    decode([0, 0, 0], fields, withAttributes)
    assert.throws(
      () => decode([0, 0, 1, 0, 0], fields, withAttributes),
      /a packed row (ends inside a value|does not end where its copies do)/,
      `an own attribute section, attributes ${withAttributes}`,
    )
  }
  assert.throws(() => decode([0, 0, 0, 9], fields, true), /does not end where its copies do/)
  // a copy, then bytes that are no part of it, whether or not attributes are read
  for (const withAttributes of [true, false])
    assert.throws(
      () => decode([0, 0, 1, 0, 1, 0, 0, 2, 2, 0], fields, withAttributes),
      /does not end where its copies do/,
      `bytes past a copy, attributes ${withAttributes}`,
    )
  // a copy whose string is not there
  const named = [{ name: 'name', kind: 'string' }]
  for (const withAttributes of [true, false]) {
    decode([0, 0, 1, 0, 1, 0, 0, 1, 2, 0x68, 0x69], named, withAttributes)
    assert.throws(
      () => decode([0, 0, 1, 0, 1, 0, 0, 1, 5], named, withAttributes),
      /does not end where its copies do/,
      `a missing string, attributes ${withAttributes}`,
    )
  }
  // a varint takes ten bytes at most
  const long = [...new Array(10).fill(0x80), 0]
  for (const withAttributes of [true, false])
    assert.throws(
      () => decode([...long, 0, 0], fields, withAttributes),
      /a varint of more than ten bytes/,
    )
  assert.throws(
    () =>
      decodeSelectedRow(
        Uint8Array.from([1, 1, 3, 0, 0, ...long, 0, 2, 2, 0]),
        'MultiPolygon',
        [],
        new GeometryBuilder(),
        true,
        1,
        0,
        add,
      ),
    /a varint of more than ten bytes/,
  )
}
