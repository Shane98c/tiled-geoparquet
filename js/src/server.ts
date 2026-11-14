/**
 * One file's Mapbox Vector Tiles and TileJSON over HTTP, as a handler on the
 * web-standard `Request` and `Response`: `GET /{z}/{x}/{y}.mvt` (or `.pbf`)
 * and `GET /tiles.json`. It runs wherever those exist, with any reader
 * `openReader` returns.
 */
import { attributeKind } from './decode.js'
import { MAX_ZOOM, sourceLimits } from './source-limits.js'
import type { TileReader } from './spatial.js'
import { packedAttributes, renderTile } from './tile.js'

export interface TileHandlerOptions {
  /** MVT layer name (default `features`). */
  layer?: string
  /** Attribute names to copy into tile properties (default: the packed attributes, none without LODs). */
  properties?: string[]
  /** `maxzoom` in the TileJSON (default: the finest LOD zoom, 22 without LODs). Tiles are served at any zoom. */
  maxzoom?: number
  /** Path the handler is mounted under, such as `/buildings` (default: the root). */
  base?: string
  /** URL clients reach the handler at, for the TileJSON's tile URLs, when a
   * proxy changes the scheme, host or path a request arrives with. */
  publicUrl?: string
  /** gzip tiles for clients that accept it (default true). */
  compress?: boolean
}

const CORS = { 'Access-Control-Allow-Origin': '*' }
/** Smaller tiles gain nothing from gzip: its framing outweighs what it saves. */
const MIN_GZIP_BYTES = 1024

const text = (status: number, message: string, headers: Record<string, string> = {}): Response =>
  new Response(message, {
    status,
    headers: { ...CORS, 'Content-Type': 'text/plain; charset=utf-8', ...headers },
  })

/** A canonical decimal tile coordinate, or NaN. */
const coordinate = (value: string): number => (/^(0|[1-9]\d*)$/.test(value) ? Number(value) : NaN)

const acceptsGzip = (header: string | null): boolean =>
  (header ?? '').split(',').some((part) => {
    const [coding, ...parameters] = part
      .trim()
      .toLowerCase()
      .split(/\s*;\s*/)
    const weight = parameters.find((parameter) => parameter.startsWith('q='))
    return (coding === 'gzip' || coding === 'x-gzip') && (!weight || Number(weight.slice(2)) > 0)
  })

const gzip = (bytes: ArrayBuffer): Promise<ArrayBuffer> =>
  new Response(new Blob([bytes]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer()

export function tileHandler(
  reader: TileReader,
  options: TileHandlerOptions = {},
): (request: Request) => Promise<Response> {
  const { layer = 'features', properties, compress = true } = options
  const base = (options.base ?? '').replace(/\/+$/, '')
  const publicUrl = options.publicUrl?.replace(/\/+$/, '')
  const limits = sourceLimits(reader, options.maxzoom)
  const carried = new Map((reader.lod?.attributes ?? []).map((a) => [a.name, a.type]))
  // TileJSON describes a field in words. A carried attribute's type is in the
  // file's metadata; any other column's is known only from its footer.
  const describe = (name: string): string => {
    const type = carried.get(name)
    if (type === undefined) return 'Source column'
    const kind = attributeKind(type)
    return kind === 'string' ? 'String' : kind === 'bool' ? 'Boolean' : 'Number'
  }
  const fields = Object.fromEntries(
    (properties ?? packedAttributes(reader.lod)).map((name) => [name, describe(name)]),
  )

  return async (request) => {
    if (request.method === 'OPTIONS')
      return new Response(null, {
        status: 204,
        headers: {
          ...CORS,
          'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
          'Access-Control-Allow-Headers':
            request.headers.get('Access-Control-Request-Headers') ?? '*',
          'Access-Control-Max-Age': '86400',
        },
      })
    if (request.method !== 'GET' && request.method !== 'HEAD')
      return text(405, 'Method not allowed', { Allow: 'GET, HEAD, OPTIONS' })
    const url = new URL(request.url)
    if (!url.pathname.startsWith(`${base}/`)) return text(404, 'Not found')
    const path = url.pathname.slice(base.length)
    if (path === '/tiles.json')
      return new Response(
        JSON.stringify({
          tilejson: '3.0.0',
          tiles: [`${publicUrl ?? url.origin + base}/{z}/{x}/{y}.mvt`],
          ...limits,
          vector_layers: [{ id: layer, fields }],
        }),
        { headers: { ...CORS, 'Content-Type': 'application/json' } },
      )
    const address = /^\/([^/]+)\/([^/]+)\/([^/]+)\.(?:mvt|pbf)$/.exec(path)
    if (!address) return text(404, 'Not found')
    const [z, x, y] = address.slice(1).map(coordinate)
    if (!(z <= MAX_ZOOM && x < 2 ** z && y < 2 ** z)) return text(400, 'Invalid tile address')
    try {
      let tile = await renderTile(reader, z, x, y, { layer, properties, signal: request.signal })
      const headers: Record<string, string> = {
        ...CORS,
        'Content-Type': 'application/vnd.mapbox-vector-tile',
      }
      if (compress) {
        headers.Vary = 'Accept-Encoding'
        if (
          tile.byteLength >= MIN_GZIP_BYTES &&
          acceptsGzip(request.headers.get('Accept-Encoding'))
        ) {
          tile = await gzip(tile)
          headers['Content-Encoding'] = 'gzip'
        }
      }
      // Cloudflare Workers gzips a body labelled gzip unless told it is
      // encoded already; other runtimes send it as given and ignore the field.
      return new Response(tile, { headers, encodeBody: 'manual' } as ResponseInit)
    } catch (error) {
      // the client has gone, so no one reads this status; it marks the request in logs
      if (request.signal.aborted) return new Response(null, { status: 499, headers: CORS })
      console.error(error)
      return text(500, 'Tile read failed')
    }
  }
}
