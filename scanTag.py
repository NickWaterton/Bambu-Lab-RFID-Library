# -*- coding: utf-8 -*-

# Read a Bambu Lab RFID tag using Proxmark3 and add the data to the library.
# Created for https://github.com/Bambu-Research-Group/RFID-Tag-Guide
#
# Usage:
#   python scanTag.py
#
# Run from the Bambu-Lab-RFID-Library directory (or anywhere — the library
# root is always resolved relative to this script's own location).

import os
import re
import sys
import json
import time
import shutil
import argparse
import itertools
import tempfile
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

from lib import get_proxmark3_location, run_command
from deriveKeys import kdf
from categories import CATEGORY_MAP, MULTI_COLOR_MATERIAL_MAP, resolve_material
from parse import Tag
from convert import sync_directory
from update_readme import run as update_readme

# The library root is the directory containing this script.
LIBRARY_ROOT = Path(__file__).parent.resolve()

pm3Location = None
pm3Command   = "bin/pm3"

# ---------------------------------------------------------------------------
# Proxmark3 helpers
# ---------------------------------------------------------------------------

def setup():
    global pm3Location
    pm3Location = get_proxmark3_location()
    if not pm3Location:
        sys.exit(1)


def read_uid():
    """Return the UID string (e.g. 'E4E447D1') from the tag on the reader."""
    output = run_command([pm3Location / pm3Command, "-d", "1", "-c", "hf mf info"])
    if not output:
        return None
    m = re.search(r'\[\+\]\s+UID:\s+((?:[0-9A-Fa-f]{2}\s*)+)', output)
    if not m:
        return None
    return m.group(1).replace(' ', '').strip().upper()


def _poll_uid_silent():
    """
    Try to read a UID once without printing anything.
    Uses subprocess directly so the spinner line isn't overwritten by run_command output.
    Returns UID string or None.
    """
    try:
        result = subprocess.run(
            [str(pm3Location / pm3Command), "-c", "hf mf info"],
            shell=(os.name == 'nt'),
            capture_output=True,
            timeout=12,
        )
        if result.returncode in (0, 1):
            output = result.stdout.decode('utf-8', errors='replace')
            m = re.search(r'\[\+\]\s+UID:\s+((?:[0-9A-Fa-f]{2}\s*)+)', output)
            if m:
                return m.group(1).replace(' ', '').strip().upper()
    except Exception:
        pass
    return None


def wait_for_tag():
    """
    Poll continuously until a tag is placed on the reader.
    Shows a spinner so the user knows the search is active.
    Returns the UID string.
    """
    spinner = itertools.cycle('|/-\\')
    print("Move the spool slowly over the Proxmark3 until the tag is detected.")
    print("(Ctrl+C to cancel)\n")
    try:
        while True:
            print(f"\r  Searching... {next(spinner)}", end='', flush=True)
            uid = _poll_uid_silent()
            if uid:
                print(f"\r  Tag detected! UID: {uid}          ")
                return uid
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n\nCancelled.")
        sys.exit(0)


def write_key_file(uid_hex, dest_path):
    """Derive Bambu keys from the UID and write a binary key file."""
    uid_bytes = bytes.fromhex(uid_hex)
    keys_a, keys_b = kdf(uid_bytes)
    with open(dest_path, 'wb') as f:
        for k in keys_a:
            f.write(k)
        for k in keys_b:
            f.write(k)


