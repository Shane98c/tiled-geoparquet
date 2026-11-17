"""Derive a writer source from a Tiled GeoParquet file by dropping its map columns.

python benchmarks/attribute-columns/derive-source.py FILE OUT [--check ORIGINAL]

FILE is a local path or an http(s) URL; a URL is read with range requests,
one row group at a time, so memory stays near one row group and nothing but
OUT is written to disk. The covering bbox, the key column, the LOD geometry
column and any attribute-column struct are dropped, along with their entries
in the `geo` metadata. A plain GeoParquet file passes through unchanged.
--check compares OUT with ORIGINAL: the same columns and types in any order,
and the same rows as a multiset.
"""

import argparse
import io
import json
import time
import urllib.request

import pyarrow as pa
import pyarrow.parquet as pq

LOD_KEY = 'lod_columns'
# some hosts refuse urllib's default User-Agent
USER_AGENT = 'tiled-geoparquet-benchmark'


class HttpFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests."""

    def __init__(self, url: str, retries: int = 5):
        self.url = url
        self.retries = retries
        self.position = 0
        self.requests = 0
        self.bytes = 0
        head = urllib.request.Request(url, method='HEAD', headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(head, timeout=60) as response:
            self.size = int(response.headers['Content-Length'])

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = base + offset
        return self.position

    def readinto(self, buffer):
        end = min(self.size, self.position + len(buffer))
        if end <= self.position:
            return 0
        data = self._range(self.position, end)
        buffer[: len(data)] = data
        self.position += len(data)
        return len(data)

    def _range(self, start: int, end: int) -> bytes:
        request = urllib.request.Request(
            self.url, headers={'Range': f'bytes={start}-{end - 1}', 'User-Agent': USER_AGENT}
        )
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    if response.status != 206:
                        raise OSError(f'range request returned {response.status}')
                    data = response.read()
                if len(data) != end - start:
                    raise OSError(f'short range: {len(data)} of {end - start} bytes')
                self.requests += 1
                self.bytes += len(data)
                return data
            except OSError:
                if attempt == self.retries - 1:
                    raise
                time.sleep(2**attempt)


def open_parquet(path: str) -> pq.ParquetFile:
    if path.startswith(('http://', 'https://')):
        return pq.ParquetFile(HttpFile(path), pre_buffer=True)
    return pq.ParquetFile(path, pre_buffer=True)


def _named_columns(value) -> set[str]:
    """Every value of a 'column' key anywhere in the LOD metadata."""
    if isinstance(value, dict):
        found = {value['column']} if isinstance(value.get('column'), str) else set()
        for child in value.values():
            found |= _named_columns(child)
        return found
    if isinstance(value, list):
        return set().union(*map(_named_columns, value)) if value else set()
    return set()


def source_schema(schema: pa.Schema) -> tuple[pa.Schema, list[str]]:
    """The schema without map columns and their metadata, and the columns kept."""
    metadata = dict(schema.metadata or {})
    geo = json.loads(metadata.get(b'geo', b'{}'))
    dropped = _named_columns(geo.pop(LOD_KEY, {}))
    for column in geo.get('columns', {}).values():
        covering = column.pop('covering', {})
        dropped |= {path[0] for path in covering.get('bbox', {}).values()}
    keep = [name for name in schema.names if name not in dropped]
    if b'geo' in metadata:
        metadata[b'geo'] = json.dumps(geo).encode()
    fields = [schema.field(name) for name in keep]
    return pa.schema(fields, metadata=metadata), keep


def derive(path: str, out: str, log=print) -> None:
    started = time.monotonic()
    parquet = open_parquet(path)
    schema, keep = source_schema(parquet.schema_arrow)
    dropped = [name for name in parquet.schema_arrow.names if name not in keep]
    log(f'dropping {dropped}, keeping {keep}')
    with pq.ParquetWriter(out, schema, compression='zstd') as writer:
        groups = parquet.metadata.num_row_groups
        for group in range(groups):
            table = parquet.read_row_group(group, columns=keep)
            writer.write_table(table.replace_schema_metadata(schema.metadata))
            log(
                f'[{time.monotonic() - started:7.1f}s] row group {group + 1}/{groups}, '
                f'{table.num_rows:,} rows'
            )
            del table


def check(out: str, original: str) -> bool:
    import duckdb

    a, b = pq.read_schema(out), pq.read_schema(original)
    fields = {(f.name, str(f.type)) for f in a}, {(f.name, str(f.type)) for f in b}
    same_schema = fields[0] == fields[1]
    print(f'schema: {"same" if same_schema else "DIFFERENT"} apart from order')
    if not same_schema:
        print(f'  only in {out}: {sorted(fields[0] - fields[1])}')
        print(f'  only in {original}: {sorted(fields[1] - fields[0])}')
        return False
    con = duckdb.connect()
    columns = ', '.join(f'"{name}"' for name in b.names)
    counts = {}
    for name, (x, y) in {'extra': (out, original), 'missing': (original, out)}.items():
        counts[name] = con.execute(
            f"SELECT count(*) FROM (SELECT {columns} FROM read_parquet('{x}') "
            f"EXCEPT ALL SELECT {columns} FROM read_parquet('{y}'))"
        ).fetchone()[0]
    rows = [
        con.execute(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
        for p in (out, original)
    ]
    same_rows = counts == {'extra': 0, 'missing': 0} and rows[0] == rows[1]
    print(
        f'rows: {rows[0]:,} derived, {rows[1]:,} original; {counts["extra"]} only in derived, '
        f'{counts["missing"]} only in original: {"same" if same_rows else "DIFFERENT"} multiset'
    )
    return same_rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file')
    parser.add_argument('out')
    parser.add_argument('--check', metavar='ORIGINAL')
    args = parser.parse_args()
    derive(args.file, args.out)
    if args.check and not check(args.out, args.check):
        raise SystemExit(1)
