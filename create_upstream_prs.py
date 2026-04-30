# -*- coding: utf-8 -*-
"""
create_upstream_prs.py -- Create structured pull requests for upstream contribution.

Manages a set of pre-defined PRs that contribute data renames and new/modified
scripts back to the upstream repository (queengooborg/Bambu-Lab-RFID-Library).
Each PR is built in a temporary git worktree rooted on upstream/main, so none
of your local changes bleed in beyond what each PR explicitly includes.

Requirements:
    - git remote 'upstream' pointing to queengooborg/Bambu-Lab-RFID-Library
    - GitHub CLI (gh) installed and authenticated (run: gh auth login)

Usage:
    python create_upstream_prs.py                     # list all PRs + status
    python create_upstream_prs.py --preview BRANCH    # show what a PR would do
    python create_upstream_prs.py --create BRANCH     # create/update one PR
    python create_upstream_prs.py --create all        # create/update all PRs
"""

import re
import sys
import json
import shutil
import subprocess
import argparse
import tempfile
from pathlib import Path
from urllib.parse import quote as url_quote

from sync_from_upstream import (
    LIBRARY_ROOT, UPSTREAM_REF,
    ensure_upstream_remote, fetch_upstream,
    _git,
)
from contribute_to_upstream import (
    ORIGIN_REMOTE, UPSTREAM_REPO,
    _gh, check_gh_available, get_origin_owner,
)

# ---------------------------------------------------------------------------
# PR manifest
# ---------------------------------------------------------------------------
# Each entry defines one PR branch.  Operations are applied in order inside
# a fresh worktree rooted on upstream/main.
#
# Operation types:
#   {'op': 'rename',       'from': 'A/B/C',  'to': 'A/B/D'}
#       git mv the directory (upstream must have it).
#   {'op': 'merge_folder', 'src': 'A/B/C',  'into': 'A/B/D'}
#       Copy UIDs from src into into (both already in upstream), then remove src.
#       Use when upstream has two folders that should be one.
#   {'op': 'copy_file',    'src': 'foo.py'}
#       Copy one file from LIBRARY_ROOT into the worktree root.
#   {'op': 'copy_dir',     'src': 'lib'}
#       Copy a directory from LIBRARY_ROOT into the worktree root.
#   {'op': 'update_readme'}
#       Run update_readme.run() against the worktree to refresh status icons.
# ---------------------------------------------------------------------------

