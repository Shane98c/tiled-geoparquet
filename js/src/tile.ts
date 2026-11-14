import { asDeclared, attributeKind } from './decode.js'
import { MVT_BUFFER, MVT_EXTENT, toMvt } from './mvt.js'
import { geometryColumnFromWkb } from '@cogp/reader'
import type { LodGeometry } from './geometry.js'
import type { Bbox, Lod, LodRows } from './reader.js'
import { decodeTile, type AttributeTask, type PreparedTile, type TileReader } from './spatial.js'
import { HALF_WORLD } from './web-mercator.js'

const VECTOR_TILE_SIZE = 512

export interface TileOptions {
  /** MVT layer name for shape LODs (default `features`). */
  layer?: string
  /** MVT layer name for point LODs (default: same as `layer`). */
  pointLayer?: string
  /** Attribute or source column names to copy into MVT properties, each
   * read only when named (default none: geometry only). */
  properties?: string[]
  signal?: AbortSignal
}

const tileYToLatitude = (y: number, tiles: number) =>
  (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / tiles))) * 180) / Math.PI

/** The clip buffer, in `MVT_EXTENT` units, for a file whose straddler copies
 * reach `margin` pixels past a tile: geometry clipped nearer than that would cut
 * the strokes the copies are there to draw. */
export const clipBuffer = (margin: number): number => {
  if (!Number.isFinite(margin) || margin < 0) throw new Error(`invalid margin ${margin}`)
  return Math.max(MVT_BUFFER, Math.ceil((margin / VECTOR_TILE_SIZE) * MVT_EXTENT))
}

export const tileBounds = (z: number, x: number, y: number, buffer = MVT_BUFFER): Bbox => {
  const tiles = 2 ** z
  const padding = buffer / MVT_EXTENT
  return [
    ((x - padding) / tiles) * 360 - 180,
    tileYToLatitude(y + 1 + padding, tiles),
    ((x + 1 + padding) / tiles) * 360 - 180,
    tileYToLatitude(y - padding, tiles),
  ]
}

/** The buffered tile in EPSG:3857 metres, the CRS LOD coordinates are stored in. */
const mercatorTileBounds = (z: number, x: number, y: number, buffer: number): Bbox => {
  const cell = (2 * HALF_WORLD) / 2 ** z
  const padding = (buffer / MVT_EXTENT) * cell
  return [
    -HALF_WORLD + x * cell - padding,
    HALF_WORLD - (y + 1) * cell - padding,
    -HALF_WORLD + (x + 1) * cell + padding,
    HALF_WORLD - y * cell + padding,
  ]
}

/** One MVT tile from the LOD that serves zoom `z`, or from the primary geometry
 * past the finest LOD and at every zoom of a file without LODs. Feature ids are
 * file row indexes. */
export const renderTile = async (
  reader: TileReader,
  z: number,
  x: number,
  y: number,
  options: TileOptions = {},
): Promise<ArrayBuffer> => {
  const { signal } = options
  const buffer = reader.lod ? clipBuffer(reader.lod.margin) : MVT_BUFFER
  const bbox = tileBounds(z, x, y, buffer)
  const declared = reader.lod?.attributes ?? []
  const names = options.properties ?? []
  const lod = reader.lod ? reader.lodForZoom(z) : undefined
  const pointFamily = lod?.geometry_type === 'Point'
  const layer = (pointFamily ? options.pointLayer : undefined) ?? options.layer ?? 'features'
  if (!lod || z > reader.finestZoom) {
    // finer than any LOD: the exact primary geometry of the rows in the tile,
    // with attributes in their declared types, as the LODs hold them, so a
    // feature's properties are the same on both sides of the finest zoom
    const { rows, wkb, columns } = await reader.readPrimary(bbox, names, signal)
    signal?.throwIfAborted()
    for (const { name, type } of declared) {
      const kind = attributeKind(type)
      if (columns[name]) columns[name] = columns[name].map((value) => asDeclared(value, kind))
    }
    return toMvt(
      { geometry: geometryColumnFromWkb(wkb), rowIndex: rows, columns },
      { z, x, y, layer, signal, buffer },
    )
  }
  const data = await reader.readLodByTile(lod, z, x, y, names, signal)
  signal?.throwIfAborted()
  return renderDecodedTile(normalizeTile(lod, data, z, x, y, buffer), options)
}

export const tileResolution = (z: number, y: number) => {
  const latitude = tileYToLatitude(y + 0.5, 2 ** z)
  return (360 * Math.cos((latitude * Math.PI) / 180)) / (VECTOR_TILE_SIZE * 2 ** z)
}

export interface DecodedTile {
  lod: Lod
  z: number
  x: number
  y: number
  rows: number[]
  columns: Record<string, unknown[]>
  geometry: LodGeometry
  /** Clip buffer in `MVT_EXTENT` units, from the file's margin. */
  buffer: number
  /** Attributes with no entry in `columns` yet: their own features' values
   * are in pages the tile has not read. */
  pending?: string[]
  /** While any attribute is pending: 0 for a boundary copy and, for a
   * feature of the tile's own, one more than its position among the rows its
   * LOD page shows, where an attribute page holds its value. */
  own?: Uint32Array
  /** Per pending attribute, the values its boundary copies pack, one
   * per copy in feature order. */
  copies?: Record<string, unknown[]>
  /** While any attribute is pending: the first row of each LOD page
   * the tile read and how many rows it shows, which bound the values an
   * attribute page beside it may hold. */
  shown?: number[]
}

