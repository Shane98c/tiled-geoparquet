import { decompressPage } from 'hyparquet/src/datapage.js'
import { deserializeTCompactProtocol } from 'hyparquet/src/thrift.js'
import { compressors } from './compressors.js'
import { half } from './decode.js'

/** Visit only present values in a page of the profile's
 * required-struct/optional-binary column: a v1 data page of PLAIN values with
 * RLE definition levels and no repetition levels. Scan definition-level runs
 * directly, so sparse overviews don't allocate a null slot for every
 * analytical row. */
export function visitPlainPage(
  buffer: ArrayBuffer,
  codec: string,
  rows: number,
  visit: (row: number, value: Uint8Array) => void,
): void {
  const reader = { view: new DataView(buffer), offset: 0 }
  const header = deserializeTCompactProtocol(reader)
  const data = header.field_5 as Record<string, number> | undefined
  if (header.field_1 !== 0 || !data || data.field_2 !== 0 || data.field_3 !== 3)
    throw new Error('spatial LOD pages must be v1 data pages of PLAIN values')
  if (data.field_1 !== rows) throw new Error('spatial page row count mismatch')
  const compressedSize = Number(header.field_3)
  if (reader.offset + compressedSize !== buffer.byteLength) throw new Error('invalid page length')
  const bytes = decompressPage(
    new Uint8Array(buffer, reader.offset, compressedSize),
    Number(header.field_2),
    codec as never,
    compressors,
  )
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const definitionEnd = 4 + view.getUint32(0, true)
  if (definitionEnd > bytes.length) throw new Error('invalid definition level length')
  let definition = 4,
    value = definitionEnd,
    row = 0
  const emit = (at: number) => {
    const length = view.getUint32(value, true)
    value += 4
    if (value + length > bytes.length) throw new Error('truncated packed LOD value')
    visit(at, bytes.subarray(value, value + length))
    value += length
  }
  while (row < rows) {
    let run = 0,
      scale = 1,
      byte: number
    do {
      if (definition >= definitionEnd || scale > 2 ** 28) throw new Error('invalid definition run')
      byte = bytes[definition++]
      run += (byte & 127) * scale
      scale *= 128
    } while (byte & 128)
    if (run < 2) throw new Error('empty definition run')
    if (run % 2) {
      const groups = Math.floor(run / 2)
      if (definition + groups > definitionEnd) throw new Error('truncated definition bits')
      for (let group = 0; group < groups; group++) {
        const bits = bytes[definition++]
        const end = Math.min(rows, row + 8)
        if (bits) for (let bit = 0; row + bit < end; bit++) if (bits & (1 << bit)) emit(row + bit)
        row = end
      }
    } else {
      if (definition >= definitionEnd) throw new Error('truncated definition run')
      const present = bytes[definition++]
      if (present > 1) throw new Error('invalid optional binary definition level')
      const end = Math.min(rows, row + run / 2)
      if (present) for (let at = row; at < end; at++) emit(at)
      row = end
    }
  }
  if (definition !== definitionEnd || value !== bytes.length)
    throw new Error('invalid packed page payload length')
}

const utf8 = new TextDecoder()

/** Value width in bytes of each attribute column type with fixed-width
 * values, by declared Arrow type: integers narrower than 64 bits are Parquet
 * INT32, a value table index among them. */
const WIDTHS: Record<string, number> = {
  int8: 4,
  int16: 4,
  int32: 4,
  uint8: 4,
  uint16: 4,
  uint32: 4,
  int64: 8,
  uint64: 8,
  halffloat: 2,
  float16: 2,
  float: 4,
  float32: 4,
  double: 8,
  float64: 8,
}

/**
 * The present values of a page of an LOD's attribute column: a v1 data page
 * of PLAIN or BYTE_STREAM_SPLIT values with RLE definition levels, one level
 * per row, of a column whose Parquet type the attribute's declared Arrow
 * `type` gives; an attribute with a value table (`indexed`) holds uint8
 * indexes into it. Returns the rows within the page holding a value,
 * ascending, and the values in that order: numbers, booleans or strings.
 */
