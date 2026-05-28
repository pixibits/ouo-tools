#!/usr/bin/env python3
"""Set home fields and clear saved overrides/contents on placed bookcases."""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


DYNAMIC_INDEX_RECORD_SIZE = 12
END_TOKEN = b"end"
ID_PREFIX = b"id="
TYPE_PREFIX = b"type="
STAT_PREFIX = b"stat="
LOC_PREFIX = b"loc="
HOME_PREFIX = b"home="
HOME_OBJVAR_PREFIX = b"wom_var=loc home "
DECAY_COUNT_PREFIX = b"decayCount="
OVERLOADED_WEIGHT_PREFIX = b"wom_var=int overloadedWeight "
CONTAINER_PREFIX = b"cont="
EQPOS_PREFIX = b"eqpos="
FILLED_PREFIX = b"wom_var=int filled"
CALLBACK_PREFIX = b"callback="
DEFAULT_TARGET_TYPES = "2711-2716"
ITEM_FLAG_MOVABLE = 0x01


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
class ChangeSample:
    block_index: int
    serial: int
    type_id: int
    location: Location
    source: str
    added_home_field: bool
    added_home_objvar: bool
    cleared_decay_count: bool
    removed_overloaded_weight: bool
    cleared_movable_stat: bool
    removed_filled_objvars: int
    removed_fill_callbacks: int
    stale_children_for_parent: int


@dataclass(frozen=True)
class ChildRemovalSample:
    block_index: int
    serial: int | None
    type_id: int | None
    parent_serial: int
    location: Location | None


@dataclass(frozen=True)
class CleanupContext:
    parent_serials: set[int]
    parent_types: dict[int, int]
    stale_child_parent_counts: Counter[int]


@dataclass
class ScanStats:
    total_blocks: int = 0
    nonempty_blocks: int = 0
    changed_blocks: int = 0
    records_scanned: int = 0
    target_records: int = 0
    changed_records: int = 0
    home_fields_added: int = 0
    home_objvars_added: int = 0
    decay_counts_cleared: int = 0
    overloaded_weights_removed: int = 0
    movable_stat_flags_cleared: int = 0
    stale_child_records_removed: int = 0
    parent_fill_states_reset: int = 0
    filled_objvars_removed: int = 0
    fill_callbacks_removed: int = 0
    malformed_stat_fields: int = 0
    skipped_existing_home: int = 0
    skipped_contained_equipped: int = 0
    skipped_missing_id: int = 0
    skipped_missing_loc: int = 0
    skipped_malformed_home: int = 0
    target_types: Counter[int] = field(default_factory=Counter)
    changed_types: Counter[int] = field(default_factory=Counter)
    stale_child_parent_types: Counter[int] = field(default_factory=Counter)
    stale_child_types: Counter[int | None] = field(default_factory=Counter)
    samples: list[ChangeSample] = field(default_factory=list)
    child_samples: list[ChildRemovalSample] = field(default_factory=list)


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
    script_default_data_dir = Path(__file__).resolve().parent.parent / ".rundir" / "uogolddemo"
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply home fields and clear saved decay counters on "
            "placed bookcase dynamic records, remove overloadedWeight, and "
            "empty saved contents."
        )
    )
    parser.add_argument(
        "data_dir",
        nargs="?",
        default=str(script_default_data_dir),
        help="directory containing dynidx0.mul and dynamic0.mul (default: .rundir/uogolddemo/)",
    )
    parser.add_argument(
        "--types",
        default=DEFAULT_TARGET_TYPES,
        help="comma-separated item types or ranges to fix (default: 2711-2716)",
    )
    parser.add_argument("--apply", action="store_true", help="write dynidx0.mul and dynamic0.mul; default is dry run")
    parser.add_argument("--top", type=int, default=20, help="number of item types to print (default: 20)")
    parser.add_argument("--samples", type=int, default=12, help="number of changed record samples to print (default: 12)")
    args = parser.parse_args()
    try:
        args.target_types = parse_type_set(args.types)
    except ValueError as exc:
        parser.error(str(exc))
    if args.top < 0:
        parser.error("--top must be non-negative")
    if args.samples < 0:
        parser.error("--samples must be non-negative")
    return args


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"required file not found: {path}")


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


