import { VisibleTileCache } from './visible-cache.js'
import { zstdReady } from './compressors.js'
import { decodeProjection } from './projection.js'
/// <reference lib="webworker" />
import type { Transfer, WorkerRequest, WorkerResponse } from './tiles.js'
import { coalescing, httpSource, s3Mirrors } from './range-buffer.js'
import { decodeAttributes, openReader, SpatialReader, type TileReader } from './spatial.js'
import {
  attributeTask,
  decodePreparedTile,
  filledColumns,
  renderDecodedTile,
  renderTile,
} from './tile.js'
import { attributeFields } from './decode.js'
import { TilePool, geometryTransfers } from './tile-pool.js'
import type { CpuRequest, TileReply } from './tile-protocol.js'
import type { RangeSource } from './reader.js'

const scope = self as unknown as DedicatedWorkerGlobalScope
const visible = new VisibleTileCache()
let geometryDecodes = 0,
  projectionJobs = 0
const readers = new Map<string, Promise<TileReader>>()
/** The readers that have opened, for what each has read by part. */
const opened = new Map<string, TileReader>()
let workerCount = 2
let sourceOptions: { cache?: RequestCache; rangeParam?: string } = {}
let pool: Promise<TilePool | undefined> | undefined
let helpers: TilePool | undefined
let started = false
// A bundler may emit this worker as a classic script, and only a module worker
// refuses importScripts, so helpers start the way this worker was started.
const workerType: WorkerType = (() => {
  try {
    scope.importScripts()
    return 'classic'
  } catch {
    return 'module'
  }
})()
/** The helper pool once ready, or none with one worker or when the helpers
 * cannot start; without one this worker does the CPU work itself. */
const getPool = (): Promise<TilePool | undefined> => {
  if (workerCount === 1) return Promise.resolve(undefined)
  return (pool ??= (async () => {
    try {
      const candidate = new TilePool(workerCount, scope.location.href, workerType)
      await candidate.ready
      helpers = candidate
      return candidate
    } catch {
      return undefined
    }
  })())
}
const stats: Record<string, Transfer> = {}
/** A request's URL without the range parameter, so a file's requests are
 * counted together. */
const fileUrl = (url: string): string => {
  const name = sourceOptions.rangeParam
  if (name === undefined) return url
  const param = `${encodeURIComponent(name)}=`
  const hash = url.indexOf('#')
  const end = hash < 0 ? url.length : hash
  const at = url.lastIndexOf(param, end)
  const separated = at > 0 && (url[at - 1] === '?' || url[at - 1] === '&')
  return separated && /^\d+-\d+$/.test(url.slice(at + param.length, end))
    ? url.slice(0, at - 1) + url.slice(end)
    : url
}

/** When each request the server has yet to answer was sent. */
const unanswered = new Set<{ began: number }>()
const fetched = scope.fetch.bind(scope)
scope.fetch = async (input, init) => {
  const sent = { began: performance.now() }
  unanswered.add(sent)
  const response = await fetched(input, init).finally(() => unanswered.delete(sent))
  const key = fileUrl(String(input instanceof Request ? input.url : input))
  const entry = (stats[key] ??= { requests: 0, bytes: 0 })
  entry.requests++
  if (init?.method !== 'HEAD') entry.bytes += Number(response.headers.get('content-length') ?? 0)
  return response
}
const ages = (began: Iterable<{ began: number }>): number[] =>
  Array.from(began, (entry) => Math.round(performance.now() - entry.began))
const controllers = new Map<number, AbortController>()
/** When each range read in flight began. */
const reads = new Set<{ began: number }>()
const timed = (file: RangeSource): RangeSource => ({
  byteLength: file.byteLength,
  slice(start, end, signal) {
    const read = { began: performance.now() }
    reads.add(read)
    return file.slice(start, end, signal).finally(() => reads.delete(read))
  },
})

