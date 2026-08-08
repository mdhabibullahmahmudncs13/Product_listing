"""
chaldal_scraper.py
-------------------
Scrapes product name, price, and image from https://chaldal.com/oil
Downloads each product image into ./chaldal_oil_scrape/images/
Writes all product data into ./chaldal_oil_scrape/products.json

WHY SELENIUM:
Chaldal.com is a JavaScript-rendered (Angular) single-page app. The product
grid does not exist in the raw HTML returned by a simple `requests.get()` —
it's injected into the DOM by JS after the page loads, and more products are
added as you scroll. So we use a real (headless) browser to render the page
first, then parse the fully-loaded DOM.

INSTALL (once):
    pip install selenium webdriver-manager requests beautifulsoup4

RUN:
    python chaldal_scraper.py
    python chaldal_scraper.py https://chaldal.com/ghee
    python chaldal_scraper.py https://chaldal.com/shemai-suji --headless=false

If you get zero products, pass --headless=false and watch the browser
to see what's happening (site markup / anti-bot measures can change).
"""

import argparse
import csv
import json
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

# --------------------------------------------------------------------------
# CONFIG (defaults — can be overridden by command-line arguments, see below)
# --------------------------------------------------------------------------
DEFAULT_TARGET_URL = "https://chaldal.com/shemai-suji"

HEADLESS = True          # overridden by --headless=false
SCROLL_PAUSE_SEC = 1.5   # wait time between scrolls for lazy-loaded content
MAX_SCROLL_ATTEMPTS = 40 # safety cap so it can't scroll forever

# Chaldal's image CDN always serves product images through this path.
# We use this as the anchor to find product cards instead of relying on
# CSS class names, which are more likely to change.
IMAGE_URL_HINT = "_mpimage"

# Matches a Bangladeshi Taka price like "৳ 355" or "355" appearing near the
# currency symbol in the card's text.
PRICE_RE = re.compile(r"(\d[\d,]*)")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def get_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,2000")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def extract_img_tags(soup):
    """Find product images, checking every attribute lazy-load libraries
    commonly use (src is often a blank placeholder until the image
    scrolls into view)."""
    candidates = []
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "srcset"):
            val = img.get(attr)
            if val and IMAGE_URL_HINT in val:
                # srcset can contain multiple "url size," entries — take the first url
                url = val.split(",")[0].strip().split(" ")[0]
                candidates.append((img, url))
                break
    return candidates


