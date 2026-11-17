/**
 * Worker-backed Mapbox Vector Tiles for any map library. `tileUrl(config)`
 * is a `{z}/{x}/{y}` template; `fetchTile(url)` returns one expanded tile's
 * MVT bytes, and `registerProtocol(maplibregl)` wires it into MapLibre.
 * `vectorSource(config)` is that template as a MapLibre source with the
 * file's zoom limits and bounds. Tiles are rendered in a worker so page
 * scrolling stays smooth; `prefetch(url)` opens the file before the map exists.
 */
import { sourceLimits, type SourceLimits } from './source-limits.js'
import type { TileOptions } from './tile.js'
import type { Bbox, LodMetadata } from './reader.js'

export const PROTOCOL = 'lod'

export interface SourceConfig {
  url: string
  layer?: string
  pointLayer?: string
  /** Attribute or column names in the tiles (default none: geometry only). */
  properties?: string[]
}

export const tileUrl = (config: SourceConfig): string =>
  `${PROTOCOL}://tile/${encode(config)}/{z}/{x}/{y}.pbf`

export const parseTileUrl = (
  url: string,
): { config: SourceConfig; z: number; x: number; y: number } => {
  const match = new RegExp(`^${PROTOCOL}://tile/([A-Za-z0-9_-]+)/(\\d+)/(\\d+)/(\\d+)\\.pbf$`).exec(
    url,
  )
  if (!match) throw new Error(`invalid ${PROTOCOL} tile url: ${url}`)
  const [z, x, y] = match.slice(2).map(Number)
  return { config: decode(match[1]), z, x, y }
}

const encode = (value: unknown): string => {
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  let binary = ''
  for (let i = 0; i < bytes.length; i += 8192)
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192))
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

const decode = <T = SourceConfig>(token: string): T => {
  const binary = atob(token.replace(/-/g, '+').replace(/_/g, '/'))
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0))))
}

/** The requests a URL has answered and the body bytes those answers declared. */
export interface Transfer {
  requests: number
  bytes: number
}

/** What the worker waits on, as ages in ms. */
export interface Waiting {
  /** Range reads in flight. */
  reads: number[]
  /** Requests the server has yet to answer. */
  unanswered: number[]
  /** Tile jobs in a helper worker. */
  jobs: number[]
  /** How many jobs wait for a helper. */
  queued: number
}

export type WorkerRequest =
  | { id: number; type: 'tile'; config: SourceConfig; z: number; x: number; y: number }
  | { id: number; type: 'row'; url: string; rowIndex: number; columns: string[] }
  | { id: number; type: 'stats' }
  | { id: number; type: 'open'; url: string }
  | { id: number; type: 'cancel' }
  | {
      id: number
      type: 'configure'
      workers: number
      cache?: RequestCache
      rangeParam?: string
    }
export type WorkerResponse =
  | { id: number; ok: true; configured: true }
  | { id: number; ok: true; data: ArrayBuffer }
  | { id: number; ok: true; row: Record<string, unknown> }
  | {
      id: number
      ok: true
      stats: Record<string, Transfer>
      parts?: Record<string, Record<string, number>>
      cache?: { bytes: number; tiles: number; decodes: number; projections: number }
      waiting: Waiting
    }
  | {
      id: number
      ok: true
      opened: true
      metadata: { lod: LodMetadata | undefined; bounds: Bbox; columns: string[] }
    }
  | { id: number; ok: false; error: string; name: string }

let worker: Worker | undefined
let workerCount = Math.min(2, Math.max(1, (globalThis.navigator?.hardwareConcurrency ?? 4) - 1))
/** Set 1–4 tile CPU workers before prefetch, readMetadata, or registerProtocol.
 * One disables the pool. Indexed sources share one I/O coordinator in all modes.
 */