def dump_tag(uid, key_path, output_base):
    """
    Run hf mf dump and return the path to the resulting .bin file, or None.
    proxmark3 creates <output_base>-dump.bin.

    Note: pm3.bat cd's to <pm3Location>/client/ before running proxmark3.
    The pm3 binary prepends CWD to the -f output path, so absolute temp-dir
    paths end up mangled (e.g. D:\\Proxmark3\\client\\/C:/Users/...).
    We work around this by passing only the bare filename so proxmark3 writes
    into the client/ directory, then moving the file to output_base afterwards.
    """
    kp = str(key_path).replace('\\', '/')
    rel_name = output_base.name          # e.g. "hf-mf-A7E95F2A"
    client_dir = pm3Location / "client"  # where pm3.bat cds to

    output = run_command([pm3Location / pm3Command, "-c",
                          f"hf mf dump --1k --keys {kp} -f {rel_name}"])
    if output:
        print(output)

    # proxmark3 appends -dump.bin to the base name; fall back to plain .bin
    for suffix in ("-dump.bin", ".bin"):
        src = client_dir / f"{rel_name}{suffix}"
        if src.exists():
            dest = Path(str(output_base) + suffix)
            shutil.move(str(src), dest)
            return dest

    return None

# ---------------------------------------------------------------------------
# Library helpers
# ---------------------------------------------------------------------------

def find_existing_entries(uid, library_root):
    """
    Search the library for any folder whose name matches uid.
    The UID is always the 4th-level folder (category/material/colour/uid),
    so this finds the tag regardless of which category or colour it was filed under.
    Returns a list of matching Path objects (should normally be 0 or 1).
    """
    results = []
    for p in library_root.rglob(uid):
        if not p.is_dir():
            continue
        parts = p.relative_to(library_root).parts
        # Must be exactly at depth 4 and not inside an internal folder (_quarantine etc.)
        if len(parts) == 4 and not parts[0].startswith('_'):
            results.append(p)
    return results


def dest_dir(tag_data, color_name, library_root):
    category = CATEGORY_MAP.get(tag_data['filament_type'], tag_data['filament_type'])
    material = resolve_material(tag_data)
    uid      = tag_data['uid']
    return library_root / category / material / color_name / uid

# ---------------------------------------------------------------------------
# Bambu Studio colour database
# ---------------------------------------------------------------------------

# Authoritative source — maintained by Bambu Lab alongside BambuStudio releases.
COLOR_DB_URL = (
    "https://raw.githubusercontent.com/bambulab/BambuStudio/master"
    "/resources/profiles/BBL/filament/filaments_color_codes.json"
)

# Local fallback — present when Bambu Studio is installed on this machine.
COLOR_DB_LOCAL_PATHS = [
    Path(r"C:\Program Files\Bambu Studio\resources\profiles\BBL\filament\filaments_color_codes.json"),
    Path(r"C:\Program Files (x86)\Bambu Studio\resources\profiles\BBL\filament\filaments_color_codes.json"),
]

# Network timeout in seconds for the GitHub fetch.
COLOR_DB_TIMEOUT = 5


def _parse_color_db(raw):
    """Extract the list of entries from the parsed JSON (handles {"data":[...]} wrapper)."""
    if isinstance(raw, dict):
        return raw.get('data', [])
    return raw  # already a plain list


def load_color_database():
    """
    Load the Bambu Studio filament colour database.

    Tries GitHub first (always up to date); falls back to the locally
    installed copy if the network is unavailable.  Returns a list of
    colour entries, or an empty list if neither source is reachable.
    """
    # --- 1. Try GitHub ---
    try:
        req = urllib.request.Request(
            COLOR_DB_URL,
            headers={'User-Agent': 'scanTag/1.0 (Bambu-Research-Group)'},
        )
        with urllib.request.urlopen(req, timeout=COLOR_DB_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
        entries = _parse_color_db(raw)
        if entries:
            print(f"Loaded colour database from GitHub ({len(entries)} entries).")
            return entries
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyboardInterrupt):
        pass  # fall through to local copy

    # --- 2. Fall back to local Bambu Studio installation ---
    for path in COLOR_DB_LOCAL_PATHS:
        if path.exists():
            try:
                with open(path, encoding='utf-8') as f:
                    raw = json.load(f)
                entries = _parse_color_db(raw)
                if entries:
                    print(f"(GitHub unreachable — using local colour database, {len(entries)} entries.)")
                    return entries
            except Exception as e:
                print(f"Warning: could not read local colour database at {path}: {e}")

    print("(Colour database not available — colour name must be entered manually.)")
    return []


