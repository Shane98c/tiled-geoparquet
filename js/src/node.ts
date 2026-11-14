/// <reference types="node" />
/** Node.js pieces for serving a file: a byte source over a local path and an
 * adapter from a `Request`/`Response` handler to `node:http`. */
import { open } from 'node:fs/promises'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { RangeSource } from './reader.js'

/** A local file as a byte source. It reads the file as it is when opened: a
 * file moved into its place later is not picked up. */
export async function fileSource(path: string): Promise<RangeSource & { close(): Promise<void> }> {
  const file = await open(path, 'r')
  const byteLength = (await file.stat()).size
  return {
    byteLength,
    async slice(start, end = byteLength, signal) {
      signal?.throwIfAborted()
      if (
        !Number.isSafeInteger(start) ||
        !Number.isSafeInteger(end) ||
        start < 0 ||
        end < start ||
        end > byteLength
      )
        throw new Error('invalid byte range')
      const bytes = new Uint8Array(end - start)
      for (let filled = 0; filled < bytes.length;) {
        const { bytesRead } = await file.read(bytes, filled, bytes.length - filled, start + filled)
        if (!bytesRead) throw new Error(`truncated read from ${path}`)
        filled += bytesRead
      }
      return bytes.buffer
    },
    close: () => file.close(),
  }
}

/** A `node:http` or `node:https` request listener that answers with
 * `handler`. The request's signal aborts when the client disconnects before
 * the response is sent. */
export const nodeListener =
  (handler: (request: Request) => Promise<Response>) =>
  async (incoming: IncomingMessage, outgoing: ServerResponse): Promise<void> => {
    const controller = new AbortController()
    outgoing.on('close', () => {
      if (!outgoing.writableFinished) controller.abort()
    })
    let request: Request
    try {
      const scheme = (incoming.socket as { encrypted?: boolean }).encrypted ? 'https' : 'http'
      const headers = new Headers()
      for (const [name, value] of Object.entries(incoming.headers))
        if (value !== undefined) headers.set(name, Array.isArray(value) ? value.join(', ') : value)
      request = new Request(`${scheme}://${incoming.headers.host}${incoming.url}`, {
        method: incoming.method,
        headers,
        signal: controller.signal,
      })
    } catch {
      // a Host, target or method that no Request can be made of
      outgoing.writeHead(400, { 'Content-Length': 0 }).end()
      return
    }
    let response: Response
    let body: Buffer
    try {
      response = await handler(request)
      body = Buffer.from(await response.arrayBuffer())
    } catch (error) {
      console.error(error)
      response = new Response(null, { status: 500 })
      body = Buffer.alloc(0)
    }
    const headers: Record<string, string | number> = { 'Content-Length': body.byteLength }
    response.headers.forEach((value, name) => {
      headers[name] = value
    })
    outgoing.writeHead(response.status, headers).end(body)
  }
