// A row read of flat columns on an indexed file locates each column's page
// through that column's row directory, without the footer or any Parquet page
// index: a leaf, then a page, per column. Other columns open the footer in one
// request.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { coalescing, openReader } from '../dist/index.js'

const [path, casesPath] = process.argv.slice(2)
if (!casesPath) throw new Error('row-read.test.mjs needs a file and its cases')
const data = await readFile(path)
const { row, columns, expected, indexes, xmin } = JSON.parse(await readFile(casesPath, 'utf8'))
const reads = []
const reader = await openReader(
  coalescing(
    {
      byteLength: data.length,
      async slice(a, b) {
        reads.push([a, b])
        return Uint8Array.from(data.subarray(a, b)).buffer
      },
    },
    { windowMs: 1 },
  ),
)
reads.length = 0

const values = await reader.readRow(row, columns)
assert.deepEqual(values, expected)

const footerStart = data.length - 8 - data.readUInt32LE(data.length - 8)
const footerReads = () => reads.filter(([, b]) => b > footerStart).length
assert.equal(footerReads(), 0, 'a row read of flat columns skips the footer')
const [first, last] = indexes
assert.equal(reads.filter(([a, b]) => a < last && b > first).length, 0, 'and the offset indexes')
const requests = reads.length
assert.ok(requests <= 2 * columns.length, 'a directory leaf and a page per column')
reads.length = 0
assert.deepEqual(await reader.readRow(row + 1, columns), await reader.readRow(row + 1, columns))
assert.ok(reads.length <= columns.length, 'cached leaves locate the next row')

const nested = await reader.readColumns([row], ['bbox.xmin'])
assert.equal(nested['bbox.xmin'][0], xmin)
assert.equal(footerReads(), 1, 'other columns open the footer and its trailer in one request')
console.log('row read:', JSON.stringify({ requests }))
