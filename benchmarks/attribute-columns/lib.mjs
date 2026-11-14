// Shared by the attribute-column measurements: a counting byte source for local
// files and http(s) URLs, tile arithmetic, MVT feature counts, and the inputs
// every script takes.
import { spawn } from 'node:child_process'
import { open, readFile, stat } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { basename, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'

export const HERE = fileURLToPath(new URL('.', import.meta.url))
export const JS = resolve(HERE, '../../js')
export const dist = (name) => pathToFileURL(join(JS, 'dist', name)).href
export const nodeModule = (path) => pathToFileURL(join(JS, 'node_modules', path)).href

const LOCAL_HOST = 'local.invalid'
// taken before any Net is installed as `globalThis.fetch`, so a later Net does
// not forward through an earlier one and add its latency again
const runtimeFetch = globalThis.fetch
const isUrl = (path) => /^https?:\/\//.test(path)

/**
 * A `fetch` for the readers under test. Local files are served from disk under
 * http://local.invalid/ so a local path and a URL take the same reader code;
 * other URLs go to the network. Every request waits `latency` ms more, and at
 * most `connections` requests per host are in flight (0: no limit). Counters
 * cover the requests issued since the last `reset()`; a request's depth is one
 * more than the deepest request that had completed when it was issued, so the
 * deepest is the number of rounds the window needed.
 */
export class Net {
  constructor({ latency = 0, connections = 0 } = {}) {
    this.latency = latency
    this.connections = connections
    this.local = new Map()
    this.hosts = new Map()
    this.inFlight = 0
    this.realFetch = runtimeFetch
    this.fetch = this.fetch.bind(this)
    this.reset()
  }

  /** The URL a reader should open for `path`, a local path or an http(s) URL. */
  async register(path) {
    if (isUrl(path)) return path
    const absolute = resolve(path)
    const url = `http://${LOCAL_HOST}/${this.local.size}/${encodeURIComponent(basename(absolute))}`
    const handle = await open(absolute, 'r')
    this.local.set(url, { handle, size: (await stat(absolute)).size })
    return url
  }

  reset() {
    this.window = { requests: 0, bytes: 0, done: 0, rounds: 0, waited: 0, log: [] }
    this.started = performance.now()
  }

  /** The counters since `reset()`, with the time since then. */
  snapshot() {
    const { requests, bytes, rounds, waited, log } = this.window
    return { requests, bytes, rounds, waited, ms: performance.now() - this.started, log }
  }

  /** Wait until no request has been in flight for `quietMs`. */
  async settle(quietMs = 50) {
    let quietSince = performance.now()
    while (performance.now() - quietSince < quietMs + this.latency) {
      await new Promise((r) => setTimeout(r, 10))
      if (this.inFlight) quietSince = performance.now()
    }
  }

  async close() {
    for (const { handle } of this.local.values()) await handle.close()
  }

  async slot(host) {
    if (!this.connections) return () => {}
    const state = this.hosts.get(host) ?? { active: 0, queue: [] }
    this.hosts.set(host, state)
    if (state.active >= this.connections) await new Promise((r) => state.queue.push(r))
    state.active++
    return () => {
      state.active--
      state.queue.shift()?.()
    }
  }

  async fetch(input, init = {}) {
    const url = String(input instanceof Request ? input.url : input)
    const method = init.method ?? 'GET'
    const range = new Headers(init.headers).get('range')
    const window = this.window
    const depth = window.done + 1
    window.requests++
    window.rounds = Math.max(window.rounds, depth)
    if (depth > 1) window.waited++
    const entry = [depth, url, range, 0]
    window.log.push(entry)
    this.inFlight++
    const release = await this.slot(new URL(url).host)
    try {
      if (this.latency) await delay(this.latency, init.signal)
      const response = this.local.has(url)
        ? await this.serve(this.local.get(url), method, range)
        : await this.forward(url, init)
      entry[3] = Number(response.headers.get('content-length') ?? 0)
      if (method !== 'HEAD') window.bytes += entry[3]
      window.done = Math.max(window.done, depth)
      return response
    } finally {
      release()
      this.inFlight--
    }
  }

  async serve({ handle, size }, method, range) {
    if (method === 'HEAD' || !range)
      return new Response(method === 'HEAD' ? null : await readFile(handle), {
        status: 200,
        headers: { 'content-length': String(size) },
      })
    const [, a, b] = /^bytes=(\d+)-(\d*)$/.exec(range)
    const start = Number(a)
    if (start >= size)
      return new Response(null, { status: 416, headers: { 'content-range': `bytes */${size}` } })
    const end = Math.min(size, b === '' ? size : Number(b) + 1)
    const buffer = Buffer.alloc(end - start)
    await handle.read(buffer, 0, buffer.byteLength, start)
    return new Response(buffer, {
      status: 206,
      headers: {
        'content-length': String(buffer.byteLength),
        'content-range': `bytes ${start}-${end - 1}/${size}`,
      },
    })
  }

  async forward(url, init) {
    const response = await this.realFetch(url, init)
    const body = init.method === 'HEAD' ? null : await response.arrayBuffer()
    const headers = new Headers(response.headers)
    if (body) headers.set('content-length', String(body.byteLength))
    return new Response(body, { status: response.status, headers })
  }
}

const delay = (ms, signal) =>
  new Promise((resolve, reject) => {
    signal?.throwIfAborted()
    const timer = setTimeout(resolve, ms)
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(timer)
        reject(signal.reason)
      },
      { once: true },
    )
  })

