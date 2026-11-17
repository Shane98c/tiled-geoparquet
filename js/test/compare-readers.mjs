// Bytes and requests per tile for five readers of the same buildings, served from one place:
// this layout, COGP (@cogp/reader), Esri's SOP reference reader, the reader of GeoParquet with
// baked PMTiles, and PMTiles. Expects
// japan_buildings.{lod6.parquet,cogp.parquet,pmtiles,sop.parquet,baked.parquet} under BASE
// (default http://localhost:8099/), and the Esri reader bundled for Node first. COGP names
// another COGP file to measure, such as the one behind COGP's own demo, LOD another file in
// this layout and BAKED another baked file; PROPERTIES=height,class has this layout's tiles
// hold those attributes, where the other readers' tiles hold what their files give them:
//   npx esbuild vendor/esri-sop/index.ts --bundle --platform=node --format=esm --outfile=test/esri-node.mjs
//   node test/compare-readers.mjs
// The baked reader is bundled from a clone of gsueur/geoparquet-pmtiles after `npm install` there:
//   npx esbuild web/baked.js --bundle --platform=node --format=esm --outfile=<here>/baked-node.mjs
//   npx esbuild web/src/detail-worker.js --bundle --format=esm --outfile=<here>/baked-worker-node.mjs
import { openReader, httpSource, coalescing, renderTile, tileBounds } from '../dist/index.js'
import { CogpReader, MVT_BUFFER, MVT_EXTENT, toMvt } from '@cogp/reader'
import * as pmtiles from 'pmtiles'
import { Query } from './esri-node.mjs'
import { PbfReader } from 'pbf'

// feature count of an MVT: layers are field 3 of the tile, features field 2 of a layer
const features = (buf) => {
  let n = 0
  const layer = (tag, _, pbf) => {
    if (tag === 2) n++
    pbf.skip(pbf.type)
  }
  new PbfReader(new Uint8Array(buf)).readFields((tag, _, pbf) => {
    if (tag === 3) pbf.readMessage(layer, {})
    else pbf.skip(pbf.type)
  }, {})
  return n
}

const base = process.env.BASE ?? 'http://localhost:8099/'
const cogpFile = process.env.COGP ?? base + 'japan_buildings.cogp.parquet'
const lodFile = process.env.LOD ?? base + 'japan_buildings.lod15.parquet'
const pmFile = process.env.PMTILES ?? 'japan_buildings_z0.pmtiles'
const bakedFile = process.env.BAKED ?? base + 'japan_buildings.baked.parquet'
const properties = (process.env.PROPERTIES ?? '').split(',').filter(Boolean)
// SKIP=cogp,esri,baked leaves those readers out, e.g. against a bucket holding only the other two files
const skip = new Set((process.env.SKIP ?? '').split(',').filter(Boolean))
const stats = {}
// six requests in flight at most, as a browser sends them to one host: Node would open a
// connection for each of the thousand requests one Esri tile makes
let flying = 0
const queued = []
const slot = () =>
  flying < 6 ? (flying++, Promise.resolve()) : new Promise((go) => queued.push(go))
const release = () => {
  const next = queued.shift()
  if (next) next()
  else flying--
}
const real = globalThis.fetch
globalThis.fetch = async (input, init) => {
  await slot()
  const r = await real(input, init).finally(release)
  const key = String(input instanceof Request ? input.url : input).split('?')[0]
  const e = (stats[key] ??= { requests: 0, bytes: 0 })
  e.requests++
  if ((init?.method ?? 'GET') !== 'HEAD') e.bytes += Number(r.headers.get('content-length') ?? 0)
  return r
}
const snap = (key) => ({
  ...(stats[key.startsWith('http') ? key : base + key] ?? { requests: 0, bytes: 0 }),
})
const delta = (a, b) => ({ requests: b.requests - a.requests, bytes: b.bytes - a.bytes })
const fmt = (d, extra = '') => `${(d.bytes / 1e6).toFixed(2)} MB, ${d.requests} req${extra}`

const tiles = [
  [8, 139.76, 35.68, 'metro, Tokyo'],
  [11, 139.76, 35.68, 'city, Tokyo'],
  [13, 139.7, 35.66, 'downtown, Shibuya'],
  [13, 139.95, 35.45, 'suburbs, Chiba'],
  [15, 139.76, 35.68, 'street, Tokyo station'],
  [15, 135.5, 34.69, 'street, Osaka'],
]
const xy = (z, lon, lat) => [
  Math.floor(((lon + 180) / 360) * 2 ** z),
  Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z),
]