PR_MANIFEST = [

    # ------------------------------------------------------------------
    # Data PRs
    # ------------------------------------------------------------------

    {
        'branch':  'data/pla-glow-renames',
        'title':   'PLA Glow: rename colours to official Bambu Studio names',
        'body': (
            "Renames the PLA Glow colour folders to match the official names used in "
            "Bambu Studio (e.g. **Blue** → **Glow Blue**, **Green** → **Glow Green**).\n\n"
            "The upstream folder `Orange` is merged into the existing `Glow Orange` folder "
            "(which was already correctly named) and the redundant `Orange` folder is removed.\n\n"
            "README status links are updated to match the new paths."
        ),
        'ops': [
            {'op': 'rename',       'from': 'PLA/PLA Glow/Blue',   'to': 'PLA/PLA Glow/Glow Blue'},
            {'op': 'rename',       'from': 'PLA/PLA Glow/Green',   'to': 'PLA/PLA Glow/Glow Green'},
            {'op': 'rename',       'from': 'PLA/PLA Glow/Pink',    'to': 'PLA/PLA Glow/Glow Pink'},
            {'op': 'rename',       'from': 'PLA/PLA Glow/Yellow',  'to': 'PLA/PLA Glow/Glow Yellow'},
            # 'Glow Orange' already exists upstream; move UIDs from 'Orange' into it
            {'op': 'merge_folder', 'src': 'PLA/PLA Glow/Orange',  'into': 'PLA/PLA Glow/Glow Orange'},
            {'op': 'update_readme'},
        ],
    },

    {
        'branch':  'data/pla-basic-blue-gray',
        'title':   'PLA Basic: rename Blue Grey to Blue Gray (official Bambu Studio name)',
        'body': (
            "Renames `PLA/PLA Basic/Blue Grey` to `PLA/PLA Basic/Blue Gray` to match "
            "the official colour name used in Bambu Studio.\n\n"
            "README link and status updated to match."
        ),
        'ops': [
            {'op': 'rename',       'from': 'PLA/PLA Basic/Blue Grey', 'to': 'PLA/PLA Basic/Blue Gray'},
            {'op': 'update_readme'},
        ],
    },

    {
        'branch':  'data/pla-basic-grey-merges',
        'title':   'PLA Basic: merge Grey/Dark Grey/Light Grey into Gray/Dark Gray/Light Gray',
        'body': (
            "Upstream currently has duplicate `Grey`/`Gray` folder pairs in PLA Basic. "
            "The README already links to the `Gray` spellings (matching Bambu Studio), "
            "but the old `Grey` folders still exist alongside them with a small number "
            "of UIDs not yet consolidated.\n\n"
            "This PR merges the orphaned UIDs into the canonical folders and removes "
            "the now-empty `Grey` variants:\n\n"
            "- `PLA Basic/Grey` (2 UIDs) → merged into `PLA Basic/Gray`\n"
            "- `PLA Basic/Dark Grey` (2 UIDs) → merged into `PLA Basic/Dark Gray`\n"
            "- `PLA Basic/Light Grey` (2 UIDs) → merged into `PLA Basic/Light Gray`\n\n"
            "Note: `Blue Grey` → `Blue Gray` is handled in a separate PR."
        ),
        'ops': [
            {'op': 'merge_folder', 'src': 'PLA/PLA Basic/Grey',       'into': 'PLA/PLA Basic/Gray'},
            {'op': 'merge_folder', 'src': 'PLA/PLA Basic/Dark Grey',   'into': 'PLA/PLA Basic/Dark Gray'},
            {'op': 'merge_folder', 'src': 'PLA/PLA Basic/Light Grey',  'into': 'PLA/PLA Basic/Light Gray'},
            {'op': 'update_readme'},
        ],
    },

    {
        'branch':  'data/pc-fr-grey-rename',
        'title':   'PC FR: rename Grey to Gray (official Bambu Studio name)',
        'body': (
            "Renames `PC/PC FR/Grey` to `PC/PC FR/Gray` to match the official colour "
            "name used in Bambu Studio.  The README already links to `Gray` (the correct "
            "spelling), so this rename also fixes the broken link in the status table.\n\n"
            "4 UIDs are moved from `Grey/` to `Gray/`."
        ),
        'ops': [
            {'op': 'rename', 'from': 'PC/PC FR/Grey', 'to': 'PC/PC FR/Gray'},
            {'op': 'update_readme'},
        ],
    },

    {
        'branch':  'data/tpu-for-ams-cleanup',
        'title':   'TPU for AMS: merge leftover For AMS Black/Neon Green folders',
        'body': (
            "Upstream currently has duplicate folder pairs in `TPU/TPU for AMS`:\n\n"
            "- `For AMS Black` (D57FFDA1, E6BAEEEB + a duplicate of ED77573E) "
            "alongside the canonical `Black` folder\n"
            "- `For AMS Neon Green` (AA6722FE, already duplicated in `Neon Green`)\n\n"
            "This PR merges any UIDs that are not already present in the canonical "
            "folder, removes the `For AMS` variants, and deduplcates the overlap "
            "(ED77573E and AA6722FE are already in the correct folders).\n\n"
            "The README already uses `Black` and `Neon Green` as the link targets, "
            "so no README change is required beyond a status refresh."
        ),
        'ops': [
            {'op': 'merge_folder', 'src': 'TPU/TPU for AMS/For AMS Black',
                                  'into': 'TPU/TPU for AMS/Black'},
            {'op': 'merge_folder', 'src': 'TPU/TPU for AMS/For AMS Neon Green',
                                  'into': 'TPU/TPU for AMS/Neon Green'},
            {'op': 'update_readme'},
        ],
    },

    {
        'branch':  'data/velvet-eclipse-rename',
        'title':   'PLA Silk Multi-Color: rename Velvet Eclipse to include colour description',
        'body': (
            "Renames `Velvet Eclipse` to `Velvet Eclipse (Black-Red)` to include the "
            "colour description in the folder name, matching the official Bambu Studio "
            "product name.\n\n"
            "README link and status updated to match."
        ),
        'ops': [
            {'op': 'rename',
             'from': 'PLA/PLA Silk Multi-Color/Velvet Eclipse',
             'to':   'PLA/PLA Silk Multi-Color/Velvet Eclipse (Black-Red)'},
            {'op': 'update_readme'},
        ],
    },

    # ------------------------------------------------------------------
    # Script PRs
    # ------------------------------------------------------------------

    {
        'branch':  'scripts/enhance-existing',
        'title':   'Update existing scripts with enhancements and bug fixes',
        'body': (
            "Updates the scripts already present in the repository with improvements "
            "developed in [NickWaterton/Bambu-Lab-RFID-Library]"
            "(https://github.com/NickWaterton/Bambu-Lab-RFID-Library):\n\n"
            "- `parse.py` — enhanced tag parsing\n"
            "- `convert.py` — format conversion improvements\n"
            "- `library_checker.py` — additional checks\n"
            "- `repair.py` — key repair improvements\n"
            "- `scrape_filaments.py` — scraper enhancements\n"
        ),
        'ops': [
            {'op': 'copy_file', 'src': 'parse.py'},
            {'op': 'copy_file', 'src': 'convert.py'},
            {'op': 'copy_file', 'src': 'library_checker.py'},
            {'op': 'copy_file', 'src': 'repair.py'},
            {'op': 'copy_file', 'src': 'scrape_filaments.py'},
        ],
    },

    {
        'branch':  'scripts/foundation',
        'title':   'Add foundation scripts: categories, key derivation, shared utilities',
        'body': (
            "Adds three new foundational modules used by other scripts:\n\n"
            "- `categories.py` — shared category/material lookup tables "
            "(maps `filament_type` values to top-level folder names, handles "
            "multi-colour material routing)\n"
            "- `deriveKeys.py` — derives Mifare sector keys from tag UID without "
            "requiring a sniffing session\n"
            "- `lib/` — shared utilities for locating the Proxmark3 installation "
            "and running Proxmark3 commands\n"
        ),
        'ops': [
            {'op': 'copy_file', 'src': 'categories.py'},
            {'op': 'copy_file', 'src': 'deriveKeys.py'},
            {'op': 'copy_dir',  'src': 'lib'},
        ],
    },

    {
        'branch':  'scripts/library-tools',
        'title':   'Add fix_library.py and update_readme.py',
        'body': (
            "Adds two new library maintenance scripts:\n\n"
            "- `fix_library.py` — scans all dump files and reports/fixes entries "
            "where the folder path does not match the tag data (wrong category, "
            "wrong material, wrong colour name). Also detects duplicate UIDs and "
            "validates colour names against the Bambu Studio database. Supports "
            "interactive review of colour renames and optional quarantine of "
            "suspicious entries.\n\n"
            "- `update_readme.py` — scans the library and updates the ✅/❌ status "
            "icons and variant ID columns in README.md to reflect what is actually "
            "on disk. Also warns when a ✅ row links to a colour folder that no "
            "longer exists (e.g. after a rename).\n"
        ),
        'ops': [
            {'op': 'copy_file', 'src': 'fix_library.py'},
            {'op': 'copy_file', 'src': 'update_readme.py'},
        ],
    },

    {
        'branch':  'scripts/color-database',
        'title':   'Add colordb.py — Bambu Studio colour database helpers',
        'body': (
            "Adds `colordb.py`, a shared module for looking up official Bambu Lab "
            "colour names from the Bambu Studio colour database.\n\n"
            "Features:\n"
            "- Fetches the live database from GitHub (always up to date)\n"
            "- Falls back to a local Bambu Studio installation if offline\n"
            "- Falls back to a bundled `filaments_color_codes.json` as a last resort "
            "(updated automatically on each successful GitHub fetch)\n"
            "- Exact hex-colour lookup filtered by material type and colour count\n"
            "- Nearest-colour matching by Euclidean RGBA distance when no exact match\n\n"
            "Also includes `filaments_color_codes.json` — a bundled snapshot of the "
            "Bambu Studio colour database, committed to the repository so the tools "
            "always have a working fallback even when offline.\n"
        ),
        'ops': [
            {'op': 'copy_file', 'src': 'colordb.py'},
            {'op': 'copy_file', 'src': 'filaments_color_codes.json'},
        ],
    },

    {
        'branch':  'scripts/scan-write',
        'title':   'Add scanTag.py and writeTag.py',
        'body': (
            "Adds two new Proxmark3-based scripts:\n\n"
            "- `scanTag.py` — reads a Bambu Lab RFID tag, derives sector keys from "
            "the UID, dumps all sectors, parses the dump, looks up the official colour "
            "name from the Bambu Studio database, and saves the data in the correct "
            "`Category/Material/Colour/UID/` folder with confirmation prompts.\n\n"
            "- `writeTag.py` — writes an existing library dump to a blank writable "
            "RFID tag (Gen 2 FUID / Gen 4 FUID / Gen 4 UFUID). Detects tag type "
            "automatically, shows the filament data that will be written, and "
            "permanently write-locks the tag after confirmation.\n"
        ),
        'ops': [
            {'op': 'copy_file', 'src': 'scanTag.py'},
            {'op': 'copy_file', 'src': 'writeTag.py'},
        ],
    },

    {
        'branch':  'scripts/workflow',
        'title':   'Add sync_from_upstream.py and contribute_to_upstream.py',
        'body': (
            "Adds two workflow scripts for managing a fork against this repository:\n\n"
            "- `sync_from_upstream.py` — fetches new tag UIDs from this repository and "
            "imports any UID directories absent from the local library. UIDs are matched "
            "by their 8-character hex name regardless of colour/material folder, so "
            "renamed tags are correctly recognised as already present. Also excludes "
            "quarantined UIDs from the import candidate list.\n\n"
            "- `contribute_to_upstream.py` — finds local UIDs not yet in this repository "
            "and creates or updates a single persistent pull-request branch "
            "(`contribute/pending`) rooted on `upstream/main`. Requires the "
            "[GitHub CLI](https://cli.github.com/) (`gh auth login`). Re-running after "
            "new scans keeps the PR current; a fresh PR is opened automatically once "
            "the previous one is merged or closed.\n"
        ),
        'ops': [
            {'op': 'copy_file', 'src': 'sync_from_upstream.py'},
            {'op': 'copy_file', 'src': 'contribute_to_upstream.py'},
        ],
    },

    {
        'branch':  'scripts/menu',
        'title':   'Add menu.py — interactive text-based menu for all tools',
        'body': (
            "Adds `menu.py`, an interactive text-based menu that brings together all "
            "the scripts in one place. Loads the Bambu Studio colour database once at "
            "startup and auto-detects the Proxmark3 on first use.\n\n"
            "**Menu options:**\n"
            "1. Read tag — display all parsed fields and library location\n"
            "2. Scan tag to database — full scan-and-add workflow\n"
            "3. Write tag from database — browse library and write to blank tag\n"
            "4. Fix database — find and fix misplaced/misnamed entries\n"
            "5. Sync from upstream — import new tags from this repository\n"
            "6. Contribute to upstream — create/update a PR with new local scans\n\n"
            "Requires `sync_from_upstream.py` and `contribute_to_upstream.py` "
            "(see the workflow scripts PR).\n\n"
            "```\npython menu.py\n```\n"
        ),
        'ops': [
            {'op': 'copy_file', 'src': 'menu.py'},
        ],
    },

    # ------------------------------------------------------------------
    # Documentation PR
    # ------------------------------------------------------------------

    {
        'branch':  'docs/readme-tools',
        'title':   'docs: add comprehensive Tools section to README',
        'body': (
            "Replaces the minimal 'Viewing Tag Data' section with a comprehensive "
            "**Tools** section documenting all scripts, and enhances the "
            "**Contributing** section with step-by-step instructions.\n\n"
            "Content adapted from "
            "[NickWaterton/Bambu-Lab-RFID-Library]"
            "(https://github.com/NickWaterton/Bambu-Lab-RFID-Library), with all "
            "self-referential links updated to point to this repository.\n\n"
            "**Scripts documented:**\n"
            "- `menu.py` — interactive menu (recommended entry point)\n"
            "- `scanTag.py` — scan a tag and add to the library\n"
            "- `writeTag.py` — write a library dump to a blank tag\n"
            "- `parse.py` — parse and display tag data\n"
            "- `fix_library.py` — find and fix library issues\n"
            "- `colordb.py` — Bambu Studio colour database helpers\n"
            "- `library_checker.py` — check for errors and colour mismatches\n"
            "- `sync_from_upstream.py` — import new tags from upstream\n"
            "- `contribute_to_upstream.py` — contribute new scans back to upstream\n"
            "- `update_readme.py` — sync README status from library data\n"
            "- `convert.py` — convert dumps to additional formats\n"
            "- `repair.py` — restore missing sector-trailer keys\n"
            "- `deriveKeys.py` — derive sector keys for a UID\n"
            "- `scrape_filaments.py` — discover new filaments from the Bambu store\n"
        ),
        'ops': [
            {'op': 'patch_readme_tools'},
        ],
    },

]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url_path(parts):
    return '/'.join(url_quote(p, safe='') for p in parts)


