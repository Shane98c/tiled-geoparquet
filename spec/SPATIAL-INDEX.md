# Tiled GeoParquet spatial page index, version 9

An optional physical profile of a packed LOD file. The logical
`geo.lod_columns` metadata and every table value are unchanged, but that an
attribute column's values lead the pages of their LOD
([Attribute pages](#attribute-pages)); the profile
decides where column chunks and pages sit and adds directories over the
overview pages, over the primary geometry's own pages, over each flat
column's pages by row, and over the pages of the attributes, so a reader
finds a tile's pages, and the pages of any flat column for the tile's rows,
from the first 16 KiB of the file without reading the footer, at every zoom
and past the finest overview. Where a style uses an attribute, the leaves
listing its pages are read in the same request round as the directory leaves
they belong to; a reader that styles by no attribute need request none of
them. It is not a standardized
GeoParquet extension. Compatibility has been exercised with PyArrow, DuckDB,
and the hyparquet-based reader in this repository.

## File layout

```text
PAR1
LODIDX01 | root_length:u32le | gzip(root JSON) | padding to byte 16384
column chunks, physically grouped by column, then logical row group
standard Parquet offset indexes, then column indexes, each by row group
gzip directory nodes: leaves, each level-one node's followed by its slot block, and optional branches
standard Parquet footer | footer_length:u32le | PAR1
```

The footer retains the original row groups, columns, schema, statistics, and
GeoParquet metadata, with relocated offsets and updated physical sizes.
`lod_spatial_index` in its key/value metadata advertises the profile version
and bootstrap location, `{"version":9,"offset":4,"length":16380}`. Primary
geometry and source attribute chunks are copied byte for byte. LOD chunks are
re-encoded in the original row order, with the same nullable binary schema and
zstd, with page breaks at the LOD's finest tile grid. Every LOD page is a v1
data page (a `DATA_PAGE` header) of PLAIN values with RLE/bit-packed hybrid
definition levels, and LOD chunks have no dictionary page, so each page decodes
on its own. Multiple pages may serve a dense tile, and a row-group boundary can
split a tile. The row cap scales with the chunk's LOD density to target 2,048
present values, up to 262,144 analytical rows per page, alongside an
uncompressed byte target (64 KiB by default). The target is advisory: write
batches and individual large values may exceed it. Runs of entirely null tiles
are encoded together and omitted from the spatial directory; standard offset
indexes still describe every page.

## Attribute pages

A file with attributes holds them in attribute columns at every LOD
([Layout](SPEC.md#layout) 5): the profile refuses a file whose
`attribute_columns.lods` leaves an LOD out. An LOD's attribute column chunks
are re-encoded page by page beside the same row group's LOD chunk, and its
values lead the LOD's pages. Among the rows of an LOD page, those the LOD
shows are the rows whose packed value holds a geometry of its own: every
non-null value of a point LOD, and every non-null value of a shape LOD
whose leading varint, its count of parts, is not zero, in whichever of its
ten bytes at most that varint is written. A row that only hosts copies
counts no parts. The
attribute's value for the i-th of them, counted from zero, is on the page's
i-th row, null where the row has none, and every row of the page past as
many as it shows is null. A page of the attribute so states which of its
rows hold a value in a few bytes, however few of its LOD page's rows are
shown, where a value on its own row costs more to place than to hold at an
LOD showing one row in hundreds. An LOD
page showing a row with a value of the attribute has an attribute page
holding exactly its rows, and each maximal run of consecutive LOD pages of
the row group showing no row with one is a single attribute page,
of nulls, holding the run's rows. Each is a v1 data page with RLE/bit-packed hybrid definition levels and
no dictionary, of PLAIN values, or BYTE_STREAM_SPLIT ones for a fixed-width
numeric type; the reference writer encodes a chunk both ways and keeps the
smaller. All attribute column chunks share one codec, the root's
`attribute_codec`. The column remains an ordinary nullable Parquet column,
whose values a reader matches to rows through the LOD's pages. A reader
MUST refuse an attribute page it reads that holds a value past as many rows
as its LOD page shows. The pages a tile styled by an attribute reads therefore hold
the rows of the overview pages it reads that hold a value, in the same
request round, and for the overview pages that hold none it reads nothing.

Every column chunk remains contiguous; pages from different chunks are not
interleaved. A tile's data is one contiguous range of its LOD column, so a
cold tile normally costs one directory read and one data read; row-group and
directory boundaries can add reads.

## Bootstrap

The compressed root is bounded by the first 16 KiB and contains:

- `version: 9`, `file_bytes`: format and full file length checks. A reader
  refuses any other version.
- `footer_bytes`: the length of the standard footer's metadata, so a reader
  fetches the footer and its trailer in one request.
- `lod`, `primary`: copies of the logical LOD metadata and primary column name.
- `bounds`, `columns`: map extent and the scalar (non-nested) columns other
  than the primary geometry, including the key column, so a map can
  initialize without another reader open.
- `codecs`: LOD name to its Parquet compression codec.
- `attribute_codec`: the Parquet compression codec of every attribute
  column chunk, present exactly when the file has attributes.
- `attribute_slots`: per tree, each LOD's in `lod.lods` order and then the
  primary directory's, the size in bytes of the
  [attribute slots](#attribute-slots) of its leaves, present exactly when
  the file has attributes.
- `directories`: LOD name to the top node of its directory, at level one or
  above.
- `column_index`: the indexed columns, what decodes their pages, and their
  [row directories](#row-directories).
- `primary_index`: the [primary directory](#primary-directory).

## Directory nodes

Each LOD's directory is a tree over its pages in key order. The root holds the
top node; the nodes below are gzip JSON. Every node is a JSON array of its
level and parallel columns with one value per entry:

```text
[level, cells, spans, gaps, lengths]                                level > 0
[0, cells, spans, gaps, lengths, rows, counts, fences, nulls]       a leaf
```

A leaf's entries are pages; the entries of a node at level `L > 0` are nodes
at level `L - 1`, and no level exceeds 32. Positions are delta coded against
the previous entry, and the first entry's against zero:

- `cells`: the entry's minimum cell minus the previous entry's.
- `spans`: its maximum cell minus its minimum cell.
- `gaps`: its byte offset minus the previous entry's end, its offset plus
  its length.
- `lengths`: its compressed length in bytes.
- `rows`: the page's first global row minus the previous page's end row.
- `counts`: the page's rows.
- `fences`: the page's fences after the implied first `[minimum cell, 0]`, a
  flat array of `[cell, row]` pairs, each minus the pair before. A fence marks
  the first row, counted within the page, of each run of rows in one cell.
- `nulls`: the page's trailing rows whose key is null.

Cells are Hilbert cells at `z = min(key.depth, finest zoom of the LOD)`: key
`k` lies in cell `floor(k / 4^(key.depth - z))`, and a null key counts as cell
`4^z`, past every other. Entries are in key order: each entry's minimum cell
is at least the previous entry's maximum. A node's minimum and maximum cell
are its first entry's minimum and its last entry's maximum, and every node
below the top is located by exactly one entry, which gives its byte range and
those two cells. An entry with cells `lo..hi` stands for the keys from
`lo * 4^(key.depth - z)` to `(hi + 1) * 4^(key.depth - z) - 1`. Every tile
served from the LOD, at its zooms or coarser, is a whole number of cells, so
these bounds select the same entries exact key bounds would.

Page fences enforce exact tile membership without fetching the key column;
tile-aligned pages have only the implied fence. Rows past `counts - nulls`
have null keys: they remain in the analytical table and have null LODs. The
reference writer puts 1,024 pages in a leaf and, while the root would exceed
the bootstrap, adds a level of branches of 64 entries to a tree: to the row
directory whose top node is largest, since no overview tile reads the row
directories, and to the LOD or primary tree whose top node is largest only
once every row directory has branched. It first fits the root as it would be
without the attributes' metadata, the attribute codec and the attribute slot
sizes, so the trees branch as in the same file without attributes, and then
with them. A synthetic directory of 15 LODs, 21 attributes, 25 indexed
columns and 50 or 500 million rows has the same LOD and primary trees with
and without the attributes.

## Attribute slots

The leaves under one level-one node of an LOD directory or of the primary
directory, in entry order, are contiguous in the file: each starts where the
previous one ends. In a file with attributes they are followed immediately
by the node's _slot block_: for each attribute in metadata order, one slot
per leaf of the node, in leaf order, each holding
that attribute's attribute leaf for the leaf:

```text
length:u32le | attribute:u32le | leaf:u64le | gzip(attribute leaf JSON) | zero padding
```

Every slot of one tree has the same size, the tree's entry in
`attribute_slots`: 16 plus the length of the tree's largest gzip attribute
leaf. `length` is the gzip attribute leaf's and MUST be positive and at most
the slot size minus 16, `attribute` MUST be the position of the slot's
attribute among the attributes, `leaf` MUST be the byte
offset in the file of the directory leaf the slot belongs to, and the
padding MUST be zeros; a reader refuses a slot that breaks any of these, so
a slot read for the wrong leaf, tree or attribute fails rather than lending
its pages to another. Row directory leaves and the nodes above the leaves
have no slots, and no byte of a directory node lies in a slot block.

For the leaf at byte `leaf` and position `j` among the `n` entries of its
level-one node in tree `t`, whose last leaf ends at byte `end`, the slot of
the attribute at position `a` among the attributes is the
`attribute_slots[t]` bytes from `end + (a * n + j) * attribute_slots[t]`,
framed with `a` and `leaf`. All of these follow from the level-one node,
which a reader holds when it reaches the leaf, so it fetches a leaf and the
slots of the attributes a style uses in the same round, and a styled tile
reads in as many rounds as the same tile without the style, for any number
of attributes. The slots of one attribute for consecutive leaves of
a node are adjacent, so a reader reaching several of those leaves reads
their slots as one range.

A reader that styles by no attribute need name no slot byte in its
requests, and the reference reader names none. The leaves under one
level-one node are laid out as in the same source written without
attributes, so a view under one level-one node takes the requests it takes
from that source. A slot block lies between the
leaf runs of consecutive level-one nodes, so where one view reaches the
leaves of two of them, a source that coalesces nearby ranges may fetch a
small slot block as filler, or make one more request than without
attributes. Measured on 12,000 shapes with one page per leaf and both
level-one nodes held, a view over the last leaf of one and the first of the
next makes 4 requests against 3 with 24 attributes, and with one attribute
makes 3 requests and fetches the 3,392-byte slot block between them.

The reference writer settles which trees the root fitting branches before
it writes the leaves, laying out each tree's leaves either under its top
node or in level-one branches of 64; where the slot blocks of one layout
change which trees branch, it gives those trees their first level of
branches before fitting the rest.

An attribute leaf beside an LOD leaf lists, beside each page the leaf lists,
one each and in the same order, the page of the LOD's column of the
attribute holding the same rows, by its byte range:

```text
[0, gaps, lengths]
```

with each gap counted from the end of the previous page, the first from
zero. Its page count MUST equal the leaf's. An entry of length 0 is
_empty_: the attribute is null for every row its LOD page shows, and that
page has no attribute page of its own. An empty entry occupies no bytes: its gap MUST
be 0, and the next entry's gap counts from where the entry before it ends.
Every other entry has a positive length and lists the attribute page
holding exactly its LOD page's rows. An entry MUST be empty exactly when its
LOD page holds no value of the attribute. Only an attribute leaf beside an LOD leaf has empty entries: an
entry of LOD leaves, of the nodes above them, of row directories and of the
attribute leaves beside primary leaves MUST have a positive length, and a
primary leaf's length of 0 marks a run in the previous entry's page, as
below. An attribute leaf beside a
primary leaf has the form of a row directory leaf,
`[0, gaps, lengths, rows, counts]` ([Row directories](#row-directories)),
and lists the pages of the attribute's source column holding a row of the
leaf's runs, in row order, with rows that may skip between them.

## Lookup

To read a tile, descend from the LOD's top node: in every node, take the
entries whose bounds intersect the tile's key interval, plus the first entry
whose maximum reaches the interval start, or the final entry if none does.
Among the pages, that first page contains the tile's copy host when any copies
exist. Read consecutive pages together. Decode each page with the root's codec
and the required-struct/nullable-binary schema, scanning its definition-level
runs to skip null rows. Use the fences to retain own rows inside the tile
interval, and the host row for its copies tagged with the requested tile.
Global row indices are the feature ids. For each attribute a style
uses, load its slots for the leaves reached along with them,
and read with the overview pages the attribute pages beside those holding
the tile's own rows, none beside an empty entry. An own feature's value is
its row's value in those pages, which may be null, or null beside an empty
entry, and a copy's value is the one it packs. The directory stores no geometry or
styling payload and is not an MVT archive.

## Row directories

A flat column's values for given rows are located by the column's own
directory, keyed by row. A column is _indexed_ when it is a flat (non-nested,
non-repeated) top-level column whose chunk in every row group has an offset
index and no dictionary page, with one codec across row groups; the primary
geometry must be. Every flat column the reference writer writes is indexed.
The root's `column_index` holds:

- `columns`: every indexed column, in schema order, and no other.
- `codecs`: each column's Parquet codec.
- `schema`: base64 of a Thrift `FileMetaData` whose schema holds the root
  element, then the element of each column in `columns`, with no row groups.
  With `codecs` it decodes their pages without the footer.
- `directories`: each column's top node, at level one or above.

A row directory is a tree over its column's pages in row order. A leaf lists
consecutive pages, the first counted from zero:

```text
[0, gaps, lengths, rows, counts]
```

- `gaps`: the page's byte offset minus the previous page's end.
- `lengths`: its compressed length in bytes.
- `rows`: its first global row minus the previous page's end row; 0 after
  the first, as a column's pages hold its rows back to back.
- `counts`: its rows.

The nodes above the leaves are directory nodes as above,
`[level, cells, spans, gaps, lengths]`, whose cells are global rows: an
entry's minimum and maximum are the first and last rows of the node it
locates. Every page of the column, data pages only, is listed once, in row
order, with the byte range and rows its offset index gives it, so the leaves
together hold the rows from 0 to the file's last. The reference writer puts
1,024 pages in a leaf.

To read rows of an indexed column, descend from its top node into every entry
whose rows meet a requested row, read those pages, adjacent pages together,
and decode each with the column's codec and schema element. A page is any
data page but a dictionary-encoded one, of exactly the rows its entry gives
it; its header counts one value, null or not, per row: a v1 header's values
and a v2 header's values and rows are the page's rows, and a v2 header's
nulls at most as many. Other columns are read through the footer.

A tile's own rows are known once its overview pages are: they are the rows
of the fences, among the selected pages, whose cell lies in the tile, up to
each page's `counts - nulls`. A reader styling a tile by a source column
that is no attribute can therefore fetch that column's pages for those rows
alongside the overview pages, and after decoding read only the rows of the
tile's boundary copies, which belong to features keyed elsewhere.

## Primary directory

Past the finest LOD the reader draws the primary geometry itself, which no
overview copies. Its pages, copied byte for byte and never re-paged, are
located by a directory routed by key, as the LOD directories are, whose leaves
list runs of geometry rows with the box of each. A feature reaching into a
tile from outside it is found by its box, so the primary geometry needs no
boundary copies. The root's `primary_index` holds `directory`, the top node,
at level one or above. The primary geometry decodes with its codec and
schema element in `column_index`, and the attribute leaves in the slots of
the primary leaves locate the pages of the attributes' source columns, so a
tile reads them in the same request round as its geometry.

Its cells are Hilbert cells at `z = min(key.depth, finest zoom)`, the finest
LOD's. The nodes above its leaves are directory nodes as above,
`[level, cells, spans, gaps, lengths]`, and the leaves' cells, in order, are
every cell at `z` from 0 to `4^z - 1`, each in one leaf. A leaf's _region_ is
its cells' tiles at zoom `z`.

Boxes are whole cells of 2^-12 degrees, about 27 m of latitude: a longitude
`x` lies in cell column `floor((x + 180) * 4096)` and a latitude `y` in cell
row `floor((y + 90) * 4096)`, computed in IEEE double precision, for a
covering value from its float32. A row's box is the cells of its covering's
minimum and maximum, inclusive, so it contains the covering. A row has an
extent when its four covering values are present and finite with each
minimum at most its maximum; a row whose geometry has no finite bounds
([Layout](SPEC.md#layout) 2) has a null covering and none. A tile's
cells are those of its edges: tile column `t` at zoom `z` spans the cell
columns of the longitudes `t * 360 / 2^z - 180` to `(t + 1) * 360 / 2^z - 180`,
and tile row `t` the cell rows of the latitudes
`atan(sinh(pi * (1 - 2 * (t + 1) / 2^z)))` to
`atan(sinh(pi * (1 - 2 * t / 2^z)))` in degrees, the first row reaching
latitude 90 and the last -90. A box reaches a tile, or a region, when it
shares a cell with it.

A leaf is a JSON array of level 0 and parallel columns with one value per
entry, the first entry's values against zero:

```text
[0, wests, souths, widths, heights, gaps, lengths, rows, counts]
```

Each entry is a run of one geometry page's rows, in row order; a page's runs
are consecutive entries holding all its rows.

- `wests`, `souths`: the run's box's minimum cell column and row minus the
  previous entry's.
- `widths`, `heights`: its maximum cell column and row minus its minimum.
- `gaps`, `lengths`: its page's byte offset minus the end of the previous
  entry's page, and its page's compressed length; both 0 when its page is
  the previous entry's.
- `rows`, `counts`: its first row minus the previous entry's end row, and its
  rows.

A run's box is the union of its rows' boxes, and it holds a row with an
extent. A leaf lists every page holding a row whose box reaches its region,
and a page's runs are the same in every leaf that lists it. When the Hilbert
curve leaves the data and re-enters it far away, one page can hold rows of
distant places; its runs keep the empty space between them out of the
lookup. The reference writer cuts a page in two at the row where the halves'
boxes cover the fewest tiles of 32 cells, when they cover at most 70% of those
one box over all its rows would, then each half once more the same way. It
ends a leaf after about 1,024 pages, before the coarsest aligned cell among
those of its last half of pages, so a leaf's region is a few large tiles: a
tile past the finest LOD lies in one leaf, and its buffer seldom reaches
another.

To read the rows whose bbox intersects a WGS84 query, a tile's buffered bounds
for an MVT tile, descend from the top node into every entry whose cells hold a
tile at zoom `z` sharing a point with the query; the reference reader takes
the tiles at the finest zoom up to `z` at which those holding the query's
corners, latitudes clamped to the Mercator limit, span at most two columns and
two rows. In each leaf reached, take the runs whose boxes meet the query's
box, the cells of its corners as above. A row whose covering intersects the
query shares a point with it; the row's box reaches the tile holding that
point, so the tile's leaf lists the row's page, and the row's run is taken.
Read each page once, adjacent pages together. Decode each page as a row
directory's page, of exactly its runs' rows. Keep the rows of the taken
runs whose geometry is present and whose bbox intersects the query: the
reference reader takes it from the WKB, rounded outward to float32, which
[Layout](SPEC.md#layout) 2 makes the covering, so it keeps the rows the
covering would. Alongside the geometry, read the pages of each needed
attribute that the attribute leaves in the slots of the same leaves locate
over the taken runs' rows, loaded with the leaves, and the pages of any other
indexed column over them from its row directory. A source column's value is cast to the declared
type, a float rounded to it.

## Analytical access and publishing

Row reads of indexed columns use the row directories; reads of other
columns, nested ones included, open the normal Parquet footer lazily. The
standard page indexes describe every page, so ordinary Parquet readers see an
ordinary file, and a row group's offset indexes are adjacent, so a footer
read across columns fetches them in one request. The directories are derived
from the final footer, which stays authoritative; a validator checks they
agree.

Publish files immutably: the profile is
specific to one file write, and ordinary Parquet writers need not preserve it.
The reference `write()` builds the layout as its final step, writes to
temporary files, and publishes only after the completed footer is readable.
Its layout pass rejects encrypted or external chunks, bloom filters and legacy
index pages.

The reference reader's caching, worker pool, and cancellation behaviour are
described in the [usage guide](../docs/usage.md#reader-behaviour); none of it
changes the stored bytes.
