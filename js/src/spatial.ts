import { SharedLoad } from './shared-load.js'
import { decodeProjection, type ProjectionExecutor, type ProjectionTask } from './projection.js'
import { parquetMetadata, parquetSchema } from 'hyparquet'
import { readColumn } from 'hyparquet/src/column.js'
import { DEFAULT_PARSERS } from 'hyparquet/src/convert.js'
import { deserializeTCompactProtocol } from 'hyparquet/src/thrift.js'
import { compressors } from './compressors.js'
import {
  asColumnValue,
  attributeFields,
  decodeSelectedRow,
  showsRow,
  type AttributeField,
} from './decode.js'
import { decodeAttributePage, visitPlainPage } from './pages.js'
import { GeometryBuilder } from './geometry.js'
import { cellInterval, tileInterval, type Interval } from './key.js'
import {
  GeoParquetReader,
  checkIndexVersion,
  checkLod,
  type Bbox,
  type Lod,
  type LodMetadata,
  type LodRows,
  type PrimaryRows,
  type RangeSource,
} from './reader.js'
import { float32Outward, wkbBounds } from './wkb.js'
import { MAX_LATITUDE } from './web-mercator.js'

export const BOOTSTRAP_BYTES = 16384
const MAGIC = 'LODIDX01'
const MAX_LEVEL = 32
/** An attribute slot's length and attribute, each a u32
 * little-endian, and its leaf's offset, a u64 little-endian */
const SLOT_HEADER = 16
/** Primary directory boxes are whole cells of 2^-12 degrees. */
const BOX_CELLS_PER_DEGREE = 4096
// [minimum key, maximum key, offset, length, first global row, row count, fences, valid key rows]
type Page = [number, number, number, number, number, number, number[], number?]
// [minimum key, maximum key, offset, length, level of the node located]
type Shard = [number, number, number, number, number]
type ByteRange = { offset: number; length: number }
/** Where the slots beside a leaf lie: its tree, an LOD's in metadata order
 * or past them the primary directory's, the leaf's offset, the end of its
 * level-one node's last leaf, where the node's slot block starts, that
 * node's leaves and the leaf's position among them. */
type SlotPlace = { tree: number; leaf: number; end: number; count: number; index: number }
/** What an attribute slot names: its attribute's position and its
 * leaf's offset. */
type SlotFrame = { attribute: number; leaf: number }
/** A node loaded or loading, read at `length` bytes, from an attribute slot
 * framed `frame` when given, and for an LOD leaf, where its slots lie. */
type Cached<T> = { length: number; node: SharedLoad<T>; frame?: SlotFrame; place?: SlotPlace }
/** A node's entries: pages in a leaf, else the nodes one level down. */
type DirectoryNode = { level: 0; entries: Page[] } | { level: number; entries: Shard[] }
/** Columns x0..x1 and rows y0..y1 of cells, inclusive. */
type Box = [number, number, number, number]
/** A page of a column chunk: byte range and global rows. */
interface SourcePage {
  offset: number
  length: number
  first: number
  count: number
}
/** Rows `first` to `first + count - 1`. */
type RowSpan = { first: number; count: number }
/** A run of a geometry page's rows and the box of their cells. */
type Run = RowSpan & { box: Box }
/** A geometry page cut into runs, whose rows together are the page's. */
type PrimaryPage = SourcePage & { runs: Run[] }
/** A primary directory leaf: geometry pages cut into runs. */
type PrimaryLeaf = { level: 0; pages: PrimaryPage[] }
interface PrimaryIndex {
  /** The top node of the primary directory */
  directory: unknown
}
interface ColumnIndex {
  /** The flat columns whose pages decode on their own, in schema order */
  columns: string[]
  /** Each column's Parquet codec */
  codecs: string[]
  /** Base64 Thrift metadata whose schema holds those columns */
  schema: string
  /** The top node of each column's row directory, at level one or above */
  directories: unknown[]
}
/** What decodes one indexed column's pages without the footer. */
type ColumnDecoder = ProjectionTask['decoder'] & { codec: string }
interface SpatialRoot {
  columns?: string[]
  bounds?: Bbox
  version: number
  file_bytes: number
  footer_bytes?: number
  lod: LodMetadata
  primary: string
  codecs: Record<string, string>
  /** The top node of each LOD's directory tree, at level one or above */
  directories: Record<string, unknown>
  primary_index: PrimaryIndex
  column_index: ColumnIndex
  /** The codec of every LOD's attribute columns, when any LOD has them */
  attribute_codec?: string
  /** Per tree, each LOD's in metadata order and then the primary
   * directory's, the size of the attribute slots of its leaves, in a file
   * with attributes */
  attribute_slots?: number[]
}

/** An attribute's pages over a tile's own rows, from the LOD's
 * attribute column, decoded by the declared Arrow `type`. */
export interface AttributePages {
  name: string
  codec: string
  type: string
  pages: SourcePage[]
  payloads: ArrayBuffer[]
}

/** CPU work of filling attributes: the own `rows` of a tile,
 * ascending, each one's position among the rows its LOD page shows in
 * `ranks`, the pages holding their values and, in `shown`, the first row of
 * each LOD page of the tile and how many rows it shows. */
export interface AttributeTask {
  rows: number[]
  ranks: number[]
  shown: number[]
  fields: AttributeField[]
  attributes: AttributePages[]
}

/** `promise`, whose rejection its later awaiting reports, not the runtime
 * before then. */
const awaited = <T>(promise: Promise<T>): Promise<T> => {
  promise.catch(() => {})
  return promise
}

const integers = (values: unknown): values is number[] =>
  Array.isArray(values) && values.every(Number.isSafeInteger)

/** Cells of a WGS84 bbox, rounded outward, as the primary directory's boxes are. */
const cellBox = ([west, south, east, north]: Bbox): Box => [
  Math.floor((west + 180) * BOX_CELLS_PER_DEGREE),
  Math.floor((south + 90) * BOX_CELLS_PER_DEGREE),
  Math.floor((east + 180) * BOX_CELLS_PER_DEGREE),
  Math.floor((north + 90) * BOX_CELLS_PER_DEGREE),
]

const overlaps = (a: Box | Bbox, b: Box | Bbox) =>
  a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1]

/** Pages stored as `[gaps, lengths, rows, counts]`: each page's bytes after
 * the previous page's end, length, first row after the previous page's end
 * row, and rows, the first counted from zero. */
function decodeSources(stored: unknown): SourcePage[] {
  const [gaps, lengths, rows, counts] = (Array.isArray(stored) ? stored : []) as number[][]
  if (
    !Array.isArray(stored) ||
    stored.length !== 4 ||
    ![gaps, lengths, rows, counts].every(
      (column) => integers(column) && column.length === gaps.length,
    )
  )
    throw new Error('malformed directory pages')
  const pages: SourcePage[] = []
  let end = 0,
    row = 0
  for (let i = 0; i < gaps.length; i++) {
    if (gaps[i] < 0 || lengths[i] <= 0 || rows[i] < 0 || counts[i] <= 0)
      throw new Error('malformed directory pages')
    const offset = end + gaps[i],
      first = row + rows[i]
    end = offset + lengths[i]
    row = first + counts[i]
    if (!Number.isSafeInteger(end) || !Number.isSafeInteger(row))
      throw new Error('malformed directory pages')
    pages.push({ offset, length: lengths[i], first, count: counts[i] })
  }
  return pages
}

/** Byte ranges stored as `[gaps, lengths]`: each range's start after the
 * previous one's end, and its length, the first counted from zero. With
 * `empty`, a range of length 0 and gap 0 holds no bytes. */