const getReader = (url: string): Promise<TileReader> => {
  let reader = readers.get(url)
  if (!reader) {
    // Batch neighbouring tile messages and directory completions across microtasks.
    // Cancelled slices are removed before this short window dispatches its ranges.
    // through the cache, hostnames stand in for the URLs a range parameter gives
    const mirrored =
      sourceOptions.rangeParam === undefined && (sourceOptions.cache ?? 'no-store') !== 'no-store'
    reader = httpSource(mirrored ? s3Mirrors(url) : [url], sourceOptions).then((file) =>
      openReader(coalescing(timed(file), { windowMs: 1 })),
    )
    readers.set(url, reader)
    reader.then(
      (open) => opened.set(url, open),
      () => readers.delete(url),
    )
  }
  return reader
}

scope.addEventListener('message', (event: MessageEvent<WorkerRequest | CpuRequest>) => {
  const request = event.data
  // CPU workers run this same script, so the pool adds no second JS bundle.
  // They receive prepared pages or decoded geometry, never their own reader.
  if (request.type === 'cpu-init') {
    zstdReady.then(() => scope.postMessage({ ready: true } satisfies TileReply))
    return
  }
  if (
    request.type === 'decode' ||
    request.type === 'encode' ||
    request.type === 'project' ||
    request.type === 'attributes'
  ) {
    try {
      if (request.type === 'project') {
        scope.postMessage({
          id: request.id,
          values: decodeProjection(request.task),
        } satisfies TileReply)
      } else if (request.type === 'attributes') {
        scope.postMessage({
          id: request.id,
          columns: decodeAttributes(request.task),
        } satisfies TileReply)
      } else if (request.type === 'decode') {
        const tile = decodePreparedTile(request.task)
        const data = request.options ? renderDecodedTile(tile, request.options) : undefined
        scope.postMessage({ id: request.id, tile, data } satisfies TileReply, [
          ...geometryTransfers(tile),
          ...(data ? [data] : []),
        ])
      } else {
        const data = renderDecodedTile(request.tile, request.options)
        scope.postMessage({ id: request.id, data } satisfies TileReply, [data])
      }
    } catch (error) {
      scope.postMessage({
        id: request.id,
        error: String(error),
        name: (error as Error).name ?? 'Error',
      } satisfies TileReply)
    }
    return
  }
  if (request.type === 'configure') {
    if (
      started ||
      !Number.isInteger(request.workers) ||
      request.workers < 1 ||
      request.workers > 4 ||
      request.rangeParam === ''
    ) {
      scope.postMessage({
        id: request.id,
        ok: false,
        name: 'Error',
        error: 'Configure 1–4 workers and a named range parameter before opening a source',
      } satisfies WorkerResponse)
    } else {
      workerCount = request.workers
      sourceOptions = {
        cache: request.cache,
        rangeParam: request.rangeParam,
      }
      scope.postMessage({ id: request.id, ok: true, configured: true } satisfies WorkerResponse)
    }
    return
  }
  if (request.type === 'cancel') {
    controllers.get(request.id)?.abort()
    return
  }
  if (request.type === 'stats') {
    scope.postMessage({
      id: request.id,
      ok: true,
      stats,
      parts: Object.fromEntries(
        [...opened].flatMap(([url, reader]) =>
          reader instanceof SpatialReader ? [[url, reader.bytesByPart()]] : [],
        ),
      ),
      cache: {
        bytes: visible.bytes,
        tiles: visible.size,
        decodes: geometryDecodes,
        projections: projectionJobs,
      },
      waiting: {
        reads: ages(reads),
        unanswered: ages(unanswered),
        ...(helpers?.waiting ?? { jobs: [], queued: 0 }),
      },
    } satisfies WorkerResponse)
    return
  }
  if (request.type === 'open') {
    started = true
    Promise.all([getReader(request.url), getPool()])
      .then(([reader]) => reader)
      .then((reader) =>
        scope.postMessage({
          id: request.id,
          ok: true,
          opened: true,
          metadata: { lod: reader.lod, bounds: reader.bounds, columns: reader.scalarColumns() },
        } satisfies WorkerResponse),
      )
      .catch((error: Error) =>
        scope.postMessage({
          id: request.id,
          ok: false,
          error: error?.message ?? String(error),
          name: error?.name ?? 'Error',
        } satisfies WorkerResponse),
      )
    return
  }
  started = true
  const controller = new AbortController()
  controllers.set(request.id, controller)
  const work =
    request.type === 'row'
      ? getReader(request.url)
          .then((reader) => reader.readRow(request.rowIndex, request.columns, controller.signal))
          .then((row) =>
            scope.postMessage({ id: request.id, ok: true, row } satisfies WorkerResponse),
          )
      : getReader(request.config.url)
          .then(async (reader) => {
            const options = {
              layer: request.config.layer,
              pointLayer: request.config.pointLayer,
              properties: request.config.properties ?? [],
              signal: controller.signal,
            }
            const cpu = await getPool()
            const { properties } = options
            if (reader instanceof SpatialReader && request.z <= reader.finestZoom) {
              const source = properties.filter(
                (name) => !reader.lod.attributes.some((a) => a.name === name),
              )
              const wanted = properties.filter((name) => reader.attributeNames.includes(name))
              const lod = reader.lodForZoom(request.z)
              const key = JSON.stringify([request.config.url, request.z, request.x, request.y])
              let rendered: ArrayBuffer | undefined
              const tile = await visible.getOrLoad(
                key,
                async (sharedSignal) => {
                  // Keep shared work only while at least one tile still needs it.
                  const task = await reader.prepareTile(
                    lod,
                    request.z,
                    request.x,
                    request.y,
                    sharedSignal,
                    source,
                    wanted,
                  )
                  geometryDecodes++
                  const settings = source.length ? undefined : { ...options, signal: undefined }
                  if (cpu) {
                    const result = await cpu.decode(task, settings, sharedSignal)
                    rendered = result.data
                    return result.tile
                  }
                  sharedSignal.throwIfAborted()
                  const decoded = decodePreparedTile(task)
                  if (settings) rendered = renderDecodedTile(decoded, settings)
                  return decoded
                },
                controller.signal,
              )
              if (rendered) return rendered
              const missing = source.filter((name) => !Object.hasOwn(tile.columns, name))
              // a style asking for an attribute the cached tile lacks
              // fetches that attribute's pages over the tile's own rows
              const unfilled = wanted.filter((name) => tile.pending?.includes(name))
              // another request for this tile may fill its last pending attribute
              // and drop the tile's marks before this one's pages arrive
              const own = tile.own ?? new Uint32Array(tile.rows.length)
              const copies = tile.copies ?? {}
              const shown = tile.shown ?? []
              const [extras, filled] = await Promise.all<Record<string, unknown[]>>([
                missing.length
                  ? reader.readColumns(
                      tile.rows,
                      missing,
                      controller.signal,
                      cpu
                        ? (task, signal) => {
                            projectionJobs++
                            return cpu.project(task, signal)
                          }
                        : undefined,
                    )
                  : {},
                unfilled.length
                  ? reader
                      .prepareAttributes(
                        lod,
                        request.z,
                        request.x,
                        request.y,
                        unfilled,
                        controller.signal,
                      )
                      .then((pages) => {
                        const task = attributeTask(
                          tile,
                          own,
                          shown,
                          attributeFields(reader.lod.attributes),
                          pages,
                        )
                        return cpu
                          ? cpu.attributes(task, controller.signal)
                          : decodeAttributes(task)
                      })
                      .then((values) => filledColumns(own, copies, values))
                  : {},
              ])
              controller.signal.throwIfAborted()
              Object.assign(extras, filled)
              visible.addColumns(key, tile, extras)
              // New styles never modify the cached geometry or attribute columns.
              const columns = Object.fromEntries(
                properties.map((name) => [name, extras[name] ?? tile.columns[name]]),
              )
              const {
                own: _own,
                copies: _copies,
                pending: _pending,
                shown: _shown,
                ...decoded
              } = tile
              const styled = { ...decoded, columns }
              return cpu ? cpu.render(styled, options) : renderDecodedTile(styled, options)
            }
            return renderTile(reader, request.z, request.x, request.y, options)
          })
          .then((data) =>
            scope.postMessage({ id: request.id, ok: true, data } satisfies WorkerResponse, [data]),
          )
  work
    .catch((error: Error) =>
      scope.postMessage({
        id: request.id,
        ok: false,
        error: error?.message ?? String(error),
        name: error?.name ?? 'Error',
      } satisfies WorkerResponse),
    )
    .finally(() => controllers.delete(request.id))
})
