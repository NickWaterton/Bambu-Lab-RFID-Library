# -*- coding: utf-8 -*-
"""
contribute_to_upstream.py -- Contribute new local tag UIDs to the upstream repository.

Compares your local library against the upstream repo and creates (or updates)
a single persistent pull-request branch containing every UID that is present
locally but absent upstream.  The branch is rooted on upstream/main, so none
of your local naming convention changes are included.

Each run rebuilds the branch from scratch and force-pushes it, so the open PR
stays up to date as you scan more tags.  When the upstream author merges or
closes the PR, the next run detects no open PR and creates a fresh one.

Requirements:
    - git remote 'upstream' pointing to queengooborg/Bambu-Lab-RFID-Library
    - GitHub CLI (gh) installed and authenticated (run: gh auth login)

Usage:
    python contribute_to_upstream.py              # fetch + preview
    python contribute_to_upstream.py --apply      # fetch + create/update PR
    python contribute_to_upstream.py --no-fetch   # preview using already-fetched data
    python contribute_to_upstream.py --no-fetch --apply
"""

import re
import sys
import shutil
import subprocess
import argparse
import tempfile
from pathlib import Path

from sync_from_upstream import (
    LIBRARY_ROOT, UPSTREAM_REMOTE, UPSTREAM_URL, UPSTREAM_REF,
    LIBRARY_CATEGORIES,
    ensure_upstream_remote, fetch_upstream,
    get_upstream_uid_map, _is_uid, _group_by_material,
    _git,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ORIGIN_REMOTE       = 'origin'
UPSTREAM_REPO       = 'queengooborg/Bambu-Lab-RFID-Library'
CONTRIBUTION_BRANCH = 'contribute/pending'   # single persistent PR branch

# ---------------------------------------------------------------------------
# Local library scan
# ---------------------------------------------------------------------------

def get_local_uid_map():
    """
    Return {uid_uppercase: uid_dir_Path} for every non-quarantined UID in the
    local library (directories at depth 4: Category/Material/Colour/UID).
    """
    uid_map = {}
    for p in LIBRARY_ROOT.rglob('*'):
        if not p.is_dir():
            continue
        parts = p.relative_to(LIBRARY_ROOT).parts
        if (len(parts) == 4
                and not parts[0].startswith('_')
                and parts[0] in LIBRARY_CATEGORIES
                and _is_uid(parts[3])):
            uid_map[parts[3].upper()] = p
    return uid_map

# ---------------------------------------------------------------------------
# GitHub CLI helpers
# ---------------------------------------------------------------------------

def _gh(*args, capture=True, check=True):
    """Run a gh command in the library root; return stdout text or None."""
    result = subprocess.run(
        ['gh'] + list(args),
        capture_output=capture,
        cwd=str(LIBRARY_ROOT),
    )
    if check and result.returncode != 0:
        err = result.stderr.decode('utf-8', errors='replace').strip() if result.stderr else ''
        print(f"gh error: {err}", file=sys.stderr)
        sys.exit(1)
    if capture:
        return result.stdout.decode('utf-8', errors='replace')
    return None


def check_gh_available():
    """Return True if gh CLI is installed and the user is authenticated."""
    result = subprocess.run(['gh', 'auth', 'status'], capture_output=True)
    return result.returncode == 0


def get_origin_owner():
    """Return the GitHub username/org for the origin remote (e.g. 'NickWaterton')."""
    url = _git('remote', 'get-url', ORIGIN_REMOTE).strip()
    m = re.search(r'[:/]([^/]+)/[^/]+(?:\.git)?$', url)
    return m.group(1) if m else None


def get_open_pr_url(owner):
    """
    Return the URL of the currently open PR from owner:CONTRIBUTION_BRANCH,
    or None if no such PR exists.
    """
    result = subprocess.run(
        ['gh', 'pr', 'list',
         '--repo', UPSTREAM_REPO,
         '--head', f'{owner}:{CONTRIBUTION_BRANCH}',
         '--json', 'url',
         '--jq', '.[0].url'],
        capture_output=True,
        cwd=str(LIBRARY_ROOT),
    )
    if result.returncode != 0:
        return None
    url = result.stdout.decode('utf-8', errors='replace').strip()
    return url if url and url != 'null' else None

# ---------------------------------------------------------------------------
# Core: build contribution branch in a temporary worktree
# ---------------------------------------------------------------------------

def build_contribution_branch(uid_dirs):
    """
    (Re)create CONTRIBUTION_BRANCH from UPSTREAM_REF in a temporary worktree,
    copy all uid_dirs into it, and commit.

    Any existing local branch with that name is deleted first so the branch is
    always a clean rebuild from upstream/main.

    Returns the Path of the worktree (caller must clean it up).
    Raises on any git/IO error (cleans up before raising).
    """
    # Delete stale local branch if present
    existing = _git('branch', '--list', CONTRIBUTION_BRANCH).strip()
    if existing:
        _git('branch', '-D', CONTRIBUTION_BRANCH)

    _git('branch', CONTRIBUTION_BRANCH, UPSTREAM_REF)

    worktree_dir = Path(tempfile.mkdtemp(prefix='bambu-contribute-'))
    try:
        _git('worktree', 'add', str(worktree_dir), CONTRIBUTION_BRANCH)

        # Copy each UID directory
        for uid, local_uid_dir in sorted(uid_dirs.items(),
                                          key=lambda kv: str(kv[1])):
            rel = local_uid_dir.relative_to(LIBRARY_ROOT)
            target_dir = worktree_dir / rel
            target_dir.mkdir(parents=True, exist_ok=True)
            n_files = 0
            for f in sorted(local_uid_dir.iterdir()):
                if f.is_file():
                    shutil.copy2(str(f), str(target_dir / f.name))
                    n_files += 1
            print(f"  {rel.as_posix()}/  ({n_files} file(s))")

        # Stage all additions
        subprocess.run(['git', 'add', '-A'], cwd=str(worktree_dir), check=True)

        # Build commit message
        n_uids = len(uid_dirs)
        uid_lines = ''.join(f'  - {uid}\n' for uid in sorted(uid_dirs))
        commit_msg = (
            f"Add {n_uids} new tag scan(s)\n\n"
            f"UIDs:\n{uid_lines}"
        )
        subprocess.run(
            ['git', 'commit', '-m', commit_msg],
            cwd=str(worktree_dir),
            check=True,
        )

    except Exception:
        # Best-effort cleanup before re-raising
        try:
            _git('worktree', 'remove', '--force', str(worktree_dir))
        except Exception:
            pass
        shutil.rmtree(str(worktree_dir), ignore_errors=True)
        try:
            _git('branch', '-D', CONTRIBUTION_BRANCH)
        except Exception:
            pass
        raise

    return worktree_dir


def push_and_sync_pr(worktree_dir, uid_dirs, owner):
    """
    Force-push CONTRIBUTION_BRANCH to origin, then either open a new PR or
    update the title/body of the existing one.
    Always removes the worktree when done (success or failure).
    """
    try:
        print(f"Pushing branch '{CONTRIBUTION_BRANCH}' to {ORIGIN_REMOTE} ...")
        # --force-with-lease is safe: refuses if someone else pushed since we last fetched
        subprocess.run(
            ['git', 'push', '--force-with-lease', ORIGIN_REMOTE, CONTRIBUTION_BRANCH],
            cwd=str(LIBRARY_ROOT),
            check=True,
        )

        n_uids = len(uid_dirs)
        pr_title = f"Add {n_uids} new tag scan(s)"
        uid_list_md = ''.join(f'- `{uid}`\n' for uid in sorted(uid_dirs))
        pr_body = (
            f"## New tag scans\n\n"
            f"This PR contributes {n_uids} new UID(s) scanned from genuine Bambu Lab "
            f"filament spools.\n\n"
            f"### UIDs included\n\n"
            f"{uid_list_md}\n"
            f"_Contributed from [{owner}/Bambu-Lab-RFID-Library]"
            f"(https://github.com/{owner}/Bambu-Lab-RFID-Library)_\n"
        )

        existing_url = get_open_pr_url(owner)
        if existing_url:
            # Update the existing PR's title and body to reflect the new count/list
            print(f"Updating existing PR ...")
            _gh(
                'pr', 'edit', existing_url,
                '--repo',  UPSTREAM_REPO,
                '--title', pr_title,
                '--body',  pr_body,
                capture=False,
            )
            print(f"PR updated: {existing_url}")
        else:
            print(f"Opening PR against {UPSTREAM_REPO} ...")
            url = _gh(
                'pr', 'create',
                '--repo',  UPSTREAM_REPO,
                '--head',  f'{owner}:{CONTRIBUTION_BRANCH}',
                '--title', pr_title,
                '--body',  pr_body,
            ).strip()
            print(f"PR created: {url}")

    finally:
        try:
            _git('worktree', 'remove', '--force', str(worktree_dir))
        except Exception:
            pass
        shutil.rmtree(str(worktree_dir), ignore_errors=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Contribute new local tag UIDs to the upstream repository via a PR.'
    )
    parser.add_argument(
        '--apply', action='store_true',
        help='Actually create/update the PR branch (default: preview only).',
    )
    parser.add_argument(
        '--no-fetch', action='store_true',
        help='Skip the git fetch step and use already-fetched upstream data.',
    )
    args = parser.parse_args()

    ensure_upstream_remote()
    if not args.no_fetch:
        fetch_upstream()

    print()
    print("Scanning upstream...", end=' ', flush=True)
    upstream_map = get_upstream_uid_map()
    print(f"{len(upstream_map)} UIDs.")

    print("Scanning local library...", end=' ', flush=True)
    local_uid_map = get_local_uid_map()
    print(f"{len(local_uid_map)} UIDs.")

    # UIDs present locally but absent upstream
    to_contribute = {uid: path for uid, path in local_uid_map.items()
                     if uid not in upstream_map}

    if not to_contribute:
        print("\nNothing to contribute -- all local UIDs are already in upstream.")
        return

    # Display grouped by material
    path_str_map = {uid: p.relative_to(LIBRARY_ROOT).as_posix()
                    for uid, p in to_contribute.items()}
    groups = _group_by_material(path_str_map)
    print(f"\n{len(to_contribute)} UID(s) to contribute across {len(groups)} material group(s):\n")
    for mat_key in sorted(groups):
        entries = groups[mat_key]
        print(f"  {mat_key}/  ({len(entries)} UID(s))")
        for colour, uid in sorted(entries):
            print(f"    {colour}/{uid}")

    if not args.apply:
        print()
        print("Preview only -- run with --apply to create/update the PR branch.")
        print()
        print("Prerequisites:")
        print("  - GitHub CLI installed:   https://cli.github.com/")
        print("  - Authenticated:          gh auth login")
        return

    # --- Prerequisites check ---
    if not check_gh_available():
        print("\nERROR: GitHub CLI (gh) is not installed or not authenticated.")
        print("  Install: https://cli.github.com/")
        print("  Then:    gh auth login")
        sys.exit(1)

    owner = get_origin_owner()
    if not owner:
        print("\nERROR: Could not determine GitHub username from origin remote URL.")
        sys.exit(1)

    print(f"\nBuilding branch '{CONTRIBUTION_BRANCH}' from {UPSTREAM_REF} ...")
    worktree_dir = build_contribution_branch(to_contribute)
    push_and_sync_pr(worktree_dir, to_contribute, owner)

    print()
    print(f"Branch '{CONTRIBUTION_BRANCH}' on origin will be updated each run")
    print("until the PR is merged or closed, then a new PR will be opened.")
    print("View/manage the PR:")
    print(f"  gh pr view --repo {UPSTREAM_REPO}")


if __name__ == '__main__':
    main()