def _readme_rename(worktree_dir, old_rel, new_rel):
    """
    Update README.md links: replace old_rel path with new_rel path.
    Handles both URL-encoded and plain versions of the path.
    """
    readme = worktree_dir / 'README.md'
    if not readme.exists():
        return
    text = readme.read_text(encoding='utf-8')

    old_parts = Path(old_rel).parts
    new_parts = Path(new_rel).parts

    old_encoded = _url_path(old_parts)
    new_encoded = _url_path(new_parts)
    old_plain   = '/'.join(old_parts)
    new_plain   = '/'.join(new_parts)

    updated = (text
               .replace(f'./{old_encoded}', f'./{new_encoded}')
               .replace(f'./{old_plain}',   f'./{new_plain}'))

    if updated != text:
        readme.write_text(updated, encoding='utf-8')
        old_colour = old_parts[-1]
        new_colour = new_parts[-1]
        print(f"  README: '{old_colour}' -> '{new_colour}'")


def _patch_readme_tools(worktree_dir):
    """
    Replace the Tools + Contributing section in the upstream README with our
    enhanced version.  Links that reference NickWaterton/Bambu-Lab-RFID-Library
    are rewritten to queengooborg/Bambu-Lab-RFID-Library.  Links to the Tag
    Guide (NickWaterton/Bambu-Lab-RFID-Tag-Guide) are preserved unchanged.
    The material table and History section are taken entirely from upstream.
    """
    upstream_readme = worktree_dir / 'README.md'
    our_readme      = LIBRARY_ROOT / 'README.md'

    if not upstream_readme.exists():
        print("  WARNING: README.md not found in worktree — skipping.")
        return False
    if not our_readme.exists():
        print("  WARNING: local README.md not found — skipping.")
        return False

    upstream_text = upstream_readme.read_text(encoding='utf-8')
    our_text      = our_readme.read_text(encoding='utf-8')

    MATERIAL_MARKER = '## List of Bambu Lab Materials'

    # ---- Find where to start replacing in upstream ----------------------
    # Upstream currently has "## Viewing Tag Data"; might already have "## Tools"
    # if this PR is re-run.  Replace from whichever comes first.
    upstream_replace_start = None
    for candidate in ('## Viewing Tag Data', '## Tools', '## Contributing'):
        idx = upstream_text.find(f'\n{candidate}')
        if idx >= 0:
            upstream_replace_start = idx + 1  # skip the leading \n
            break

    if upstream_replace_start is None:
        print("  WARNING: could not find Tools/Contributing anchor in upstream README.")
        return False

    # ---- Find where the material table starts in upstream ---------------
    idx = upstream_text.find(f'\n{MATERIAL_MARKER}')
    if idx < 0:
        print(f"  WARNING: '{MATERIAL_MARKER}' not found in upstream README.")
        return False
    upstream_material_start = idx + 1  # skip leading \n

    # ---- Extract Tools + Contributing from our README -------------------
    our_tools_start    = our_text.find('\n## Tools')
    our_material_start = our_text.find(f'\n{MATERIAL_MARKER}')

    if our_tools_start < 0:
        print("  WARNING: '## Tools' not found in local README — skipping.")
        return False
    if our_material_start < 0:
        print(f"  WARNING: '{MATERIAL_MARKER}' not found in local README — skipping.")
        return False

    # Slice: from "## Tools\n" up to (but not including) "## List of..."
    our_section = our_text[our_tools_start + 1 : our_material_start + 1]

    # ---- Rewrite self-referential links ---------------------------------
    our_section = our_section.replace(
        'NickWaterton/Bambu-Lab-RFID-Library',
        'queengooborg/Bambu-Lab-RFID-Library',
    )
    # NickWaterton/Bambu-Lab-RFID-Tag-Guide links are intentionally kept.

    # ---- Splice and write -----------------------------------------------
    new_text = (
        upstream_text[:upstream_replace_start]
        + our_section
        + upstream_text[upstream_material_start:]
    )

    if new_text == upstream_text:
        print("  README: no changes required (already up to date).")
        return True

    upstream_readme.write_text(new_text, encoding='utf-8')

    # Count sections inserted for a helpful summary
    n_sections = sum(1 for h in ('## Tools', '## Contributing')
                     if h in our_section)
    print(f"  README: replaced upstream Tools/Contributing with our version "
          f"({n_sections} section(s) inserted).")
    print( "  README: rewrote NickWaterton/Bambu-Lab-RFID-Library "
           "→ queengooborg/Bambu-Lab-RFID-Library.")
    return True