def wait_for_products(driver, timeout=20, poll=1.0):
    """Wait until at least one product image has rendered in the DOM.

    Chaldal's grid is injected asynchronously; if we start scrolling before
    it loads, the page height is still small and the scroll loop "stabilises"
    and exits before any product is captured. Return True if products were
    seen, False if we timed out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        if extract_img_tags(soup):
            return True
        time.sleep(poll)
    return False


def scroll_and_collect(driver, url):
    """Load the page and scroll down step by step, extracting products
    from the DOM at EACH step (not just at the end).

    This matters because Chaldal's grid appears to use virtual/recycled
    scrolling — off-screen product cards get removed from the DOM as you
    scroll past them, so a single snapshot taken after scrolling to the
    bottom only shows whatever happened to still be mounted (often just
    the static top banner). Capturing incrementally avoids losing items.
    """
    driver.get(url)
    if not wait_for_products(driver):
        # give it one more chance: reload once, then wait again
        time.sleep(2)
        driver.get(url)
        wait_for_products(driver, timeout=25)
    time.sleep(1)  # settle after grid appears

    collected = {}  # image_url -> (name, price)
    stable_rounds = 0
    last_height = 0

    for step in range(MAX_SCROLL_ATTEMPTS):
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for img, img_url in extract_img_tags(soup):
            img_url = urljoin(url, img_url)
            if img_url in collected:
                continue
            card = find_card(img)
            card_text = card.get_text(separator="\n", strip=True)
            name, price = parse_card(card_text)
            collected[img_url] = (name, price)

        driver.execute_script("window.scrollBy(0, Math.round(window.innerHeight * 0.8));")
        time.sleep(SCROLL_PAUSE_SEC)

        new_height = driver.execute_script("return document.body.scrollHeight")
        at_bottom = driver.execute_script(
            "return (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 10"
        )

        if new_height == last_height and at_bottom:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_height = new_height

        if stable_rounds >= 3 and collected:
            break

    # one final pass in case the last scroll revealed new items
    soup = BeautifulSoup(driver.page_source, "html.parser")
    for img, img_url in extract_img_tags(soup):
        img_url = urljoin(url, img_url)
        if img_url not in collected:
            card = find_card(img)
            card_text = card.get_text(separator="\n", strip=True)
            name, price = parse_card(card_text)
            collected[img_url] = (name, price)

    return collected


def find_card(img_tag):
    """Walk up the DOM from a product image until we find an ancestor whose
    text contains a price-like number. That ancestor is treated as the
    'product card'. Falls back to a few levels up if no price is found."""
    node = img_tag
    for _ in range(6):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(separator="\n", strip=True)
        if PRICE_RE.search(text) and len(text) < 400:
            return node
    return img_tag.parent


def parse_card(card_text):
    """Given the raw text of a product card, split out price and name.

    Typical structure (from Chaldal's rendered grid):
        ৳
        355
        Rahul Pure Mustard Oil
        1 ltr
        1 hr
    """
    lines = [l.strip() for l in card_text.split("\n") if l.strip()]

    price = None
    name_parts = []
    time_re = re.compile(r"^\d+\s*(hr|hrs|min|mins)$", re.IGNORECASE)
    fee_re = re.compile(r"^\+৳")

    for line in lines:
        if line in ("৳",):
            continue
        if fee_re.match(line):
            continue  # e.g. "+৳10" extra shipping fee, not the price
        if price is None and re.fullmatch(r"[\d,]+", line):
            price = line.replace(",", "")
            continue
        if time_re.match(line):
            continue  # e.g. "1 hr" delivery estimate
        name_parts.append(line)

    name = " ".join(name_parts).strip() if name_parts else None
    return name, price


def sanitize_filename(name, fallback):
    if not name:
        name = fallback
    name = re.sub(r"[^a-zA-Z0-9\-_ ]", "", name).strip().replace(" ", "_")
    return name[:80] if name else fallback


def download_image(url, dest_path):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return True
    except requests.RequestException as e:
        print(f"  [!] Failed to download {url}: {e}")
        return False


def slug_from_url(url):
    """Derive a filesystem-friendly folder name from the category URL,
    e.g. https://chaldal.com/shemai-suji -> chaldal_shemai_suji_scrape"""
    path = urlparse(url).path.strip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_").lower()
    return f"chaldal_{slug or 'category'}_scrape"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape product name, price, and images from a Chaldal category page."
    )
    parser.add_argument(
        "url", nargs="?", default=DEFAULT_TARGET_URL,
        help=f"Chaldal category URL to scrape (default: {DEFAULT_TARGET_URL})",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Folder to save images/json/csv into (default: derived from the URL)",
    )
    parser.add_argument(
        "--headless", default="true", choices=["true", "false"],
        help="Run Chrome headless (true) or visibly for debugging (false). Default: true",
    )
    args = parser.parse_args()
    if args.output_dir is None:
        args.output_dir = slug_from_url(args.url)
    args.headless = args.headless == "true"
    return args


def main():
    args = parse_args()
    target_url = args.url
    output_dir = args.output_dir
    images_dir = os.path.join(output_dir, "images")
    json_path = os.path.join(output_dir, "products.json")
    csv_path = os.path.join(output_dir, "products.csv")

    os.makedirs(images_dir, exist_ok=True)

    print(f"Launching {'headless' if args.headless else 'visible'} browser and loading {target_url} ...")
    driver = get_driver(headless=args.headless)
    try:
        collected = scroll_and_collect(driver, target_url)
    finally:
        driver.quit()

    print(f"Found {len(collected)} candidate product images.")

    products = []

    for idx, (img_url, (name, price)) in enumerate(collected.items(), start=1):
        if not name:
            # Fall back to the slug in the image URL, e.g.
            # ".../rahul-pure-mustard-oil-1-ltr?src=..." -> readable name
            slug_match = re.search(r"_mpimage/([a-z0-9\-]+)", img_url)
            if slug_match:
                name = slug_match.group(1).replace("-", " ").title()

        filename_base = sanitize_filename(name, f"product_{idx}")
        ext = ".jpg"
        image_filename = f"{filename_base}{ext}"
        image_path = os.path.join(images_dir, image_filename)

        # avoid filename collisions
        counter = 1
        while os.path.exists(image_path):
            image_filename = f"{filename_base}_{counter}{ext}"
            image_path = os.path.join(images_dir, image_filename)
            counter += 1

        ok = download_image(img_url, image_path)
        relative_image_path = os.path.join("images", image_filename) if ok else None

        product = {
            "name": name or f"Unknown Product {idx}",
            "price": price,
            "currency": "BDT",
            "image_url": img_url,
            "image_location": relative_image_path,
        }
        products.append(product)
        print(f"  [{idx}] {product['name']} - ৳{product['price']} -> {relative_image_path}")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        fieldnames = ["name", "price", "currency", "image_url", "image_location"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(products)

    print(f"\nDone. {len(products)} products saved to {json_path} and {csv_path}")
    print(f"Images saved in {images_dir}/")


if __name__ == "__main__":
    main()
    