export function decodeAttributePage(
  buffer: ArrayBuffer,
  codec: string,
  rows: number,
  type: string,
  indexed: boolean,
): { rows: Int32Array; values: ArrayLike<unknown> } {
  const reader = { view: new DataView(buffer), offset: 0 }
  const header = deserializeTCompactProtocol(reader)
  const data = header.field_5 as Record<string, number> | undefined
  if (header.field_1 !== 0 || !data || data.field_3 !== 3)
    throw new Error('attribute pages must be v1 data pages with RLE definition levels')
  const encoding = data.field_2
  if (encoding !== 0 && encoding !== 9)
    throw new Error(`unsupported attribute encoding ${encoding}`)
  if (data.field_1 !== rows) throw new Error('attribute page row count mismatch')
  const compressedSize = Number(header.field_3)
  if (reader.offset + compressedSize !== buffer.byteLength) throw new Error('invalid page length')
  const bytes = decompressPage(
    new Uint8Array(buffer, reader.offset, compressedSize),
    Number(header.field_2),
    codec as never,
    compressors,
  )
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  if (bytes.length < 4) throw new Error('invalid definition level length')
  const definitionEnd = 4 + view.getUint32(0, true)
  if (definitionEnd > bytes.length) throw new Error('invalid definition level length')
  const t = type.toLowerCase()
  const isString =
    !indexed && (t === 'string' || t === 'utf8' || t === 'large_string' || t === 'large_utf8')
  const isBoolean = !indexed && (t === 'bool' || t === 'boolean')
  const width = isString || isBoolean ? 0 : WIDTHS[indexed ? 'uint8' : t]
  if (!isString && !isBoolean && !width) throw new Error(`unsupported attribute type ${type}`)
  // the values that fit the page's value bytes bound the rows holding one
  const room = bytes.length - definitionEnd
  const present = new Int32Array(
    Math.min(rows, isBoolean ? room * 8 : Math.floor(room / (isString ? 4 : width)) + 1),
  )
  let count = 0,
    definition = 4,
    row = 0
  const keep = (at: number) => {
    if (count === present.length) throw new Error('truncated attribute values')
    present[count++] = at
  }
  while (row < rows) {
    let run = 0,
      scale = 1,
      byte: number
    do {
      if (definition >= definitionEnd || scale > 2 ** 28) throw new Error('invalid definition run')
      byte = bytes[definition++]
      run += (byte & 127) * scale
      scale *= 128
    } while (byte & 128)
    if (run < 2) throw new Error('empty definition run')
    if (run % 2) {
      const groups = Math.floor(run / 2)
      if (definition + groups > definitionEnd) throw new Error('truncated definition bits')
      for (let group = 0; group < groups; group++) {
        const bits = bytes[definition++]
        const end = Math.min(rows, row + 8)
        if (bits) for (let bit = 0; row + bit < end; bit++) if (bits & (1 << bit)) keep(row + bit)
        row = end
      }
    } else {
      if (definition >= definitionEnd) throw new Error('truncated definition run')
      const level = bytes[definition++]
      if (level > 1) throw new Error('invalid optional definition level')
      const end = Math.min(rows, row + run / 2)
      if (level) for (let at = row; at < end; at++) keep(at)
      row = end
    }
  }
  if (definition !== definitionEnd) throw new Error('invalid definition level length')
  const start = definitionEnd
  let values: ArrayLike<unknown>, end: number
  if (isString) {
    if (encoding !== 0) throw new Error('string attribute pages must be PLAIN')
    const out: string[] = new Array(count)
    end = start
    for (let i = 0; i < count; i++) {
      if (end + 4 > bytes.length) throw new Error('truncated attribute values')
      const length = view.getUint32(end, true)
      if (end + 4 + length > bytes.length) throw new Error('truncated attribute values')
      out[i] = utf8.decode(bytes.subarray(end + 4, end + 4 + length))
      end += 4 + length
    }
    values = out
  } else if (isBoolean) {
    if (encoding !== 0) throw new Error('boolean attribute pages must be PLAIN')
    end = start + Math.ceil(count / 8)
    if (end > bytes.length) throw new Error('truncated attribute values')
    const out: boolean[] = new Array(count)
    for (let i = 0; i < count; i++) out[i] = ((bytes[start + (i >> 3)] >> (i & 7)) & 1) === 1
    values = out
  } else {
    const kind = indexed ? 'uint8' : t
    end = start + width * count
    if (end > bytes.length) throw new Error('truncated attribute values')
    let plain = bytes.slice(start, end)
    if (encoding === 9) {
      const joined = new Uint8Array(width * count)
      for (let b = 0; b < width; b++)
        for (let i = 0, from = b * count; i < count; i++) joined[i * width + b] = plain[from + i]
      plain = joined
    }
    const aligned = plain.buffer as ArrayBuffer
    if (width === 2) values = Array.from(new Uint16Array(aligned, 0, count), half)
    else if (kind === 'float' || kind === 'float32') values = new Float32Array(aligned, 0, count)
    else if (kind === 'double' || kind === 'float64') values = new Float64Array(aligned, 0, count)
    else if (kind === 'uint32') values = new Uint32Array(aligned, 0, count)
    else if (width === 4) values = new Int32Array(aligned, 0, count)
    else if (kind === 'uint64') values = Array.from(new BigUint64Array(aligned, 0, count), Number)
    else values = Array.from(new BigInt64Array(aligned, 0, count), Number)
  }
  if (end !== bytes.length) throw new Error('attribute page values disagree with its rows')
  return { rows: present.subarray(0, count), values }
}