/** The bytes of every request in a window, as merged [start, end) intervals per URL. */
export const coveredRanges = (log) => {
  const byUrl = new Map()
  for (const [, url, range, length] of log) {
    if (!range) continue
    const start = Number(/^bytes=(\d+)-/.exec(range)[1])
    byUrl.set(url, [...(byUrl.get(url) ?? []), [start, start + length]])
  }
  const out = new Map()
  for (const [url, intervals] of byUrl) {
    intervals.sort((a, b) => a[0] - b[0])
    const merged = []
    for (const [s, e] of intervals) {
      const last = merged.at(-1)
      if (last && s <= last[1]) last[1] = Math.max(last[1], e)
      else merged.push([s, e])
    }
    out.set(url, merged)
  }
  return out
}

export const tileXY = (z, lon, lat) => [
  Math.floor(((lon + 180) / 360) * 2 ** z),
  Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
]

const mercator = (lon, lat) => [
  (lon + 180) / 360,
  (1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2,
]

/**
 * The 512-px tiles MapLibre asks a vector source for to fill a viewport with
 * no pitch or bearing: the tile zoom is the view zoom rounded down and held to
 * the source's zoom range, so past `maxzoom` it overzooms, and tiles wholly
 * outside the source's bounds are left out. Nearest the centre first.
 */
export const coveringTiles = ({ center, zoom, width, height }, { minzoom, maxzoom, bounds }) => {
  const z = Math.max(minzoom, Math.min(maxzoom, Math.floor(zoom)))
  const n = 2 ** z
  const world = 512 * 2 ** zoom
  const [cx, cy] = mercator(...center)
  const x0 = cx - width / 2 / world
  const x1 = cx + width / 2 / world
  const y0 = cy - height / 2 / world
  const y1 = cy + height / 2 / world
  const [bx0, by1] = mercator(bounds[0], bounds[1])
  const [bx1, by0] = mercator(bounds[2], bounds[3])
  const tiles = []
  for (let x = Math.floor(x0 * n); x <= Math.floor(x1 * n - 1e-12); x++)
    for (
      let y = Math.max(0, Math.floor(y0 * n));
      y <= Math.min(n - 1, Math.floor(y1 * n - 1e-12));
      y++
    ) {
      if ((x + 1) / n <= bx0 || x / n >= bx1 || (y + 1) / n <= by0 || y / n >= by1) continue
      const d = ((x + 0.5) / n - cx) ** 2 + ((y + 0.5) / n - cy) ** 2
      tiles.push({ z, x: ((x % n) + n) % n, y, d })
    }
  return tiles.sort((a, b) => a.d - b.d).map(({ z, x, y }) => [z, x, y])
}

/** MapLibre's default tile cache size for a source: five zooms of the viewport. */
export const mapCacheSize = ({ width, height }) =>
  (Math.ceil(width / 512) + 1) * (Math.ceil(height / 512) + 1) * 5

const varint = (bytes, at) => {
  let value = 0
  let shift = 0
  for (;;) {
    const b = bytes[at.i++]
    value += (b & 0x7f) * 2 ** shift
    if (b < 0x80) return value
    shift += 7
  }
}

const skip = (bytes, at, wire) => {
  if (wire === 0) varint(bytes, at)
  else if (wire === 1) at.i += 8
  else if (wire === 2) {
    const length = varint(bytes, at)
    at.i += length
  } else if (wire === 5) at.i += 4
  else throw new Error(`unexpected wire type ${wire}`)
}

/** Features in an MVT: field 2 of each layer, field 3 of the tile. */
export const mvtFeatures = (buffer) => {
  const bytes = new Uint8Array(buffer)
  const at = { i: 0 }
  let n = 0
  while (at.i < bytes.length) {
    const key = varint(bytes, at)
    if (key >> 3 !== 3 || (key & 7) !== 2) {
      skip(bytes, at, key & 7)
      continue
    }
    const end = varint(bytes, at) + at.i
    while (at.i < end) {
      const inner = varint(bytes, at)
      if (inner >> 3 === 2) n++
      skip(bytes, at, inner & 7)
    }
  }
  return n
}

/** Options every script takes. */
export const OPTIONS = {
  dir: { type: 'string' },
  file: { type: 'string', multiple: true },
  pmtiles: { type: 'string' },
  scenario: { type: 'string', default: 'japan' },
  carried: { type: 'string', default: 'height' },
  uncarried: { type: 'string', default: 'subtype' },
  out: { type: 'string' },
}

export const parse = (extra = {}) =>
  parseArgs({ options: { ...OPTIONS, ...extra }, allowPositionals: false }).values

/**
 * The files to measure, [label, path or URL]: `--file label=path` as given,
 * else every variant in DIR/builds.json.
 */
export const variantFiles = async ({ dir, file }) => {
  if (file?.length) return file.map((f) => f.split(/=(.*)/s).slice(0, 2))
  if (!dir) throw new Error('supply --dir BUILD_DIR or --file label=path')
  const builds = JSON.parse(await readFile(join(dir, 'builds.json'), 'utf8'))
  const order = ['packed', 'columns', 'auto', 'hybrid', 'none']
  return Object.keys(builds.variants)
    .sort((a, b) => order.indexOf(a) - order.indexOf(b))
    .map((name) => [name, join(dir, builds.variants[name].file)])
}

/** A scenario by name from scenarios/, or a JSON file: named tiles and a route. */
export const loadScenario = async (name) => {
  const path = existsSync(name) ? name : join(HERE, 'scenarios', `${name}.json`)
  return JSON.parse(await readFile(path, 'utf8'))
}

/** Style name to the properties a tile request asks for. */
export const styles = ({ carried, uncarried }) => ({
  geometry: [],
  carried: [carried],
  uncarried: [uncarried],
})

export const resultPath = (values, name) =>
  values.out ?? (values.dir ? join(values.dir, name) : name)

export const absolute = (path) => (isUrl(path) || isAbsolute(path) ? path : resolve(path))

export const kb = (bytes) => (bytes / 1e3).toFixed(1)
export const pct = (a, b) => (b ? `${(((a - b) / b) * 100).toFixed(1)}%` : 'n/a')

/** One session.mjs replay of `job` in a fresh process, so every cache starts empty. */
export const sessionRun = (job) =>
  new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [join(HERE, 'session.mjs'), '--child', JSON.stringify(job)],
      { stdio: ['ignore', 'pipe', 'inherit'] },
    )
    let output = ''
    child.stdout.on('data', (chunk) => (output += chunk))
    child.on('exit', (code) =>
      code === 0
        ? resolve(JSON.parse(output.trim().split('\n').at(-1)))
        : reject(new Error(`session run failed: ${JSON.stringify(job).slice(0, 200)}`)),
    )
  })
