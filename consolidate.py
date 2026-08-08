#!/usr/bin/env python3
"""Consolidate every category scrape into a single dataset.

Creates:
    all_products/products.json   - every product (4,190) with department/category added
    all_products/products.csv    - same data, flattened
    all_products/images/         - every downloaded product image (collision-safe names)

The new image_location in the merged output is relative to all_products/.
"""
import csv
import json
import os
import re
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'all_products')
IMG_OUT = os.path.join(OUT, 'images')


def category_slug(folder):
    """chaldal_honey_scrape -> honey"""
    return re.sub(r'^chaldal_|_scrape$', '', folder)


def find_scrapes():
    scrapes = []  # (department, scrape_folder, products.json path)
    for dept in sorted(os.listdir(ROOT)):
        dept_path = os.path.join(ROOT, dept)
        if not os.path.isdir(dept_path) or dept.startswith('.'):
            continue
        for folder in sorted(os.listdir(dept_path)):
            if not folder.endswith('_scrape'):
                continue
            pj = os.path.join(dept_path, folder, 'products.json')
            if os.path.exists(pj):
                scrapes.append((dept, folder, pj))
    return scrapes


def main():
    scrapes = find_scrapes()
    print(f'Found {len(scrapes)} scrape folders')

    os.makedirs(IMG_OUT, exist_ok=True)

    # Clean the images dir of leftovers from a previous run
    for old in os.listdir(IMG_OUT):
        os.remove(os.path.join(IMG_OUT, old))

    products = []
    name_to_src = {}   # final basename -> source path (final-name uniqueness)
    orig_first = {}    # original basename -> first source path (collision detect)
    copied = 0
    no_image = 0
    collisions = 0

    for dept, folder, pj in scrapes:
        data = json.load(open(pj, encoding='utf-8'))
        for p in data:
            out_p = dict(p)
            out_p['department'] = dept
            out_p['category'] = category_slug(folder)

            loc = (p.get('image_location') or '').strip()
            if loc:
                src = os.path.normpath(os.path.join(os.path.dirname(pj), loc))
                base = os.path.basename(loc)
                if os.path.exists(src):
                    final = base
                    if base in orig_first and orig_first[base] != src:
                        # Same filename from a different category -> disambiguate
                        final = f'{category_slug(folder)}_{base}'
                        collisions += 1
                    orig_first.setdefault(base, src)
                    # Guarantee final uniqueness even for odd edge cases
                    n = 2
                    while final in name_to_src and name_to_src[final] != src:
                        final = f'{category_slug(folder)}_{n}_{base}'
                        n += 1
                    name_to_src[final] = src
                    shutil.copy2(src, os.path.join(IMG_OUT, final))
                    out_p['image_location'] = f'images/{final}'
                    copied += 1
                else:
                    out_p['image_location'] = ''
                    no_image += 1
            else:
                out_p['image_location'] = ''
                no_image += 1
            products.append(out_p)

    # ---- single JSON ----
    with open(os.path.join(OUT, 'products.json'), 'w', encoding='utf-8') as f:
        json.dump(products, f, indent=1, ensure_ascii=False)

    # ---- single CSV ----
    columns = ['name', 'price', 'currency', 'image_url', 'image_location',
               'department', 'category']
    with open(os.path.join(OUT, 'products.csv'), 'w', encoding='utf-8',
              newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(products)

    print(f'Products written : {len(products)}')
    print(f'Images copied    : {copied}')
    print(f'Collision renames: {collisions}')
    print(f'Products w/o img : {no_image}')
    print(f'Unique images    : {len(name_to_src)}')


if __name__ == '__main__':
    main()