function decodeRanges(stored: unknown, empty = false): ByteRange[] {
  const [gaps, lengths] = (Array.isArray(stored) ? stored : []) as number[][]
  if (
    !Array.isArray(stored) ||
    stored.length !== 2 ||
    !integers(gaps) ||
    !integers(lengths) ||
    gaps.length !== lengths.length
  )
    throw new Error('malformed byte ranges')
  let end = 0
  return gaps.map((gap, i) => {
    if (gap < 0 || lengths[i] < 0 || (!lengths[i] && (!empty || gap)))
      throw new Error('malformed byte ranges')
    const offset = end + gap
    end = offset + lengths[i]
    if (!Number.isSafeInteger(end)) throw new Error('malformed byte ranges')
    return { offset, length: lengths[i] }
  })
}

/**
 * Decode a stored primary directory leaf: level 0, then one column per field
 * with a value per entry, a run of one geometry page's rows. Every entry
 * holds its box's minimum cell column and row minus the previous entry's,
 * its width and height in cells, its page's byte gap after the previous
 * entry's page's end and length, 0 when that is its page too, its first row
 * after the previous entry's end row and its rows. The first entry counts
 * from zero, and a page's runs are consecutive entries whose rows follow on.
 */
function decodePrimaryLeaf(stored: unknown): PrimaryLeaf {
  const node: unknown[] = Array.isArray(stored) ? stored : []
  const [level, wests, souths, widths, heights, gaps, lengths, rows, counts] = node as [
    number,
    ...number[][],
  ]
  if (
    level !== 0 ||
    node.length !== 9 ||
    ![wests, souths, widths, heights, gaps, lengths, rows, counts].every(
      (column) => integers(column) && column.length === wests.length,
    )
  )
    throw new Error('malformed primary directory leaf')
  const pages: PrimaryPage[] = []
  let x = 0,
    y = 0,
    end = 0,
    row = 0
  for (let i = 0; i < wests.length; i++) {
    if (
      widths[i] < 0 ||
      heights[i] < 0 ||
      gaps[i] < 0 ||
      lengths[i] < 0 ||
      rows[i] < 0 ||
      counts[i] <= 0
    )
      throw new Error('malformed primary directory leaf')
    x += wests[i]
    y += souths[i]
    const first = row + rows[i]
    row = first + counts[i]
    const run: Run = { box: [x, y, x + widths[i], y + heights[i]], first, count: counts[i] }
    if (![...run.box, row].every(Number.isSafeInteger))
      throw new Error('malformed primary directory leaf')
    const page = pages.at(-1)
    if (lengths[i]) {
      const offset = end + gaps[i]
      end = offset + lengths[i]
      if (!Number.isSafeInteger(end)) throw new Error('malformed primary directory leaf')
      pages.push({ offset, length: lengths[i], first, count: counts[i], runs: [run] })
    } else if (page && !gaps[i] && !rows[i]) {
      page.count += run.count
      page.runs.push(run)
    } else throw new Error('malformed primary directory leaf')
  }
  return { level: 0, pages }
}

/** A row directory leaf: level 0, then one column's consecutive pages in row
 * order, as `decodeSources` reads them. */
function decodeRowLeaf(stored: unknown): SourcePage[] {
  if (!Array.isArray(stored) || stored.length !== 5 || stored[0] !== 0)
    throw new Error('malformed row directory leaf')
  const pages = decodeSources(stored.slice(1))
  // a row between two pages would be answered with a neighbouring row's value
  if (pages.some((page, i) => i && page.first !== pages[i - 1].first + pages[i - 1].count))
    throw new Error('row directory pages do not follow on')
  return pages
}

/** The `items`, ascending and disjoint in their rows `[start, end)`, holding a
 * row of any of the ascending, disjoint `spans`. */
function meeting<T>(items: T[], rows: (item: T) => [number, number], spans: RowSpan[]): T[] {
  const out: T[] = []
  let s = 0
  for (const item of items) {
    const [start, end] = rows(item)
    while (s < spans.length && spans[s].first + spans[s].count <= start) s++
    if (s === spans.length) break
    if (spans[s].first < end) out.push(item)
  }
  return out
}

/**
 * Key intervals of the tiles holding a WGS84 bbox: at the finest zoom up to
 * `zoom` at which they span at most two columns and two rows, every tile
 * between those holding its corners. The bbox is widened by far less than a
 * cell, so rounding cannot leave out a tile holding a point of it.
 */
