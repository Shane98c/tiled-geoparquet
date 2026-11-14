/**
 * Decoder of one packed LOD row, the format python/tiled_geoparquet/pack.py
 * writes: a row's geometry on the LOD grid and the straddler copies it hosts
 * with their attributes, all as LEB128 varints with string payloads trailing
 * in order.
 */

import { GeometryBuilder, type GeometryType } from './geometry.js'

export type AttributeKind = 'int' | 'float16' | 'float32' | 'float64' | 'bool' | 'string' | 'enum'

export interface AttributeField {
  name: string
  kind: AttributeKind
  /** The value table of an `enum` attribute, indexed by the packed value. */
  values?: string[]
  /** The declared Arrow type. */
  type?: string
}

/** The fields of attributes as `lod_columns` metadata declares them. */
export const attributeFields = (
  attributes: { name: string; type: string; values?: string[] }[],
): AttributeField[] =>
  attributes.map((a) => ({
    name: a.name,
    kind: a.values ? 'enum' : attributeKind(a.type),
    ...(a.values ? { values: a.values } : {}),
    type: a.type,
  }))

/** The attribute kinds the packer writes, from the Arrow type names in the metadata. */
export const attributeKind = (type: string): AttributeKind => {
  const t = type.toLowerCase()
  if (t === 'double' || t === 'float64') return 'float64'
  if (t === 'halffloat' || t === 'float16') return 'float16'
  if (t === 'float' || t === 'float32') return 'float32'
  if (t === 'bool' || t === 'boolean') return 'bool'
  if (t === 'string' || t === 'utf8' || t === 'large_string' || t === 'large_utf8') return 'string'
  if (/int/.test(t)) return 'int'
  throw new Error(`unsupported attribute type ${type}`)
}

const textDecoder = new TextDecoder()
const f32 = new DataView(new ArrayBuffer(8))

class Cursor {
  pos = 0
  /** Bits 32 to 63 of the varint `wide` read last. */
  high = 0
  constructor(readonly bytes: Uint8Array) {}

  /** A varint of up to 64 bits as unsigned halves: bits 0 to 31 returned,
   * the rest left in `high`, so no bit passes through a double. */
  wide(): number {
    let low = 0,
      high = 0,
      shift = 0
    for (;;) {
      const byte = this.bytes[this.pos++],
        bits = byte & 0x7f
      if (shift < 32) {
        low |= bits << shift
        if (shift > 25) high |= bits >>> (32 - shift)
      } else high |= bits << (shift - 32)
      if (!(byte & 0x80)) break
      if (shift === 63) throw new Error(LONG_VARINT)
      shift += 7
    }
    this.high = high >>> 0
    return low >>> 0
  }

  /** An IEEE-754 double from its 64-bit pattern. */
  float64(): number {
    f32.setUint32(0, this.wide(), true)
    f32.setUint32(4, this.high, true)
    return f32.getFloat64(0, true)
  }

  /** A zigzag integer of up to 64 bits as the nearest number: exact up to
   * 2^53 in magnitude, and rounded once past it, as `Number` rounds a BigInt. */
  int(): number {
    const zigzag = this.wide()
    let low = ((zigzag >>> 1) | (this.high << 31)) >>> 0,
      high = this.high >>> 1
    if (!(zigzag & 1)) return high * 2 ** 32 + low
    // a negative value n is stored as -2n - 1, so its magnitude is the shifted bits plus one
    low = (low + 1) >>> 0
    if (!low) high++
    return -(high * 2 ** 32 + low)
  }

  varint(): number {
    let out = 0
    let scale = 1
    for (;;) {
      const b = this.bytes[this.pos++]
      out += (b & 0x7f) * scale
      if (!(b & 0x80)) return out
      if (scale === TENTH_BYTE) throw new Error(LONG_VARINT)
      scale *= 128
    }
  }
}

/** The weight of a varint's tenth byte, the last a 64-bit value takes. */
const TENTH_BYTE = 2 ** 63
const LONG_VARINT = 'a packed row holds a varint of more than ten bytes'

const unzigzag = (v: number): number => (v % 2 ? -(v + 1) / 2 : v / 2)

/** The presence bitmap holds one bit per attribute. */
export const MAX_ATTRIBUTES = 64

/** Bit `i` of a presence bitmap given as the halves `Cursor.wide` reads. */
const isPresent = (low: number, high: number, i: number): boolean =>
  ((i < 32 ? low >>> i : high >>> (i - 32)) & 1) === 1

/** An IEEE half-precision bit pattern as a number. */
export const half = (bits: number): number => {
  const sign = bits & 0x8000 ? -1 : 1
  const exponent = (bits >> 10) & 0x1f
  const fraction = bits & 0x3ff
  if (exponent === 0x1f) return fraction ? NaN : sign * Infinity
  return sign * (exponent ? 0x400 + fraction : fraction) * 2 ** ((exponent || 1) - 25)
}