def _apply_op(op, worktree_dir):
    """Apply one manifest operation inside the worktree. Returns True on success."""
    kind = op['op']

    if kind == 'rename':
        src = op['from']
        dst = op['to']
        src_path = worktree_dir / src
        if not src_path.exists():
            print(f"  WARNING: rename source not found in upstream: {src}")
            return False
        result = subprocess.run(
            ['git', 'mv', src, dst],
            cwd=str(worktree_dir),
        )
        if result.returncode != 0:
            print(f"  ERROR: git mv failed for {src} -> {dst}")
            return False
        print(f"  Renamed: {src} -> {dst}")
        _readme_rename(worktree_dir, src, dst)
        return True

    elif kind == 'merge_folder':
        src  = op['src']
        into = op['into']
        src_path  = worktree_dir / src
        into_path = worktree_dir / into
        if not src_path.exists():
            print(f"  WARNING: merge source not found in upstream: {src}")
            return False
        if not into_path.exists():
            print(f"  WARNING: merge target not found in upstream: {into}")
            return False
        # Move each UID subdirectory from src into into
        moved = 0
        for uid_dir in sorted(src_path.iterdir()):
            if not uid_dir.is_dir():
                continue
            dest = into_path / uid_dir.name
            if dest.exists():
                print(f"  WARNING: {uid_dir.name} already in {into} — skipping")
                continue
            result = subprocess.run(
                ['git', 'mv', str(uid_dir.relative_to(worktree_dir)),
                              str((into_path / uid_dir.name).relative_to(worktree_dir))],
                cwd=str(worktree_dir),
            )
            if result.returncode == 0:
                moved += 1
        # Remove now-empty src directory
        subprocess.run(['git', 'rm', '-r', src], cwd=str(worktree_dir))
        print(f"  Merged {moved} UID(s) from '{src}' into '{into}', removed '{src}'")
        return True

    elif kind == 'copy_file':
        src_path = LIBRARY_ROOT / op['src']
        dst_path = worktree_dir / op['src']
        if not src_path.exists():
            print(f"  WARNING: source file not found: {op['src']}")
            return False
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_path), str(dst_path))
        print(f"  Copied: {op['src']}")
        return True

    elif kind == 'copy_dir':
        src_path = LIBRARY_ROOT / op['src']
        dst_path = worktree_dir / op['src']
        if not src_path.exists():
            print(f"  WARNING: source directory not found: {op['src']}")
            return False
        if dst_path.exists():
            shutil.rmtree(str(dst_path))
        shutil.copytree(str(src_path), str(dst_path))
        n = sum(1 for _ in dst_path.rglob('*') if _.is_file())
        print(f"  Copied dir: {op['src']}/ ({n} file(s))")
        return True

    elif kind == 'update_readme':
        import update_readme as _ur
        n = _ur.run(worktree_dir, dry_run=False)
        if n:
            print(f"  README: updated {n} status row(s)")
        return True

    elif kind == 'patch_readme_tools':
        return _patch_readme_tools(worktree_dir)

    else:
        print(f"  ERROR: unknown op type '{kind}'")
        return False

