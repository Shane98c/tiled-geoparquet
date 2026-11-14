// Pass or fail for attributes, from the results in a build directory.
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

/** auto against none, geometry-only tiles, and auto against columns, tiles
 * styled by the attribute both hold, cold and next: bytes, with the same
 * requests and rounds */
export const SAME_BYTES = 0.02
/** auto, a style on the attribute only it keeps, on a reader that has styled
 * another tile by it: rounds at most */
export const OTHER_ROUNDS = 2
/** auto against columns, whole-session time at SESSION_LATENCY ms per request */
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
    (await denseZooms(dir, 'columns')) ??
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
    `auto vs none, geometry only, cold and next tile, every zoom: ${sameText}`,
    pair('auto', 'none', ['geometry'], ['cold', 'next'], every, same),
  )
  add(
    `auto vs columns, the attribute both hold, cold and next tile, every zoom: ${sameText}`,
    pair('auto', 'columns', ['named'], ['cold', 'next'], every, same),
  )
  add(
    'auto, the attribute both hold vs geometry only, cold and next tile, every zoom: the same rounds',
    single &&
      tiles('auto', 'named', every)?.flatMap((r) => {
        const plain = single.rows.find(
          (o) => o.variant === 'auto' && o.style === 'geometry' && o.index === r.index,
        )
        return ['cold', 'next'].map((read) => ({
          pass: r[read].rounds === plain[read].rounds,
          text: `z${r.z} ${r.tile}, ${read}: ${counts(r[read], plain[read])}${dense.has(r.z) ? ' (dense)' : ''}`,
        }))
      }),
  )
  add(
    `auto, the attribute only it keeps, a tile on a reader that has styled another: at most ${OTHER_ROUNDS} rounds`,
    single &&
      tiles('auto', 'other', every)?.map((r) => ({
        pass: r.warm.rounds <= OTHER_ROUNDS,
        text: `z${r.z} ${r.tile}: ${r.warm.requests} / ${kb(r.warm.bytes)} KB / ${r.warm.rounds}`,
      })),
  )
  const sessions =
    session && session.runs.filter((r) => r.variant === 'auto' && r.latency === SESSION_LATENCY)
  add(
    `auto vs columns, session at ${SESSION_LATENCY} ms per request: total time no more than ${SESSION_TIME * 100}% longer, each step reported`,
    sessions?.length
      ? sessions.flatMap((a) => {
          const c = session.runs.find(
            (r) =>
              r.variant === 'columns' && r.latency === a.latency && r.connections === a.connections,
          )
          const limit = a.connections ? `${a.connections} connections` : 'no connection limit'
          return [
            {
              pass: ratio(a.totalMs, c.totalMs) <= SESSION_TIME,
              text: `${limit}, total: ${Math.round(a.totalMs)} vs ${Math.round(c.totalMs)} ms, ${signed(ratio(a.totalMs, c.totalMs))}`,
            },
            ...a.steps.map((step, i) => ({
              pass: true,
              text: `${limit}, step ${i + 1} ${step.label} z${step.zoom} ${step.style}: ${step.requests} / ${kb(step.bytes)} KB / ${step.rounds} / ${Math.round(step.ms)} ms vs ${c.steps[i].requests} / ${kb(c.steps[i].bytes)} KB / ${c.steps[i].rounds} / ${Math.round(c.steps[i].ms)} ms`,
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
          `${name}: ${v.sizes.file_bytes.toLocaleString()} bytes, built in ${v.build_s} s`,
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