export const setWorkerCount = (count: number): void => {
  if (!Number.isInteger(count) || count < 1 || count > 4) throw new Error('Expected 1–4 workers')
  if (worker) throw new Error('Set worker count before opening a source')
  workerCount = count
}
let sourceOptions: { cache?: RequestCache; rangeParam?: string } = {}
/** Set how files are requested before prefetch, readMetadata, or
 * registerProtocol. `cache` is the cache mode of every request:
 * `no-store` sends a tile's requests side by side, where Chrome's HTTP cache
 * answers the range requests of one URL one at a time. `rangeParam` names a
 * query parameter that carries each request's range, so every range has a URL
 * of its own, which the cache holds for a later visit and has in flight beside
 * the others; requests then go through the cache unless `cache` names a mode.
 */
export const setSourceOptions = (options: { cache?: RequestCache; rangeParam?: string }): void => {
  if (options.rangeParam === '') throw new Error('rangeParam must name a parameter')
  if (worker) throw new Error('Set source options before opening a source')
  sourceOptions = { ...options }
}
let nextId = 0
const pending = new Map<
  number,
  { resolve: (r: WorkerResponse) => void; reject: (e: Error) => void }
>()

const getWorker = (): Worker => {
  if (worker) return worker
  worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' })
  worker.postMessage({
    id: 0,
    type: 'configure',
    workers: workerCount,
    ...sourceOptions,
  } satisfies WorkerRequest)
  const current = worker
  const fail = (event: ErrorEvent | MessageEvent) => {
    if (worker !== current) return
    const error = new Error('message' in event ? event.message : 'Worker transport failed')
    for (const request of pending.values()) request.reject(error)
    pending.clear()
    current.terminate()
    worker = undefined
  }
  worker.addEventListener('error', fail)
  worker.addEventListener('messageerror', fail)
  worker.addEventListener('message', (event: MessageEvent<WorkerResponse>) => {
    const response = event.data
    const request = pending.get(response.id)
    if (!request) return
    pending.delete(response.id)
    if (response.ok) request.resolve(response)
    else {
      const error = new Error(response.error)
      error.name = response.name
      request.reject(error)
    }
  })
  return worker
}

type Request = WorkerRequest extends infer R ? (R extends unknown ? Omit<R, 'id'> : never) : never

