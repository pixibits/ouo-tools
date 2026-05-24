#!/usr/bin/env python3
"""Clamp region zMax values below overhead walkable static surfaces."""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


LAND_TILE_COUNT = 16384
ITEM_TILE_COUNT = 16384
LAND_TILE_RECORD_SIZE = 26
ITEM_TILE_RECORD_SIZE = 37
STATIC_RECORD_SIZE = 7
STATIC_INDEX_RECORD_SIZE = 12

TF_SURFACE = 0x00000200
TF_IMPASSABLE = 0x00000040
TF_BRIDGE = 0x00000400
LTF_IMPASSABLE = 0x00000040

DEFAULT_WIDTH = 6144
DEFAULT_HEIGHT = 4096
DEFAULT_ORIGIN_X = 0
DEFAULT_ORIGIN_Y = 0
MAP_BLOCK_SIZE = 196


@dataclass(frozen=True)
class LandTile:
    flags: int
    name: str


@dataclass(frozen=True)
class ItemTile:
    flags: int
    height: int
    name: str


@dataclass(frozen=True)
class MapConfig:
    width: int
    height: int
    origin_x: int
    origin_y: int

    @property
    def grid_width(self) -> int:
        return self.width // 8

    @property
    def grid_height(self) -> int:
        return self.height // 8


@dataclass(frozen=True)
class Region:
    line_index: int
    line_no: int
    prefix: int
    x: int
    y: int
    width: int
    height: int
    z_min: int
    z_max: int
    name: str
    zmax_span: tuple[int, int]

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class StaticSurface:
    x: int
    y: int
    z: int
    top_z: int
    item_id: int


@dataclass(frozen=True)
class ClampChange:
    region: Region
    overhead_z: int
    new_z_max: int
    coverage: float
    tile_count: int
    underlay_coverage: float
    underlay_tile_count: int
    top_tiles: list[tuple[int, str, int]]


@dataclass(frozen=True)
class ScanStats:
    examined: int
    no_overhead: int
    below_coverage: int
    invalid_clamp: int