/** The half-precision value nearest `v`, ties to even, as a number: what
 * a cast to float16 makes of it. */
export const toHalf = (v: number): number => {
  const magnitude = Math.abs(v)
  if (!(magnitude > 0) || magnitude === Infinity) return v
  // past the largest half, 65504, by half its step or more
  if (magnitude >= 65520) return Math.sign(v) * Infinity
  f32.setFloat64(0, magnitude, true)
  const exponent = Math.max(-14, ((f32.getUint32(4, true) >>> 20) & 0x7ff) - 1023)
  const step = 2 ** (exponent - 10)
  const steps = magnitude / step
  let rounded = Math.round(steps)
  if (rounded - steps === 0.5 && rounded % 2) rounded--
  return Math.sign(v) * rounded * step
}

/** A source value of an attribute of `kind` as the attribute holds it: a
 * float in its declared precision, anything else as it is. */
export const asDeclared = (value: unknown, kind: AttributeKind): unknown => {
  if (value == null) return value
  if (kind === 'float16') return toHalf(Number(value))
  if (kind === 'float32') return Math.fround(Number(value))
  if (kind === 'float64') return Number(value)
  return value
}

/** An attribute's own value, read from an attribute column or a source
 * column, as a copy's packed value decodes: a value table index as its
 * string, a float rounded to its declared precision and a 64-bit integer as
 * the nearest number. */
export const asColumnValue = (value: unknown, field: AttributeField): unknown => {
  if (value == null) return null
  if (field.kind === 'enum' && typeof value === 'number') {
    const found = field.values?.[value]
    if (found === undefined) throw new Error(`${field.name} indexes past its value table`)
    return found
  }
  if (typeof value === 'bigint') return Number(value)
  return asDeclared(value, field.kind)
}

let absent: unknown[] = []

/** One read-only array of `count` nulls, shared. */
const nulls = (count: number): unknown[] => {
  if (absent.length !== count) absent = new Array(count).fill(null)
  return absent
}

/** A copy's attribute values with string slots left as byte lengths, filled
 * from the trailing payload later. A copy packing none returns `nulls`. */
const attributes = (
  c: Cursor,
  fields: AttributeField[],
  strings: [unknown[], number, number][],
) => {
  const low = c.wide(),
    high = c.high
  if (!low && !high) return nulls(fields.length)
  const out: unknown[] = new Array(fields.length)
  for (let i = 0; i < fields.length; i++) {
    if (!isPresent(low, high, i)) {
      out[i] = null
      continue
    }
    if (fields[i].kind === 'float64') {
      out[i] = c.float64()
      continue
    }
    if (fields[i].kind === 'int') {
      out[i] = c.int()
      continue
    }
    const v = c.varint()
    switch (fields[i].kind) {
      case 'float16':
        out[i] = half(v)
        break
      case 'float32':
        f32.setUint32(0, v, true)
        out[i] = f32.getFloat32(0, true)
        break
      case 'bool':
        out[i] = v !== 0
        break
      case 'string':
        strings.push([out, i, v])
        break
      case 'enum':
        out[i] = fields[i].values?.[v] ?? null
        break
    }
  }
  return out
}

/** Decode selected geometry directly into columnar buffers. Unwanted copies
 * are scanned without allocating geometry arrays. A row's own feature holds
 * no attributes, all null; a copy's come from its packed values, their
 * strings trailing the row in order. */
export const decodeSelectedRow = (
  bytes: Uint8Array,
  type: GeometryType,
  fields: AttributeField[],
  builder: GeometryBuilder,
  own: boolean,
  id: number,
  tile: number,
  add: (id: number, attrs: unknown[], slot: number) => void,
  withAttributes = true,
): void => {
  const c = new Cursor(bytes)
  if (!withAttributes) {
    const ownGeometry = geometryInto(c, type, own ? builder : undefined)
    if (own && ownGeometry) add(id, NO_ATTRIBUTES, 0)
    const count = c.varint()
    let strings = 0
    for (let k = 0; k < count; k++) {
      const copyTile = c.varint(),
        copyId = c.varint()
      const nonempty = geometryInto(c, type, copyTile === tile ? builder : undefined)
      if (copyTile === tile && !nonempty) {
        builder.begin(type)
        if (type === 'MultiLineString') builder.startPolygon()
      }
      strings += skipAttributes(c, fields)
      if (copyTile === tile) add(copyId, NO_ATTRIBUTES, k + 1)
    }
    // All string bytes trail the row's varints; no requested attributes means
    // they are left unread, and the row ends where they do.
    if (c.pos + strings !== bytes.length)
      throw new Error('a packed row does not end where its copies do')
    return
  }
  const strings: [unknown[], number, number][] = []
  const selected: [number, unknown[], number][] = []
  const keep = geometryInto(c, type, own ? builder : undefined)
  if (own && keep) selected.push([id, nulls(fields.length), 0])
  const count = c.varint()
  for (let k = 0; k < count; k++) {
    const copyTile = c.varint()
    const copyId = c.varint()
    const nonempty = geometryInto(c, type, copyTile === tile ? builder : undefined)
    if (copyTile === tile && !nonempty) {
      builder.begin(type)
      if (type === 'MultiLineString') builder.startPolygon()
    }
    const copyAttrs = attributes(c, fields, strings)
    if (copyTile === tile) selected.push([copyId, copyAttrs, k + 1])
  }
  for (const [slot, i, length] of strings) {
    slot[i] = textDecoder.decode(bytes.subarray(c.pos, c.pos + length))
    c.pos += length
  }
  // a row is its geometry, its copies and their strings, and nothing more
  if (c.pos !== bytes.length) throw new Error('a packed row does not end where its copies do')
  for (const [row, values, slot] of selected) add(row, values, slot)
}