def field_int(record: list[bytes], prefix: bytes) -> int | None:
    for token in record:
        if token.startswith(prefix):
            try:
                return int(token[len(prefix) :].strip(), 0)
            except ValueError:
                return None
    return None


def field_location(record: list[bytes], prefix: bytes) -> tuple[Location | None, bool]:
    found = False
    for token in record:
        if not token.startswith(prefix):
            continue
        found = True
        parts = token[len(prefix) :].split()
        if len(parts) != 3:
            continue
        try:
            return (int(parts[0], 0), int(parts[1], 0), int(parts[2], 0)), True
        except ValueError:
            continue
    return None, found


def has_prefix(record: list[bytes], prefix: bytes) -> bool:
    return any(token.startswith(prefix) for token in record)


def is_filled_objvar_token(token: bytes) -> bool:
    return token == FILLED_PREFIX or token.startswith(FILLED_PREFIX + b" ")


def is_fill_callback_token(token: bytes) -> bool:
    if not token.startswith(CALLBACK_PREFIX):
        return False
    parts = token[len(CALLBACK_PREFIX) :].split()
    if len(parts) < 2:
        return False
    try:
        return int(parts[1], 0) == 0x50
    except ValueError:
        return False


def payload_end_index(record: list[bytes]) -> int:
    end = len(record)
    while end > 0 and record[end - 1] == b"":
        end -= 1
    return end


def insert_home_field(record: list[bytes], location: Location) -> None:
    token = f"home={location[0]} {location[1]} {location[2]}".encode("ascii")
    insert_at = 1 if record and is_record_marker(record[0]) else 0
    for index in range(payload_end_index(record)):
        if record[index].startswith(ID_PREFIX) or record[index].startswith(TYPE_PREFIX):
            insert_at = index + 1
        elif record[index].startswith(HOME_PREFIX):
            insert_at = index + 1
            break
    record.insert(insert_at, token)


def insert_home_objvar(record: list[bytes], location: Location) -> None:
    token = f"wom_var=loc home {location[0]} {location[1]} {location[2]}".encode("ascii")
    record.insert(payload_end_index(record), token)


def remove_decay_count_tokens(record: list[bytes]) -> tuple[list[bytes], int]:
    new_record = [token for token in record if not token.startswith(DECAY_COUNT_PREFIX)]
    return new_record, len(record) - len(new_record)


def remove_overloaded_weight_tokens(record: list[bytes]) -> tuple[list[bytes], int]:
    new_record = [token for token in record if not token.startswith(OVERLOADED_WEIGHT_PREFIX)]
    return new_record, len(record) - len(new_record)


def remove_fill_state_tokens(record: list[bytes]) -> tuple[list[bytes], int, int]:
    new_record: list[bytes] = []
    removed_filled_objvars = 0
    removed_fill_callbacks = 0

    for token in record:
        if is_filled_objvar_token(token):
            removed_filled_objvars += 1
            continue
        if is_fill_callback_token(token):
            removed_fill_callbacks += 1
            continue
        new_record.append(token)

    return new_record, removed_filled_objvars, removed_fill_callbacks


def clear_movable_stat_flag(record: list[bytes]) -> tuple[list[bytes], int, int]:
    new_record: list[bytes] = []
    cleared_count = 0
    malformed_count = 0

    for token in record:
        if not token.startswith(STAT_PREFIX):
            new_record.append(token)
            continue

        try:
            stat = int(token[len(STAT_PREFIX) :].strip(), 0)
        except ValueError:
            malformed_count += 1
            new_record.append(token)
            continue

        if not stat & ITEM_FLAG_MOVABLE:
            new_record.append(token)
            continue

        stat &= ~ITEM_FLAG_MOVABLE
        cleared_count += 1
        if stat != 0:
            new_record.append(f"stat={stat}".encode("ascii"))

    return new_record, cleared_count, malformed_count


