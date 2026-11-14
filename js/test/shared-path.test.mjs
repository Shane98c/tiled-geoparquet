// node test/shared-path.test.mjs <file>
// A file where two columns share a dotted path is refused as it opens: a
// read of that path could not tell which column is meant.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { GeoParquetReader } from '../dist/index.js'

const [path] = process.argv.slice(2)
if (!path) throw new Error('shared-path.test.mjs needs a file')
const data = await readFile(path)
await assert.rejects(
  GeoParquetReader.open({
    byteLength: data.length,
    slice: async (a, b = data.length) => Uint8Array.from(data.subarray(a, b)).buffer,
  }),
  /two columns share the path a\.b/,
)
console.log('a file whose columns share a dotted path is refused')
