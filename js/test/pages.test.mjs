import assert from 'node:assert/strict'
import { decodeAttributePage, visitPlainPage } from '../dist/pages.js'
const varint = (v) => {
  const bytes = []
  while (v >= 128) {
    bytes.push((v % 128) + 128)
    v = Math.floor(v / 128)
  }
  return [...bytes, v]
}
const i32 = (value) => {
  const b = new Uint8Array(4)
  new DataView(b.buffer).setUint32(0, value, true)
  return [...b]
}
const page = (rows, levels, values, type = 0, encoding = 0) => {
  const payload = [
    ...i32(levels.length),
    ...levels,
    ...values.flatMap((v) => [...i32(v.length), ...v]),
  ]
  // Compact Thrift PageHeader + DataPageHeader, plain/uncompressed binary.
  const header = [
    0x15,
    type * 2,
    0x15,
    ...varint(payload.length * 2),
    0x15,
    ...varint(payload.length * 2),
    0x2c,
    0x15,
    ...varint(rows * 2),
    0x15,
    encoding * 2,
    0x15,
    6,
    0x15,
    6,
    0,
    0,
  ]
  return Uint8Array.from([...header, ...payload]).buffer
}
for (const [rows, levels, indices] of [
  [19, [...varint(38), 1], Array.from({ length: 19 }, (_, i) => i)],
  [1000000, [...varint(2000000), 0], []],
  [1000001, [...varint(2000000), 0, 2, 1], [1000000]],
  [13, [5, 0b01010010, 0b00001110], [1, 4, 6, 9, 10, 11]],
  [17, [16, 0, 3, 0b10101010, 2, 1], [9, 11, 13, 15, 16]],
]) {
  const values = indices.map((_, i) => [i, 0, 255])
  const bytes = page(rows, levels, values),
    found = []
  visitPlainPage(bytes, 'UNCOMPRESSED', rows, (row, value) => found.push([row, [...value]]))
  assert.deepEqual(
    found,
    indices.map((row, i) => [row, values[i]]),
  )
  assert.throws(() => visitPlainPage(bytes, 'UNCOMPRESSED', rows + 1, () => {}), /row count/)
  assert.throws(
    () => visitPlainPage(bytes.slice(0, -1), 'UNCOMPRESSED', rows, () => {}),
    /page length/,
  )
}
for (const [type, encoding, kind] of [
  [3, 0, 'v2'],
  [0, 8, 'dictionary-encoded'],
])
  assert.throws(
    () => visitPlainPage(page(1, [2, 1], [[42]], type, encoding), 'UNCOMPRESSED', 1, () => {}),
    /v1 data pages of PLAIN values/,
    `a ${kind} page is rejected`,
  )
assert.throws(
  () => visitPlainPage(page(1, [0], []), 'UNCOMPRESSED', 1, () => {}),
  /empty definition run/,
)
console.log(
  'sparse pages: long null runs, mixed bitpacking, partial bytes, corruption, other page kinds',
)

// Attribute pages: present rows and values of each column type, PLAIN and
// BYTE_STREAM_SPLIT, refused when the values do not fill the page exactly.
{
  const attributePage = (rows, levels, values, encoding = 0) => {
    const payload = [...i32(levels.length), ...levels, ...values]
    const header = [
      0x15,
      0,
      0x15,
      ...varint(payload.length * 2),
      0x15,
      ...varint(payload.length * 2),
      0x2c,
      0x15,
      ...varint(rows * 2),
      0x15,
      encoding * 2,
      0x15,
      6,
      0x15,
      6,
      0,
      0,
    ]
    return Uint8Array.from([...header, ...payload]).buffer
  }
  const bytes = (Array, values) => [...new Uint8Array(Array.from(values).buffer)]
  const split = (raw, width) => {
    const count = raw.length / width
    return Array.from(
      { length: raw.length },
      (_, i) => raw[(i % count) * width + Math.floor(i / count)],
    )
  }
  // rows 1, 4, 6 of 8 present: one bit-packed group
  const levels = [3, 0b01010010]
  const cases = [
    ['double', bytes(Float64Array, [1.5, -2, 1e300]), [1.5, -2, 1e300], 8],
    ['float', bytes(Float32Array, [1.5, -2, 0.25]), [1.5, -2, 0.25], 4],
    ['halffloat', [0x00, 0x3e, 0x00, 0xc0, 0x00, 0x34], [1.5, -2, 0.25], 2],
    ['int32', bytes(Int32Array, [7, -8, 9]), [7, -8, 9], 4],
    ['uint32', bytes(Uint32Array, [4294967295, 0, 1]), [4294967295, 0, 1], 4],
    ['int64', bytes(BigInt64Array, [-5n, 2n ** 53n, 3n]), [-5, 2 ** 53, 3], 8],
    ['uint64', bytes(BigUint64Array, [2n ** 63n, 1n, 0n]), [2 ** 63, 1, 0], 8],
    ['bool', [0b101], [true, false, true], 0],
    ['string', [1, 0, 0, 0, 97, 0, 0, 0, 0, 2, 0, 0, 0, 98, 99], ['a', '', 'bc'], 0],
  ]
  for (const [type, raw, expected, width] of cases) {
    const found = decodeAttributePage(attributePage(8, levels, raw), 'UNCOMPRESSED', 8, type, false)
    assert.deepEqual([...found.rows], [1, 4, 6], type)
    assert.deepEqual(Array.from(found.values), expected, type)
    if (width) {
      const again = decodeAttributePage(
        attributePage(8, levels, split(raw, width), 9),
        'UNCOMPRESSED',
        8,
        type,
        false,
      )
      assert.deepEqual(Array.from(again.values), expected, `${type} split`)
    } else
      assert.throws(
        () => decodeAttributePage(attributePage(8, levels, raw, 9), 'UNCOMPRESSED', 8, type, false),
        /must be PLAIN/,
      )
    assert.throws(
      () =>
        decodeAttributePage(attributePage(8, levels, [...raw, 0]), 'UNCOMPRESSED', 8, type, false),
      /disagree with its rows/,
    )
  }
  const indexed = decodeAttributePage(
    attributePage(8, levels, bytes(Int32Array, [2, 0, 1])),
    'UNCOMPRESSED',
    8,
    'string',
    true,
  )
  assert.deepEqual(Array.from(indexed.values), [2, 0, 1], 'a value table index')
  assert.throws(
    () =>
      decodeAttributePage(attributePage(8, levels, [0, 0], 2), 'UNCOMPRESSED', 8, 'int32', false),
    /unsupported attribute encoding/,
  )
  assert.throws(
    () => decodeAttributePage(attributePage(8, levels, [0]), 'UNCOMPRESSED', 9, 'int32', false),
    /row count/,
  )
  assert.throws(
    () => decodeAttributePage(attributePage(8, [3, 0b11], [0]), 'UNCOMPRESSED', 8, 'int8', false),
    /truncated attribute values/,
  )
}
console.log('attribute pages: every column type, split bytes, value tables and corruption')