// open each reader once; per-tile cost is then the cold read on a fresh reader
const open = async (name, fn) => {
  const a = snap(name)
  const r = await fn()
  // let what opening starts in the background (index prefetch) land before counting it as open
  await new Promise((resolve) => setTimeout(resolve, 1500))
  return { r, open: delta(a, snap(name)) }
}
const lodOpen = async () => openReader(coalescing(await httpSource([lodFile])))
const cogpOpen = () => CogpReader.open(cogpFile)
const pmOpen = async () => {
  const p = new pmtiles.PMTiles(base + pmFile)
  await p.getHeader()
  return p
}
// the baked reader as its page runs it: find the archive from the file's end, read the
// archive's header and metadata, and start the worker that builds detail tiles from the rows
const baked = skip.has('baked') ? undefined : await import('./baked-node.mjs')
if (baked) globalThis.pmtiles = pmtiles
let bakedWorkers = 0
const bakedWorker = async () => {
  const pending = new Map()
  let seq = 0
  globalThis.onmessage = null
  globalThis.postMessage = (m) => {
    if (m.type === 'bytes') return
    const p = pending.get(m.id)
    pending.delete(m.id)
    if (m.ok) p.resolve(m)
    else p.reject(new Error(m.error))
  }
  await import(`./baked-worker-node.mjs?worker=${bakedWorkers++}`)
  const handler = globalThis.onmessage
  return (message) =>
    new Promise((resolve, reject) => {
      const id = ++seq
      pending.set(id, { resolve, reject })
      handler({ data: { ...message, id } })
    })
}
const bakedOpen = async () => {
  const file = await baked.openBaked(bakedFile)
  const archive = new pmtiles.PMTiles(new baked.EmbeddedSource(bakedFile, file.tiles.offset))
  const header = await archive.getHeader()
  const detail = (await archive.getMetadata()).geoparquet_detail
  const ask = await bakedWorker()
  await ask({
    type: 'init',
    url: bakedFile,
    size: file.size,
    footerLen: file.footerLen,
    layer: detail.layer,
    properties: detail.properties,
    rows: detail.rows,
  })
  return { archive, header, detail, ask }
}
// a tile as the page's style requests it: the baked tile below the detail zoom; from it, the
// tile built from the rows up to the detail source's zoom 15, and the baked tile of the wide
// features where the archive carries them
const bakedTile = async (r, z, x, y) => {
  if (z < r.detail.minzoom) {
    const t = await r.archive.getZxy(z, x, y)
    return t ? features(t.data) : 0
  }
  const dz = Math.min(z, 15)
  const wz = Math.min(z, r.header.maxZoom)
  const [rows, wide] = await Promise.all([
    r.ask({ type: 'tile', z: dz, x: x >> (z - dz), y: y >> (z - dz) }),
    r.detail.rows ? r.archive.getZxy(wz, x >> (z - wz), y >> (z - wz)) : undefined,
  ])
  return features(rows.data) + (wide ? features(wide.data) : 0)
}
const esriOpen = async () => {
  const h = await fetch(base + 'japan_buildings.sop.parquet', { method: 'HEAD' })
  const q = new Query(base + 'japan_buildings.sop.parquet', Number(h.headers.get('content-length')))
  await q.loadMetadata(new AbortController().signal)
  return q
}
const opens = {}
for (const [name, fn] of [
  ['lod', lodOpen],
  ['cogp', cogpOpen],
  ['esri', esriOpen],
  ['baked', bakedOpen],
  ['pmtiles', pmOpen],
].filter(([name]) => !skip.has(name)))
  opens[name] = (
    await open(
      name.startsWith('lod')
        ? lodFile
        : name === 'cogp'
          ? cogpFile
          : name === 'pmtiles'
            ? pmFile
            : name === 'baked'
              ? bakedFile
              : 'japan_buildings.sop.parquet',
      fn,
    )
  ).open
console.log('| reader | open |\n|---|---|')
for (const [k, v] of Object.entries(opens)) console.log(`| ${k} | ${fmt(v)} |`)

