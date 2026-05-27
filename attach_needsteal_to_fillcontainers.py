#!/usr/bin/env python3
"""Attach needsteal to fill containers in justice/city regions."""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


DYNAMIC_INDEX_RECORD_SIZE = 12
LAND_TILE_COUNT = 16384
ITEM_TILE_COUNT = 16384
LAND_TILE_RECORD_SIZE = 26
ITEM_TILE_RECORD_SIZE = 37

END_TOKEN = b"end"
ID_PREFIX = b"id="
TYPE_PREFIX = b"type="
LOC_PREFIX = b"loc="
CONTAINER_PREFIX = b"cont="
EQPOS_PREFIX = b"eqpos="
WOM_SCR_PREFIX = b"wom_scr="
TF_CONTAINER = 0x00200000


Location = tuple[int, int, int]


@dataclass(frozen=True)
class IndexEntry:
    offset: int
    length: int
    extra: int

    @property
    def has_data(self) -> bool:
        return self.offset >= 0 and self.length > 0


@dataclass(frozen=True)
class ItemTile:
    name: str
    flags: int


@dataclass(frozen=True)
class Region:
    line_no: int
    x: int
    y: int
    width: int
    height: int
    z_min: int
    z_max: int
    name: str
    description: str

    def contains(self, location: Location) -> bool:
        x, y, z = location
        return (
            self.x <= x <= self.x + self.width
            and self.y <= y <= self.y + self.height
            and self.z_min <= z <= self.z_max
        )


@dataclass(frozen=True)
class ChangeSample:
    block_index: int
    serial: int
    type_id: int
    type_name: str
    location: Location
    region_name: str


@dataclass
class TargetResolution:
    types: set[int] = field(default_factory=set)
    source_types: Counter[int] = field(default_factory=Counter)
    decoded_types: Counter[int] = field(default_factory=Counter)
    decoded_files: int = 0
    decode_failures: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class ScanStats:
    total_blocks: int = 0
    nonempty_blocks: int = 0
    changed_blocks: int = 0
    records_scanned: int = 0
    target_type_records: int = 0
    eligible_records: int = 0
    changed_records: int = 0
    already_had_script: int = 0
    skipped_contained_equipped: int = 0
    skipped_missing_id: int = 0
    skipped_missing_loc: int = 0
    skipped_missing_tiledata: int = 0
    skipped_non_container_tiledata: int = 0
    skipped_outside_region: int = 0
    target_types: Counter[int] = field(default_factory=Counter)
    eligible_types: Counter[int] = field(default_factory=Counter)
    changed_types: Counter[int] = field(default_factory=Counter)
    samples: list[ChangeSample] = field(default_factory=list)


