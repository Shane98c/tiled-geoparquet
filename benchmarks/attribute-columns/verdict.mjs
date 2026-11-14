// Pass or fail for attribute columns, from the results in a build directory.
//
//   node benchmarks/attribute-columns/verdict.mjs --dir BUILD_DIR
//
// Reads builds.json, the variants' writer logs, single-tiles.json and
// session.json; a check whose results are missing is reported as not measured.
// "Within x" means at most x heavier or slower: lighter or faster always passes.
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { kb, loadScenario, parse } from './lib.mjs'

/** hybrid against packed and auto against none, geometry-only and packed-attribute
 * tiles, cold and next: bytes, with the same requests and rounds */
export const SAME_BYTES = 0.02
/** hybrid against packed, a style on an attribute packed does not carry, on a
 * reader that has styled another tile by it: rounds at most, with fewer
 * requests than packed */
export const UNCARRIED_ROUNDS = 2
/** columns against packed, carried attribute, cold tiles at thinned zooms:
 * bytes, with no more rounds at any zoom */
export const STYLED_BYTES = 0.1
/** hybrid against packed, whole-session time at SESSION_LATENCY ms per request */
export const SESSION_TIME = 0
export const SESSION_LATENCY = 100

const readJson = async (path) =>
  path && existsSync(path) ? JSON.parse(await readFile(path, 'utf8')) : undefined

/** Zooms at which a variant's writer log shows every row, from its "rows shown" line. */
const denseZooms = async (dir, variant) => {
  const path = join(dir, `${variant}.log`)
  if (!existsSync(path)) return undefined
  const line = (await readFile(path, 'utf8')).split('\n').find((l) => l.includes('rows shown'))
  if (!line) return undefined
  const counts = [...line.matchAll(/z(\d+)=([\d,]+)/g)].map(([, z, n]) => [
    Number(z),
    Number(n.replace(/,/g, '')),
  ])
  const all = Math.max(...counts.map(([, n]) => n))
  return new Set(counts.filter(([, n]) => n === all).map(([z]) => z))
}

const ratio = (a, b) => (b ? a / b - 1 : a ? Infinity : 0)
const signed = (r) => `${r >= 0 ? '+' : ''}${(r * 100).toFixed(1)}%`