def lookup_color_name(tag_data, db):
    """
    Search the Bambu Studio colour database for entries matching this tag's
    hex colour.

    Filters by colour type (单色 / 渐变色 / 多拼色) using filament_color_count so
    that single-colour tags never match gradient entries and vice versa.

    Returns (exact_name, candidates) where:
      exact_name  -- English name when material type AND hex both matched, else None
      candidates  -- list of (name, fila_type) for entries where only hex matched
    """
    if not db:
        return None, []

    target_color = tag_data['filament_color'].upper()
    target_type  = tag_data['detailed_filament_type']
    is_multi     = tag_data.get('filament_color_count', 1) > 1

    # fila_color_type values: 单色 = single, 渐变色 = gradient, 多拼色 = multi-splice
    SINGLE_TYPE = '单色'
    MULTI_TYPES = {'渐变色', '多拼色'}

    exact_name = None
    candidates = []

    for entry in db:
        # Skip entries whose colour-count class doesn't match the tag
        entry_color_type = entry.get('fila_color_type', '')
        if is_multi and entry_color_type not in MULTI_TYPES:
            continue
        if not is_multi and entry_color_type != SINGLE_TYPE:
            continue

        colors = [c.upper() for c in entry.get('fila_color', [])]
        if target_color not in colors:
            continue

        name = entry.get('fila_color_name', {}).get('en', '').strip()
        if not name:
            continue

        if entry.get('fila_type') == target_type:
            if exact_name is None:      # take the first exact match
                exact_name = name
        else:
            candidates.append((name, entry.get('fila_type', '?')))

    return exact_name, candidates