# ---------------------------------------------------------------------------
# Branch / PR management
# ---------------------------------------------------------------------------

def _get_open_pr_url(owner, branch):
    """Return the URL of an open PR from owner:branch, or None."""
    try:
        result = subprocess.run(
            ['gh', 'pr', 'list',
             '--repo', UPSTREAM_REPO,
             '--state', 'open',
             '--limit', '100',
             '--json', 'url,headRefName,headRepositoryOwner'],
            capture_output=True,
            cwd=str(LIBRARY_ROOT),
        )
        if result.returncode != 0:
            return None
        prs = json.loads(result.stdout.decode('utf-8', errors='replace'))
        for pr in prs:
            if (pr.get('headRefName') == branch and
                    pr.get('headRepositoryOwner', {}).get('login', '').lower()
                    == owner.lower()):
                return pr['url']
        return None
    except Exception:
        return None


def build_branch(pr_def, dry_run=False):
    """
    Build a worktree for pr_def, apply all ops, commit.
    If dry_run, just preview ops without making changes.
    Returns worktree Path (or None on dry run / error).
    """
    branch = pr_def['branch']
    print(f"\n{'[PREVIEW]' if dry_run else '[BUILD]'} {branch}")
    print(f"  {pr_def['title']}")
    print()

    if dry_run:
        for op in pr_def['ops']:
            kind = op['op']
            if kind == 'rename':
                print(f"  rename:       {op['from']} -> {op['to']}")
            elif kind == 'merge_folder':
                print(f"  merge_folder: {op['src']} -> {op['into']}")
            elif kind == 'copy_file':
                print(f"  copy_file:    {op['src']}")
            elif kind == 'copy_dir':
                print(f"  copy_dir:     {op['src']}/")
            elif kind == 'update_readme':
                print(f"  update_readme")
            elif kind == 'patch_readme_tools':
                print(f"  patch_readme_tools: splice Tools+Contributing from local README into upstream")
        return None

    # Delete stale local branch if present
    if _git('branch', '--list', branch).strip():
        _git('branch', '-D', branch)
    _git('branch', branch, UPSTREAM_REF)

    worktree_dir = Path(tempfile.mkdtemp(prefix='bambu-pr-'))
    try:
        _git('worktree', 'add', str(worktree_dir), branch)

        for op in pr_def['ops']:
            _apply_op(op, worktree_dir)

        subprocess.run(['git', 'add', '-A'], cwd=str(worktree_dir), check=True)

        # Check if there's actually anything to commit
        status = subprocess.run(
            ['git', 'diff', '--cached', '--name-only'],
            capture_output=True, cwd=str(worktree_dir),
        ).stdout.decode().strip()

        if not status:
            print("  Nothing changed — branch not committed.")
            _git('worktree', 'remove', '--force', str(worktree_dir))
            shutil.rmtree(str(worktree_dir), ignore_errors=True)
            _git('branch', '-D', branch)
            return None

        subprocess.run(
            ['git', 'commit', '-m', pr_def['title']],
            cwd=str(worktree_dir), check=True,
        )

    except Exception:
        try:
            _git('worktree', 'remove', '--force', str(worktree_dir))
        except Exception:
            pass
        shutil.rmtree(str(worktree_dir), ignore_errors=True)
        try:
            _git('branch', '-D', branch)
        except Exception:
            pass
        raise

    return worktree_dir