def parse_type_set(raw: str) -> set[int]:
    type_ids: set[int] = set()
    for chunk in raw.split(","):
        part = chunk.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text.strip(), 0)
            end = int(end_text.strip(), 0)
            if end < start:
                raise ValueError(f"invalid descending range: {part}")
            type_ids.update(range(start, end + 1))
        else:
            type_ids.add(int(part, 0))

    if not type_ids:
        raise ValueError("at least one item type is required")
    for type_id in type_ids:
        if type_id < 0 or type_id > 0xFFFF:
            raise ValueError(f"item type out of range: {type_id}")
    return type_ids


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    default_data_dir = repo_root / ".rundir" / "uogolddemo"
    default_scripts_dir = repo_root / ".rundir" / "scripts"
    default_scripts_wombat_dir = repo_root / ".rundir" / "scripts.wombat"
    default_wombat = repo_root / "uotools" / "wombat" / "wombat"

    parser = argparse.ArgumentParser(
        description="Dry-run or attach a script to fill containers in justice/city regions."
    )
    parser.add_argument(
        "data_dir",
        nargs="?",
        default=str(default_data_dir),
        help="directory containing dynidx0.mul and dynamic0.mul (default: .rundir/uogolddemo/)",
    )
    parser.add_argument(
        "--regions",
        type=Path,
        help="path to regions.txt (default: data_dir/regions.txt)",
    )
    parser.add_argument(
        "--tiledata",
        type=Path,
        help="path to tiledata.mul (default: data_dir/tiledata.mul or data_dir/../tiledata.mul)",
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=default_scripts_dir,
        help="directory containing encoded scripts and sdb.txt (default: .rundir/scripts/)",
    )
    parser.add_argument(
        "--scripts-wombat-dir",
        type=Path,
        default=default_scripts_wombat_dir,
        help="directory containing decoded Wombat scripts (default: .rundir/scripts.wombat/)",
    )
    parser.add_argument(
        "--wombat",
        type=Path,
        default=default_wombat,
        help="wombat encoder/decoder path used for compiled-only scripts (default: uotools/wombat/wombat)",
    )
    parser.add_argument(
        "--sdb",
        type=Path,
        help="path to sdb.txt for decoding compiled-only scripts (default: scripts-dir/sdb.txt)",
    )
    parser.add_argument(
        "--script-name",
        default="needsteal",
        help="script name to attach (default: needsteal)",
    )
    parser.add_argument(
        "--types",
        help="comma-separated item types or ranges to target instead of resolving fill-container scripts",
    )
    parser.add_argument("--apply", action="store_true", help="write dynidx0.mul and dynamic0.mul; default is dry run")
    parser.add_argument("--top", type=int, default=20, help="number of item types to print (default: 20)")
    parser.add_argument("--samples", type=int, default=12, help="number of changed record samples to print (default: 12)")
    args = parser.parse_args()

    if args.types is not None:
        try:
            args.target_types_override = parse_type_set(args.types)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        args.target_types_override = None

    if not args.script_name or any(char.isspace() for char in args.script_name):
        parser.error("--script-name must be a non-empty script name without whitespace")
    if args.top < 0:
        parser.error("--top must be non-negative")
    if args.samples < 0:
        parser.error("--samples must be non-negative")
    return args


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"required file not found: {path}")


def require_dir(path: Path) -> None:
    if not path.is_dir():
        raise FileNotFoundError(f"required directory not found: {path}")


def find_tiledata(data_dir: Path, override: Path | None) -> Path:
    if override is not None:
        require_file(override)
        return override
    for candidate in (data_dir / "tiledata.mul", data_dir.parent / "tiledata.mul"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"tiledata.mul not found at {data_dir / 'tiledata.mul'} or {data_dir.parent / 'tiledata.mul'}"
    )


