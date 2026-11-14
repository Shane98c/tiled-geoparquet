import type { Bbox, LodMetadata } from './reader.js'
import { HALF_WORLD } from './web-mercator.js'

/** The deepest zoom TileJSON allows. */
export const MAX_ZOOM = 30
/** MapLibre's own source `maxzoom` default. */
const EXACT_MAXZOOM = 22
/** About 0.1 m. A map requests only the tiles its source's bounds reach, and
 * draws a point on a tile edge from the tile east or south of that edge; the
 * pad carries the bounds into that tile when the data ends on the edge. */
const BOUNDS_PAD = 1e-6

/** Degrees an overview shape of the finest LOD can lie past its feature's
 * extent: a line's placeholder segment runs its whole length from the
 * centroid's cell, a polygon's square reaches less, and snapping moves a
 * vertex half a cell. A map overzooms that LOD, so its tiles must all be
 * inside the bounds; a coarser LOD's shapes reach about a placeholder of
 * their own zoom past them. A degree of latitude is at least as many metres
 * as one of longitude. */
const overviewReach = (lod: LodMetadata): number => {
  const finest = Math.max(...lod.zooms)
  const level = Object.values(lod.lods).find((child) => child.zooms.includes(finest))
  if (!level) return 0
  const cells = 1 + (level.placeholder ?? 2)
  return (cells * Math.max(...level.scale) * 180) / HALF_WORLD
}

export interface SourceLimits {
  minzoom: number
  maxzoom: number
  /** West, south, east, north in degrees. */
  bounds: Bbox
}

/**
 * The zoom limits and extent a map source or TileJSON takes from a file's
 * metadata. `minzoom` is 0: tiles below the coarsest LOD zoom draw that LOD.
 * `maxzoom` defaults to the finest LOD zoom, past which a map overzooms those
 * tiles; a higher one has the map request tiles of exact primary geometry up
 * to it. A file without LODs draws exact geometry at every zoom and defaults
 * to 22.
 */
export function sourceLimits(
  metadata: { lod: LodMetadata | undefined; bounds: Bbox },
  maxzoom?: number,
): SourceLimits {
  if (maxzoom !== undefined && (!Number.isInteger(maxzoom) || maxzoom < 0 || maxzoom > MAX_ZOOM))
    throw new Error(`maxzoom must be an integer from 0 to ${MAX_ZOOM}`)
  const [west, south, east, north] = metadata.bounds
  const pad = Math.max(BOUNDS_PAD, metadata.lod ? overviewReach(metadata.lod) : 0)
  return {
    minzoom: 0,
    maxzoom: maxzoom ?? (metadata.lod ? Math.max(...metadata.lod.zooms) : EXACT_MAXZOOM),
    bounds: [
      Math.max(-180, west - pad),
      Math.max(-90, south - pad),
      Math.min(180, east + pad),
      Math.min(90, north + pad),
    ],
  }
}
