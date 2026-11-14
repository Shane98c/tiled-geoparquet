// A scripted map session replayed through the reader, without a browser.
//
//   node benchmarks/attribute-columns/session.mjs --dir BUILD_DIR [--pmtiles FILE_OR_URL]
//     [--scenario japan|denver|path.json] [--carried height] [--uncarried subtype]
//     [--latency 0,100] [--connections 0,6] [--repeat 3] [--out result.json]
//
// The route is a list of viewports (centre, zoom, size in pixels), some
// switching the style. Each viewport becomes the 512-px tiles MapLibre would
// ask for, all requested at once, and the step ends when the last arrives.
// A Tiled GeoParquet file is served by the reader's own worker module
// (js/dist/worker.js) running in this process with one CPU worker, so its
// readers, tile cache and attribute handling are the map's. PMTiles is read
// by the pmtiles package as its MapLibre protocol reads it.
//
// The map side is modelled as MapLibre does it with default options: a tile
// cache per source of five viewports' worth of tiles, emptied when the style
// changes a Tiled GeoParquet source's tile URL (setTiles), and kept for
// PMTiles, whose style switches repaint the tiles it has. Each run is a fresh
// process, so every variant starts cold.
import { writeFile } from 'node:fs/promises'
import {
  Net,
  absolute,
  coveringTiles,
  dist,
  kb,
  loadScenario,
  mapCacheSize,
  mvtFeatures,
  nodeModule,
  parse,
  resultPath,
  sessionRun,
  styles,
  variantFiles,
} from './lib.mjs'
import { printVerdict } from './verdict.mjs'

const values = parse({
  latency: { type: 'string', default: '0,100' },
  connections: { type: 'string', default: '0,6' },
  repeat: { type: 'string', default: '3' },
  child: { type: 'string' },
})

if (values.child) {
  const result = await replay(JSON.parse(values.child))
  process.stdout.write('\n' + JSON.stringify(result) + '\n')
  process.exit(0)
}

/** One session on a fresh reader: `kind` is 'lod' or 'pmtiles'. */
async function replay({ kind, path, latency, connections, scenario, properties, route }) {
  const net = new Net({ latency, connections })
  globalThis.fetch = net.fetch
  const url = await net.register(path)
  const steps = route ?? scenario.route
  const viewport = scenario.viewport
  let limits, fetchTile
  if (kind === 'lod') {
    const worker = await workerInProcess()
    // the replayed network has no connections for a reader to open ahead of its tiles
    await worker.post({ type: 'configure', workers: 1, connections: 1 })
    const { metadata } = await worker.post({ type: 'open', url })
    const { sourceLimits } = await import(dist('source-limits.js'))
    limits = sourceLimits(metadata)
    fetchTile = async ([z, x, y], style) => {
      const config = { url, layer: 'shapes', pointLayer: 'points', properties: properties[style] }
      return (await worker.post({ type: 'tile', config, z, x, y })).data
    }
  } else {
    const { PMTiles } = await import(nodeModule('pmtiles/dist/esm/index.js'))
    const archive = new PMTiles(url)
    const header = await archive.getHeader()
    limits = {
      minzoom: header.minZoom,
      maxzoom: header.maxZoom,
      bounds: [header.minLon, header.minLat, header.maxLon, header.maxLat],
    }
    fetchTile = async ([z, x, y]) => (await archive.getZxy(z, x, y))?.data
  }
  await net.settle()
  const { log: _, ...open } = net.snapshot()

  const cache = new Map()
  let style = 'geometry'
  const out = []
  for (const step of steps) {
    const view = { ...viewport, ...step }
    if (step.style && step.style !== style) {
      style = step.style
      if (kind === 'lod') cache.clear()
    }
    const tiles = step.tiles ?? coveringTiles(view, limits)
    const key = (t) => (kind === 'lod' ? `${style}/${t}` : `${t}`)
    const missing = tiles.filter((t) => !cache.has(key(t)))
    net.reset()
    const data = await Promise.all(missing.map((t) => fetchTile(t, style)))
    const { log, ...counts } = net.snapshot()
    for (const t of tiles) {
      const k = key(t)
      cache.delete(k)
      cache.set(k, true)
    }
    for (const k of cache.keys()) {
      if (cache.size <= mapCacheSize(view)) break
      cache.delete(k)
    }
    out.push({
      label: step.label,
      style,
      zoom: view.zoom,
      tiles: tiles.length,
      fetched: missing.length,
      ...counts,
      features: data.reduce((n, d) => n + (d ? mvtFeatures(d) : 0), 0),
      ...(step.log ? { log, tileList: tiles } : {}),
    })
  }
  await net.close()
  return { open, limits, steps: out }
}