class StaticWorld:
    def __init__(self, data_dir: Path, config: MapConfig, land_tiles: list[LandTile], item_tiles: list[ItemTile]) -> None:
        self.data_dir = data_dir
        self.config = config
        self.land_tiles = land_tiles
        self.item_tiles = item_tiles
        self.map_path = data_dir / "map0.mul"
        self.index_path = data_dir / "staidx0.mul"
        self.statics_path = data_dir / "statics0.mul"
        self.map_data = self.map_path.read_bytes()
        self.index_data = self.index_path.read_bytes()
        self._statics_file = self.statics_path.open("rb")
        self._block_cache: dict[int, list[StaticSurface]] = {}

        expected_records = config.grid_width * config.grid_height
        expected_map_size = expected_records * MAP_BLOCK_SIZE
        if len(self.map_data) != expected_map_size:
            raise ValueError(
                f"{self.map_path} has {len(self.map_data)} bytes, but map dimensions imply "
                f"{expected_map_size} bytes ({config.width}x{config.height})"
            )

        actual_records = len(self.index_data) // STATIC_INDEX_RECORD_SIZE
        if len(self.index_data) % STATIC_INDEX_RECORD_SIZE != 0:
            raise ValueError(f"{self.index_path} size is not a multiple of 12 bytes")
        if actual_records != expected_records:
            raise ValueError(
                f"{self.index_path} has {actual_records} blocks, but map dimensions imply "
                f"{expected_records} blocks ({config.width}x{config.height})"
            )

    def close(self) -> None:
        self._statics_file.close()

    def __enter__(self) -> "StaticWorld":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def surfaces_in_region(self, region: Region) -> list[StaticSurface]:
        if region.width <= 0 or region.height <= 0:
            return []

        bx_start = max(0, (region.x - self.config.origin_x) // 8)
        by_start = max(0, (region.y - self.config.origin_y) // 8)
        bx_end = min(self.config.grid_width - 1, (region.x + region.width - 1 - self.config.origin_x) // 8)
        by_end = min(self.config.grid_height - 1, (region.y + region.height - 1 - self.config.origin_y) // 8)

        if bx_start > bx_end or by_start > by_end:
            return []

        surfaces: list[StaticSurface] = []
        for bx in range(bx_start, bx_end + 1):
            block_base = bx * self.config.grid_height
            for by in range(by_start, by_end + 1):
                for surface in self._load_block(block_base + by):
                    if (
                        region.x <= surface.x < region.x + region.width
                        and region.y <= surface.y < region.y + region.height
                        and region.z_min < surface.top_z <= region.z_max
                    ):
                        surfaces.append(surface)
        return surfaces

    def has_lower_walkable_layer(self, region: Region, x: int, y: int, overhead_z: int, min_gap: int) -> bool:
        lower_max_z = overhead_z - min_gap
        if lower_max_z < region.z_min:
            return False

        land_top_z = self.land_top_z(x, y)
        if land_top_z is not None and region.z_min <= land_top_z < overhead_z and land_top_z <= lower_max_z:
            return True

        block_index = self.block_index(x, y)
        if block_index < 0:
            return False

        for surface in self._load_block(block_index):
            if surface.x != x or surface.y != y:
                continue
            if region.z_min <= surface.top_z < overhead_z and surface.top_z <= lower_max_z:
                return True

        return False

    def land_top_z(self, x: int, y: int) -> int | None:
        tile_id, _z = self.land_cell(x, y)
        if self.is_void_land_tile(tile_id):
            return None
        if self.land_tiles[tile_id].flags & LTF_IMPASSABLE:
            return None
        return self.avg_land_z(x, y)

    def land_cell(self, x: int, y: int) -> tuple[int, int]:
        block_index = self.block_index(x, y)
        if block_index < 0:
            return 0, 0
        local_x = (x - self.config.origin_x) & 7
        local_y = (y - self.config.origin_y) & 7
        offset = block_index * MAP_BLOCK_SIZE + 4 + (local_y * 8 + local_x) * 3
        tile_id, z = struct.unpack_from("<Hb", self.map_data, offset)
        return tile_id, z

    def land_z(self, x: int, y: int) -> int:
        return self.land_cell(x, y)[1]

    def avg_land_z(self, x: int, y: int) -> int:
        if not self.in_bounds(x + 1, y + 1):
            return self.land_z(x, y)

        tl = self.land_z(x, y)
        tr = self.land_z(x + 1, y)
        br = self.land_z(x + 1, y + 1)
        bl = self.land_z(x, y + 1)

        if abs(tl - br) > abs(tr - bl):
            return (tr + bl) // 2
        return (tl + br) // 2

    def block_index(self, x: int, y: int) -> int:
        if not self.in_bounds(x, y):
            return -1
        block_x = (x - self.config.origin_x) >> 3
        block_y = (y - self.config.origin_y) >> 3
        return block_x * self.config.grid_height + block_y

    def in_bounds(self, x: int, y: int) -> bool:
        return (
            self.config.origin_x <= x < self.config.origin_x + self.config.width
            and self.config.origin_y <= y < self.config.origin_y + self.config.height
        )

    @staticmethod
    def is_void_land_tile(tile_id: int) -> bool:
        return tile_id == 2 or 0x1AE <= tile_id <= 0x1B5 or tile_id == 0x1DB

    def _load_block(self, block_index: int) -> list[StaticSurface]:
        cached = self._block_cache.get(block_index)
        if cached is not None:
            return cached

        rec_offset = block_index * STATIC_INDEX_RECORD_SIZE
        offset, length, _extra = struct.unpack_from("<iii", self.index_data, rec_offset)
        if offset < 0 or length <= 0:
            self._block_cache[block_index] = []
            return []

        self._statics_file.seek(offset)
        block_data = self._statics_file.read(length)
        block_x = block_index // self.config.grid_height
        block_y = block_index % self.config.grid_height
        world_x = self.config.origin_x + block_x * 8
        world_y = self.config.origin_y + block_y * 8

        surfaces: list[StaticSurface] = []
        for pos in range(0, len(block_data) - STATIC_RECORD_SIZE + 1, STATIC_RECORD_SIZE):
            item_id, x_off, y_off, z, _hue = struct.unpack_from("<HBBbH", block_data, pos)
            if item_id >= len(self.item_tiles):
                continue

            flags = self.item_tiles[item_id].flags
            if (flags & (TF_SURFACE | TF_BRIDGE)) and not (flags & TF_IMPASSABLE):
                height = self.item_tiles[item_id].height
                top_z = z + height // 2 if flags & TF_BRIDGE else z + height
                surfaces.append(StaticSurface(world_x + x_off, world_y + y_off, z, top_z, item_id))

        self._block_cache[block_index] = surfaces
        return surfaces


def parse_args() -> argparse.Namespace:
    script_default_data_dir = Path(__file__).resolve().parent.parent / ".rundir" / "uogolddemo"
    parser = argparse.ArgumentParser(
        description="Dry-run or apply zMax clamps for regions that include overhead walkable static surfaces."
    )
    parser.add_argument(
        "data_dir",
        nargs="?",
        default=str(script_default_data_dir),
        help="directory containing regions.txt, staidx0.mul, and statics0.mul (default: ../.rundir/uogolddemo/)",
    )
    parser.add_argument("--tiledata", type=Path, help="path to tiledata.mul (default: data_dir or its parent)")
    parser.add_argument("--prefix", action="append", type=int, default=[], help="region prefix to examine; repeatable")
    parser.add_argument("--name", action="append", default=[], help="case-insensitive exact region name to examine")
    parser.add_argument(
        "--name-like",
        action="append",
        default=[],
        help="case-insensitive wildcard region name to examine, e.g. 'MINERSGUILD*'",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=50.0,
        help="minimum percent of the region covered by overhead tiles at the selected Z (default: 50.0)",
    )
    parser.add_argument(
        "--min-underlay-coverage",
        type=float,
        default=None,
        help="minimum percent of the region with a lower walkable layer under the overhead tiles "
        "(default: same as --min-coverage)",
    )
    parser.add_argument(
        "--min-overhead-gap",
        type=int,
        default=8,
        help="minimum Z gap between a candidate overhead tile and a lower walkable layer (default: 8)",
    )
    parser.add_argument("--apply", action="store_true", help="write regions.txt; default is dry run")
    args = parser.parse_args()
    if args.min_coverage < 0.0 or args.min_coverage > 100.0:
        parser.error("--min-coverage must be between 0 and 100")
    if args.min_underlay_coverage is None:
        args.min_underlay_coverage = args.min_coverage
    if args.min_underlay_coverage < 0.0 or args.min_underlay_coverage > 100.0:
        parser.error("--min-underlay-coverage must be between 0 and 100")
    if args.min_overhead_gap < 0:
        parser.error("--min-overhead-gap must be non-negative")
    return args


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"required file not found: {path}")


def find_tiledata(data_dir: Path, override: Path | None) -> Path:
    if override is not None:
        return override
    for candidate in (data_dir / "tiledata.mul", data_dir.parent / "tiledata.mul"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"tiledata.mul not found at {data_dir / 'tiledata.mul'} or {data_dir.parent / 'tiledata.mul'}"
    )


def parse_server_config(data_dir: Path) -> MapConfig:
    values = {
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "x": DEFAULT_ORIGIN_X,
        "y": DEFAULT_ORIGIN_Y,
    }
    server_path = data_dir / "server.txt"
    if server_path.is_file():
        text = server_path.read_text(encoding="latin-1")
        for key in values:
            match = re.search(r"<\s*" + re.escape(key) + r"\s+([^>]+)>", text, re.IGNORECASE)
            if match:
                values[key] = int(match.group(1).strip())

    config = MapConfig(values["width"], values["height"], values["x"], values["y"])
    if config.width <= 0 or config.height <= 0:
        raise ValueError(f"invalid map dimensions: {config.width}x{config.height}")
    if config.width % 8 != 0 or config.height % 8 != 0:
        raise ValueError(f"map dimensions must be divisible by 8: {config.width}x{config.height}")
    return config


def load_tiledata(tiledata_path: Path) -> tuple[list[LandTile], list[ItemTile]]:
    require_file(tiledata_path)
    land_tiles: list[LandTile] = []
    item_tiles: list[ItemTile] = []
    with tiledata_path.open("rb") as fp:
        for i in range(LAND_TILE_COUNT):
            if i % 32 == 0:
                fp.read(4)
            data = fp.read(LAND_TILE_RECORD_SIZE)
            if len(data) != LAND_TILE_RECORD_SIZE:
                raise ValueError(f"{tiledata_path} ended while reading land tile {i}")
            flags = struct.unpack_from("<I", data, 0)[0]
            raw_name = data[6:26].split(b"\0", 1)[0]
            name = raw_name.decode("latin-1", errors="replace")
            land_tiles.append(LandTile(flags, name))

        for i in range(ITEM_TILE_COUNT):
            if i % 32 == 0:
                fp.read(4)
            data = fp.read(ITEM_TILE_RECORD_SIZE)
            if len(data) != ITEM_TILE_RECORD_SIZE:
                raise ValueError(f"{tiledata_path} ended while reading item tile {i}")
            flags = struct.unpack_from("<I", data, 0)[0]
            height = struct.unpack_from("<H", data, 14)[0]
            raw_name = data[17:37].split(b"\0", 1)[0]
            name = raw_name.decode("latin-1", errors="replace")
            item_tiles.append(ItemTile(flags, height, name))
    return land_tiles, item_tiles


def read_regions(regions_path: Path) -> tuple[list[str], list[Region]]:
    require_file(regions_path)
    with regions_path.open("r", encoding="latin-1", newline="") as fp:
        lines = fp.read().splitlines(keepends=True)
    regions: list[Region] = []

    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("version"):
            continue

        token_matches = list(re.finditer(r"\S+", line))
        if len(token_matches) < 9:
            continue

        try:
            prefix = int(token_matches[0].group(0))
            x = int(token_matches[1].group(0))
            y = int(token_matches[2].group(0))
            width = int(token_matches[3].group(0))
            height = int(token_matches[4].group(0))
            z_min = int(token_matches[5].group(0))
            z_max = int(token_matches[6].group(0))
            name = token_matches[8].group(0)
        except ValueError:
            continue

        regions.append(
            Region(
                line_index=line_index,
                line_no=line_index + 1,
                prefix=prefix,
                x=x,
                y=y,
                width=width,
                height=height,
                z_min=z_min,
                z_max=z_max,
                name=name,
                zmax_span=token_matches[6].span(),
            )
        )

    return lines, regions


def region_matches(region: Region, prefixes: set[int], names: set[str], name_patterns: list[str]) -> bool:
    if not prefixes and not names and not name_patterns:
        return True
    upper_name = region.name.upper()
    if region.prefix in prefixes:
        return True
    if upper_name in names:
        return True
    return any(fnmatch.fnmatchcase(upper_name, pattern) for pattern in name_patterns)


def analyze_region(
    region: Region,
    world: StaticWorld,
    item_tiles: list[ItemTile],
    min_coverage: float,
    min_underlay_coverage: float,
    min_overhead_gap: int,
) -> tuple[ClampChange | None, str | None]:
    if region.width <= 0 or region.height <= 0 or region.area <= 0:
        return None, "invalid-region-area"
    if region.z_min >= region.z_max:
        return None, "invalid-region-z"

    surfaces = world.surfaces_in_region(region)
    if not surfaces:
        return None, "no-overhead"

    tiles_by_z: dict[int, set[tuple[int, int]]] = defaultdict(set)
    items_by_z: dict[int, Counter[int]] = defaultdict(Counter)
    for surface in surfaces:
        tiles_by_z[surface.top_z].add((surface.x, surface.y))
        items_by_z[surface.top_z][surface.item_id] += 1

    saw_coverage_candidate = False
    saw_underlay_candidate = False
    for overhead_z in sorted(tiles_by_z, reverse=True):
        tile_count = len(tiles_by_z[overhead_z])
        coverage = tile_count * 100.0 / region.area
        if coverage < min_coverage:
            continue

        saw_coverage_candidate = True
        underlay_tiles = {
            (x, y)
            for x, y in tiles_by_z[overhead_z]
            if world.has_lower_walkable_layer(region, x, y, overhead_z, min_overhead_gap)
        }
        underlay_tile_count = len(underlay_tiles)
        underlay_coverage = underlay_tile_count * 100.0 / region.area
        if underlay_coverage < min_underlay_coverage:
            continue

        saw_underlay_candidate = True
        new_z_max = overhead_z - 1
        if new_z_max < region.z_min:
            return None, "invalid-clamp"

        top_tiles = [
            (item_id, item_tiles[item_id].name, count) for item_id, count in items_by_z[overhead_z].most_common(3)
        ]
        return (
            ClampChange(
                region,
                overhead_z,
                new_z_max,
                coverage,
                tile_count,
                underlay_coverage,
                underlay_tile_count,
                top_tiles,
            ),
            None,
        )

    if saw_coverage_candidate:
        return None, "below-underlay"
    if tiles_by_z:
        return None, "below-coverage"
    if saw_underlay_candidate:
        return None, "invalid-clamp"
    return None, "no-overhead"


def replacement_line(line: str, region: Region, new_z_max: int) -> str:
    start, end = region.zmax_span
    old_width = end - start
    new_text = str(new_z_max)
    if len(new_text) <= old_width:
        new_text = new_text.rjust(old_width)
    return line[:start] + new_text + line[end:]


def format_top_tiles(top_tiles: list[tuple[int, str, int]]) -> str:
    if not top_tiles:
        return "-"
    return ", ".join(f"0x{item_id:04x} {name} x{count}" for item_id, name, count in top_tiles)


def print_changes(changes: list[ClampChange], applied: bool) -> None:
    if not changes:
        print("No region zMax changes.")
        return

    action = "Applied" if applied else "Proposed"
    print(f"{action} region zMax changes:")
    print(
        f"{'line':>5} {'prefix':>6} {'name':<32} {'old':>5} {'new':>5} "
        f"{'roofZ':>5} {'cover':>7} {'under':>7} {'tiles':>9} top tiles"
    )
    print("-" * 121)
    for change in changes:
        region = change.region
        print(
            f"{region.line_no:5d} {region.prefix:6d} {region.name:<32.32} "
            f"{region.z_max:5d} {change.new_z_max:5d} {change.overhead_z:5d} "
            f"{change.coverage:6.1f}% {change.underlay_coverage:6.1f}% "
            f"{change.tile_count:4d}/{region.area:<4d} "
            f"{format_top_tiles(change.top_tiles)}"
        )


def write_regions(regions_path: Path, lines: list[str], changes: list[ClampChange]) -> Path | None:
    if not changes:
        return None

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = regions_path.with_name(f"{regions_path.name}.bak.{timestamp}")
    suffix = 1
    while backup_path.exists():
        backup_path = regions_path.with_name(f"{regions_path.name}.bak.{timestamp}-{suffix}")
        suffix += 1

    shutil.copy2(regions_path, backup_path)

    changed_by_line = {change.region.line_index: change for change in changes}
    output_lines = list(lines)
    for line_index, change in changed_by_line.items():
        output_lines[line_index] = replacement_line(output_lines[line_index], change.region, change.new_z_max)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="latin-1",
            newline="",
            dir=regions_path.parent,
            prefix=f".{regions_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fp:
            tmp_path = fp.name
            fp.writelines(output_lines)
        shutil.copystat(regions_path, tmp_path)
        os.replace(tmp_path, regions_path)
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        raise

    return backup_path


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    regions_path = data_dir / "regions.txt"
    tiledata_override = args.tiledata.expanduser().resolve() if args.tiledata else None

    try:
        if not data_dir.is_dir():
            raise FileNotFoundError(f"data directory not found: {data_dir}")
        for filename in ("regions.txt", "map0.mul", "staidx0.mul", "statics0.mul"):
            require_file(data_dir / filename)

        tiledata_path = find_tiledata(data_dir, tiledata_override)
        config = parse_server_config(data_dir)
        land_tiles, item_tiles = load_tiledata(tiledata_path)
        lines, regions = read_regions(regions_path)

        prefixes = set(args.prefix)
        names = {name.upper() for name in args.name}
        name_patterns = [pattern.upper() for pattern in args.name_like]
        selected_regions = [region for region in regions if region_matches(region, prefixes, names, name_patterns)]

        changes: list[ClampChange] = []
        no_overhead = 0
        below_coverage = 0
        below_underlay = 0
        invalid_clamp = 0

        with StaticWorld(data_dir, config, land_tiles, item_tiles) as world:
            for region in selected_regions:
                change, reason = analyze_region(
                    region,
                    world,
                    item_tiles,
                    args.min_coverage,
                    args.min_underlay_coverage,
                    args.min_overhead_gap,
                )
                if change is not None:
                    changes.append(change)
                elif reason == "no-overhead":
                    no_overhead += 1
                elif reason == "below-coverage":
                    below_coverage += 1
                elif reason == "below-underlay":
                    below_underlay += 1
                elif reason in {"invalid-clamp", "invalid-region-area", "invalid-region-z"}:
                    invalid_clamp += 1

        backup_path = write_regions(regions_path, lines, changes) if args.apply else None

        print(f"Data directory: {data_dir}")
        print(f"Tiledata: {tiledata_path}")
        print(f"Mode: {'apply' if args.apply else 'dry run'}")
        print(f"Minimum coverage: {args.min_coverage:.1f}%")
        print(f"Minimum underlay coverage: {args.min_underlay_coverage:.1f}%")
        print(f"Minimum overhead gap: {args.min_overhead_gap}")
        print(
            f"Regions parsed: {len(regions)}; examined: {len(selected_regions)}; "
            f"changes: {len(changes)}; no overhead: {no_overhead}; "
            f"below coverage: {below_coverage}; below underlay: {below_underlay}; "
            f"skipped invalid: {invalid_clamp}"
        )
        if args.apply and backup_path is not None:
            print(f"Backup: {backup_path}")
        elif not args.apply:
            print("No files changed. Pass --apply to update regions.txt.")
        print()
        print_changes(changes, applied=args.apply)
        return 0
    except (OSError, ValueError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
