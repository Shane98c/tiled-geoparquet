import assert from 'node:assert/strict'
import { gzipSync } from 'node:zlib'
import { httpSource, openReader } from '../dist/index.js'

const realFetch = globalThis.fetch
const file = new Uint8Array(128 * 1024)
file.set(new TextEncoder().encode('PAR1'))
file.set(new TextEncoder().encode('LODIDX01'), 4)
const setRoot = (root) => {
  const packed = gzipSync(JSON.stringify(root))
  file.fill(0, 12, 16384)
  new DataView(file.buffer).setUint32(12, packed.length, true)
  file.set(packed, 16)
}
const calls = []
let exposeRange = true,
  truncate = false,
  status = 206,
  served = file.length,
  failTail = 0
let failOpeners = false,
  refuseOpeners = false,
  brokenOpeners = false,
  wholeFile = false,
  pulled = 0,
  cancelled = 0
const opener = (call) => call.headers?.Range === 'bytes=0-0'
// every request since `calls` was emptied names `sent` as its cache mode, or none
const allSent = (sent, message) => {
  assert.ok(calls.length, message)
  for (const call of calls)
    assert.deepEqual(
      [call.cache, Object.hasOwn(call, 'cache')],
      [sent, sent !== undefined],
      message,
    )
}
const MODES = [
  ['no-store', 'no-store'],
  ['reload', 'reload'],
  ['no-cache', 'no-cache'],
  ['force-cache', 'force-cache'],
  ['only-if-cached', 'only-if-cached'],
  ['default', undefined],
]
globalThis.fetch = (url, options = {}) => {
  // a runtime that takes no cache mode refuses the request before it is sent
  if (refuseOpeners && opener(options) && 'cache' in options)
    throw new TypeError("The 'cache' field is not implemented")
  return respond(url, options)
}
const respond = async (url, options) => {
  calls.push({ url: String(url), ...options })
  if (options.method === 'HEAD')
    return new Response(null, { headers: { 'Content-Length': String(file.length) } })
  if (opener(options) && failOpeners) throw new TypeError('connection refused')
  if (opener(options) && brokenOpeners)
    return new Response(
      new ReadableStream({
        pull(controller) {
          controller.error(new TypeError('connection reset'))
        },
      }),
      { status: 206 },
    )
  if (opener(options) && wholeFile)
    return new Response(
      new ReadableStream({
        pull(controller) {
          pulled++
          controller.enqueue(new Uint8Array(65536))
        },
        cancel() {
          cancelled++
        },
      }),
      { status: 200 },
    )
  const [, a, b] = options.headers.Range.match(/bytes=(\d+)-(\d+)/)
  if (Number(a) >= served) return new Response(null, { status: 416 })
  if (failTail && Number(b) === file.length - 1 && Number(a) === file.length - 8) {
    failTail--
    return new Response(null, { status: 503 })
  }
  const end = Math.min(Number(b) + 1, served)
  return new Response(file.slice(Number(a), truncate ? end - 1 : end), {
    status,
    headers: exposeRange ? { 'Content-Range': `bytes ${a}-${end - 1}/${file.length}` } : {},
  })
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
try {
  // one-byte requests beside the opening read leave six connections open to each host
  const mirrors = ['https://a.test/data.parquet', 'https://b.test/data.parquet']
  const opening = new AbortController()
  await httpSource(mirrors, { signal: opening.signal })
  assert.deepEqual(
    calls.map((call) => [call.url, call.headers.Range]),
    [
      [mirrors[0], 'bytes=0-16383'],
      ...Array.from({ length: 5 }, () => [mirrors[0], 'bytes=0-0']),
      ...Array.from({ length: 6 }, () => [mirrors[1], 'bytes=0-0']),
    ],
    'the first host has its opening read and five more, each other host six',
  )
  assert.ok(
    calls
      .filter(opener)
      .every((call) => call.cache === 'no-store' && call.signal === opening.signal),
    'they bypass the cache and stop with the opening signal',
  )
  assert.equal(calls[0].cache, 'no-store', 'the opening read bypasses the cache')
  // every request of a source carries its cache mode, and `default` names none
  for (const [cache, sent] of MODES) {
    calls.length = 0
    const moded = await httpSource(mirrors, { cache, connections: 1 })
    await moded.slice(20000, 20010)
    assert.equal(calls.length, 2)
    allSent(sent, `the opening read and a slice are sent with cache ${cache}`)
  }
  calls.length = 0
  await httpSource(mirrors, { connections: 3 })
  assert.equal(
    calls.filter(opener).length,
    5,
    'three connections a host are two and three requests',
  )
  calls.length = 0
  await httpSource(mirrors, { connections: 1 })
  assert.equal(calls.length, 1, 'one connection sends the opening read alone')
  calls.length = 0
  await httpSource(mirrors, { cache: 'default' })
  assert.equal(calls.length, 1, 'through the cache a source opens with one connection')
  // a range parameter gives every range a URL of its own, through the cache,
  // with six connections opened past it as without one
  for (const [url, joined] of [
    [mirrors[0], '?'],
    [mirrors[0] + '?v=2', '&'],
  ]) {
    calls.length = 0
    const keyed = await httpSource([url], { rangeParam: 'range' })
    await keyed.slice(20000, 20010)
    assert.deepEqual(
      calls
        .filter((call) => !opener(call))
        .map((call) => [call.url, call.headers.Range, Object.hasOwn(call, 'cache')]),
      [
        [`${url}${joined}range=0-16383`, 'bytes=0-16383', false],
        [`${url}${joined}range=20000-20009`, 'bytes=20000-20009', false],
      ],
      'each range is requested under its own URL with no cache mode',
    )
    assert.deepEqual(
      calls.filter(opener).map((call) => [call.url, call.cache]),
      Array.from({ length: 5 }, () => [url, 'no-store']),
      'the connections open on the plain URL, past the cache',
    )
  }
  calls.length = 0
  await httpSource(mirrors, { rangeParam: 'range', cache: 'no-store', connections: 1 })
  assert.deepEqual(
    [calls.length, calls[0].url, calls[0].cache],
    [1, `${mirrors[0]}?range=0-16383`, 'no-store'],
    'a cache mode and a connection count given with it are kept',
  )
  await assert.rejects(httpSource(mirrors, { rangeParam: '' }), /rangeParam must name a parameter/)
  for (const connections of [0, 1.5, -1, NaN])
    await assert.rejects(
      httpSource(mirrors, { connections }),
      /connections must be a whole number/,
      `connections ${connections} is refused`,
    )
  // a host that refuses them, or answers one with the whole file, still opens,
  // and so does a runtime that refuses their cache mode as they are sent
  failOpeners = true
  assert.equal((await httpSource(mirrors)).byteLength, file.length)
  failOpeners = false
  refuseOpeners = true
  calls.length = 0
  assert.equal(
    (await httpSource(mirrors, { cache: 'default', connections: 6 })).byteLength,
    file.length,
  )
  assert.equal(calls.length, 1, 'the opening read goes out and the refused requests do not')
  refuseOpeners = false
  // nor does one whose answer breaks off as it is read fail the open
  brokenOpeners = true
  assert.equal((await httpSource(mirrors)).byteLength, file.length)
  await tick()
  brokenOpeners = false
  wholeFile = true
  assert.equal((await httpSource(mirrors)).byteLength, file.length)
  await tick()
  assert.equal(cancelled, 11, 'a whole-file answer is cancelled')
  assert.ok(pulled <= 22, `and not read: ${pulled} chunks pulled`)
  wholeFile = false
  await tick()
  calls.length = 0
  const source = await httpSource(['https://example.test/data.parquet'], { connections: 1 })
  assert.equal(calls.length, 1, 'indexed startup is one GET')
  assert.equal((await source.slice(0, 16384)).byteLength, 16384)
  assert.equal(calls.length, 1, 'openReader reuses the bootstrap')
  await assert.rejects(
    openReader(source),
    /root length/,
    'bad directory is rejected, not silently used',
  )
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(source.slice(0, 10, controller.signal), { name: 'AbortError' })
  await assert.rejects(source.slice(-1, 10), /invalid byte range/)
  truncate = true
  await assert.rejects(source.slice(20000, 20020), /truncated range/)
  truncate = false
  calls.length = 0
  exposeRange = false
  await httpSource(['https://example.test/data.parquet'], { connections: 1 })
  assert.equal(
    calls.length,
    2,
    'HEAD fallback when Content-Range is hidden and no root sizes the file',
  )
  for (const [cache, sent] of MODES) {
    calls.length = 0
    await httpSource(['https://example.test/data.parquet'], { cache, connections: 1 })
    assert.equal(calls.filter((call) => call.method === 'HEAD').length, 1)
    allSent(sent, `the HEAD is sent with cache ${cache}`)
  }
  setRoot({ version: 4, file_bytes: file.length })
  calls.length = 0
  file.set(new TextEncoder().encode('PAR1'), file.length - 4)
  const sized = await httpSource(['https://example.test/data.parquet'], { connections: 1 })
  assert.equal(calls.length, 1, 'the root sizes the file when Content-Range is hidden')
  assert.equal(sized.byteLength, file.length)
  assert.equal((await sized.slice(20000, 20010)).byteLength, 10)
  assert.equal(calls.length, 3, 'the first read past the bootstrap checks the footer magic')
  await sized.slice(30000, 30010)
  assert.equal(calls.length, 4, 'the footer magic is checked once')
  for (const [cache, sent] of MODES) {
    calls.length = 0
    const checked = await httpSource(['https://example.test/data.parquet'], {
      cache,
      connections: 1,
    })
    await checked.slice(20000, 20010)
    assert.equal(calls.length, 3)
    allSent(sent, `the footer magic check is sent with cache ${cache}`)
  }
  for (const version of [3, 5]) {
    setRoot({ version, file_bytes: file.length })
    await assert.rejects(
      openReader(await httpSource(['https://example.test/data.parquet'])),
      new RegExp(`unsupported spatial index version ${version}`),
      `a version-${version} root is rejected, not read as this version`,
    )
  }
  // the root's copy of the LOD metadata is checked as the footer's is
  const lod = {
    version: '0.2.0',
    margin: 2,
    encoding: 'packed',
    pack_version: 3,
    key: { column: 'hilbert', depth: 24 },
    zooms: [2],
    attributes: [],
    lods: { f2: { zooms: [2], geometry_type: 'Point', scale: [1, 1], offset: [0, 0] } },
  }
  for (const [changed, error] of [
    [{ margin: undefined }, /margin undefined is not a number/],
    [{ margin: -1 }, /margin -1 is not a number/],
    [{ margin: '2' }, /margin 2 is not a number/],
    [{ margin: 129 }, /margin 129 is not a number/],
    [{ version: '0.1.0' }, /version 0.1.0 is not compatible/],
    [{ pack_version: 2 }, /encoding packed\/2 is not packed\/3/],
  ]) {
    setRoot({ version: 4, file_bytes: file.length, lod: { ...lod, ...changed } })
    await assert.rejects(
      openReader(await httpSource(['https://example.test/data.parquet'])),
      error,
      `a root whose LOD metadata has ${JSON.stringify(changed)}`,
    )
  }
  setRoot({ version: 4, file_bytes: file.length, lod: undefined })
  await assert.rejects(
    openReader(await httpSource(['https://example.test/data.parquet'])),
    /root holds no LOD metadata/,
  )
  setRoot({ version: 4, file_bytes: file.length })
  // a server error on the check is retried by the next read
  failTail = 1
  const retried = await httpSource(['https://example.test/data.parquet'])
  await assert.rejects(retried.slice(20000, 20010), /range request failed: 503/)
  assert.equal((await retried.slice(20000, 20010)).byteLength, 10)
  // an object shorter than its root claims fails on its first data read
  served = file.length / 2
  const truncated = await httpSource(['https://example.test/data.parquet'])
  await assert.rejects(truncated.slice(20000, 20010), /range request failed|does not end/)
  served = file.length
  file.fill(0, file.length - 4)
  file.fill(0, 12, 16384)
  exposeRange = true
  calls.length = 0
  file.fill(0, 4, 12)
  new DataView(file.buffer).setUint32(file.length - 8, 200, true)
  const unindexed = await httpSource(['https://example.test/data.parquet'], { connections: 1 })
  assert.equal(calls.length, 2, 'unindexed startup needs head and tail, no redundant margin read')
  await unindexed.slice(file.length - 208, file.length)
  assert.equal(calls.length, 2)
  // a footer longer than the tail read is fetched in a further request
  for (const [cache, sent] of MODES) {
    calls.length = 0
    await httpSource(['https://example.test/data.parquet'], { cache, connections: 1, tail: 64 })
    assert.equal(calls.length, 3, 'the opening read, the tail and the rest of the footer')
    allSent(sent, `the tail and footer reads are sent with cache ${cache}`)
  }
  status = 200
  await assert.rejects(httpSource(['https://example.test/data.parquet']), /range request failed/)
} finally {
  globalThis.fetch = realFetch
}
console.log('HTTP bootstrap and range tests passed')