const call = (request: Request, signal?: AbortSignal): Promise<WorkerResponse> =>
  new Promise((resolve, reject) => {
    const id = ++nextId
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const abort = () => {
      if (!pending.delete(id)) return
      worker?.postMessage({ id, type: 'cancel' } satisfies WorkerRequest)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const cleanup = () => signal?.removeEventListener('abort', abort)
    pending.set(id, {
      resolve: (response) => {
        cleanup()
        resolve(response)
      },
      reject: (error) => {
        cleanup()
        reject(error)
      },
    })
    signal?.addEventListener('abort', abort, { once: true })
    try {
      getWorker().postMessage({ ...request, id } as WorkerRequest)
    } catch (error) {
      pending.delete(id)
      cleanup()
      reject(error)
    }
  })

/** Values of flat parquet columns for one feature, by its MVT feature id (the file row). */
export const readRow = async (url: string, rowIndex: number, columns: string[]) => {
  const response = await call({ type: 'row', url, rowIndex, columns })
  return 'row' in response ? response.row : {}
}

/** Requests the worker has had answered and the bytes they declared, by URL. */
export const readStats = async (): Promise<Record<string, Transfer>> => (await readCounts()).stats

/** The transfers that `readStats` returns, with `parts` from the same worker
 * reply: for each indexed file, by URL, the bytes that its reader has read,
 * grouped by what they contain. The groups are `index`, `shapes`,
 * `column:<name>` for an attribute's pages and `table:<name>` for a source
 * column's pages. A request's bytes are counted when its response begins and
 * a part's bytes when they have arrived. Parts leave out the bytes between
 * nearby ranges that were merged into one request, so a file's parts add up
 * to no more than its bytes. */
export const readCounts = async (): Promise<{
  stats: Record<string, Transfer>
  parts: Record<string, Record<string, number>>
}> => {
  const response = await call({ type: 'stats' })
  return {
    stats: 'stats' in response ? response.stats : {},
    parts: ('parts' in response && response.parts) || {},
  }
}

/** What the worker is waiting on: a tile that never arrives shows here as a
 * read, a request or a job that never ends. */
export const readWaiting = async (): Promise<Waiting> => {
  const response = await call({ type: 'stats' })
  if (!('waiting' in response)) throw new Error('the worker sent no stats')
  return response.waiting
}

/**
 * Start the worker and open `url` now, so the spatial bootstrap is ready
 * by the time the map asks for a tile. Call it as early as the page can,
 * before the map is constructed.
 */
export const prefetch = (url: string): void => {
  void call({ type: 'open', url }).catch(() => undefined)
}

/** Map metadata from the same worker/reader that will serve tiles. */
export const readMetadata = async (
  url: string,
): Promise<{ lod: LodMetadata | undefined; bounds: Bbox; columns: string[] }> => {
  const response = await call({ type: 'open', url })
  if (!('metadata' in response)) throw new Error('worker returned no map metadata')
  return response.metadata
}

export interface VectorSource extends SourceLimits {
  type: 'vector'
  tiles: [string]
}

/**
 * A MapLibre vector source for the file, with the zoom limits and bounds its
 * metadata gives: `map.addSource(id, await vectorSource(config))`. The map
 * overzooms the finest LOD past `maxzoom`, the finest LOD zoom unless
 * `options.maxzoom` sets it. A higher one draws tiles of exact primary
 * geometry from the finest LOD zoom up to it; a file without LODs draws them
 * at every zoom and defaults to 22. Opens the file in the worker that serves
 * the tiles.
 */
export const vectorSource = async (
  config: SourceConfig,
  options: { maxzoom?: number } = {},
): Promise<VectorSource> => ({
  type: 'vector',
  tiles: [tileUrl(config)],
  ...sourceLimits(await readMetadata(config.url), options.maxzoom),
})

/**
 * A URL that names the file as a MapLibre source, for a map with the protocol
 * registered: `map.addSource(id, { type: 'vector', url: sourceUrl(config) })`,
 * or the same object in the map's style. MapLibre asks the protocol for the
 * source as it is added, which opens the file and answers with what
 * `vectorSource(config, options)` gives, so nothing is awaited and the file
 * opens while the map starts.
 */
export const sourceUrl = (config: SourceConfig, options: { maxzoom?: number } = {}): string =>
  `${PROTOCOL}://source/${encode({ config, maxzoom: options.maxzoom })}`

const SOURCE_URL = new RegExp(`^${PROTOCOL}://source/([A-Za-z0-9_-]+)$`)

/**
 * The Mapbox Vector Tile for one expanded `tileUrl`, rendered in the worker.
 * Any map library that lets you supply a tile's bytes can call this from its
 * tile loader. An empty tile is an empty buffer.
 */
export const fetchTile = async (url: string, signal?: AbortSignal): Promise<ArrayBuffer> => {
  const { config, z, x, y } = parseTileUrl(url)
  const response = await call({ type: 'tile', config, z, x, y }, signal)
  return 'data' in response ? response.data : new ArrayBuffer(0)
}

/** Register the `lod://` tile scheme once per MapLibre module instance. */
export const registerProtocol = (maplibre: {
  addProtocol: (
    scheme: string,
    action: (
      params: { url: string },
      controller: AbortController,
    ) => Promise<{ data: ArrayBuffer | Omit<VectorSource, 'type'> }>,
  ) => void
}) => {
  maplibre.addProtocol(PROTOCOL, async (params, controller) => {
    const source = SOURCE_URL.exec(params.url)
    if (!source) return { data: await fetchTile(params.url, controller.signal) }
    const { config, maxzoom } = decode<{ config: SourceConfig; maxzoom?: number }>(source[1])
    const { type: _type, ...described } = await vectorSource(config, { maxzoom })
    return { data: described }
  })
}

export type { TileOptions }