def location_source(home_loc: Location | None, home_objvar_loc: Location | None) -> str:
    if home_loc is not None:
        return "existing home="
    if home_objvar_loc is not None:
        return "existing ObjVar"
    return "loc="


def visit_dynamic_records(entries: list[IndexEntry], dynamic_data: bytes, visitor) -> None:
    for block_index, entry in enumerate(entries):
        if not entry.has_data:
            continue
        end_offset = entry.offset + entry.length
        if entry.offset < 0 or end_offset > len(dynamic_data):
            raise ValueError(
                f"invalid dynamic index entry: offset={entry.offset} length={entry.length} "
                f"dynamic0 size={len(dynamic_data)}"
            )

        tokens = split_tokens(dynamic_data[entry.offset:end_offset])
        record_start: int | None = None
        for index, token in enumerate(tokens):
            if is_record_marker(token):
                if record_start is not None:
                    visitor(block_index, tokens[record_start:index])
                record_start = index
            elif token == END_TOKEN and record_start is not None:
                visitor(block_index, tokens[record_start:index])
                record_start = None

        if record_start is not None:
            visitor(block_index, tokens[record_start:])


def collect_cleanup_context(
    entries: list[IndexEntry],
    dynamic_data: bytes,
    target_types: set[int],
) -> CleanupContext:
    parent_serials: set[int] = set()
    parent_types: dict[int, int] = {}

    def collect_parent(_block_index: int, record: list[bytes]) -> None:
        type_id = field_int(record, TYPE_PREFIX)
        if type_id not in target_types:
            return
        if has_prefix(record, CONTAINER_PREFIX) or has_prefix(record, EQPOS_PREFIX):
            return
        serial = field_int(record, ID_PREFIX)
        if serial is None:
            return
        parent_serials.add(serial)
        parent_types[serial] = type_id

    visit_dynamic_records(entries, dynamic_data, collect_parent)

    stale_child_parent_counts: Counter[int] = Counter()

    def collect_child(_block_index: int, record: list[bytes]) -> None:
        parent_serial = field_int(record, CONTAINER_PREFIX)
        if parent_serial not in parent_serials:
            return
        stale_child_parent_counts[parent_serial] += 1

    visit_dynamic_records(entries, dynamic_data, collect_child)

    return CleanupContext(
        parent_serials=parent_serials,
        parent_types=parent_types,
        stale_child_parent_counts=stale_child_parent_counts,
    )