function bboxIntervals(depth: number, zoom: number, [west, south, east, north]: Bbox): Interval[] {
  const pad = 1e-9
  for (let z = zoom; ; z--) {
    const n = 2 ** z
    const clamp = (v: number) => Math.max(0, Math.min(n - 1, Math.floor(v * n)))
    const column = (longitude: number) => clamp((longitude + 180) / 360)
    const row = (latitude: number) => {
      const phi = (Math.max(-MAX_LATITUDE, Math.min(MAX_LATITUDE, latitude)) * Math.PI) / 180
      return clamp((1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2)
    }
    const [x0, x1, y0, y1] = [
      column(west - pad),
      column(east + pad),
      row(north + pad),
      row(south - pad),
    ]
    if (z && (x1 - x0 > 1 || y1 - y0 > 1)) continue
    const out: Interval[] = []
    for (let x = x0; x <= x1; x++)
      for (let y = y0; y <= y1; y++) out.push(cellInterval(depth, z, x, y))
    return out
  }
}

const RAW_GEOMETRY_PARSERS = {
  ...DEFAULT_PARSERS,
  geometryFromBytes: (bytes: Uint8Array) => bytes,
  geographyFromBytes: (bytes: Uint8Array) => bytes,
}

/** Refuse a page unless its header shows a data page, not dictionary
 * encoded, of exactly its bytes holding the rows the directory gives it: an
 * indexed column is flat, so a value or a null per row. */
function checkPage(page: SourcePage & { bytes: ArrayBuffer }): void {
  const reader = { view: new DataView(page.bytes), offset: 0 }
  const header = deserializeTCompactProtocol(reader)
  const v1 = header.field_1 === 0 && (header.field_5 as Record<string, number> | undefined)
  const v2 = header.field_1 === 3 && (header.field_8 as Record<string, number> | undefined)
  const [valueCount, rowCount, nullCount, encoding] = v1
    ? [v1.field_1, v1.field_1, 0, v1.field_2]
    : v2
      ? [v2.field_1, v2.field_3, v2.field_2, v2.field_4]
      : [NaN, NaN, NaN, NaN]
  if (
    valueCount !== page.count ||
    rowCount !== page.count ||
    !(nullCount >= 0 && nullCount <= valueCount) ||
    encoding === 2 ||
    encoding === 8 ||
    reader.offset + Number(header.field_3) !== page.bytes.byteLength
  )
    throw new Error('page header disagrees with the spatial index')
}

/** A page's values, decoded whole once `checkPage` accepts it. */
function pageValues(
  decoder: ColumnDecoder,
  page: SourcePage & { bytes: ArrayBuffer },
): ArrayLike<unknown> {
  checkPage(page)
  const { data } = readColumn(
    { view: new DataView(page.bytes), offset: 0 },
    { groupStart: page.first, selectStart: 0, selectEnd: page.count } as never,
    { ...decoder, compressors, parsers: RAW_GEOMETRY_PARSERS } as never,
  )
  const values = data.length === 1 ? data[0] : data.flatMap((chunk) => Array.from(chunk))
  if (values.length !== page.count) throw new Error('page rows disagree with the spatial index')
  return values
}

/** Visit each row of the ascending, disjoint `runs` with its value from
 * consecutive `pages` of one column; every run must lie in the pages. */
function visitRuns(
  decoder: ColumnDecoder,
  pages: (SourcePage & { bytes: ArrayBuffer })[],
  runs: RowSpan[],
  visit: (row: number, value: unknown) => void,
): void {
  let r = 0,
    row = runs[0]?.first ?? 0
  for (const page of pages) {
    if (r === runs.length || row < page.first) break
    const end = page.first + page.count
    if (row >= end) continue
    const values = pageValues(decoder, page)
    while (row < end) {
      const stop = runs[r].first + runs[r].count
      for (; row < Math.min(end, stop); row++) visit(row, values[row - page.first] ?? null)
      if (row < stop || ++r === runs.length) break
      if (runs[r].first < row) throw new Error('primary directory runs overlap')
      row = runs[r].first
    }
  }
  if (r !== runs.length) throw new Error('primary directory pages miss requested rows')
}

/** Ascending rows as runs of consecutive rows. */
function spans(rows: number[]): RowSpan[] {
  const out: RowSpan[] = []
  for (const row of rows) {
    const last = out.at(-1)
    if (last && last.first + last.count === row) last.count++
    else out.push({ first: row, count: 1 })
  }
  return out
}

/**
 * Decode a stored directory node: its level, then one column per field with a
 * value per entry. Every entry holds its minimum cell minus the previous
 * entry's, cell span, byte gap after the previous entry's end, and length; a
 * leaf, at level 0, adds the row gap after the previous page's end, row count,
 * fences after the implied [minimum cell, 0] as pairs minus the pair before,
 * and trailing null-key rows. The first entry counts from zero, and each
 * starts at or after the previous one's maximum cell. Bounds widen to whole
 * cells of `span` keys, which select the same entries for any tile interval
 * made of whole cells.
 */
function decodeNode(stored: unknown, span: number): DirectoryNode {
  const node: unknown[] = Array.isArray(stored) ? stored : []
  const [level, cells, spans, gaps, lengths, rows, counts, fences, nulls] = node as [
    number,
    number[],
    number[],
    number[],
    number[],
    number[],
    number[],
    number[][],
    number[],
  ]
  if (
    !Number.isSafeInteger(level) ||
    level < 0 ||
    level > MAX_LEVEL ||
    node.length !== (level ? 5 : 9) ||
    ![cells, spans, gaps, lengths, ...(level ? [] : [rows, counts, fences, nulls])].every(
      (column) => Array.isArray(column) && column.length === cells.length,
    ) ||
    ![cells, spans, gaps, lengths, ...(level ? [] : [rows, counts, nulls])].every(integers) ||
    (!level && !fences.every((f) => integers(f) && f.length % 2 === 0))
  )
    throw new Error('malformed spatial directory node')
  const pages: Page[] = [],
    shards: Shard[] = []
  let cell = 0,
    end = 0,
    row = 0
  for (let i = 0; i < cells.length; i++) {
    if (
      spans[i] < 0 ||
      gaps[i] < 0 ||
      lengths[i] <= 0 ||
      (!level && (rows[i] < 0 || counts[i] <= 0 || nulls[i] < 0 || nulls[i] >= counts[i]))
    )
      throw new Error('malformed spatial directory node')
    if (cells[i] < (i ? spans[i - 1] : 0))
      throw new Error('spatial directory entries out of key order')
    cell += cells[i]
    const offset = end + gaps[i]
    end = offset + lengths[i]
    const low = cell * span,
      high = (cell + spans[i] + 1) * span - 1
    if (level) {
      shards.push([low, high, offset, lengths[i], level - 1])
      continue
    }
    const first = row + rows[i]
    row = first + counts[i]
    const pairs = [cell, 0]
    for (let j = 0; j < fences[i].length; j += 2)
      pairs.push(pairs[j] + fences[i][j], pairs[j + 1] + fences[i][j + 1])
    pages.push([low, high, offset, lengths[i], first, counts[i], pairs, counts[i] - nulls[i]])
  }
  return level ? { level, entries: shards } : { level: 0, entries: pages }
}

/** Where the slots beside the leaf `shard` of tree `tree` lie, from the
 * `entries` of its level-one node. */
function slotPlace(entries: Shard[], shard: Shard, tree: number): SlotPlace {
  const last = entries.at(-1)!
  return {
    tree,
    leaf: shard[2],
    end: last[2] + last[3],
    count: entries.length,
    index: entries.indexOf(shard),
  }
}

export function hasSpatialIndex(bytes: ArrayBuffer): boolean {
  return bytes.byteLength >= 16 && new TextDecoder().decode(new Uint8Array(bytes, 4, 8)) === MAGIC
}

/** The gzip attribute leaf an attribute slot framed `frame` holds: its
 * length, its attribute and its leaf's offset, the leaf, then zeros. */
function unslot(bytes: ArrayBuffer, frame: SlotFrame): ArrayBuffer {
  const view = new DataView(bytes)
  const length = bytes.byteLength >= SLOT_HEADER ? view.getUint32(0, true) : 0
  if (!length || length > bytes.byteLength - SLOT_HEADER)
    throw new Error('attribute slot length overruns its slot')
  if (view.getUint32(4, true) !== frame.attribute)
    throw new Error('attribute slot holds another attribute')
  if (view.getBigUint64(8, true) !== BigInt(frame.leaf))
    throw new Error('attribute slot is beside another leaf')
  if (new Uint8Array(bytes, SLOT_HEADER + length).some((byte) => byte !== 0))
    throw new Error('attribute slot padding is not zero')
  return bytes.slice(SLOT_HEADER, SLOT_HEADER + length)
}

const gunzip = (bytes: ArrayBuffer): Response =>
  new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip')))

async function gunzipJson<T>(bytes: ArrayBuffer): Promise<T> {
  return JSON.parse(await gunzip(bytes).text()) as T
}

/** Parquet metadata from its Thrift bytes, framed as a footer for hyparquet. */
function metadataFrom(thrift: ArrayBuffer) {
  const footer = new Uint8Array(thrift.byteLength + 8)
  footer.set(new Uint8Array(thrift))
  new DataView(footer.buffer).setUint32(thrift.byteLength, thrift.byteLength, true)
  footer.set(new TextEncoder().encode('PAR1'), thrift.byteLength + 4)
  return parquetMetadata(footer.buffer)
}

/** The file length an indexed file's root records, from its leading bytes, or
 * null when `prefix` holds no readable root. */
export async function rootFileBytes(prefix: ArrayBuffer): Promise<number | null> {
  if (!hasSpatialIndex(prefix)) return null
  const length = new DataView(prefix).getUint32(12, true)
  if (!length || length > prefix.byteLength - 16) return null
  try {
    const root = await gunzipJson<Partial<SpatialRoot>>(prefix.slice(16, 16 + length))
    return Number.isSafeInteger(root.file_bytes) ? root.file_bytes! : null
  } catch {
    return null
  }
}

/** Common rendering/inspection API. Full Parquet metadata is loaded only when
 * an analytical row or the exact-geometry fallback is actually requested. */
export type TileReader = Pick<
  GeoParquetReader,
  | 'scalarColumns'
  | 'bounds'
  | 'lod'
  | 'lods'
  | 'primary'
  | 'finestZoom'
  | 'lodForZoom'
  | 'readLodByTile'
  | 'readPrimary'
  | 'readRow'
  | 'readColumns'
>

export async function openReader(file: RangeSource): Promise<TileReader> {
  const head = await file.slice(0, Math.min(BOOTSTRAP_BYTES, file.byteLength))
  if (!hasSpatialIndex(head)) return GeoParquetReader.open(file)
  const length = new DataView(head).getUint32(12, true)
  if (!length || length > head.byteLength - 16) throw new Error('invalid spatial index root length')
  const root = await gunzipJson<SpatialRoot>(head.slice(16, 16 + length))
  checkIndexVersion(root.version)
  if (root.file_bytes !== file.byteLength)
    throw new Error('spatial index file length disagrees with the file')
  if (!root.lod) throw new Error('spatial index root holds no LOD metadata')
  checkLod(root.lod)
  return new SpatialReader(file, root)
}

/** A directory over the original Parquet pages, not a second tile payload.
 * Only compressed pages are cached, bounded to 16 MiB; callers receive their
 * own decoded arrays. Neighbouring tiles share pending fetches and cached pages.
 */
export class SpatialReader implements TileReader {
  readonly bounds: Bbox
  readonly lod: LodMetadata
  readonly lods: Record<string, Lod>
  readonly primary: string
  readonly finestZoom: number
  private readonly fields: AttributeField[]
  private readonly trees: Record<string, DirectoryNode>
  /** The primary directory's top node */
  private readonly exact: DirectoryNode
  /** Keys per primary directory cell */
  private readonly exactSpan: number
  /** Decoders of the indexed columns, by name */
  private readonly decoders = new Map<string, ColumnDecoder>()
  /** Top nodes of the indexed columns' row directories, by name */
  private readonly rowTrees = new Map<string, DirectoryNode>()
  /** Loaded directory nodes by offset, an LOD leaf with where its slots lie */
  private readonly directories = new Map<number, Cached<DirectoryNode>>()
  private readonly primaryLeaves = new Map<number, Cached<PrimaryLeaf>>()
  private readonly rowLeaves = new Map<number, Cached<SourcePage[]>>()
  /** Attributes, in metadata order */
  readonly attributeNames: string[]
  /** Attributes' leaves beside the LOD leaves, by offset */
  private readonly attributeLeaves = new Map<number, Cached<ByteRange[]>>()
  /** Attributes' leaves beside the primary leaves, by offset */
  private readonly sourceLeaves = new Map<number, Cached<SourcePage[]>>()
  private readonly pages = new Map<number, { size: number; data: SharedLoad<ArrayBuffer> }>()
  private pageBytes = 0
  private fallback?: SharedLoad<GeoParquetReader>

  constructor(
    private readonly file: RangeSource,
    private readonly root: SpatialRoot,
  ) {
    checkIndexVersion(root.version)
    this.bounds = root.bounds ?? [-180, -85.051129, 180, 85.051129]
    this.lod = root.lod
    this.primary = root.primary
    this.finestZoom = Math.max(...this.lod.zooms)
    this.lods = Object.fromEntries(
      Object.entries(this.lod.lods).map(([name, lod]) => [name, { ...lod, name }]),
    )
    this.fields = attributeFields(this.lod.attributes)
    this.attributeNames = this.fields.map((f) => f.name)
    this.trees = Object.fromEntries(
      Object.entries(root.directories ?? {}).map(([name, node]) => {
        if (!this.lods[name]) throw new Error(`spatial directory for unknown LOD ${name}`)
        const top = decodeNode(node, this.cellSpan(this.lods[name]))
        if (!top.level) throw new Error(`spatial directory for ${name} must start above its leaves`)
        return [name, top]
      }),
    )
    const columns = root.column_index
    if (
      !Array.isArray(columns?.columns) ||
      !columns.columns.every((name) => typeof name === 'string') ||
      new Set(columns.columns).size !== columns.columns.length ||
      !Array.isArray(columns.codecs) ||
      columns.codecs.length !== columns.columns.length ||
      !Array.isArray(columns.directories) ||
      columns.directories.length !== columns.columns.length ||
      typeof columns.schema !== 'string'
    )
      throw new Error('malformed row directories')
    const schema = parquetSchema(
      metadataFrom(Uint8Array.from(atob(columns.schema), (c) => c.charCodeAt(0)).buffer),
    )
    columns.columns.forEach((name, i) => {
      const column = schema.children.find((c) => c.element.name === name)
      if (!column || column.children.length || !column.element.type)
        throw new Error(`row directory schema lacks ${name}`)
      const { element } = column
      this.decoders.set(name, {
        type: element.type!,
        codec: String(columns.codecs[i]),
        element,
        schemaPath: [schema, column],
        pathInSchema: [name],
        utf8:
          name !== root.primary &&
          (element.converted_type === 'UTF8' ||
            (element.logical_type as { type?: string } | undefined)?.type === 'STRING'),
      })
      const top = decodeNode(columns.directories[i], 1)
      if (!top.level) throw new Error(`the row directory of ${name} must start above its leaves`)
      this.rowTrees.set(name, top)
    })
    if (this.decoders.get(root.primary)?.type !== 'BYTE_ARRAY')
      throw new Error('the primary geometry is not an indexed binary column')
    const index = root.primary_index
    if (!index || typeof index !== 'object') throw new Error('malformed primary directory')
    if (this.attributeNames.length) {
      const withColumns = this.lod.attribute_columns?.lods ?? []
      if (withColumns.length !== Object.keys(this.lods).length)
        throw new Error('the spatial index needs attribute columns at every LOD')
      if (typeof root.attribute_codec !== 'string')
        throw new Error('the spatial index names no attribute codec')
      if (!this.attributeNames.every((name) => this.decoders.has(name)))
        throw new Error('an attribute has no indexed source column')
      const slots = root.attribute_slots
      if (
        !Array.isArray(slots) ||
        slots.length !== withColumns.length + 1 ||
        !slots.every((size) => Number.isSafeInteger(size) && size >= SLOT_HEADER)
      )
        throw new Error('malformed attribute slot sizes')
    } else if (root.attribute_slots) throw new Error('attribute slots without attributes')
    this.exactSpan = 4 ** Math.max(0, this.lod.key!.depth - this.finestZoom)
    this.exact = decodeNode(index.directory, this.exactSpan)
    if (!this.exact.level) throw new Error('the primary directory must start above its leaves')
  }

  /** Keys per directory cell: cells are at the LOD's finest zoom, at most the key depth. */
  private cellSpan(lod: Lod): number {
    return 4 ** Math.max(0, this.lod.key!.depth - Math.max(...lod.zooms))
  }

  lodForZoom(zoom: number): Lod {
    const target = Math.min(this.finestZoom, Math.max(Math.min(...this.lod.zooms), zoom))
    return Object.values(this.lods).find((lod) => lod.zooms.includes(target))!
  }

  scalarColumns(): string[] {
    return this.root.columns ?? this.lod.attributes.map((a) => a.name)
  }

  private analytical(signal?: AbortSignal): Promise<GeoParquetReader> {
    if (!this.fallback || this.fallback.abandoned) {
      const current = new SharedLoad((signal) =>
        GeoParquetReader.open(this.file, signal, this.root.footer_bytes),
      )
      this.fallback = current
      current.promise.catch(() => {
        if (this.fallback === current) this.fallback = undefined
      })
    }
    return this.fallback.get(signal)
  }

  async readRow(row: number, columns: string[], signal?: AbortSignal) {
    const values = await this.readColumns([row], columns, signal)
    return Object.fromEntries(columns.map((name) => [name, values[name][0]]))
  }

  /**
   * Values of `columns` for `rows`, in their order. An indexed column's pages
   * come from its row directory, decoded by `execute` when given; other
   * columns through the footer.
   */
  async readColumns(
    rows: number[],
    columns: string[],
    signal?: AbortSignal,
    execute?: ProjectionExecutor,
  ): Promise<Record<string, unknown[]>> {
    signal?.throwIfAborted()
    const indexed = columns.filter((name) => this.rowTrees.has(name))
    const others = columns.filter((name) => !this.rowTrees.has(name))
    const sorted = [...new Set(rows)].sort((a, b) => a - b)
    if (indexed.length && !sorted.every((row) => Number.isSafeInteger(row) && row >= 0))
      throw new Error('rows must be non-negative integers')
    const wanted = spans(sorted)
    const [values, rest] = await Promise.all([
      Promise.all(
        indexed.map(async (name) => {
          const pages = await this.columnPages(name, wanted, signal)
          const payloads = await this.pageData(pages, signal)
          const runs = pages.map((page, i) => ({ ...page, bytes: payloads[i] }))
          runs.forEach(checkPage)
          const task: ProjectionTask = {
            rows: sorted,
            decoder: this.decoders.get(name)!,
            dictionary: null,
            runs: runs.map(({ first, count, bytes }) => ({ first, count, bytes })),
          }
          return execute ? execute(task, signal) : decodeProjection(task)
        }),
      ),
      others.length
        ? this.analytical(signal).then((reader) =>
            reader.readColumns(rows, others, signal, execute),
          )
        : {},
    ])
    signal?.throwIfAborted()
    const position = new Map(sorted.map((row, i) => [row, i]))
    const located = Object.fromEntries(
      indexed.map((name, i) => [name, rows.map((row) => values[i][position.get(row)!] ?? null)]),
    )
    return Object.fromEntries(
      columns.map((name) => [name, located[name] ?? (rest as Record<string, unknown[]>)[name]]),
    )
  }

  /** The pages of the indexed column `name` holding any of the ascending,
   * disjoint row `spans`, in row order, from its row directory. */
  private columnPages(name: string, spans: RowSpan[], signal?: AbortSignal): Promise<SourcePage[]> {
    return this.rowLookup(this.rowTrees.get(name)!, 0, spans, new Set(), signal)
  }

  /** The pages under `node`, whose rows start at `first`, holding any of `spans`. */
  private async rowLookup(
    node: DirectoryNode,
    first: number,
    spans: RowSpan[],
    reached: Set<number>,
    signal?: AbortSignal,
  ): Promise<SourcePage[]> {
    const entries = node.entries as Shard[]
    if (entries.some((entry, i) => entry[0] !== (i ? entries[i - 1][1] + 1 : first)))
      throw new Error('row directory entries do not follow on')
    const shards = meeting(entries, ([low, high]) => [low, high + 1], spans)
    const selected = await Promise.all(
      shards.map(async (shard) => {
        const [low, high, offset, length, level] = shard
        if (reached.has(offset)) throw new Error('spatial directory node referenced twice')
        reached.add(offset)
        if (level)
          return this.rowLookup(await this.directory(shard, 1, signal), low, spans, reached, signal)
        const pages = await this.load(this.rowLeaves, offset, length, decodeRowLeaf, signal)
        const last = pages.at(-1)
        if (!last || pages[0].first !== low || last.first + last.count - 1 !== high)
          throw new Error('row directory node disagrees with its parent entry')
        return meeting(pages, (page) => [page.first, page.first + page.count], spans)
      }),
    )
    return selected.flat()
  }

  /** Start fetching the pages of the indexed `columns` holding any of the
   * row `spans`, so a read of those rows soon after finds them cached or in
   * flight. */
  private prefetch(columns: string[], spans: RowSpan[], signal?: AbortSignal): void {
    if (!spans.length) return
    for (const name of columns)
      if (this.rowTrees.has(name))
        this.columnPages(name, spans, signal)
          .then((pages) => this.pageData(pages, signal))
          .catch(() => {})
  }

  /**
   * Rows whose geometry's bbox intersects `bbox`, with their exact primary
   * geometry as WKB and the named source attributes. The primary directory
   * routes the bbox to the leaves of the tiles holding it, whose runs of
   * geometry rows with boxes reaching it choose the pages; their WKB gives
   * each row's extent, rounded outward to float32 as the covering stores it.
   * Attributes come from the pages the attribute leaves beside the same
   * leaves locate, fetched alongside the geometry; other columns through
   * `readColumns`.
   */
  async readPrimary(
    bbox: Bbox,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<PrimaryRows> {
    signal?.throwIfAborted()
    const located = attributes.filter((a) => this.attributeNames.includes(a))
    const query = cellBox(bbox)
    const depth = this.lod.key!.depth
    const intervals = bboxIntervals(depth, Math.min(depth, this.finestZoom), bbox)
    // the attribute leaves beside each primary leaf reached load with it
    const beside = new Map<number, Promise<SourcePage[]>[]>()
    const leaves = await this.exactLeaves(
      this.exact,
      intervals,
      new Set(),
      signal,
      (shard, place) =>
        beside.set(
          shard[2],
          located.map((name) =>
            awaited(this.slot(this.sourceLeaves, place, name, decodeSourceLeaf, signal)),
          ),
        ),
    )
    const sides = new Map(
      await Promise.all(
        leaves.map(
          async ({ offset }) => [offset, await Promise.all(beside.get(offset) ?? [])] as const,
        ),
      ),
    )
    // each page with a run reaching the query once, with the runs that do; a
    // neighbouring leaf lists the same page with the same runs
    const chosen = new Map<number, { page: PrimaryPage; live: Run[]; offset: number }>()
    for (const { leaf, offset } of leaves)
      for (const page of leaf.pages) {
        if (chosen.has(page.offset)) continue
        const live = page.runs.filter((run) => overlaps(run.box, query))
        if (live.length) chosen.set(page.offset, { page, live, offset })
      }
    const hits = [...chosen.values()].sort((a, b) => a.page.first - b.page.first)
    const pages = hits.map((hit) => hit.page)
    // per attribute, its pages over the live runs' rows, from the attribute
    // leaves beside the leaves that chose them
    const sources = located.map((_, i) => {
      const wanted = new Map<number, SourcePage>()
      for (const { live, offset } of hits)
        for (const page of sides.get(offset)![i])
          if (live.some((r) => page.first < r.first + r.count && page.first + page.count > r.first))
            wanted.set(page.offset, page)
      return [...wanted.values()].sort((a, b) => a.first - b.first)
    })
    const others = attributes.filter((a) => !located.includes(a))
    this.prefetch(
      others,
      hits.flatMap(({ live }) => live),
      signal,
    )
    const [payloads, sourcePayloads] = await Promise.all([
      this.pageData(pages, signal),
      Promise.all(sources.map((list) => this.pageData(list, signal))),
    ])
    signal?.throwIfAborted()
    const out: PrimaryRows = {
      rows: [],
      wkb: [],
      columns: Object.fromEntries(attributes.map((a) => [a, []])),
    }
    visitRuns(
      this.decoders.get(this.primary)!,
      pages.map((page, i) => ({ ...page, bytes: payloads[i] })),
      hits.flatMap(({ live }) => live),
      (row, value) => {
        if (!(value instanceof Uint8Array)) return
        const extent = wkbBounds(value)
        if (!extent || !overlaps(float32Outward(extent), bbox)) return
        out.rows.push(row)
        out.wkb.push(value)
      },
    )
    const taken = spans(out.rows)
    for (const [i, name] of located.entries())
      visitRuns(
        this.decoders.get(name)!,
        sources[i].map((page, j) => ({ ...page, bytes: sourcePayloads[i][j] })),
        taken,
        (_, value) => out.columns[name].push(value),
      )
    if (others.length) Object.assign(out.columns, await this.readColumns(out.rows, others, signal))
    signal?.throwIfAborted()
    return out
  }

  /** The primary directory leaves under `node` holding keys in any of
   * `intervals`. `reached` holds the offsets of the nodes already descended
   * into; a tree reaches each node once. */
  private async exactLeaves(
    node: DirectoryNode,
    intervals: Interval[],
    reached: Set<number>,
    signal?: AbortSignal,
    onLeaf?: (shard: Shard, place: SlotPlace) => void,
  ): Promise<{ leaf: PrimaryLeaf; offset: number }[]> {
    const entries = node.entries as Shard[]
    const shards = entries.filter(([low, high]) =>
      intervals.some(([start, end]) => low < end && high >= start),
    )
    const selected = await Promise.all(
      shards.map(async (shard) => {
        const [, , offset, length, level] = shard
        if (!level) onLeaf?.(shard, slotPlace(entries, shard, Object.keys(this.lods).length))
        const child = level
          ? await this.directory(shard, this.exactSpan, signal)
          : await this.load(this.primaryLeaves, offset, length, decodePrimaryLeaf, signal)
        if (reached.has(offset)) throw new Error('spatial directory node referenced twice')
        reached.add(offset)
        return 'pages' in child
          ? [{ leaf: child, offset }]
          : this.exactLeaves(child, intervals, reached, signal, onLeaf)
      }),
    )
    return selected.flat()
  }

  /** `length` bytes of the file from `offset`, refused before any request
   * unless they lie inside the file. Every directory and page read is one. */
  private read(offset: number, length: number, signal?: AbortSignal): Promise<ArrayBuffer> {
    if (
      !Number.isSafeInteger(offset) ||
      !Number.isSafeInteger(length) ||
      offset < 0 ||
      length <= 0 ||
      offset + length > this.file.byteLength
    )
      return Promise.reject(new Error('spatial index byte range outside the file'))
    return this.file.slice(offset, offset + length, signal)
  }

  /** A node from `cache` or loaded and decoded, read at `length` bytes,
   * which hold the attribute slot framed `frame` when given. */
  private load<T>(
    cache: Map<number, Cached<T>>,
    offset: number,
    length: number,
    decode: (stored: unknown) => T,
    signal?: AbortSignal,
    frame?: SlotFrame,
  ): Promise<T> {
    let cached = cache.get(offset)
    if (!cached || cached.node.abandoned) {
      const current: Cached<T> = {
        length,
        frame,
        node: new SharedLoad(async (signal) => {
          const bytes = await this.read(offset, length, signal)
          return decode(await gunzipJson(frame ? unslot(bytes, frame) : bytes))
        }),
      }
      while (cache.size >= 128) cache.delete(cache.keys().next().value!)
      cache.set(offset, current)
      current.node.promise.catch(() => {
        if (cache.get(offset) === current) cache.delete(offset)
      })
      cached = current
    } else {
      cache.delete(offset)
      cache.set(offset, cached)
    }
    if (
      cached.length !== length ||
      cached.frame?.attribute !== frame?.attribute ||
      cached.frame?.leaf !== frame?.leaf
    )
      return Promise.reject(new Error('spatial directory node disagrees with its parent entry'))
    return cached.node.get(signal)
  }

  /** The node a parent's entry locates, which must match that entry's
   * length, level and cell bounds, from cache or loaded, kept with `place`,
   * where the slots beside it lie, which an LOD leaf of a file with
   * attributes must have. */
  private async directory(
    shard: Shard,
    span: number,
    signal?: AbortSignal,
    place?: SlotPlace,
  ): Promise<DirectoryNode> {
    const [low, high, offset, length, level] = shard
    if (!level && !place && this.attributeNames.length)
      throw new Error('an LOD leaf loads without where its slots lie')
    const loading = this.load(
      this.directories,
      offset,
      length,
      (stored) => decodeNode(stored, span),
      signal,
    )
    if (place) this.directories.get(offset)!.place = place
    const node = await loading
    const entries: (Page | Shard)[] = node.entries
    if (
      node.level !== level ||
      !entries.length ||
      entries[0][0] !== low ||
      entries.at(-1)![1] !== high
    )
      throw new Error('spatial directory node disagrees with its parent entry')
    return node
  }

  /** The pages under `node` of LOD tree `tree` holding keys in [start,
   * end), plus, when `hosted`, the first page whose maximum key reaches
   * `start`, or the last page if none does, in key order. `reached` holds
   * the offsets of the nodes already descended into; a tree reaches each
   * node once. The leaves reached are kept with where their slots lie. */
  private async lookup(
    node: DirectoryNode,
    tree: number,
    start: number,
    end: number,
    hosted: boolean,
    span: number,
    reached: Set<number>,
    signal?: AbortSignal,
    onLeaves?: (leaves: [Shard, SlotPlace][]) => void,
  ): Promise<Page[]> {
    const entries: (Page | Shard)[] = node.entries
    const host = hosted ? (entries.find((e) => e[1] >= start) ?? entries.at(-1)) : undefined
    const hits = entries.filter((e) => (e[0] < end && e[1] >= start) || e === host)
    if (!node.level) return hits as Page[]
    const shards = hits as Shard[]
    const places =
      node.level === 1 ? shards.map((shard) => slotPlace(entries as Shard[], shard, tree)) : []
    if (node.level === 1) onLeaves?.(shards.map((shard, i) => [shard, places[i]]))
    const selected = await Promise.all(
      shards.map(async (shard, i) => {
        const child = await this.directory(shard, span, signal, places[i])
        if (reached.has(shard[2])) throw new Error('spatial directory node referenced twice')
        reached.add(shard[2])
        return this.lookup(child, tree, start, end, shard === host, span, reached, signal, onLeaves)
      }),
    )
    return selected.flat()
  }

  /** The bytes of each of `pages`, which may belong to any column, from the
   * compressed page cache or fetched; pages adjacent in the file share one
   * range. */
  private pageData(
    pages: { offset: number; length: number }[],
    signal?: AbortSignal,
  ): Promise<ArrayBuffer[]> {
    signal?.throwIfAborted()
    const runs: { offset: number; length: number }[][] = []
    for (const p of [...pages].sort((a, b) => a.offset - b.offset)) {
      const cached = this.pages.get(p.offset)
      if (cached && !cached.data.abandoned) continue
      if (cached) {
        this.pages.delete(p.offset)
        this.pageBytes -= cached.size
      }
      const last = runs.at(-1)?.at(-1)
      if (last && last.offset + last.length === p.offset) runs.at(-1)!.push(p)
      else if (!last || last.offset !== p.offset) runs.push([p])
    }
    const wanted = new Map<number, Promise<ArrayBuffer>>()
    for (const p of pages) {
      const cached = this.pages.get(p.offset)
      if (cached) {
        this.pages.delete(p.offset)
        this.pages.set(p.offset, cached)
        wanted.set(p.offset, cached.data.get(signal))
      }
    }
    for (const run of runs) {
      const start = run[0].offset,
        last = run.at(-1)!
      // Each page subscribes to the run; the last departing page aborts I/O.
      const fetched = new SharedLoad((signal) =>
        this.read(start, last.offset + last.length - start, signal),
      )
      for (const p of run) {
        const data = new SharedLoad((signal) =>
          fetched
            .get(signal)
            .then((bytes) => bytes.slice(p.offset - start, p.offset - start + p.length)),
        )
        wanted.set(p.offset, data.get(signal))
        while (this.pages.size && this.pageBytes + p.length > 16 * 1024 * 1024) {
          const oldest = this.pages.keys().next().value!
          this.pageBytes -= this.pages.get(oldest)!.size
          this.pages.delete(oldest)
        }
        if (p.length <= 16 * 1024 * 1024) {
          this.pages.set(p.offset, { size: p.length, data })
          this.pageBytes += p.length
          data.promise.catch(() => {
            if (this.pages.get(p.offset)?.data === data) {
              this.pages.delete(p.offset)
              this.pageBytes -= p.length
            }
          })
        }
      }
    }
    return Promise.all(pages.map((p) => wanted.get(p.offset)!))
  }

  async readLodByTile(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    attributes: string[] = [],
    signal?: AbortSignal,
  ): Promise<LodRows> {
    const named = attributes.filter((a) => this.attributeNames.includes(a))
    const source = attributes.filter((a) => !named.includes(a))
    const prepared = await this.prepareTile(lod, z, x, y, signal, source, named)
    const { rows, columns: values, geometry } = decodeTile(prepared, named, signal)
    if (source.length) Object.assign(values, await this.readColumns(rows, source, signal))
    return { rows, columns: values, geometry }
  }

  /** The pages of a tile, ready to decode. The pages of the indexed
   * `columns` over the tile's own rows start to load alongside, for a
   * `readColumns` of its rows once it is decoded. The pages of the
   * `attributes` over its own rows are fetched with the overview pages. */
  async prepareTile(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    signal?: AbortSignal,
    columns: string[] = [],
    attributes: string[] = [],
  ): Promise<PreparedTile> {
    const found = await this.locate(lod, z, x, y, attributes, signal)
    const { pages, firstCell, endCell, start, own } = found
    const hostPage = pages.find((p) => p[1] >= start) ?? pages.at(-1)
    let hostRow = -1
    if (hostPage) {
      const fences = hostPage[6]
      const fence = fences.findIndex((value, i) => i % 2 === 0 && value >= firstCell)
      hostRow =
        hostPage[4] +
        (fence >= 0 && fences[fence + 1] < (hostPage[7] ?? hostPage[5])
          ? fences[fence + 1]
          : (hostPage[7] ?? hostPage[5]) - 1)
    }
    if (columns.length) this.prefetch(columns, own, signal)
    const [payloads, located] = await Promise.all([
      this.pageData(
        pages.map((p) => ({ offset: p[2], length: p[3] })),
        signal,
      ),
      this.attributePayloads(found.attributes, signal),
    ])
    signal?.throwIfAborted()
    return {
      lod,
      z,
      x,
      y,
      fields: this.fields,
      codec: this.root.codecs[lod.name],
      pages,
      firstCell,
      endCell,
      hostRow,
      payloads,
      margin: this.lod.margin,
      attributes: located,
    }
  }

  /** The pages of the `attributes` over the own rows of a tile, for
   * filling a decoded tile's attributes a later style asks for. */
  async prepareAttributes(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    attributes: string[],
    signal?: AbortSignal,
  ): Promise<AttributePages[]> {
    const found = await this.locate(lod, z, x, y, attributes, signal)
    return this.attributePayloads(found.attributes, signal)
  }

  /** A tile's overview pages and own rows from the LOD's directory, and the
   * pages of the `attributes` over those rows, from the attribute
   * leaves beside the leaves the tile reaches, which load with them. */
  private async locate(
    lod: Lod,
    z: number,
    x: number,
    y: number,
    attributes: string[],
    signal?: AbortSignal,
  ) {
    signal?.throwIfAborted()
    // Directory bounds and fences are whole cells of the LOD's finest zoom, so
    // they are exact only for tiles made of whole cells.
    if (z > Math.max(...lod.zooms)) throw new Error(`zoom ${z} is finer than ${lod.name}`)
    for (const name of attributes)
      if (!this.attributeNames.includes(name)) throw new Error(`${name} is not an attribute`)
    const [start, end] = tileInterval(this.lod.key!.depth, z, x, y)
    const tree = this.trees[lod.name]
    if (!tree) throw new Error(`missing spatial directory for ${lod.name}`)
    const span = this.cellSpan(lod)
    const treeIndex = Object.keys(this.lods).indexOf(lod.name)
    if (attributes.length) this.spreadAttributeLeaves(attributes)
    // the attribute leaves beside each LOD leaf reached, which load with it
    const reachedLeaves: [Shard, SlotPlace][] = []
    const beside = new Map<number, Promise<ByteRange[]>[]>()
    const onLeaves = (leaves: [Shard, SlotPlace][]) => {
      if (!attributes.length) return
      for (const [shard, place] of leaves) {
        reachedLeaves.push([shard, place])
        beside.set(
          shard[2],
          attributes.map((name) =>
            awaited(this.slot(this.attributeLeaves, place, name, decodeAttributeLeaf, signal)),
          ),
        )
      }
    }
    const pages = await this.lookup(
      tree,
      treeIndex,
      start,
      end,
      true,
      span,
      new Set(),
      signal,
      onLeaves,
    )
    const firstCell = start / span,
      endCell = end / span
    const own = ownRows(pages, firstCell, endCell)
    // each page's leaf and position in it, for the attribute pages beside it,
    // by the page's byte offset: a leaf evicted and loaded again decodes to
    // other objects
    const position = new Map<number, { leaf: number; index: number; pages: number }>()
    for (const [shard, place] of reachedLeaves) {
      const leaf = await this.directory(shard, span, signal, place)
      leaf.entries.forEach((page, index) =>
        position.set((page as Page)[2], { leaf: shard[2], index, pages: leaf.entries.length }),
      )
    }
    const sides = new Map<number, ByteRange[][]>()
    for (const [offset, loads] of beside) sides.set(offset, await Promise.all(loads))
    signal?.throwIfAborted()
    const meets = meeting(pages, (p) => [p[4], p[4] + p[5]], own)
    return {
      pages,
      firstCell,
      endCell,
      start,
      own,
      attributes: attributes.map((name, i) => {
        const field = this.fields.find((f) => f.name === name)!
        const located = meets.map((page) => {
          const at = position.get(page[2])
          const leaf = at && sides.get(at.leaf)?.[i]
          // an attribute leaf entry stands for the overview page at its
          // position, so a leaf of another length would misplace values
          if (!at || !leaf || leaf.length !== at.pages)
            throw new Error(`${name} leaf disagrees with the ${lod.name} leaf`)
          return { ...leaf[at.index], first: page[4], count: page[5] }
        })
        return {
          name,
          codec: this.root.attribute_codec!,
          type: field.type!,
          pages: located,
          payloads: [],
        }
      }),
    }
  }

  /** Start loading the leaves of `attributes` beside every LOD leaf
   * the reader holds, in the round of the tile asking, so a later tile over a
   * held leaf, such as one zoomed back out to, needs no round of its own. */
  private spreadAttributeLeaves(attributes: string[]): void {
    for (const { place } of this.directories.values())
      if (place)
        for (const name of attributes)
          awaited(this.slot(this.attributeLeaves, place, name, decodeAttributeLeaf))
  }

  /** Attribute `name`'s leaf from `cache` or loaded from its slot beside
   * the leaf at `place`. A level-one node's slot block follows its last
   * leaf: per attribute in metadata order, a slot per leaf in
   * leaf order. */
  private slot<T>(
    cache: Map<number, Cached<T>>,
    place: SlotPlace,
    name: string,
    decode: (stored: unknown) => T,
    signal?: AbortSignal,
  ): Promise<T> {
    const size = this.root.attribute_slots![place.tree]
    const attribute = this.attributeNames.indexOf(name)
    const offset = place.end + (attribute * place.count + place.index) * size
    return this.load(cache, offset, size, decode, signal, { attribute, leaf: place.leaf })
  }

  /** `located` with each one's page bytes, none for an empty page. */
  private async attributePayloads(
    located: AttributePages[],
    signal?: AbortSignal,
  ): Promise<AttributePages[]> {
    const payloads = await Promise.all(
      located.map((entry) =>
        this.pageData(
          entry.pages.filter((page) => page.length),
          signal,
        ),
      ),
    )
    return located.map((entry, i) => {
      const held = payloads[i][Symbol.iterator]()
      return {
        ...entry,
        payloads: entry.pages.map((page) =>
          page.length ? held.next().value! : new ArrayBuffer(0),
        ),
      }
    })
  }
}

/** An attribute leaf beside a primary leaf: level 0, then the source
 * column's pages over the leaf's runs, as `decodeSources` reads them, in row
 * order with rows that may skip between them. */
function decodeSourceLeaf(stored: unknown): SourcePage[] {
  if (!Array.isArray(stored) || stored.length !== 5 || stored[0] !== 0)
    throw new Error('malformed attribute leaf')
  return decodeSources(stored.slice(1))
}

/** An attribute leaf beside an LOD leaf: level 0, then the byte ranges of
 * the attribute pages beside the leaf's pages, one each and in order, of
 * length 0 beside a page whose rows hold no value of the attribute. */
function decodeAttributeLeaf(stored: unknown): ByteRange[] {
  if (!Array.isArray(stored) || stored.length !== 3 || stored[0] !== 0)
    throw new Error('malformed attribute leaf')
  return decodeRanges(stored.slice(1), true)
}

/** The rows of `pages` with a key in cells `firstCell` up to `endCell`, from
 * their fences, as ascending runs. */
function ownRows(pages: Page[], firstCell: number, endCell: number): RowSpan[] {
  const out: RowSpan[] = []
  for (const [, , , , first, count, fences, valid = count] of pages)
    for (let i = 0; i < fences.length; i += 2) {
      if (fences[i] < firstCell || fences[i] >= endCell) continue
      const start = first + fences[i + 1],
        end = first + Math.min(i + 3 < fences.length ? fences[i + 3] : valid, valid)
      if (end <= start) continue
      const last = out.at(-1)
      if (last && last.first + last.count === start) last.count += end - start
      else if (!last || last.first + last.count < start)
        out.push({ first: start, count: end - start })
    }
  return out
}

export interface PreparedTile {
  lod: Lod
  z: number
  x: number
  y: number
  fields: AttributeField[]
  codec: string
  pages: Page[]
  firstCell: number
  endCell: number
  hostRow: number
  payloads: ArrayBuffer[]
  margin: number
  /** Pages of the attributes requested, over the own rows */
  attributes?: AttributePages[]
}

/** A reader of an attribute's values for ascending rows, from pages
 * that hold every row asked for. A page holds its values on its leading
 * rows: the value of the row at position `rank` among those its LOD page
 * shows is on the page's row `rank`. */
function attributeValues(entry: AttributePages, field: AttributeField) {
  let p = 0,
    i = 0
  const decoded: { rows: Int32Array; values: ArrayLike<unknown> }[] = []
  const page = (q: number) =>
    (decoded[q] ??= decodeAttributePage(
      entry.payloads[q],
      entry.codec,
      entry.pages[q].count,
      entry.type,
      !!field.values,
    ))
  return {
    value(row: number, rank: number): unknown {
      while (p < entry.pages.length && entry.pages[p].first + entry.pages[p].count <= row) {
        p++
        i = 0
      }
      const at = entry.pages[p]
      if (!at || row < at.first) throw new Error(`no ${entry.name} page holds row ${row}`)
      if (!at.length) return null
      const present = page(p)
      while (i < present.rows.length && present.rows[i] < rank) i++
      return i < present.rows.length && present.rows[i] === rank
        ? asColumnValue(present.values[i], field)
        : null
    },
    /** Raise unless every page of the attribute holds its values within the
     * rows its LOD page shows: `shown` lists each LOD page's first row and
     * how many rows it shows. A value past them would be no row's. */
    check(shown: number[]): void {
      const counts = new Map<number, number>()
      for (let k = 0; k < shown.length; k += 2) counts.set(shown[k], shown[k + 1])
      entry.pages.forEach((at, q) => {
        if (!at.length) return
        const count = counts.get(at.first)
        if (count === undefined)
          throw new Error(`${entry.name} has a page beside no page of the tile`)
        const { rows } = page(q)
        if (rows.length && rows[rows.length - 1] >= count)
          throw new Error(`${entry.name} holds a value past the rows its page shows`)
      })
    },
  }
}

/** The values of each attribute in `task` for its rows, in order. */
export function decodeAttributes(task: AttributeTask): Record<string, unknown[]> {
  return Object.fromEntries(
    task.attributes.map((entry) => {
      const field = task.fields.find((f) => f.name === entry.name)
      if (!field) throw new Error(`${entry.name} is not an attribute`)
      const reader = attributeValues(entry, field)
      const values = task.rows.map((row, k) => reader.value(row, task.ranks[k]))
      reader.check(task.shown)
      return [entry.name, values]
    }),
  )
}

/** Pure CPU stage: no reader, cache, or network access is needed in a tile
 * worker. A requested attribute whose pages the task lacks is
 * `pending`: it has no entry in `columns`, and `copies` holds the values its
 * boundary copies pack, in feature order. `own` is 0 for a boundary copy and,
 * for a feature of the tile's own, one more than its position among the rows
 * its LOD page shows, where an attribute page holds its value. `shown` lists
 * each LOD page's first row and how many rows it shows. */
export function decodeTile(
  task: PreparedTile,
  attributes: string[],
  signal?: AbortSignal,
): LodRows & {
  own: Uint32Array
  pending: string[]
  copies: Record<string, unknown[]>
  shown: number[]
} {
  signal?.throwIfAborted()
  const { lod, z, x, y, fields, codec, pages, firstCell, endCell, hostRow, payloads } = task
  const wanted = attributes.map((a) => fields.findIndex((f) => f.name === a))
  for (const [i, name] of attributes.entries())
    if (wanted[i] < 0) throw new Error(`${name} is not an attribute`)
  const rows: number[] = []
  const own: number[] = []
  const builder = new GeometryBuilder()
  const tile = z * 2 ** 48 + x * 2 ** z + y
  const filled = attributes.map((a, i) => {
    const entry = task.attributes?.find((s) => s.name === a)
    return entry && attributeValues(entry, fields[wanted[i]])
  })
  const isPending = attributes.map((_, i) => !filled[i])
  const pending = attributes.filter((_, i) => isPending[i])
  const held = attributes.flatMap((_, i) => (isPending[i] ? [] : [i]))
  const sparse = attributes.flatMap((_, i) => (isPending[i] ? [i] : []))
  const values: unknown[][] = attributes.map(() => [])
  // the position of the row being decoded among those its page shows
  let rank = 0
  const add = (id: number, attrs: unknown[], slot: number) => {
    own.push(slot ? 0 : rank + 1)
    rows.push(id)
    for (const i of held) {
      const reader = filled[i]
      values[i].push(reader && !slot ? reader.value(id, rank) : attrs[wanted[i]])
    }
    if (slot) for (const i of sparse) values[i].push(attrs[wanted[i]])
  }
  const shownRows: number[] = []
  for (let i = 0; i < pages.length; i++) {
    const page = pages[i]
    let fence = 0
    let shown = 0
    visitPlainPage(payloads[i], codec, page[5], (relative, value) => {
      const row = page[4] + relative
      rank = shown
      if (showsRow(value, lod.geometry_type)) shown++
      while (fence + 2 < page[6].length && page[6][fence + 3] <= relative) fence += 2
      const cell = page[6][fence]
      const inside = cell >= firstCell && cell < endCell
      if (!inside && row !== hostRow) return
      decodeSelectedRow(
        value,
        lod.geometry_type,
        fields,
        builder,
        inside,
        row,
        tile,
        add,
        wanted.length > 0,
      )
    })
    shownRows.push(page[4], shown)
  }
  for (const reader of filled) reader?.check(shownRows)
  return {
    rows,
    columns: Object.fromEntries(held.map((i) => [attributes[i], values[i]])),
    geometry: builder.finish(lod.scale, lod.offset),
    own: Uint32Array.from(own),
    shown: shownRows,
    pending,
    copies: Object.fromEntries(sparse.map((i) => [attributes[i], values[i]])),
  }
}