/**
 * The reader's worker module in this process: `self` is a stand-in whose
 * postMessage resolves the request with the same id.
 */
async function workerInProcess() {
  const listeners = []
  const pending = new Map()
  globalThis.self = {
    fetch: globalThis.fetch,
    location: { href: dist('worker.js') },
    addEventListener: (type, listener) => type === 'message' && listeners.push(listener),
    postMessage: (message) => {
      const request = pending.get(message.id)
      if (!request) return
      pending.delete(message.id)
      if (message.ok) request.resolve(message)
      else request.reject(Object.assign(new Error(message.error), { name: message.name }))
    },
  }
  await import(dist('worker.js'))
  let id = 0
  return {
    post: (request) =>
      new Promise((resolve, reject) => {
        const message = { ...request, id: ++id }
        pending.set(message.id, { resolve, reject })
        for (const listener of listeners) listener({ data: message })
      }),
  }
}

/** Run `job` `repeat` times and keep the run with the median total time. */
const median = async (job, repeat) => {
  const runs = []
  for (let i = 0; i < repeat; i++) runs.push(await sessionRun(job))
  const total = (r) => r.steps.reduce((ms, s) => ms + s.ms, 0)
  runs.sort((a, b) => total(a) - total(b))
  const chosen = runs[Math.floor((runs.length - 1) / 2)]
  return { ...chosen, totalMs: total(chosen), runsMs: runs.map(total) }
}

{
  const scenario = await loadScenario(values.scenario)
  const properties = styles(values)
  const files = (await variantFiles(values)).map(([label, path]) => ['lod', label, absolute(path)])
  if (values.pmtiles) files.push(['pmtiles', 'pmtiles', absolute(values.pmtiles)])
  const latencies = values.latency.split(',').map(Number)
  const connections = values.connections.split(',').map(Number)
  const repeat = Number(values.repeat)
  const result = {
    scenario: values.scenario,
    carried: values.carried,
    uncarried: values.uncarried,
    viewport: scenario.viewport,
    runs: [],
  }
  for (const latency of latencies)
    for (const limit of connections)
      for (const [kind, label, path] of files) {
        const job = { kind, path, latency, connections: limit, scenario, properties }
        const session = await median(job, repeat)
        result.runs.push({ variant: label, latency, connections: limit, ...session })
        console.error(
          `${label} latency ${latency} ms, connections ${limit || 'unlimited'}: ` +
            `${Math.round(session.totalMs)} ms (runs ${session.runsMs.map(Math.round).join(', ')})`,
        )
      }
  const out = resultPath(values, 'session.json')
  await writeFile(out, JSON.stringify(result, null, 2) + '\n')

  const labels = [...new Set(result.runs.map((r) => r.variant))]
  const total = (r, field) => r.steps.reduce((n, s) => n + s[field], 0)
  for (const latency of latencies)
    for (const limit of connections) {
      const runs = labels.map((label) =>
        result.runs.find(
          (r) => r.variant === label && r.latency === latency && r.connections === limit,
        ),
      )
      console.log(
        `\n### Session, ${latency} ms added per request, ${limit ? `${limit} connections per host` : 'no connection limit'}: requests / KB / rounds / ms per step\n`,
      )
      console.log(`| step | style | tiles (fetched) | ${labels.join(' | ')} |`)
      console.log(`|---|---|---|${labels.map(() => '---|').join('')}`)
      runs[0].steps.forEach((step, i) => {
        const cells = runs.map((r) => {
          const s = r.steps[i]
          return `${s.requests} / ${kb(s.bytes)} / ${s.rounds} / ${Math.round(s.ms)}`
        })
        const fetched = runs.map((r) => r.steps[i].fetched).join('/')
        console.log(
          `| ${i + 1} ${step.label} z${step.zoom} | ${step.style} | ${step.tiles} (${fetched}) | ${cells.join(' | ')} |`,
        )
      })
      const cells = runs.map(
        (r) =>
          `${total(r, 'requests')} / ${kb(total(r, 'bytes'))} / ${total(r, 'rounds')} / ${Math.round(r.totalMs)}`,
      )
      console.log(`| total | | | ${cells.join(' | ')} |`)
      const opens = runs.map(
        (r) => `${r.open.requests} / ${kb(r.open.bytes)} / ${Math.round(r.open.ms)}`,
      )
      console.log(`| open (not in total) | | | ${opens.join(' | ')} |`)
    }
  console.log(`\nwritten ${out}`)
  await printVerdict(values)
}