def process_record(
    record: list[bytes],
    stats: ScanStats,
    target_types: set[int],
    context: CleanupContext,
    block_index: int,
    sample_limit: int,
) -> tuple[list[bytes], bool, bool]:
    stats.records_scanned += 1

    type_id = field_int(record, TYPE_PREFIX)
    parent_serial = field_int(record, CONTAINER_PREFIX)
    if parent_serial in context.parent_serials:
        stats.changed_records += 1
        stats.stale_child_records_removed += 1
        stats.stale_child_types[type_id] += 1
        parent_type = context.parent_types.get(parent_serial)
        if parent_type is not None:
            stats.stale_child_parent_types[parent_type] += 1
        if len(stats.child_samples) < sample_limit:
            child_location, _child_location_found = field_location(record, LOC_PREFIX)
            stats.child_samples.append(
                ChildRemovalSample(
                    block_index=block_index,
                    serial=field_int(record, ID_PREFIX),
                    type_id=type_id,
                    parent_serial=parent_serial,
                    location=child_location,
                )
            )
        return [], True, True

    if type_id not in target_types:
        return record, False, False

    stats.target_records += 1
    stats.target_types[type_id] += 1

    serial = field_int(record, ID_PREFIX)
    if serial is None:
        stats.skipped_missing_id += 1
        return record, False, False

    if has_prefix(record, CONTAINER_PREFIX) or has_prefix(record, EQPOS_PREFIX):
        stats.skipped_contained_equipped += 1
        return record, False, False

    loc, loc_found = field_location(record, LOC_PREFIX)
    if loc is None:
        stats.skipped_missing_loc += 1
        return record, False, False

    home_loc, home_found = field_location(record, HOME_PREFIX)
    home_objvar_loc, home_objvar_found = field_location(record, HOME_OBJVAR_PREFIX)
    if (home_found and home_loc is None) or (home_objvar_found and home_objvar_loc is None):
        stats.skipped_malformed_home += 1
        return record, False, False

    desired_location = home_loc or home_objvar_loc or loc
    add_home_field = not home_found
    add_home_objvar = not home_objvar_found

    new_record, decay_count_tokens_removed = remove_decay_count_tokens(record)
    new_record, overloaded_weight_tokens_removed = remove_overloaded_weight_tokens(new_record)
    new_record, movable_stat_flags_cleared, malformed_stat_fields = clear_movable_stat_flag(new_record)
    new_record, filled_objvars_removed, fill_callbacks_removed = remove_fill_state_tokens(new_record)
    if add_home_field:
        insert_home_field(new_record, desired_location)
        stats.home_fields_added += 1
    if add_home_objvar:
        insert_home_objvar(new_record, desired_location)
        stats.home_objvars_added += 1
    if decay_count_tokens_removed:
        stats.decay_counts_cleared += decay_count_tokens_removed
    if overloaded_weight_tokens_removed:
        stats.overloaded_weights_removed += overloaded_weight_tokens_removed
    if movable_stat_flags_cleared:
        stats.movable_stat_flags_cleared += movable_stat_flags_cleared
    if malformed_stat_fields:
        stats.malformed_stat_fields += malformed_stat_fields
    if filled_objvars_removed:
        stats.filled_objvars_removed += filled_objvars_removed
    if fill_callbacks_removed:
        stats.fill_callbacks_removed += fill_callbacks_removed
    if filled_objvars_removed or fill_callbacks_removed:
        stats.parent_fill_states_reset += 1

    if (
        not add_home_field
        and not add_home_objvar
        and decay_count_tokens_removed == 0
        and overloaded_weight_tokens_removed == 0
        and movable_stat_flags_cleared == 0
        and filled_objvars_removed == 0
        and fill_callbacks_removed == 0
    ):
        stats.skipped_existing_home += 1
        return record, False, False

    stats.changed_records += 1
    stats.changed_types[type_id] += 1
    if len(stats.samples) < sample_limit:
        stats.samples.append(
            ChangeSample(
                block_index=block_index,
                serial=serial,
                type_id=type_id,
                location=desired_location,
                source=location_source(home_loc, home_objvar_loc),
                added_home_field=add_home_field,
                added_home_objvar=add_home_objvar,
                cleared_decay_count=decay_count_tokens_removed > 0,
                removed_overloaded_weight=overloaded_weight_tokens_removed > 0,
                cleared_movable_stat=movable_stat_flags_cleared > 0,
                removed_filled_objvars=filled_objvars_removed,
                removed_fill_callbacks=fill_callbacks_removed,
                stale_children_for_parent=context.stale_child_parent_counts[serial],
            )
        )
    return new_record, True, False


