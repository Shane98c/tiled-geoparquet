// Cold single-tile reads of each variant, and of PMTiles as the outside reference.
//
//   node benchmarks/attribute-columns/single-tiles.mjs --dir BUILD_DIR [--pmtiles FILE_OR_URL]
//     [--scenario japan|denver|path.json] [--named height] [--other subtype]
//     [--latency 20] [--out result.json]
//
// For every named tile, variant and style (geometry only, the attribute the
// columns variant names, an attribute only the auto variant keeps) a fresh reader opens
// the file, then renders the tile, `cold`, and, on the same reader, the tile
// east of it, `next`. Another fresh reader first renders, with the same style,
// the tile four columns west and four rows south, then the tile, `warm`: a
// tile in a new area on a reader that has styled another, its directories warm.
// Each read counts requests, rounds (requests that had to wait on an earlier
// one), bytes and features. The added latency only spaces requests so rounds
// are counted the same way on local files as over a network.
import {
  Net,
  absolute,
  dist,
  kb,
  loadScenario,
  mvtFeatures,
  nodeModule,
  parse,
  resultPath,
  styles,
  tileXY,
  variantFiles,
} from './lib.mjs'
import { writeFile } from 'node:fs/promises'
import { printVerdict } from './verdict.mjs'

const values = parse({ latency: { type: 'string', default: '20' } })
const latency = Number(values.latency)
const { openReader, httpSource, coalescing, renderTile } = await import(dist('index.js'))
// the replayed network has no connections for a reader to open ahead of its tiles
const ONE = { connections: 1 }
const { zstdReady } = await import(dist('compressors.js'))
await zstdReady
const { PMTiles } = await import(nodeModule('pmtiles/dist/esm/index.js'))

const scenario = await loadScenario(values.scenario)
const files = (await variantFiles(values)).map(([label, path]) => [label, absolute(path)])
const styleMap = styles(values)

/** A fresh `Net` installed as the global fetch, and `path` registered on it. */
const fresh = async (path) => {
  const net = new Net({ latency })
  globalThis.fetch = net.fetch
  return { net, url: await net.register(path) }
}

const measure = async (net, render) => {
  net.reset()
  const data = await render()
  const { log, ...counts } = net.snapshot()
  return { ...counts, features: data ? mvtFeatures(data) : 0, tileBytes: data?.byteLength ?? 0 }
}

const result = {
  scenario: values.scenario,
  named: values.named,
  other: values.other,
  latency,
  opens: {},
  rows: [],
  pmtiles: [],
}

for (const [label, path] of files) {
  const { net, url } = await fresh(path)
  const reader = await openReader(coalescing(await httpSource([url], ONE), { windowMs: 1 }))
  await net.settle()
  const { log, ...open } = net.snapshot()
  result.opens[label] = { ...open, zooms: reader.lod.zooms }
  await net.close()
}

for (const [index, [z, lon, lat, name]] of scenario.tiles.entries()) {
  const [x, y] = tileXY(z, lon, lat)
  for (const [label, path] of files)
    for (const [style, properties] of Object.entries(styleMap)) {
      const { net, url } = await fresh(path)
      const reader = await openReader(coalescing(await httpSource([url], ONE), { windowMs: 1 }))
      await net.settle()
      const options = { layer: 'shapes', pointLayer: 'points', properties }
      const cold = await measure(net, () => renderTile(reader, z, x, y, options))
      const next = await measure(net, () => renderTile(reader, z, x + 1, y, options))
      await net.close()
      const primed = await fresh(path)
      const again = await openReader(
        coalescing(await httpSource([primed.url], ONE), { windowMs: 1 }),
      )
      await renderTile(again, z, x - 4, y + 4, options)
      await primed.net.settle()
      const warm = await measure(primed.net, () => renderTile(again, z, x, y, options))
      result.rows.push({ index, tile: name, z, x, y, variant: label, style, cold, next, warm })
      await primed.net.close()
    }
  if (values.pmtiles) {
    const { net, url } = await fresh(values.pmtiles)
    const archive = new PMTiles(url)
    const header = await archive.getHeader()
    await net.settle()
    const { log, ...open } = net.snapshot()
    result.opens.pmtiles = open
    const zz = Math.min(z, header.maxZoom)
    const [px, py] = tileXY(zz, lon, lat)
    const get = async (tx) => (await archive.getZxy(zz, tx, py))?.data
    const cold = await measure(net, () => get(px))
    const next = await measure(net, () => get(px + 1))
    result.pmtiles.push({ index, tile: name, z, tileZoom: zz, x: px, y: py, cold, next })
    await net.close()
  }
}

const out = resultPath(values, 'single-tiles.json')
await writeFile(out, JSON.stringify(result, null, 2) + '\n')

const cell = (c) => `${c.requests} / ${kb(c.bytes)} / ${c.rounds} / ${c.features.toLocaleString()}`
console.log(`\nOpen: requests / KB`)
for (const [label, o] of Object.entries(result.opens))
  console.log(`  ${label}: ${o.requests} / ${kb(o.bytes)}`)
const styleNames = Object.keys(styleMap)
const heads = styleNames.map((s) => `${s}${styleMap[s].length ? ` (${styleMap[s]})` : ''}`)
const titles = {
  cold: 'Cold tile on a fresh reader',
  next: 'Next tile east, same reader',
  warm: 'Tile on a reader that has styled the tile four west and four south',
}
for (const which of ['cold', 'next', 'warm']) {
  console.log(`\n### ${titles[which]}: requests / KB / rounds / features\n`)
  console.log(`| tile | variant | ${heads.join(' | ')} |`)
  console.log(`|---|---|${heads.map(() => '---|').join('')}`)
  for (const [index, [z, , , name]] of scenario.tiles.entries()) {
    for (const [label] of files) {
      const cells = styleNames.map((style) =>
        cell(
          result.rows.find((r) => r.index === index && r.variant === label && r.style === style)[
            which
          ],
        ),
      )
      console.log(`| z${z} ${name} | ${label} | ${cells.join(' | ')} |`)
    }
    const pm = result.pmtiles.find((r) => r.index === index)
    if (pm && which !== 'warm')
      console.log(
        `| z${z} ${name} | pmtiles z${pm.tileZoom} | ${cell(pm[which])} (every style) | | |`,
      )
  }
}
console.log(`\nwritten ${out}`)
await printVerdict(values)