export async function verdict({ dir, single: singlePath, session: sessionPath, scenario }) {
  const builds = await readJson(join(dir, 'builds.json'))
  const single = await readJson(singlePath ?? join(dir, 'single-tiles.json'))
  const session = await readJson(sessionPath ?? join(dir, 'session.json'))
  const dense =
    (await denseZooms(dir, 'packed')) ??
    (scenario?.dense_from !== undefined
      ? { has: (z) => z >= scenario.dense_from }
      : { has: () => false })
  const checks = []
  const add = (name, results) => {
    if (!results) return checks.push({ name, status: 'not measured', details: [] })
    if (!results.length) return checks.push({ name, status: 'no cases', details: [] })
    checks.push({
      name,
      status: results.every((r) => r.pass) ? 'PASS' : 'FAIL',
      details: results.map((r) => `${r.pass ? 'ok  ' : 'FAIL'} ${r.text}`),
    })
  }
  const tiles = (variant, style, zooms) =>
    single?.rows.filter((r) => r.variant === variant && r.style === style && zooms(r.z))
  /** `test` of each of variant `a`'s tiles at `zooms` against `b`'s, per
   * style and read: `cold`, `next` or `warm`. */
  const pair = (a, b, styles, reads, zooms, test) => {
    if (!styles.every((style) => tiles(a, style, zooms)?.length && tiles(b, style, zooms)?.length))
      return undefined
    return styles.flatMap((style) =>
      tiles(a, style, zooms).flatMap((r) => {
        const other = single.rows.find(
          (o) => o.variant === b && o.style === style && o.index === r.index,
        )
        return reads.map((read) =>
          test(r[read], other[read], `z${r.z} ${r.tile}, ${style}, ${read}`, dense.has(r.z)),
        )
      }),
    )
  }
  const counts = (x, y) =>
    `${x.requests} / ${kb(x.bytes)} KB / ${x.rounds} vs ${y.requests} / ${kb(y.bytes)} KB / ${y.rounds}`
  const same = (x, y, name) => ({
    pass:
      x.requests === y.requests && x.rounds === y.rounds && ratio(x.bytes, y.bytes) <= SAME_BYTES,
    text: `${name}: ${counts(x, y)}, ${signed(ratio(x.bytes, y.bytes))} bytes`,
  })
  const every = () => true
  const sameText = `same requests and rounds, bytes within ${SAME_BYTES * 100}%`

  add(
    `hybrid vs packed, geometry only and carried attribute, cold and next tile, every zoom: ${sameText}`,
    pair('hybrid', 'packed', ['geometry', 'carried'], ['cold', 'next'], every, same),
  )
  add(
    `auto vs none, geometry only, cold and next tile, every zoom: ${sameText}`,
    pair('auto', 'none', ['geometry'], ['cold', 'next'], every, same),
  )
  add(
    `hybrid vs packed, un-carried attribute, a tile on a reader that has styled another: at most ${UNCARRIED_ROUNDS} rounds and fewer requests`,
    pair('hybrid', 'packed', ['uncarried'], ['warm'], every, (h, p, name) => ({
      pass: h.rounds <= UNCARRIED_ROUNDS && h.requests < p.requests,
      text: `${name}: ${counts(h, p)}, ${signed(ratio(h.bytes, p.bytes))} bytes`,
    })),
  )
  add(
    `columns vs packed, carried attribute, cold tiles: no more rounds; bytes within ${STYLED_BYTES * 100}% at thinned zooms, reported at dense ones`,
    pair('columns', 'packed', ['carried'], ['cold'], every, (c, p, name, isDense) => ({
      pass: c.rounds <= p.rounds && (isDense || ratio(c.bytes, p.bytes) <= STYLED_BYTES),
      text: `${name}${isDense ? ' (dense)' : ''}: ${counts(c, p)}, ${signed(ratio(c.bytes, p.bytes))} bytes`,
    })),
  )
  const sessions =
    session && session.runs.filter((r) => r.variant === 'hybrid' && r.latency === SESSION_LATENCY)
  add(
    `hybrid vs packed, session at ${SESSION_LATENCY} ms per request: total time no more than ${SESSION_TIME * 100}% longer, each step reported`,
    sessions?.length
      ? sessions.flatMap((h) => {
          const p = session.runs.find(
            (r) =>
              r.variant === 'packed' && r.latency === h.latency && r.connections === h.connections,
          )
          const limit = h.connections ? `${h.connections} connections` : 'no connection limit'
          return [
            {
              pass: ratio(h.totalMs, p.totalMs) <= SESSION_TIME,
              text: `${limit}, total: ${Math.round(h.totalMs)} vs ${Math.round(p.totalMs)} ms, ${signed(ratio(h.totalMs, p.totalMs))}`,
            },
            ...h.steps.map((step, i) => ({
              pass: true,
              text: `${limit}, step ${i + 1} ${step.label} z${step.zoom} ${step.style}: ${step.requests} / ${kb(step.bytes)} KB / ${step.rounds} / ${Math.round(step.ms)} ms vs ${p.steps[i].requests} / ${kb(p.steps[i].bytes)} KB / ${p.steps[i].rounds} / ${Math.round(p.steps[i].ms)} ms`,
            })),
          ]
        })
      : undefined,
  )
  const variants = builds && Object.entries(builds.variants)
  if (variants)
    checks.push({
      name: 'file size and build time, reported',
      status: 'REPORT',
      details: variants.map(
        ([name, v]) =>
          `${name}: ${v.storage.file_bytes.toLocaleString()} bytes, built in ${v.build_s} s`,
      ),
    })
  return checks
}

export async function printVerdict(values) {
  if (!values.dir) {
    console.log('\nVerdict: needs --dir, where builds.json and the results are')
    return
  }
  const scenario = await loadScenario(values.scenario).catch(() => undefined)
  const checks = await verdict({ dir: values.dir, scenario })
  console.log('\n## Verdict\n')
  for (const check of checks) {
    console.log(`${check.status.padEnd(12)} ${check.name}`)
    for (const line of check.details) console.log(`             ${line}`)
  }
  const measured = checks.filter((c) => c.status === 'PASS' || c.status === 'FAIL')
  const failed = measured.filter((c) => c.status === 'FAIL').length
  const unmeasured = checks.filter(
    (c) => c.status === 'not measured' || c.status === 'no cases',
  ).length
  console.log(
    `\n${failed ? 'FAIL' : 'PASS'}: ${measured.length - failed} of ${measured.length} measured checks pass` +
      (unmeasured ? `, ${unmeasured} not measured` : ''),
  )
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await printVerdict(parse())