def process_block(
    block_data: bytes,
    stats: ScanStats,
    target_types: set[int],
    context: CleanupContext,
    block_index: int,
    sample_limit: int,
) -> tuple[bytes, bool]:
    tokens = split_tokens(block_data)
    replacements: list[tuple[int, int, list[bytes]]] = []
    record_start: int | None = None

    def finish_record(record_end: int, include_end_on_delete: bool = False) -> None:
        nonlocal record_start
        if record_start is None:
            return
        record = tokens[record_start:record_end]
        new_record, changed, deleted = process_record(record, stats, target_types, context, block_index, sample_limit)
        if changed:
            replacement_end = record_end + 1 if deleted and include_end_on_delete else record_end
            replacements.append((record_start, replacement_end, new_record))
        record_start = None

    for index, token in enumerate(tokens):
        if is_record_marker(token):
            finish_record(index)
            record_start = index
        elif token == END_TOKEN:
            finish_record(index, include_end_on_delete=True)

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
    apply_changes: bool,
    sample_limit: int,
) -> tuple[ScanStats, bytes | None, bytes | None]:
    index_data = index_path.read_bytes()
    dynamic_data = data_path.read_bytes()
    entries, trailer = parse_index(index_data)
    context = collect_cleanup_context(entries, dynamic_data, target_types)
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
        new_block_data, changed = process_block(block_data, stats, target_types, context, block_index, sample_limit)
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


def format_type(type_id: int) -> str:
    return f"0x{type_id:04X} {type_id}"


def format_type_optional(type_id: int | None) -> str:
    if type_id is None:
        return "<missing>"
    return format_type(type_id)


def format_location(location: Location) -> str:
    return f"{location[0]} {location[1]} {location[2]}"


def format_location_optional(location: Location | None) -> str:
    if location is None:
        return "<missing>"
    return format_location(location)


