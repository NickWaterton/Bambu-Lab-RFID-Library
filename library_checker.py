# -*- coding: utf-8 -*-

# Python script to search the library looking for mismatched color data.
# Reads each directory and compares the color data in it; if there are tags
# with two different color codes, print them out.
# Also prints out an error if the tag doesn't appear to be in the correct directory according
# to its data.
# Also print errors if it encounters tags which don't parse correctly.

import sys
import argparse

from rich.console import Console

from pathlib import Path

from parse import Tag, bytes_to_hex, BLOCKS_PER_SECTOR, TOTAL_SECTORS

if not sys.version_info >= (3, 6):
  raise Exception("Python 3.6 or higher is required!")

DUMP_SUFFIX = "-dump.bin"
LIBRARY_ROOT = Path.cwd()

# These map dicts map the 'filament_type' and 'detailed_filament_type' in the tags
# to the names used in the library.

CATEGORY_MAP = {
  'PA-S': 'Support Material',
  'PLA-S': 'Support Material',
  'Support': 'Support Material',
  'PVA': 'Support Material',
  'ABS-S': 'Support Material',
  'PETG-CF': 'PETG',
  'TPU-AMS': 'TPU',
  'ABS-GF': 'ABS',
  'PLA-CF': 'PLA',
  'PA-CF': 'PA',
  'ASA-CF': 'ASA',
  'ASA Aero': 'ASA',
}

# Maps tag detailed_filament_type values for materials shared across multiple library folders.
# Value is (single_colour_folder, multi_colour_folder).
# Note: 'PLA Silk+' stores 'PLA Silk+' in the tag and needs no entry here.
#       'PLA Silk' covers two distinct products:
#         - PLA Silk (discontinued single-colour) → PLA Silk/
#         - PLA Silk Multi-Color                  → PLA Silk Multi-Color/
MULTI_COLOR_MATERIAL_MAP = {
  'PLA Silk': ('PLA Silk', 'PLA Silk Multi-Color'),
}

MATERIAL_MAP = {
  'Support for PA': ['Support for PA-PET'],
  'Support For PA': ['Support for PA-PET'],
  'Support G': ['Support for PA-PET'],
  'Support W': ['Support for PLA-PETG'],
  'Support for PLA': ['Support for PLA (New Version)', 'Support for PLA-PETG'],
  'PETG-CF': ['PETG CF'],
  'PETG HF': ['PETG Translucent'],
  'PLA Basic': ['PLA Basic Gradient'],
  'PLA': ['PLA Silk Multi-Color'],
}


def resolve_material(tag_data):
    """
    Return the canonical library folder name for a tag's material.
    For materials in MULTI_COLOR_MATERIAL_MAP, multi-colour tags map to the
    multi folder; single-colour tags map to the single folder.
    """
    base = tag_data['detailed_filament_type']
    if base in MULTI_COLOR_MATERIAL_MAP:
        single_folder, multi_folder = MULTI_COLOR_MATERIAL_MAP[base]
        return multi_folder if tag_data.get('filament_color_count', 1) > 1 else single_folder
    return base

def load_library(print_error=False, debug_color=None):
  library = {}

  for file in LIBRARY_ROOT.rglob(f'*{DUMP_SUFFIX}'):
    if file.parent == LIBRARY_ROOT:
      # skip files that are in the root
      continue
    try:
      with open(file, 'rb') as f:
        tag = Tag(file.name, f.read(), fail_on_warn=True)
    except Exception as e:
      if print_error:
        print(f'\t[!] Library load failed to parse {file.relative_to(LIBRARY_ROOT)}: {e}')
        continue

    # Assumes dir structure is <Category>/<Material>/<Color Name>/<UUID>/-dump.bin

    cat_dir, mat_dir, color_dir = file.parts[-5:-2]

    if (category := tag.data['filament_type']) not in library:
       library.update({category:{}})
    if (material := tag.data['detailed_filament_type']) not in library[category]:
       library[category].update({material:{}})
    if (color_dir) not in library[category][material]:
       library[category][material].update({color_dir:[]})
    if (color_hex := tag.data['filament_color']) not in library[category][material][color_dir]:
        library[category][material][color_dir].append(color_hex)

    category = CATEGORY_MAP.get(category, category)
    resolved = resolve_material(tag.data)
    # For MULTI_COLOR_MATERIAL_MAP entries, only the mapped folder names are valid.
    if material in MULTI_COLOR_MATERIAL_MAP:
      single_folder, multi_folder = MULTI_COLOR_MATERIAL_MAP[material]
      if tag.data.get('filament_color_count', 1) > 1:
        material_list = [multi_folder]
      else:
        material_list = [single_folder]
    else:
      extra = MATERIAL_MAP.get(material, [])
      if not isinstance(extra, list):
        extra = [extra]
      material_list = list(dict.fromkeys([resolved, material] + extra))
    if debug_color:
      debug_color.print('\u2588', style=color_hex[0:7], end=' ')
      print(f'{color_hex} {file.relative_to(LIBRARY_ROOT)}')
    if print_error and (cat_dir != category or mat_dir not in material_list):
      print(f"\t[!] {file.relative_to(LIBRARY_ROOT)} may be in the wrong directory! Should be in {category}/{resolved}")

  if debug_color:
    print("Loading done")
  return library


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Check the library for tag location, parsing and color errors')
  parser.add_argument('dir', nargs='*', default='', help='Path to library root; defaults to current directory')
  parser.add_argument('--color_list', '-c', action='store_true', help='Print a list of color codes found in each directory')
  parser.add_argument('--dump_colors', '-d', action='store_true', help='While parsing the library print out the color code found in each file')
  args = parser.parse_args()

  console = Console()
  library = load_library(True, debug_color=console if args.dump_colors else None)

  good_colors = []
  for category, cat_dict in library.items():
    for material, mat_dict in cat_dict.items():
      for color_dir, color_list in mat_dict.items():
        path = f'{category}/{material}/{color_dir}'
        if len(color_list) > 1:
          console.print("Found multiple color codes in the same directory!")
          for color in color_list:
            console.print('\u2588', style=color[0:7], end=' ')
            print(f'{color} {path}')
          console.print()
        else:
          good_colors.append((color_list[0], path))

  if args.color_list:
    for color, path in good_colors:
      console.print('\u2588', style=color[0:7], end=' ')
      console.print(f'{color} {path}')