def parse_index(index_data: bytes) -> tuple[list[IndexEntry], bytes]:
    record_bytes = (len(index_data) // DYNAMIC_INDEX_RECORD_SIZE) * DYNAMIC_INDEX_RECORD_SIZE
    trailer = index_data[record_bytes:]
    entries: list[IndexEntry] = []
    for pos in range(0, record_bytes, DYNAMIC_INDEX_RECORD_SIZE):
        offset, length, extra = struct.unpack_from("<iii", index_data, pos)
        entries.append(IndexEntry(offset=offset, length=length, extra=extra))
    return entries, trailer


def split_tokens(block_data: bytes) -> list[bytes]:
    tokens = block_data.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    return tokens


def serialize_tokens(tokens: list[bytes]) -> bytes:
    if not tokens:
        return b""
    return b"\0".join(tokens) + b"\0"


def is_record_marker(token: bytes) -> bool:
    return len(token) == 3 and token.startswith(b"@=")


def payload_end_index(record: list[bytes]) -> int:
    end = len(record)
    while end > 0 and record[end - 1] == b"":
        end -= 1
    return end


def field_int(record: list[bytes], prefix: bytes) -> int | None:
    for token in record:
        if token.startswith(prefix):
            try:
                return int(token[len(prefix) :].strip(), 0)
            except ValueError:
                return None
    return None


def field_location(record: list[bytes], prefix: bytes) -> Location | None:
    for token in record:
        if not token.startswith(prefix):
            continue
        parts = token[len(prefix) :].split()
        if len(parts) != 3:
            return None
        try:
            return (int(parts[0], 0), int(parts[1], 0), int(parts[2], 0))
        except ValueError:
            return None
    return None


def has_prefix(record: list[bytes], prefix: bytes) -> bool:
    return any(token.startswith(prefix) for token in record)


def script_token_name(token: bytes) -> str | None:
    if not token.startswith(WOM_SCR_PREFIX):
        return None
    value = token[len(WOM_SCR_PREFIX) :].split(None, 1)
    if not value:
        return None
    return value[0].decode("ascii", errors="ignore")


def has_script(record: list[bytes], script_name: str) -> bool:
    wanted = script_name.lower()
    for token in record:
        name = script_token_name(token)
        if name is not None and name.lower() == wanted:
            return True
    return False


def insert_script(record: list[bytes], script_name: str) -> None:
    token = f"wom_scr={script_name} 0".encode("ascii")
    insert_at = payload_end_index(record)
    for index in range(insert_at):
        if record[index].startswith(WOM_SCR_PREFIX):
            insert_at = index + 1
    record.insert(insert_at, token)


def load_tiledata(tiledata_path: Path) -> dict[int, ItemTile]:
    item_tiles: dict[int, ItemTile] = {}
    with tiledata_path.open("rb") as fp:
        for i in range(LAND_TILE_COUNT):
            if i % 32 == 0:
                fp.read(4)
            data = fp.read(LAND_TILE_RECORD_SIZE)
            if len(data) != LAND_TILE_RECORD_SIZE:
                raise ValueError(f"{tiledata_path} ended while reading land tile {i}")

        for i in range(ITEM_TILE_COUNT):
            if i % 32 == 0:
                fp.read(4)
            data = fp.read(ITEM_TILE_RECORD_SIZE)
            if len(data) != ITEM_TILE_RECORD_SIZE:
                raise ValueError(f"{tiledata_path} ended while reading item tile {i}")
            flags = struct.unpack_from("<I", data, 0)[0]
            raw_name = data[17:37].split(b"\0", 1)[0]
            name = raw_name.decode("latin-1", errors="replace")
            item_tiles[i] = ItemTile(name=name, flags=flags)
    return item_tiles


def is_target_inherits(source: str) -> bool:
    stripped = source.lstrip()
    return (
        stripped.startswith("inherits fillcontainer;")
        or stripped.startswith("inherits fillcontainerwithfurniture;")
        or stripped.startswith("inherits fillbookshelves;")
    )


def numeric_stem(path: Path) -> int | None:
    try:
        type_id = int(path.stem, 0)
    except ValueError:
        return None
    if type_id < 0 or type_id > 0xFFFF:
        return None
    return type_id


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def decode_script(wombat_path: Path, sdb_path: Path, encoded_path: Path) -> str:
    with tempfile.NamedTemporaryFile("r", encoding="utf-8", errors="replace", delete=False) as fp:
        tmp_name = fp.name
    tmp_path = Path(tmp_name)
    try:
        result = subprocess.run(
            [str(wombat_path), "-d", "-s", str(sdb_path), str(encoded_path), str(tmp_path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise ValueError(detail)
        return tmp_path.read_text(encoding="utf-8", errors="replace")
    finally:
        tmp_path.unlink(missing_ok=True)


def resolve_target_types(
    scripts_wombat_dir: Path,
    scripts_dir: Path,
    wombat_path: Path,
    sdb_path: Path,
) -> TargetResolution:
    require_dir(scripts_wombat_dir)
    require_dir(scripts_dir)

    resolution = TargetResolution()
    source_type_ids: set[int] = set()

    for source_path in sorted(scripts_wombat_dir.glob("*.m")):
        type_id = numeric_stem(source_path)
        if type_id is None:
            continue
        source_type_ids.add(type_id)
        if is_target_inherits(read_text(source_path)):
            resolution.types.add(type_id)
            resolution.source_types[type_id] += 1

    compiled_only = [
        encoded_path
        for encoded_path in sorted(scripts_dir.glob("*.m"))
        if numeric_stem(encoded_path) is not None and numeric_stem(encoded_path) not in source_type_ids
    ]
    if compiled_only:
        require_file(wombat_path)
        require_file(sdb_path)

    for encoded_path in compiled_only:
        type_id = numeric_stem(encoded_path)
        assert type_id is not None
        try:
            source = decode_script(wombat_path, sdb_path, encoded_path)
        except ValueError as exc:
            if len(resolution.decode_failures) < 20:
                resolution.decode_failures.append((encoded_path, str(exc)))
            continue
        resolution.decoded_files += 1
        if is_target_inherits(source):
            resolution.types.add(type_id)
            resolution.decoded_types[type_id] += 1

    return resolution


def load_regions(regions_path: Path) -> list[Region]:
    require_file(regions_path)
    regions: list[Region] = []
    for line_no, line in enumerate(regions_path.read_text(encoding="latin-1", errors="replace").splitlines(), 1):
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit():
            continue
        parts = stripped.split(None, 14)
        if len(parts) < 9:
            continue
        try:
            x = int(parts[1], 0)
            y = int(parts[2], 0)
            width = int(parts[3], 0)
            height = int(parts[4], 0)
            z_min = int(parts[5], 0)
            z_max = int(parts[6], 0)
        except ValueError:
            continue
        name = parts[8]
        description = parts[14] if len(parts) > 14 else ""
        name_lower = name.lower()
        if not (name_lower.startswith("justice") or name_lower.startswith("city")):
            continue
        regions.append(
            Region(
                line_no=line_no,
                x=x,
                y=y,
                width=width,
                height=height,
                z_min=z_min,
                z_max=z_max,
                name=name,
                description=description,
            )
        )
    return regions


def find_region(location: Location, regions: list[Region]) -> Region | None:
    for region in regions:
        if region.contains(location):
            return region
    return None


def process_record(
    record: list[bytes],
    stats: ScanStats,
    target_types: set[int],
    item_tiles: dict[int, ItemTile],
    regions: list[Region],
    script_name: str,
    block_index: int,
    sample_limit: int,
) -> tuple[list[bytes], bool]:
    stats.records_scanned += 1

    type_id = field_int(record, TYPE_PREFIX)
    if type_id not in target_types:
        return record, False

    stats.target_type_records += 1
    stats.target_types[type_id] += 1

    serial = field_int(record, ID_PREFIX)
    if serial is None:
        stats.skipped_missing_id += 1
        return record, False

    if has_prefix(record, CONTAINER_PREFIX) or has_prefix(record, EQPOS_PREFIX):
        stats.skipped_contained_equipped += 1
        return record, False

    location = field_location(record, LOC_PREFIX)
    if location is None:
        stats.skipped_missing_loc += 1
        return record, False

    tile = item_tiles.get(type_id)
    if tile is None:
        stats.skipped_missing_tiledata += 1
        return record, False
    if not tile.flags & TF_CONTAINER:
        stats.skipped_non_container_tiledata += 1
        return record, False

    region = find_region(location, regions)
    if region is None:
        stats.skipped_outside_region += 1
        return record, False

    stats.eligible_records += 1
    stats.eligible_types[type_id] += 1

    if has_script(record, script_name):
        stats.already_had_script += 1
        return record, False

    new_record = list(record)
    insert_script(new_record, script_name)

    stats.changed_records += 1
    stats.changed_types[type_id] += 1
    if len(stats.samples) < sample_limit:
        stats.samples.append(
            ChangeSample(
                block_index=block_index,
                serial=serial,
                type_id=type_id,
                type_name=tile.name,
                location=location,
                region_name=region.name,
            )
        )

    return new_record, True


def process_block(
    block_data: bytes,
    stats: ScanStats,
    target_types: set[int],
    item_tiles: dict[int, ItemTile],
    regions: list[Region],
    script_name: str,
    block_index: int,
    sample_limit: int,
) -> tuple[bytes, bool]:
    tokens = split_tokens(block_data)
    replacements: list[tuple[int, int, list[bytes]]] = []
    record_start: int | None = None

    def finish_record(record_end: int) -> None:
        nonlocal record_start
        if record_start is None:
            return
        record = tokens[record_start:record_end]
        new_record, changed = process_record(
            record,
            stats,
            target_types,
            item_tiles,
            regions,
            script_name,
            block_index,
            sample_limit,
        )
        if changed:
            replacements.append((record_start, record_end, new_record))
        record_start = None

    for index, token in enumerate(tokens):
        if is_record_marker(token):
            finish_record(index)
            record_start = index
        elif token == END_TOKEN:
            finish_record(index)

    if record_start is not None:
        finish_record(len(tokens))

    if not replacements:
        return block_data, False

    out_tokens: list[bytes] = []
    cursor = 0
    for start, end, new_record in replacements:
        out_tokens.extend(tokens[cursor:start])
        out_tokens.extend(new_record)
        cursor = end
    out_tokens.extend(tokens[cursor:])
    return serialize_tokens(out_tokens), True


def scan_dynamic(
    index_path: Path,
    data_path: Path,
    target_types: set[int],
    item_tiles: dict[int, ItemTile],
    regions: list[Region],
    script_name: str,
    apply_changes: bool,
    sample_limit: int,
) -> tuple[ScanStats, bytes | None, bytes | None]:
    index_data = index_path.read_bytes()
    dynamic_data = data_path.read_bytes()
    entries, trailer = parse_index(index_data)
    stats = ScanStats(total_blocks=len(entries))

    out_index = bytearray() if apply_changes else None
    out_dynamic = bytearray() if apply_changes else None

    for block_index, entry in enumerate(entries):
        if not entry.has_data:
            if apply_changes:
                assert out_index is not None
                out_index += struct.pack("<iii", -1, -1, entry.extra)
            continue

        stats.nonempty_blocks += 1
        end_offset = entry.offset + entry.length
        if entry.offset < 0 or end_offset > len(dynamic_data):
            raise ValueError(
                f"invalid dynamic index entry: offset={entry.offset} length={entry.length} "
                f"dynamic0 size={len(dynamic_data)}"
            )

        block_data = dynamic_data[entry.offset:end_offset]
        new_block_data, changed = process_block(
            block_data,
            stats,
            target_types,
            item_tiles,
            regions,
            script_name,
            block_index,
            sample_limit,
        )
        if changed:
            stats.changed_blocks += 1

        if apply_changes:
            assert out_dynamic is not None and out_index is not None
            offset = len(out_dynamic)
            out_dynamic += new_block_data
            out_index += struct.pack("<iii", offset, len(new_block_data), entry.extra)

    if apply_changes:
        assert out_index is not None and out_dynamic is not None
        out_index += trailer
        return stats, bytes(out_index), bytes(out_dynamic)
    return stats, None, None


def make_backup_paths(index_path: Path, data_path: Path) -> tuple[Path, Path]:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = 0
    while True:
        suffix_text = "" if suffix == 0 else f"-{suffix}"
        index_backup = index_path.with_name(f"{index_path.name}.bak.{timestamp}{suffix_text}")
        data_backup = data_path.with_name(f"{data_path.name}.bak.{timestamp}{suffix_text}")
        if not index_backup.exists() and not data_backup.exists():
            return index_backup, data_backup
        suffix += 1


def write_temp_bytes(final_path: Path, data: bytes) -> Path:
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=final_path.parent,
            prefix=f".{final_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fp:
            tmp_path = fp.name
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        shutil.copystat(final_path, tmp_path)
        return Path(tmp_path)
    except Exception:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)
        raise


def apply_outputs(index_path: Path, data_path: Path, new_index: bytes, new_dynamic: bytes) -> tuple[Path, Path]:
    tmp_index: Path | None = None
    tmp_dynamic: Path | None = None
    index_backup: Path | None = None
    data_backup: Path | None = None

    try:
        tmp_index = write_temp_bytes(index_path, new_index)
        tmp_dynamic = write_temp_bytes(data_path, new_dynamic)

        index_backup, data_backup = make_backup_paths(index_path, data_path)
        shutil.copy2(index_path, index_backup)
        shutil.copy2(data_path, data_backup)

        os.replace(tmp_dynamic, data_path)
        tmp_dynamic = None
        try:
            os.replace(tmp_index, index_path)
            tmp_index = None
        except Exception:
            if data_backup is not None:
                shutil.copy2(data_backup, data_path)
            raise
    except Exception:
        if tmp_index is not None:
            tmp_index.unlink(missing_ok=True)
        if tmp_dynamic is not None:
            tmp_dynamic.unlink(missing_ok=True)
        raise

    assert index_backup is not None and data_backup is not None
    return index_backup, data_backup


def format_location(location: Location) -> str:
    return f"{location[0]} {location[1]} {location[2]}"


def format_type(type_id: int, item_tiles: dict[int, ItemTile]) -> str:
    tile = item_tiles.get(type_id)
    if tile is None:
        return f"0x{type_id:04X} {type_id}"
    return f"0x{type_id:04X} {type_id} flags=0x{tile.flags:08X} {tile.name}"


def print_report(
    data_dir: Path,
    index_path: Path,
    data_path: Path,
    regions_path: Path,
    tiledata_path: Path,
    scripts_dir: Path,
    scripts_wombat_dir: Path,
    script_name: str,
    target_resolution: TargetResolution,
    target_types: set[int],
    item_tiles: dict[int, ItemTile],
    regions: list[Region],
    stats: ScanStats,
    applied: bool,
    top: int,
    backups: tuple[Path, Path] | None,
) -> None:
    action = "Applied" if applied else "Proposed"
    print(f"Data directory: {data_dir}")
    print(f"Index: {index_path}")
    print(f"Data: {data_path}")
    print(f"Regions: {regions_path}")
    print(f"Tiledata: {tiledata_path}")
    print(f"Scripts: {scripts_dir}")
    print(f"Decoded scripts: {scripts_wombat_dir}")
    print(f"Attach script: {script_name}")
    print(f"Mode: {'apply' if applied else 'dry run'}")
    print(f"Justice/city regions loaded: {len(regions)}")
    print(
        f"Target fill-container types: {len(target_types)} "
        f"(source: {len(target_resolution.source_types)}, decoded: {len(target_resolution.decoded_types)})"
    )
    if target_resolution.decode_failures:
        print(f"Compiled script decode failures ignored: {len(target_resolution.decode_failures)}")
    print(
        f"Blocks: {stats.total_blocks}; non-empty: {stats.nonempty_blocks}; changed: {stats.changed_blocks}"
    )
    print(
        f"Records scanned: {stats.records_scanned}; target-type records: {stats.target_type_records}; "
        f"eligible: {stats.eligible_records}; {action.lower()}: {stats.changed_records}"
    )
    print(
        f"Skipped already had {script_name}: {stats.already_had_script}; "
        f"skipped contained/equipped: {stats.skipped_contained_equipped}"
    )
    print(
        f"Skipped outside justice/city: {stats.skipped_outside_region}; "
        f"skipped non-container tiledata: {stats.skipped_non_container_tiledata}"
    )
    print(
        f"Skipped missing id: {stats.skipped_missing_id}; missing loc: {stats.skipped_missing_loc}; "
        f"missing tiledata: {stats.skipped_missing_tiledata}"
    )
    if backups is not None:
        index_backup, data_backup = backups
        print(f"Index backup: {index_backup}")
        print(f"Data backup: {data_backup}")
    elif not applied:
        print("No files changed. Pass --apply to update dynidx0.mul and dynamic0.mul.")
    print()

    if stats.target_types and top:
        print("Target records by item type:")
        print(f"{'target':>7} {'eligible':>8} {'changed':>7} item")
        print("-" * 96)
        for type_id, count in stats.target_types.most_common(top):
            print(
                f"{count:7d} {stats.eligible_types[type_id]:8d} {stats.changed_types[type_id]:7d} "
                f"{format_type(type_id, item_tiles)}"
            )
        if len(stats.target_types) > top:
            shown = stats.target_types.most_common(top)
            remaining_target = sum(stats.target_types.values()) - sum(count for _type_id, count in shown)
            remaining_eligible = sum(stats.eligible_types.values()) - sum(
                stats.eligible_types[type_id] for type_id, _count in shown
            )
            remaining_changed = sum(stats.changed_types.values()) - sum(
                stats.changed_types[type_id] for type_id, _count in shown
            )
            print(
                f"{remaining_target:7d} {remaining_eligible:8d} {remaining_changed:7d} "
                f"... {len(stats.target_types) - top} more item type(s)"
            )
        print()

    if stats.changed_records == 0:
        print(f"No eligible fill-container records need {script_name}.")
        return

    if stats.samples:
        print(f"{action} record samples:")
        print(f"{'block':>7} {'serial':>10} {'type':>11} {'location':>15} {'region':>22} item")
        print("-" * 96)
        for sample in stats.samples:
            print(
                f"{sample.block_index:7d} 0x{sample.serial:08X} "
                f"0x{sample.type_id:04X} {sample.type_id:5d} "
                f"{format_location(sample.location):>15} {sample.region_name:>22} {sample.type_name}"
            )


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    index_path = data_dir / "dynidx0.mul"
    data_path = data_dir / "dynamic0.mul"
    regions_path = (args.regions.expanduser().resolve() if args.regions else data_dir / "regions.txt")
    tiledata_override = args.tiledata.expanduser().resolve() if args.tiledata else None
    scripts_dir = args.scripts_dir.expanduser().resolve()
    scripts_wombat_dir = args.scripts_wombat_dir.expanduser().resolve()
    wombat_path = args.wombat.expanduser().resolve()
    sdb_path = args.sdb.expanduser().resolve() if args.sdb else scripts_dir / "sdb.txt"

    try:
        if not data_dir.is_dir():
            raise FileNotFoundError(f"data directory not found: {data_dir}")
        require_file(index_path)
        require_file(data_path)

        tiledata_path = find_tiledata(data_dir, tiledata_override)
        item_tiles = load_tiledata(tiledata_path)
        regions = load_regions(regions_path)
        if not regions:
            raise ValueError(f"no justice/city regions found in {regions_path}")

        if args.target_types_override is not None:
            target_resolution = TargetResolution(types=set(args.target_types_override))
            target_types = target_resolution.types
        else:
            target_resolution = resolve_target_types(scripts_wombat_dir, scripts_dir, wombat_path, sdb_path)
            target_types = target_resolution.types

        if not target_types:
            raise ValueError("no target fill-container item types were resolved")

        stats, new_index, new_dynamic = scan_dynamic(
            index_path,
            data_path,
            target_types,
            item_tiles,
            regions,
            args.script_name,
            args.apply,
            args.samples,
        )

        backups = None
        if args.apply and stats.changed_records > 0:
            assert new_index is not None and new_dynamic is not None
            backups = apply_outputs(index_path, data_path, new_index, new_dynamic)

        print_report(
            data_dir,
            index_path,
            data_path,
            regions_path,
            tiledata_path,
            scripts_dir,
            scripts_wombat_dir,
            args.script_name,
            target_resolution,
            target_types,
            item_tiles,
            regions,
            stats,
            args.apply,
            args.top,
            backups,
        )
        return 0
    except (OSError, ValueError, struct.error, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
