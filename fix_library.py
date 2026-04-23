# -*- coding: utf-8 -*-
# Scan the Bambu Lab RFID library for misplaced tag folders and optionally fix them.
# Run with no flags to report mismatches; add --fix to move folders and update README.
# Add --quarantine to move suspicious entries to _quarantine/ instead of their expected location.
# Usage: python fix_library.py [library_root] [--fix] [--quarantine]

import sys
import shutil
import argparse
from pathlib import Path

from update_readme import run as update_readme

from parse import Tag
from categories import (
    CATEGORY_MAP, MULTI_COLOR_MATERIAL_MAP, MATERIAL_MAP,
    resolve_material, allowed_material_folders,
)

DUMP_SUFFIX = "-dump.bin"


def is_suspicious(tag_data):
    """
    Return a warning string if the tag data looks internally inconsistent,
    or None if it looks plausible.

    Detects:
    - blank detailed_filament_type (corrupt or unwritten tag)
    - filament_color_count > 1 for a material with no known multi-colour variant
    """
    base = tag_data['detailed_filament_type']
    if not base:
        return (
            f"detailed_filament_type is blank — tag may be corrupt or unwritten "
            f"(color={tag_data['filament_color']}, variant={tag_data['variant_id']})"
        )
    count = tag_data.get('filament_color_count', 1)
    if count > 1 and base not in MULTI_COLOR_MATERIAL_MAP:
        return (
            f"tag claims {count} colours but '{base}' has no known multi-colour variant "
            f"(color={tag_data['filament_color']})"
        )
    return None


def scan_library(library_root):
    mismatches = []
    parse_errors = []

    for dump_file in sorted(library_root.rglob(f'*{DUMP_SUFFIX}')):
        rel = dump_file.relative_to(library_root)
        parts = rel.parts
        # Skip internal folders (quarantine, cache, etc.)
        if parts[0].startswith('_'):
            continue
        # Expected structure: category / material / color / uid / <file>
        if len(parts) < 5:
            continue

        cat_dir, mat_dir, color_dir, uid_dir = parts[0], parts[1], parts[2], parts[3]

        try:
            with open(dump_file, 'rb') as f:
                tag = Tag(dump_file.name, f.read(), fail_on_warn=False)
        except Exception as e:
            parse_errors.append((rel, str(e)))
            continue

        expected_cat = CATEGORY_MAP.get(tag.data['filament_type'], tag.data['filament_type'])
        raw_mat      = tag.data['detailed_filament_type']
        expected_mat = resolve_material(tag.data)
        allowed_mats = allowed_material_folders(tag.data)

        warning = is_suspicious(tag.data)

        if cat_dir != expected_cat or mat_dir not in allowed_mats:
            mismatches.append({
                'uid':      uid_dir,
                'current':  str(library_root / cat_dir / mat_dir / color_dir / uid_dir),
                'expected': str(library_root / expected_cat / expected_mat / color_dir / uid_dir),
                'rel_current':  f"{cat_dir}/{mat_dir}/{color_dir}/{uid_dir}",
                'rel_expected': f"{expected_cat}/{expected_mat}/{color_dir}/{uid_dir}",
                'src':      library_root / cat_dir / mat_dir / color_dir / uid_dir,
                'dst':      library_root / expected_cat / expected_mat / color_dir / uid_dir,
                'tag_cat':  tag.data['filament_type'],
                'tag_mat':  raw_mat,
                'warning':  warning,
            })

    return mismatches, parse_errors


