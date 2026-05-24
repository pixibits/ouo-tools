# clamp_region_zmax.py

`clamp_region_zmax.py` scans UO demo world data and `regions.txt` to find
region `zMax` values that extend up to building roof or upper-floor surfaces.
It can then lower those `zMax` values so region-based spawns stay below the
roof.

The tool is conservative by default:

- It performs a dry run unless `--apply` is passed.
- It writes only `regions.txt`.
- On apply, it creates a timestamped backup of only `regions.txt`.
- It preserves line formatting and replaces only the `zMax` token on changed
  region lines.

## Usage

From the `ouo` repo directory:

```sh
python3 ../tools/clamp_region_zmax.py [data_dir]
```

If `data_dir` is omitted, it defaults to:

```text
../.rundir/uogolddemo/
```

Required files in `data_dir`:

- `regions.txt`
- `map0.mul`
- `staidx0.mul`
- `statics0.mul`

`tiledata.mul` is found automatically at either:

- `data_dir/tiledata.mul`
- `data_dir/../tiledata.mul`

You can override that with:

```sh
python3 ../tools/clamp_region_zmax.py --tiledata ../.rundir/tiledata.mul
```

## Dry Run And Apply

Dry run is the default:

```sh
python3 ../tools/clamp_region_zmax.py --prefix 2160
```

Apply changes:

```sh
python3 ../tools/clamp_region_zmax.py --prefix 2160 --apply
```

When applying, the tool creates a backup such as:

```text
regions.txt.bak.20260524-080829
```

It does not modify `regions.mul` or any other world data file.

## Region Selection

Without filters, every region line in `regions.txt` is examined independently.
Regions are never merged or unioned.

Filter by prefix:

```sh
python3 ../tools/clamp_region_zmax.py --prefix 2160
```

Filter by exact region name, case-insensitive:

```sh
python3 ../tools/clamp_region_zmax.py --name MINERSGUILD_BRITAIN
```

Filter by wildcard name, case-insensitive:

```sh
python3 ../tools/clamp_region_zmax.py --name-like 'MINERSGUILD*'
python3 ../tools/clamp_region_zmax.py --name-like '*BRITAIN*'
```

Filters are repeatable. If multiple filters are supplied, any matching region
line is examined, but each matching region is still analyzed on its own
rectangle and `zMin..zMax` range.

## Roof Detection

The tool looks for a walkable upper static surface that behaves like a roof or
upper floor, not merely any walkable surface.

For a static surface to be considered an overhead candidate:

1. It must be inside the region rectangle.
2. Its walkable top Z must satisfy `zMin < topZ <= zMax`.
3. Its tiledata flags must include `TF_SURFACE` or `TF_BRIDGE`.
4. Its tiledata flags must not include `TF_IMPASSABLE`.

Then the candidate must pass an underlay test:

1. At the same `(x, y)`, there must be a lower walkable layer beneath it.
2. The lower layer may be land from `map0.mul` or another static surface.
3. The lower layer must be at least `--min-overhead-gap` Z units below the
   candidate surface.
4. Enough candidate tiles must have this lower layer to satisfy
   `--min-underlay-coverage`.

This avoids false positives such as dock planks. Dock planks may be walkable
static surfaces, but they are the floor, not a roof; they normally do not have
a lower walkable layer far enough beneath them.

## Clamp Rules

For each individual region:

1. Group qualifying overhead candidates by top Z.
2. Check Z levels from highest to lowest.
3. Select the highest Z where:
   - overhead coverage is at least `--min-coverage`
   - underlay coverage is at least `--min-underlay-coverage`
4. Propose `new_zMax = overheadZ - 1`.
5. Skip the region if the proposed `zMax` would be below `zMin`.

Coverage is calculated against the full region area:

```text
unique matching (x,y) tiles / (region width * region height)
```

## Thresholds

Default overhead coverage:

```text
--min-coverage 50.0
```

Default underlay coverage:

```text
same value as --min-coverage
```

Default required vertical gap:

```text
--min-overhead-gap 8
```

Examples:

```sh
python3 ../tools/clamp_region_zmax.py --prefix 2160 --min-coverage 50
python3 ../tools/clamp_region_zmax.py --prefix 2160 --min-coverage 90
python3 ../tools/clamp_region_zmax.py --name-like '*BRITAIN*' --min-overhead-gap 10
python3 ../tools/clamp_region_zmax.py --name-like 'MINERSGUILD*' --min-underlay-coverage 75
```

## Output

Example dry-run output:

```text
Proposed region zMax changes:
 line prefix name                               old   new roofZ   cover   under     tiles top tiles
-------------------------------------------------------------------------------------------------------------------------
  249   2160 MINERSGUILD_BRITAIN                 50    49    50   83.7%   83.7%  128/153  0x051d stone pavers x128
```

Columns:

- `line`: line number in `regions.txt`
- `prefix`: region prefix
- `name`: region name
- `old`: current `zMax`
- `new`: proposed or applied `zMax`
- `roofZ`: detected overhead surface Z
- `cover`: overhead tile coverage for the selected Z
- `under`: same-region coverage with a lower walkable layer beneath `roofZ`
- `tiles`: matching overhead tiles over region area
- `top tiles`: most common tile IDs and names at the selected overhead Z

The summary also reports skipped categories:

- `no overhead`: no walkable static candidate in the region z range
- `below coverage`: candidates existed but did not meet `--min-coverage`
- `below underlay`: candidates met overhead coverage but failed the lower-layer test
- `skipped invalid`: invalid region shape or clamp would make `zMax < zMin`

## Known Behavior

The tool intentionally examines each region independently. If a broad city
region overlaps a shop region, the city region does not change how the shop
region is analyzed.

This matches the server's spawn setup: each matching geographic region becomes
its own resbank subregion using that region's own rectangle and `zMin..zMax`.
Lowering a shop region's `zMax` does not rely on changing the surrounding city
region.
