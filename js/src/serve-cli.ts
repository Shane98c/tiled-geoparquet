#!/usr/bin/env node
/// <reference types="node" />
import { createServer } from 'node:http'
import { parseArgs } from 'node:util'
import { fileSource, nodeListener } from './node.js'
import { coalescing, httpSource } from './range-buffer.js'
import type { RangeSource } from './reader.js'
import { tileHandler } from './server.js'
import { MAX_ZOOM } from './source-limits.js'
import { openReader } from './spatial.js'

const NAME = 'tiled-geoparquet-serve'
const USAGE = `Usage: ${NAME} <path-or-url> [options]

Serve a Tiled GeoParquet or plain GeoParquet file as Mapbox Vector Tiles:
  /{z}/{x}/{y}.mvt   tiles (.pbf works too)
  /tiles.json        TileJSON

Options:
  --port <number>      port to listen on (default 8080; 0 picks a free one)
  --host <address>     address to listen on (default 127.0.0.1)
  --layer <name>       MVT layer name (default features)
  --properties <a,b>   attributes in the tiles (default: the packed attributes;
                       "" for none)
  --maxzoom <zoom>     maxzoom in tiles.json (default: the finest LOD zoom, 22
                       without LODs)
  --public-url <url>   URL clients reach this server at, for tiles.json, when a
                       proxy or tunnel is in front
  -h, --help           show this help
`

function fail(message: string, code = 1): never {
  console.error(`${NAME}: ${message}`)
  process.exit(code)
}

const parse = () => {
  try {
    return parseArgs({
      allowPositionals: true,
      options: {
        port: { type: 'string', default: '8080' },
        host: { type: 'string', default: '127.0.0.1' },
        layer: { type: 'string' },
        properties: { type: 'string' },
        maxzoom: { type: 'string' },
        'public-url': { type: 'string' },
        help: { type: 'boolean', short: 'h' },
      },
    })
  } catch (error) {
    return fail(`${(error as Error).message}\n\n${USAGE}`, 2)
  }
}

const { values, positionals } = parse()
if (values.help) {
  console.log(USAGE)
  process.exit(0)
}
if (positionals.length !== 1) fail(`expected one path or URL\n\n${USAGE}`, 2)
const [target] = positionals
const port = Number(values.port)
if (!Number.isInteger(port) || port < 0 || port > 65535) fail(`invalid port ${values.port}`, 2)
const maxzoom = values.maxzoom === undefined ? undefined : Number(values.maxzoom)
if (maxzoom !== undefined && !(Number.isInteger(maxzoom) && maxzoom >= 0 && maxzoom <= MAX_ZOOM))
  fail(`invalid maxzoom ${values.maxzoom}`, 2)

let listener: ReturnType<typeof nodeListener>
try {
  const source: RangeSource = /^https?:\/\//i.test(target)
    ? coalescing(await httpSource([target]))
    : await fileSource(target)
  listener = nodeListener(
    tileHandler(await openReader(source), {
      layer: values.layer,
      properties: values.properties?.split(',').filter(Boolean),
      maxzoom,
      publicUrl: values['public-url'],
    }),
  )
} catch (error) {
  fail(`cannot serve ${target}: ${(error as Error).message}`)
}

const server = createServer((incoming, outgoing) => void listener(incoming, outgoing))
server.on('error', (error) => fail(error.message))
server.listen(port, values.host, () => {
  const address = server.address()
  if (!address || typeof address === 'string') return
  const host = address.family === 'IPv6' ? `[${address.address}]` : address.address
  const origin = `http://${host}:${address.port}`
  console.log(`Serving ${target}`)
  console.log(`  TileJSON  ${origin}/tiles.json`)
  console.log(`  Tiles     ${origin}/{z}/{x}/{y}.mvt`)
})

const stop = () => {
  server.close(() => process.exit(0))
  server.closeAllConnections()
}
process.once('SIGINT', stop)
process.once('SIGTERM', stop)
