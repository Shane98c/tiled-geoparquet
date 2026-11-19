# Contributing

This is a draft format with one implementation, so expect the layout to
change while it is being evaluated.

## Development

Use Python 3.12+, Node 22+, and uv. From the repository root:

```sh
uv sync --locked --group dev
npm --prefix js ci
npm --prefix js run build
uv run ruff check python tests examples benchmarks
uv run pytest -q
npm --prefix js test
npm --prefix js run demo:bundle
```

Build JavaScript before running pytest: Python integration tests invoke the JS
reader against generated fixtures. The standalone JS spill test skips without
a fixture; pytest supplies one and checks it. Tests generate their own small
data and do not need the Japan archives. DuckDB may download its spatial
extension on first use. CI runs both language suites and bundles the demo.

For a build smoke test without an external dataset:

```sh
uv run python examples/quickstart.py /tmp/lod-example
uv run tiled-geoparquet validate /tmp/lod-example/buildings.lod.parquet
```

This creates 5,000 synthetic rectangles to check the build pipeline, not to
demonstrate realistic map performance. Choose a fresh output directory; the
example does not overwrite existing files.

For a local comparison with your own archives:

```sh
npm --prefix js run demo:compare -- --lod /path/indexed.parquet --pmtiles /path/baseline.pmtiles
```

Optional `--cogp` and `--esri` paths enable the other readers; the
[reader guide](js/README.md#demo) describes the page. For a scripted
five-reader measurement, see
[the comparison page](docs/comparison.md#reproducing).

## The hosted page

The public comparison at <https://tiled-geoparquet.pages.dev/> is a static
build of `demo/` pointed at the hosted files
([Files](docs/comparison.md#files)). Deploying it needs write access to the
Cloudflare Pages project, and uploading a file needs write access to the S3
bucket. From the repository root, bundle the reader and build a separate
static folder:

```sh
npm --prefix js run demo:bundle
node demo/build-share.mjs \
  --out /private/tmp/geoparquet-lod-share \
  --url https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/5503c5a/japan_buildings.parquet \
  --pmtiles https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.pmtiles \
  --cogp https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.cogp-2.2.2.parquet \
  --esri https://carbonplan-share.s3.us-west-2.amazonaws.com/tiled-geoparquet/releases/6c3a4f9/japan_buildings.sop.parquet \
  --release 5503c5a \
  --hide id \
  --style class \
  --domains demo/japan-domains.json
wrangler pages deploy /private/tmp/geoparquet-lod-share \
  --project-name tiled-geoparquet --branch main
```

The `--domains` option takes a JSON file of fixed colour scales.
`demo/japan-domains.json` sets the scales for `facade_color` and `roof_color`.
These two columns in the style menu are looked up in the table, so the
GeoParquet file has no summary for them. `demo/domains.py` prints scales for
the columns without a summary, so generate the JSON from a local copy of the
file with `uv run python demo/domains.py japan_buildings.parquet > demo/japan-domains.json`.

`demo/build-share.mjs` supplies public defaults only when query parameters are absent, so
links can override the initial view, style, or datasets. It leaves the local
demo configuration unchanged. Upload new datasets under a new release prefix instead
of replacing immutable objects:

```sh
aws s3 cp japan_buildings.parquet \
  s3://carbonplan-share/tiled-geoparquet/releases/<tag>/japan_buildings.parquet \
  --cache-control "public, max-age=31536000, immutable"
```

Upload with `aws s3 cp`, which sends 8 MiB parts: three of these files
uploaded by rclone in 64 MiB parts answered each request about 0.06 s later
than the same bytes in 8 MiB parts.

## Changes and reports

- For a bug, say what data you used, what you expected and what happened. A
  small synthetic example is easier to work with than a large private
  dataset.
- A format change updates `spec/` along with the writer and reader, with a
  test that reads back what was written. The source geometry and attributes
  stay readable as an ordinary table. Changing the row order changes the row
  IDs a map uses.
- For a performance report, it helps to have the commit, how the file was
  built, the reader versions, the tiles or route read, the style, the
  browser and device, the network, and whether caches were warm. Feature
  counts and any visible differences help too, beside bytes, requests and
  timing.
- If you compare with another tool, say whether you ran its release as
  published or changed it. One dataset, or files that draw different things,
  do not support a general speed ranking.
- Please leave generated datasets, credentials, caches and deployment state
  out of commits. Small result files and the scripts that make them are
  welcome.

Work that would help next: independent readers, conformance fixtures, other
kinds of geometry, memory measurements, adaptive thinning, and bringing the
access design into other GeoParquet overview proposals.

## Repository map

| Path                       | Purpose                                               |
| -------------------------- | ----------------------------------------------------- |
| `python/tiled_geoparquet/` | Writer, layout finalization, validator, CLI           |
| `js/src/`                  | Range reader, tile rendering, tile workers            |
| `js/vendor/`               | Vendored Esri SOP reader for the comparison pane only |
| `spec/`                    | Logical payload contract and optional physical index  |
| `tests/`, `js/test/`       | Correctness tests and measurement harnesses           |
| `examples/`                | Small runnable construction examples                  |
| `benchmarks/`              | Dataset-specific builds and recorded results          |
| `docs/`                    | Writer guide, comparison, design alternatives         |
| `demo/`                    | Local and hosted comparison UI and deck.gl example    |

See [THIRD_PARTY.md](THIRD_PARTY.md) before modifying or redistributing vendored
code. Package publishing and hosted dataset replacement are separate from a
source-code change; neither happens automatically in CI.