console.log('\n| tile | lod | cogp | esri | baked | pmtiles |\n|---|---|---|---|---|---|')
for (const [z, lon, lat, label] of tiles) {
  const [x, y] = xy(z, lon, lat)
  const bbox = tileBounds(z, x, y)
  const row = []
  // ours: fresh reader, geometry and the attributes PROPERTIES names
  {
    const key = lodFile
    const r = await lodOpen()
    await new Promise((resolve) => setTimeout(resolve, 1500))
    const a = snap(key)
    const t0 = performance.now()
    const mvt = await renderTile(r, z, x, y, { properties })
    const ms = Math.round(performance.now() - t0)
    const cold = delta(a, snap(key))
    // the neighbouring tile on the same reader: what a tile costs once the indexes are read
    const b = snap(key)
    await renderTile(r, z, x + 1, y, { properties })
    const warm = delta(b, snap(key))
    row.push(
      fmt(
        cold,
        `, ${ms} ms, ${(mvt.byteLength / 1e3).toFixed(0)} KB mvt, ${features(mvt)} features; next tile ${fmt(warm)}`,
      ),
    )
  }
  // cogp: fresh reader, the tile bounds, resolution and read call of @cogp/maplibre's tile.js
  if (skip.has('cogp')) row.push('skipped')
  else {
    const key = cogpFile
    const r = await cogpOpen()
    const a = snap(key)
    const n = 2 ** z
    const latAt = (yy) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * yy) / n))) * 180) / Math.PI
    const pad = MVT_BUFFER / MVT_EXTENT
    const cogpBbox = [
      ((x - pad) / n) * 360 - 180,
      latAt(y + 1 + pad),
      ((x + 1 + pad) / n) * 360 - 180,
      latAt(y - pad),
    ]
    const res = (360 * Math.cos((latAt(y + 0.5) * Math.PI) / 180)) / (512 * n)
    const level = r.selectLevel(res)
    const batch = await r.read({
      bbox: cogpBbox,
      maxLevel: level,
      columns: [r.primaryGeometryColumn],
      useOverview: true,
    })
    const mvt = toMvt(batch, { z, x, y })
    row.push(
      fmt(
        delta(a, snap(key)),
        `, ${(mvt.byteLength / 1e3).toFixed(0)} KB mvt, level ${level}, ${batch.length} features`,
      ),
    )
  }
  // esri: fresh query, the reference reader's view query for the tile extent
  if (skip.has('esri')) row.push('skipped')
  else {
    const key = 'japan_buildings.sop.parquet'
    const q = await esriOpen()
    const a = snap(key)
    const res = await q.execute({
      queryExtents: [{ xmin: bbox[0], ymin: bbox[1], xmax: bbox[2], ymax: bbox[3] }],
      sourceResolution: 360 / (512 * 2 ** z),
      signal: new AbortController().signal,
    })
    row.push(
      fmt(
        delta(a, snap(key)),
        `, level ${res.lod}, ${res.featureCollection.features.length} features${res.featureLimitReached ? ' (limit)' : ''}`,
      ),
    )
  }
  // baked PMTiles: fresh reader, the tiles the page's style requests
  if (skip.has('baked')) row.push('skipped')
  else {
    const key = bakedFile
    const r = await bakedOpen()
    const a = snap(key)
    const n = await bakedTile(r, z, x, y)
    const cold = delta(a, snap(key))
    const b = snap(key)
    await bakedTile(r, z, x + 1, y)
    const warm = delta(b, snap(key))
    row.push(fmt(cold, `, ${n} features; next tile ${fmt(warm)}`))
  }
  // pmtiles: fresh archive handle; parent tile beyond maxzoom
  {
    const key = pmFile
    const p = await pmOpen()
    const a = snap(key)
    const zz = Math.min(z, 14)
    const [px, py] = xy(zz, lon, lat)
    const t0 = performance.now()
    const t = await p.getZxy(zz, px, py)
    const ms = Math.round(performance.now() - t0)
    const cold = delta(a, snap(key))
    const b = snap(key)
    await p.getZxy(zz, px + 1, py)
    const warm = delta(b, snap(key))
    row.push(
      fmt(
        cold,
        (t
          ? `, ${ms} ms, ${(t.data.byteLength / 1e3).toFixed(0)} KB tile, ${features(t.data)} features`
          : ', empty') + `; next tile ${fmt(warm)}`,
      ),
    )
  }
  console.log(`| z${z} ${label} | ${row.join(' | ')} |`)
}
