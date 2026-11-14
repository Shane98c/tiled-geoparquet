// Checks one step of the session replay against independent measurements.
//
//   node benchmarks/attribute-columns/check-session.mjs --file label=path
//     [--scenario denver] [--named height] [--other class] [--step 2] [--latency 100]
//
// The step runs alone on a fresh reader, and each of its tiles runs alone on
// a fresh reader of its own. The bytes the tiles fetch between them, less what
// two or more of them fetched (what a shared cache saves), must be what the
// step fetched, apart from the gaps coalescing reads between wanted ranges;
// no byte may be fetched twice in the step, and every byte a tile needed must
// be in it. Then the step runs at 0 ms and at the added latency: the
// difference should be about one latency per round, not per request.
import { absolute, coveredRanges, kb, loadScenario, parse, sessionRun, styles } from './lib.mjs'
import { variantFiles } from './lib.mjs'

const values = parse({
  step: { type: 'string', default: '1' },
  latency: { type: 'string', default: '100' },
  repeat: { type: 'string', default: '3' },
})
const [[label, path]] = await variantFiles(values)
const scenario = await loadScenario(values.scenario)
const properties = styles(values)
const index = Number(values.step) - 1
const style = scenario.route.slice(0, index + 1).findLast((s) => s.style)?.style ?? 'geometry'
const step = { ...scenario.route[index], style, log: true }
// spacing requests the way single-tiles.mjs does keeps the coalescing windows comparable
const SPACING = 20
const job = (route, latency = SPACING) => ({
  kind: 'lod',
  path: absolute(path),
  latency,
  connections: 0,
  scenario,
  properties,
  route,
})

const session = (await sessionRun(job([step]))).steps[0]
const singles = []
for (const tile of session.tileList)
  singles.push((await sessionRun(job([{ ...step, tiles: [tile] }]))).steps[0])

const length = (ranges) => [...ranges.values()].flat().reduce((n, [s, e]) => n + e - s, 0)
const intersect = (a, b) => {
  let n = 0
  for (const [url, xs] of a)
    for (const [s, e] of xs)
      for (const [t, f] of b.get(url) ?? []) n += Math.max(0, Math.min(e, f) - Math.max(s, t))
  return n
}
const sum = (field) => singles.reduce((n, s) => n + s[field], 0)
const needed = coveredRanges(singles.flatMap((s) => s.log))
const fetched = coveredRanges(session.log)
const neededBytes = length(needed)
const fetchedBytes = length(fetched)
const saved = sum('bytes') - neededBytes
const covered = intersect(needed, fetched)
const twice = session.bytes - fetchedBytes
const gaps = fetchedBytes - covered
// a gap byte is coalescing's only if the request holding it starts and ends on wanted bytes
const edges = session.log.every(([, url, range, n]) => {
  const start = Number(/^bytes=(\d+)-/.exec(range)[1])
  const inside = (x) => (needed.get(url) ?? []).some(([s, e]) => x >= s && x < e)
  return inside(start) && inside(start + n - 1)
})
const distinct = new Set(singles.flatMap((s) => s.log.map(([, url, range]) => `${url} ${range}`)))

console.log(
  `${label}, step ${index + 1} "${step.label}" z${step.zoom}, ${style} style, ${session.tileList.length} tiles\n`,
)
console.log(`| | requests | bytes | features |\n|---|---|---|---|`)
singles.forEach((s, i) =>
  console.log(
    `| tile ${session.tileList[i].join('/')} alone | ${s.requests} | ${s.bytes.toLocaleString()} | ${s.features.toLocaleString()} |`,
  ),
)
console.log(
  `| sum of the tiles alone | ${sum('requests')} | ${sum('bytes').toLocaleString()} | ${sum('features').toLocaleString()} |`,
)
console.log(
  `| the step | ${session.requests} | ${session.bytes.toLocaleString()} | ${session.features.toLocaleString()} |`,
)
console.log(`
Bytes: ${sum('bytes').toLocaleString()} summed over the tiles
  - ${saved.toLocaleString()} fetched by more than one tile (what shared caches save)
  = ${neededBytes.toLocaleString()} distinct bytes the tiles need
  + ${gaps.toLocaleString()} gap bytes read by coalescing between wanted ranges
  + ${twice.toLocaleString()} bytes the step fetched twice
  = ${session.bytes.toLocaleString()} bytes the step reports
  wanted bytes the step missed: ${(neededBytes - covered).toLocaleString()}; every step request starts and ends on wanted bytes: ${edges}
Requests: ${sum('requests')} summed over the tiles, ${distinct.size} distinct ranges among them, ${session.requests} in the step
Features: ${sum('features') === session.features ? 'same' : 'DIFFERENT'} in the step as in the tiles alone`)
const bytesOk =
  neededBytes === covered && twice === 0 && edges && sum('features') === session.features
console.log(`Byte check: ${bytesOk ? 'PASS' : 'FAIL'}`)

const latency = Number(values.latency)
const fastest = async (ms) => {
  const runs = []
  for (let i = 0; i < Number(values.repeat); i++)
    runs.push((await sessionRun(job([{ ...step, log: false }], ms))).steps[0])
  return runs.sort((a, b) => a.ms - b.ms)[0]
}
const [base, slow] = [await fastest(0), await fastest(latency)]
const added = slow.ms - base.ms
console.log(`
Latency: ${Math.round(base.ms)} ms at 0 ms, ${Math.round(slow.ms)} ms at ${latency} ms per request (fastest of ${values.repeat})
  added ${Math.round(added)} ms over ${slow.rounds} rounds and ${slow.requests} requests: ${(added / slow.rounds).toFixed(0)} ms per round, ${(added / slow.requests).toFixed(0)} ms per request`)
const latencyOk = Math.abs(added / slow.rounds - latency) <= latency * 0.25
console.log(`Latency check (within 25% of ${latency} ms per round): ${latencyOk ? 'PASS' : 'FAIL'}`)