def prompt_color_name(tag_data, db):
    """
    Interactively ask the user for the colour name, pre-filling from the
    Bambu Studio database where possible.

    Returns the chosen colour name string, or exits if the user cancels.
    """
    exact_name, candidates = lookup_color_name(tag_data, db)

    print()
    print(f"Hex colour: {tag_data['filament_color']}")

    suggested = None

    if exact_name:
        # Perfect match — type AND colour both match
        print(f"Official Bambu Studio name: {exact_name}")
        suggested = exact_name
    elif candidates:
        if len(candidates) == 1:
            name, ftype = candidates[0]
            print(f"Possible name from Bambu Studio: {name!r}")
            print(f"  (matched by colour only — database type is '{ftype}', tag type is"
                  f" '{tag_data['detailed_filament_type']}')")
            suggested = name
        else:
            print("No exact type+colour match; possible names from Bambu Studio:")
            for name, ftype in candidates:
                print(f"  {name!r}  (database type: '{ftype}')")
    elif db:
        print("(Colour not found in Bambu Studio database — enter manually.)")
    else:
        print("(Bambu Studio not found — enter colour name manually.)")

    print("Enter the colour name as it appears in the Bambu Lab store,")
    print("or press Enter to accept the suggestion above." if suggested else
          "or press Enter to cancel.")

    raw = input(f"Colour name [{suggested}]: " if suggested else "Colour name: ").strip()

    if not raw:
        if suggested:
            print(f"Using: {suggested}")
            return suggested
        else:
            print("Cancelled.")
            sys.exit(0)

    if suggested and raw != suggested:
        print(f"  *** WARNING: the official Bambu Studio name is '{suggested}' ***")
        print(f"      You entered '{raw}'.")
        confirm = input("  Use your name instead? (y/N) ").strip()
        if confirm.lower() not in ('y', 'yes'):
            print(f"  Reverted to: {suggested}")
            return suggested

    return raw

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Read a Bambu Lab RFID tag with Proxmark3 and add it to the library.'
    )
    args = parser.parse_args()

    setup()

    color_db = load_color_database()

    print("--------------------------------------------------------")
    print("RFID Tag Scanner - Bambu Research Group")
    print("--------------------------------------------------------")

    # --- Step 1: locate tag ---
    uid = wait_for_tag()

    # --- Step 2: check if this UID is already in the library ---
    existing = find_existing_entries(uid, LIBRARY_ROOT)
    if existing:
        print(f"\nThis tag is already in the library:")
        for entry in existing:
            print(f"  {entry.relative_to(LIBRARY_ROOT)}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir     = Path(tmpdir)
        base_name  = f"hf-mf-{uid}"
        key_path   = tmpdir / f"{base_name}-key.bin"
        dump_base  = tmpdir / base_name

        # --- Step 3: derive keys and write key file ---
        print("\nDeriving Bambu keys from UID...")
        try:
            write_key_file(uid, key_path)
        except Exception as e:
            print(f"Error deriving keys: {e}")
            sys.exit(1)

        # --- Step 4: dump all sectors ---
        print("Dumping tag sectors (this may take a moment)...")
        dump_file = dump_tag(uid, key_path, dump_base)
        if not dump_file:
            print("Error: dump file was not created.")
            print("The tag may not be a Bambu Lab tag (keys are UID-derived),")
            print("or the tag may be out of range.")
            sys.exit(1)

        # --- Step 5: parse the dump and display tag details ---
        try:
            with open(dump_file, 'rb') as f:
                tag = Tag(dump_file.name, f.read(), fail_on_warn=False)
        except Exception as e:
            print(f"Error parsing dump: {e}")
            sys.exit(1)

        print()
        print("Tag data:")
        resolved = resolve_material(tag.data)
        raw      = tag.data['detailed_filament_type']
        material_display = (
            f"{resolved} (tag: {raw})" if resolved != raw else resolved
        )
        print(f"  Material:   {material_display} ({tag.data['filament_type']})")
        print(f"  Colors:     {tag.data['filament_color']} ({tag.data['filament_color_count']} color(s))")
        print(f"  Variant ID: {tag.data['variant_id']}")
        print(f"  UID:        {tag.data['uid']}")
        if tag.warnings:
            print("  Warnings:")
            for w in tag.warnings:
                print(f"    - {w}")

        # --- Step 6: if already present, ask whether to proceed ---
        if existing:
            print()
            confirm = input("Add to library anyway? (y/N) ")
            if confirm.lower() not in ('y', 'yes'):
                print("Skipped.")
                return

        # --- Step 7: ask for the colour name (with database lookup) ---
        color_name = prompt_color_name(tag.data, color_db)

        # --- Step 8: confirm destination ---
        dst = dest_dir(tag.data, color_name, LIBRARY_ROOT)
        print()
        print(f"Will write to: {dst.relative_to(LIBRARY_ROOT)}")

        if dst.exists() and any(dst.iterdir()):
            print("Warning: destination already exists and contains files.")
            confirm = input("Continue and overwrite? (y/N) ")
            if confirm.lower() not in ('y', 'yes'):
                print("Cancelled.")
                sys.exit(0)

        dst.mkdir(parents=True, exist_ok=True)

        # --- Step 9: copy dump and key files ---
        shutil.copy2(dump_file, dst / f"{base_name}-dump.bin")
        shutil.copy2(key_path,  dst / f"{base_name}-key.bin")
        print("Copied dump and key files.")

        # --- Step 10: generate JSON / NFC / additional formats ---
        print("Generating additional formats (JSON, NFC)...")
        try:
            sync_directory(dst)
        except Exception as e:
            print(f"Warning: could not generate additional formats: {e}")

        print()
        print(f"Done! Tag added at:")
        print(f"  {dst.relative_to(LIBRARY_ROOT)}")
        print()

        # --- Step 11: optionally update README ---
        confirm = input("Update README.md to reflect the new entry? (y/N) ")
        if confirm.lower() in ('y', 'yes'):
            update_readme(LIBRARY_ROOT)


if __name__ == "__main__":
    main()