/** Whether a packed row holds a geometry of its own, which makes it a row
 * its LOD shows: every row of a point LOD, and a shape row whose leading
 * varint, its count of parts, is not zero. A row that only hosts copies
 * counts none. */
export const showsRow = (bytes: Uint8Array, type: GeometryType): boolean => {
  if (type === 'Point') return true
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i] & 0x7f) return true
    if (!(bytes[i] & 0x80)) return false
  }
  return false
}

function geometryInto(c: Cursor, type: GeometryType, builder?: GeometryBuilder): boolean {
  if (type === 'Point') {
    const x = unzigzag(c.varint()),
      y = unzigzag(c.varint())
    if (builder) {
      builder.begin(type)
      builder.startPolygon()
      builder.startRing()
      const i = builder.reserveCoordinates(1)
      builder.x[i] = x
      builder.y[i] = y
    }
    return true
  }
  const parts = c.varint()
  // Empty host geometries are omitted by the own-row path. Copies are never
  // empty in files produced by the writer.
  if (parts && builder) builder.begin(type)
  if (type === 'MultiLineString') {
    if (parts && builder) builder.startPolygon()
    for (let i = 0; i < parts; i++) ringInto(c, builder)
  } else {
    for (let i = 0; i < parts; i++) {
      builder?.startPolygon()
      const rings = c.varint()
      for (let j = 0; j < rings; j++) ringInto(c, builder, true)
    }
  }
  return parts > 0
}

/** A polygon ring is stored without its closing vertex; `closed` repeats the first. */
function ringInto(c: Cursor, builder?: GeometryBuilder, closed = false) {
  const n = c.varint()
  if (!builder) {
    for (let i = 0; i < n * 2; i++) c.varint()
    return
  }
  builder.startRing()
  const start = builder.reserveCoordinates(closed && n ? n + 1 : n)
  const bytes = c.bytes,
    xs = builder.x,
    ys = builder.y
  let pos = c.pos,
    x = 0,
    y = 0
  for (let i = start; i < start + n; i++) {
    // Cursor.varint and unzigzag in line: most deltas are one byte
    let b = bytes[pos++]
    if (!(b & 0x80)) x += (b >>> 1) ^ -(b & 1)
    else {
      let v = b & 0x7f,
        scale = 128
      for (;;) {
        b = bytes[pos++]
        v += (b & 0x7f) * scale
        if (!(b & 0x80)) break
        if (scale === TENTH_BYTE) throw new Error(LONG_VARINT)
        scale *= 128
      }
      x += unzigzag(v)
    }
    b = bytes[pos++]
    if (!(b & 0x80)) y += (b >>> 1) ^ -(b & 1)
    else {
      let v = b & 0x7f,
        scale = 128
      for (;;) {
        b = bytes[pos++]
        v += (b & 0x7f) * scale
        if (!(b & 0x80)) break
        if (scale === TENTH_BYTE) throw new Error(LONG_VARINT)
        scale *= 128
      }
      y += unzigzag(v)
    }
    xs[i] = x
    ys[i] = y
  }
  c.pos = pos
  if (closed && n) {
    xs[start + n] = xs[start]
    ys[start + n] = ys[start]
  }
}

const NO_ATTRIBUTES: unknown[] = []
/** Pass over a copy's attributes, and return the bytes its strings take
 * after the row's varints. */
function skipAttributes(c: Cursor, fields: AttributeField[]): number {
  const low = c.wide(),
    high = c.high
  let strings = 0
  for (let i = 0; i < fields.length; i++) {
    if (!isPresent(low, high, i)) continue
    const kind = fields[i].kind
    if (kind === 'float64' || kind === 'int') c.wide()
    else if (kind === 'string') strings += c.varint()
    else c.varint()
  }
  return strings
}