def main():
    parser = argparse.ArgumentParser(
        description='Find (and optionally fix) misplaced RFID tag folders in the library.'
    )
    parser.add_argument(
        'library_root', nargs='?', default='.',
        help='Path to library root (default: current directory)'
    )
    parser.add_argument(
        '--fix', action='store_true',
        help='Move misplaced folders to their correct location'
    )
    parser.add_argument(
        '--quarantine', action='store_true',
        help='When used with --fix, move suspicious entries to _quarantine/ '
             'instead of their expected location'
    )
    args = parser.parse_args()

    library_root = Path(args.library_root).resolve()
    if not library_root.exists():
        print(f"Error: {library_root} does not exist")
        sys.exit(1)

    print(f"Scanning {library_root} ...")
    mismatches, parse_errors = scan_library(library_root)

    if parse_errors:
        print(f"\n{len(parse_errors)} file(s) failed to parse:")
        for path, err in parse_errors:
            print(f"  [!] {path}: {err}")

    if not mismatches:
        print("\nNo mismatches found - library is correctly organised.")
        return

    # Split into suspicious vs normal mismatches
    suspicious = [m for m in mismatches if m['warning']]
    normal     = [m for m in mismatches if not m['warning']]

    if normal:
        print(f"\n{len(normal)} misplaced folder(s):\n")
        for m in normal:
            print(f"  {m['rel_current']}")
            print(f"    -> {m['rel_expected']}  (tag: {m['tag_cat']} / {m['tag_mat']})")

    if suspicious:
        print(f"\n{len(suspicious)} suspicious entry(s) - tag data may be corrupt or incorrect:\n")
        for m in suspicious:
            print(f"  {m['rel_current']}")
            print(f"    -> {m['rel_expected']}  (tag: {m['tag_cat']} / {m['tag_mat']})")
            print(f"    [!] {m['warning']}")

    if not args.fix:
        hint = "--fix"
        if suspicious:
            hint += " --quarantine"
        print(f"\nRun with {hint} to move these folders.")
        return

    quarantine_root = library_root / "_quarantine"
    print()
    moved, skipped = 0, 0

    # Process normal mismatches first
    if normal:
        print(f"Moving {len(normal)} misplaced folder(s) ...")
    for m in normal:
        src, dst = m['src'], m['dst']
        if dst.exists():
            # Destination already exists — check if src is just a stale duplicate
            src_dumps = sorted(src.glob(f'*{DUMP_SUFFIX}'))
            dst_dumps = sorted(dst.glob(f'*{DUMP_SUFFIX}'))
            if src_dumps and dst_dumps and \
                    src_dumps[0].read_bytes() == dst_dumps[0].read_bytes():
                # Identical data: src is a leftover copy — remove it
                shutil.rmtree(str(src))
                print(f"  Removed stale duplicate: {m['rel_current']}")
                print(f"     (data already at {m['rel_expected']})")
                moved += 1
            else:
                print(f"  [!] Skipped {m['uid']}: destination exists with different data")
                print(f"      src: {m['rel_current']}")
                print(f"      dst: {m['rel_expected']}")
                skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"  Moved: {m['rel_current']}")
        print(f"      -> {m['rel_expected']}")
        moved += 1

    # Process suspicious mismatches
    if suspicious:
        if args.quarantine:
            print(f"\nQuarantining {len(suspicious)} suspicious folder(s) ...")
            for m in suspicious:
                src = m['src']
                dst = quarantine_root / m['rel_current']
                note_path = dst / "_quarantine.txt"
                if dst.exists():
                    print(f"  [!] Skipped {m['uid']}: quarantine destination already exists")
                    skipped += 1
                    continue
                dst.mkdir(parents=True, exist_ok=True)
                # Move contents of the UID folder into the quarantine destination
                for item in src.iterdir():
                    shutil.move(str(item), str(dst / item.name))
                src.rmdir()
                note_path.write_text(
                    f"Quarantined from: {m['rel_current']}\n"
                    f"Expected location: {m['rel_expected']}\n"
                    f"Tag data: {m['tag_cat']} / {m['tag_mat']}\n"
                    f"Warning: {m['warning']}\n",
                    encoding='utf-8'
                )
                print(f"  Quarantined: {m['rel_current']}")
                print(f"           -> _quarantine/{m['rel_current']}")
                moved += 1
        else:
            print(f"\n{len(suspicious)} suspicious folder(s) were NOT moved.")
            print("Review them manually, then re-run with --fix --quarantine to quarantine,")
            print("or --fix alone to move them to their tag-data location anyway.")

    print(f"\nDone: {moved} moved/quarantined, {skipped} skipped.")

    if moved:
        print("\nUpdating README.md ...")
        update_readme(library_root)


if __name__ == "__main__":
    main()