def push_and_open_pr(pr_def, worktree_dir, owner):
    """Push branch and create/update PR. Always cleans up the worktree."""
    branch = pr_def['branch']
    try:
        print(f"  Pushing '{branch}' to {ORIGIN_REMOTE} ...")
        subprocess.run(
            ['git', 'push', '--force-with-lease', ORIGIN_REMOTE, branch],
            cwd=str(LIBRARY_ROOT), check=True,
        )

        existing_url = _get_open_pr_url(owner, branch)
        if existing_url:
            _gh('pr', 'edit', existing_url,
                '--repo',  UPSTREAM_REPO,
                '--title', pr_def['title'],
                '--body',  pr_def['body'],
                capture=False)
            print(f"  PR updated: {existing_url}")
        else:
            url = _gh(
                'pr', 'create',
                '--repo',  UPSTREAM_REPO,
                '--head',  f'{owner}:{branch}',
                '--title', pr_def['title'],
                '--body',  pr_def['body'],
            ).strip()
            print(f"  PR created: {url}")

    finally:
        try:
            _git('worktree', 'remove', '--force', str(worktree_dir))
        except Exception:
            pass
        shutil.rmtree(str(worktree_dir), ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _find_pr(branch_or_all):
    if branch_or_all == 'all':
        return PR_MANIFEST
    matches = [p for p in PR_MANIFEST if p['branch'] == branch_or_all]
    if not matches:
        print(f"ERROR: no PR defined with branch '{branch_or_all}'")
        print("Available branches:")
        for p in PR_MANIFEST:
            print(f"  {p['branch']}")
        sys.exit(1)
    return matches


def main():
    parser = argparse.ArgumentParser(
        description='Create structured pull requests for upstream contribution.'
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--preview', metavar='BRANCH',
                       help='Preview what a PR would do (use "all" for all PRs).')
    group.add_argument('--create',  metavar='BRANCH',
                       help='Create/update a PR branch (use "all" for all PRs).')
    parser.add_argument('--no-fetch', action='store_true',
                        help='Skip the git fetch step.')
    args = parser.parse_args()

    ensure_upstream_remote()
    if not args.no_fetch:
        fetch_upstream()

    # Default: list all PRs with open/closed status
    if not args.preview and not args.create:
        if check_gh_available():
            owner = get_origin_owner()
        else:
            owner = None

        print(f"\n{len(PR_MANIFEST)} PR(s) defined:\n")
        for pr_def in PR_MANIFEST:
            branch = pr_def['branch']
            status = ''
            if owner:
                url = _get_open_pr_url(owner, branch)
                status = f'  [OPEN: {url}]' if url else '  [no open PR]'
            print(f"  {branch}{status}")
            print(f"    {pr_def['title']}")
        print()
        print("Use --preview BRANCH to see what a PR would do.")
        print("Use --create BRANCH  (or 'all') to create/update PRs.")
        return

    if args.preview:
        prs = _find_pr(args.preview)
        for pr_def in prs:
            build_branch(pr_def, dry_run=True)
        return

    # --create
    if not check_gh_available():
        print("ERROR: GitHub CLI (gh) is not installed or not authenticated.")
        print("  Install: https://cli.github.com/  then: gh auth login")
        sys.exit(1)

    owner = get_origin_owner()
    if not owner:
        print("ERROR: could not determine GitHub username from origin remote URL.")
        sys.exit(1)

    prs = _find_pr(args.create)
    for pr_def in prs:
        worktree_dir = build_branch(pr_def, dry_run=False)
        if worktree_dir:
            push_and_open_pr(pr_def, worktree_dir, owner)
        print()

    print("Done.")


if __name__ == '__main__':
    main()
