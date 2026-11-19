// Local comparison server: expose only the demo and explicitly supplied files.
// npm --prefix js run demo:compare -- --lod /path/file.parquet [--pmtiles /path/file.pmtiles]
import { createServer } from 'node:http'
import { createReadStream } from 'node:fs'
import { open, stat } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'
import { pipeline } from 'node:stream/promises'

const { values } = parseArgs({
  options: {
    lod: { type: 'string' },
    pmtiles: { type: 'string' },
    cogp: { type: 'string' },
    esri: { type: 'string' },
    center: { type: 'string' },
    zoom: { type: 'string' },
    port: { type: 'string', default: '8094' },
  },
})
if (!values.lod)
  throw new Error('Supply --lod /path/file.parquet [--pmtiles /path/file.pmtiles] [--port 8094]')
const root = fileURLToPath(new URL('.', import.meta.url))
const files = new Map([['/data/current.parquet', resolve(values.lod)]])
if (values.pmtiles) files.set('/data/baseline.pmtiles', resolve(values.pmtiles))
if (values.cogp) files.set('/data/baseline.cogp.parquet', resolve(values.cogp))
if (values.esri) files.set('/data/baseline.sop.parquet', resolve(values.esri))
for (const path of files.values()) {
  if (!(await stat(path)).isFile()) throw new Error(`Not a file: ${path}`)
}
const port = Number(values.port)
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port')
/** The centre and zoom that show the whole file, from its bounds. */
async function wholeFile(path) {
  const { openReader } = await import('./dist/index.js')
  const file = await open(path)
  try {
    const byteLength = (await file.stat()).size
    const reader = await openReader({
      byteLength,
      async slice(start, end = byteLength) {
        const bytes = new Uint8Array(end - start)
        await file.read(bytes, 0, bytes.length, start)
        return bytes.buffer
      },
    })
    const [west, south, east, north] = reader.bounds
    const zoom = Math.log2(360 / Math.max(east - west, north - south, 1e-6))
    return {
      center: `${((west + east) / 2).toFixed(5)},${((south + north) / 2).toFixed(5)}`,
      zoom: Math.min(16, Math.max(0, zoom)).toFixed(2),
    }
  } finally {
    await file.close()
  }
}
const view = values.center && values.zoom ? {} : await wholeFile(files.get('/data/current.parquet'))
const params = new URLSearchParams({
  url: '/data/current.parquet',
  center: values.center ?? view.center,
  zoom: values.zoom ?? view.zoom,
  basemap: '0',
  demoServer: '1',
  latency: '50',
  panes: 'lod,pmtiles',
})
if (values.pmtiles) params.set('pmtiles', '/data/baseline.pmtiles')
if (values.cogp) params.set('cogp', '/data/baseline.cogp.parquet')
if (values.esri) params.set('esri', '/data/baseline.sop.parquet')
const comparePath = '/demo/compare.html?' + params
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost')
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405, { Allow: 'GET, HEAD' }).end()
      return
    }
    if (url.pathname === '/') {
      res.writeHead(302, { Location: comparePath }).end()
      return
    }
    let path = files.get(url.pathname)
    const data = !!path
    if (!path) {
      if (!url.pathname.startsWith('/demo/')) {
        res.writeHead(404).end()
        return
      }
      path = resolve(root, decodeURIComponent(url.pathname.slice('/demo/'.length)))
      if (
        !path.startsWith(root.endsWith(sep) ? root : root + sep) ||
        !(
          path.endsWith('.html') ||
          path.startsWith(resolve(root, 'dist') + sep) ||
          (path.startsWith(resolve(root, 'deck') + sep) && path.endsWith('.js'))
        )
      ) {
        res.writeHead(404).end()
        return
      }
    }
    const info = await stat(path)
    if (!info.isFile()) {
      res.writeHead(404).end()
      return
    }
    const size = info.size
    let start = 0,
      end = size - 1,
      status = 200
    if (data && req.headers.range) {
      const match = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range)
      start = match ? Number(match[1]) : NaN
      end = match?.[2] ? Math.min(size - 1, Number(match[2])) : size - 1
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || start > end) {
        res.writeHead(416, { 'Content-Range': `bytes */${size}` }).end()
        return
      }
      status = 206
    }
    if (data) {
      const delay = Number(url.searchParams.get('latency') ?? 0)
      if (![0, 50, 100].includes(delay)) {
        res.writeHead(400).end('Invalid latency')
        return
      }
      if (delay) await new Promise((done) => setTimeout(done, delay))
    }
    if (res.destroyed) return
    const headers = {
      'Content-Type': types[extname(path)] ?? 'application/octet-stream',
      'Content-Length': end - start + 1,
      'Cache-Control': 'no-store',
      'Accept-Ranges': 'bytes',
    }
    if (status === 206) headers['Content-Range'] = `bytes ${start}-${end}/${size}`
    res.writeHead(status, headers)
    if (req.method === 'HEAD' || size === 0) {
      res.end()
      return
    }
    await pipeline(createReadStream(path, { start, end }), res)
  } catch (error) {
    if (res.destroyed) return
    if (!res.headersSent) res.writeHead(error.code === 'ENOENT' ? 404 : 500)
    res.end()
  }
})
server.listen(port, '127.0.0.1', () => {
  console.log(`Compare: http://127.0.0.1:${port}${comparePath}`)
  console.log('All data files use the selected per-request delay; bandwidth is not limited.')
})