/** A tile decoded with every attribute: those whose pages the task lacks
 * are pending. */
export function decodePreparedTile(task: PreparedTile): DecodedTile {
  const { own, pending, copies, shown, ...data } = decodeTile(
    task,
    task.fields.map((f) => f.name),
  )
  const tile = normalizeTile(
    task.lod,
    data,
    task.z,
    task.x,
    task.y,
    clipBuffer(task.margin),
    own,
    copies,
  )
  if (pending.length) return { ...tile, pending, shown }
  const { own: _own, copies: _copies, ...complete } = tile
  return complete
}

/** The work of filling a decoded tile's pending attributes from their
 * pages over its own rows, which `own` marks, checked against the rows its
 * LOD pages show, `shown`. */
export function attributeTask(
  tile: DecodedTile,
  own: Uint32Array,
  shown: number[],
  fields: AttributeTask['fields'],
  attributes: AttributeTask['attributes'],
): AttributeTask {
  return {
    rows: tile.rows.filter((_, i) => own[i]),
    ranks: Array.from(own.filter(Boolean), (position) => position - 1),
    shown,
    fields,
    attributes,
  }
}

/** A tile's columns of `values`, the values a task decoded for the features
 * `own` marks, merged with the values `copies` holds for its boundary copies. */
export function filledColumns(
  own: Uint32Array,
  copies: Record<string, unknown[]>,
  values: Record<string, unknown[]>,
): Record<string, unknown[]> {
  return Object.fromEntries(
    Object.entries(values).map(([name, ownValues]) => {
      const copied = copies[name] ?? []
      const column: unknown[] = new Array(own.length)
      let k = 0
      let c = 0
      for (let i = 0; i < own.length; i++) column[i] = own[i] ? ownValues[k++] : copied[c++]
      if (k !== ownValues.length || c !== copied.length)
        throw new Error(`${name} values disagree with the tile`)
      return [name, column]
    }),
  )
}

export function renderDecodedTile(tile: DecodedTile, options: TileOptions = {}): ArrayBuffer {
  const { lod, rows, geometry, z, x, y, buffer } = tile
  const names = options.properties ?? Object.keys(tile.columns)
  const columns = Object.fromEntries(
    names.map((name) => {
      if (!(name in tile.columns)) throw new Error(`missing hydrated attribute ${name}`)
      return [name, tile.columns[name]]
    }),
  )
  const layer =
    (lod.geometry_type === 'Point' ? options.pointLayer : undefined) ?? options.layer ?? 'features'
  return toMvt(
    { geometry: geometry as never, rowIndex: rows, columns },
    { z, x, y, layer, buffer, signal: options.signal },
  )
}

/** Point filtering happens once before caching; rendering is read-only. */
function normalizeTile(
  lod: Lod,
  data: LodRows,
  z: number,
  x: number,
  y: number,
  buffer: number,
  own?: Uint32Array,
  copies?: Record<string, unknown[]>,
): DecodedTile {
  const bbox = mercatorTileBounds(z, x, y, buffer)
  const names = Object.keys(data.columns)
  const pointFamily = lod.geometry_type === 'Point'
  const { rows, columns } = data
  let { geometry } = data
  if (pointFamily) {
    // Keep points inside the buffered tile. Point offsets are the identity
    // sequence; compact coordinates and attributes in place.
    const copied = Object.values(copies ?? {})
    let count = 0
    let copy = 0
    let keptCopies = 0
    for (let i = 0; i < rows.length; i++) {
      const isCopy = own !== undefined && !own[i]
      if (isCopy) copy++
      const mx = lod.offset[0] + lod.scale[0] * geometry.x[i]
      const my = lod.offset[1] + lod.scale[1] * geometry.y[i]
      if (mx < bbox[0] || mx > bbox[2] || my < bbox[1] || my > bbox[3]) continue
      geometry.x[count] = geometry.x[i]
      geometry.y[count] = geometry.y[i]
      rows[count] = rows[i]
      if (own) own[count] = own[i]
      for (const name of names) columns[name][count] = columns[name][i]
      if (isCopy) {
        for (const values of copied) values[keptCopies] = values[copy - 1]
        keptCopies++
      }
      count++
    }
    rows.length = count
    for (const name of names) columns[name].length = count
    for (const values of copied) values.length = keptCopies
    if (own) own = own.subarray(0, count)
    geometry = {
      ...geometry,
      length: count,
      x: geometry.x.subarray(0, count),
      y: geometry.y.subarray(0, count),
      types: geometry.types.subarray(0, count),
      geometryOffsets: geometry.geometryOffsets.subarray(0, count + 1),
      polygonOffsets: geometry.polygonOffsets.subarray(0, count + 1),
      ringOffsets: geometry.ringOffsets.subarray(0, count + 1),
    }
  }
  return {
    lod,
    z,
    x,
    y,
    rows,
    columns,
    geometry,
    buffer,
    ...(own ? { own } : {}),
    ...(copies ? { copies } : {}),
  }
}