def print_report(
    data_dir: Path,
    index_path: Path,
    data_path: Path,
    target_types: set[int],
    stats: ScanStats,
    applied: bool,
    top: int,
    backups: tuple[Path, Path] | None,
) -> None:
    action = "Applied" if applied else "Proposed"
    type_list = ", ".join(format_type(type_id) for type_id in sorted(target_types))
    print(f"Data directory: {data_dir}")
    print(f"Index: {index_path}")
    print(f"Data: {data_path}")
    print(f"Target types: {type_list}")
    print(f"Mode: {'apply' if applied else 'dry run'}")
    print(
        f"Blocks: {stats.total_blocks}; non-empty: {stats.nonempty_blocks}; changed: {stats.changed_blocks}"
    )
    print(
        f"Records scanned: {stats.records_scanned}; target records: {stats.target_records}; "
        f"{action.lower()}: {stats.changed_records}"
    )
    print(
        f"Fields added: home= {stats.home_fields_added}; "
        f"wom_var=loc home {stats.home_objvars_added}"
    )
    print(f"Saved decayCount fields cleared to load as 0: {stats.decay_counts_cleared}")
    print(
        f"overloadedWeight fields removed: {stats.overloaded_weights_removed}; "
        f"movable stat flags cleared: {stats.movable_stat_flags_cleared}"
    )
    print(
        f"Removed bookcase child records: {stats.stale_child_records_removed}; "
        f"reset parent fill state: {stats.parent_fill_states_reset}"
    )
    print(
        f"Removed filled objvars: {stats.filled_objvars_removed}; "
        f"removed fill callbacks: {stats.fill_callbacks_removed}"
    )
    print(
        f"Skipped parent records already complete with decayCount 0, no overloadedWeight, "
        f"and no fill state: "
        f"{stats.skipped_existing_home}; "
        f"skipped contained/equipped: {stats.skipped_contained_equipped}"
    )
    print(
        f"Skipped missing id: {stats.skipped_missing_id}; missing loc: {stats.skipped_missing_loc}; "
        f"malformed existing home field: {stats.skipped_malformed_home}"
    )
    if stats.malformed_stat_fields:
        print(f"Malformed stat fields left unchanged: {stats.malformed_stat_fields}")
    if backups is not None:
        index_backup, data_backup = backups
        print(f"Index backup: {index_backup}")
        print(f"Data backup: {data_backup}")
    elif not applied:
        print("No files changed. Pass --apply to update dynidx0.mul and dynamic0.mul.")
    print()

    if stats.target_types and top:
        print("Target records by item type:")
        print(f"{'count':>7} {'changed':>7} {'children':>8} item")
        print("-" * 48)
        for type_id, count in stats.target_types.most_common(top):
            changed = stats.changed_types[type_id]
            children = stats.stale_child_parent_types[type_id]
            print(f"{count:7d} {changed:7d} {children:8d} {format_type(type_id)}")
        if len(stats.target_types) > top:
            shown = stats.target_types.most_common(top)
            remaining_count = sum(stats.target_types.values()) - sum(count for _type_id, count in shown)
            remaining_changed = sum(stats.changed_types.values()) - sum(
                stats.changed_types[type_id] for type_id, _count in shown
            )
            remaining_children = sum(stats.stale_child_parent_types.values()) - sum(
                stats.stale_child_parent_types[type_id] for type_id, _count in shown
            )
            print(
                f"{remaining_count:7d} {remaining_changed:7d} {remaining_children:8d} "
                f"... {len(stats.target_types) - top} more item type(s)"
            )
        print()

    if stats.stale_child_types and top:
        print("Removed child records by item type:")
        print(f"{'removed':>7} item")
        print("-" * 32)
        for type_id, count in stats.stale_child_types.most_common(top):
            print(f"{count:7d} {format_type_optional(type_id)}")
        if len(stats.stale_child_types) > top:
            shown = stats.stale_child_types.most_common(top)
            remaining_removed = sum(stats.stale_child_types.values()) - sum(count for _type_id, count in shown)
            print(f"{remaining_removed:7d} ... {len(stats.stale_child_types) - top} more item type(s)")
        print()

    if stats.changed_records == 0:
        print(
            "No target bookcase records or contained child records need home, decayCount, "
            "overloadedWeight, fill-state, stat, or contents changes."
        )
        return

    if stats.samples:
        print(f"{action} parent record samples:")
        print(f"{'block':>7} {'serial':>10} {'type':>11} {'location':>15} changes")
        print("-" * 72)
        for sample in stats.samples:
            added_parts = []
            if sample.added_home_field:
                added_parts.append("home=")
            if sample.added_home_objvar:
                added_parts.append("ObjVar")
            if sample.cleared_decay_count:
                added_parts.append("decayCount=0")
            if sample.removed_overloaded_weight:
                added_parts.append("remove overloadedWeight")
            if sample.cleared_movable_stat:
                added_parts.append("clear stat movable")
            if sample.removed_filled_objvars:
                added_parts.append(f"remove filled x{sample.removed_filled_objvars}")
            if sample.removed_fill_callbacks:
                added_parts.append(f"remove callback 0x50 x{sample.removed_fill_callbacks}")
            if sample.stale_children_for_parent:
                added_parts.append(f"children={sample.stale_children_for_parent}")
            added = "+".join(added_parts) if added_parts else "-"
            print(
                f"{sample.block_index:7d} 0x{sample.serial:08X} {format_type(sample.type_id):>11} "
                f"{format_location(sample.location):>15} {added} from {sample.source}"
            )
        print()

    if stats.child_samples:
        print(f"{action} child record removals:")
        print(f"{'block':>7} {'serial':>10} {'type':>16} {'parent':>10} {'location':>15}")
        print("-" * 72)
        for sample in stats.child_samples:
            serial = "<missing>" if sample.serial is None else f"0x{sample.serial:08X}"
            print(
                f"{sample.block_index:7d} {serial:>10} "
                f"{format_type_optional(sample.type_id):>16} "
                f"0x{sample.parent_serial:08X} {format_location_optional(sample.location):>15}"
            )


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    index_path = data_dir / "dynidx0.mul"
    data_path = data_dir / "dynamic0.mul"

    try:
        if not data_dir.is_dir():
            raise FileNotFoundError(f"data directory not found: {data_dir}")
        require_file(index_path)
        require_file(data_path)

        stats, new_index, new_dynamic = scan_dynamic(
            index_path,
            data_path,
            args.target_types,
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
            args.target_types,
            stats,
            args.apply,
            args.top,
            backups,
        )
        return 0
    except (OSError, ValueError, struct.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
