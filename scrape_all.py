"""Scrape ALL remaining chaldal.com product categories.

Computes the missing categories from the site's own service state
(CategoryService + RouterService.categoryRoutes), maps each to a
department-grouped output folder (mirroring the existing breakfast/,
cooking/, ... structure), verifies the URL is live, then runs sc.py
per category in parallel.

Usage:
    python scrape_all.py --workers 4            # full run
    python scrape_all.py --only-slug wafers     # test a single category
    python scrape_all.py --limit 5              # first 5 (for testing)
"""
import argparse
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, "venv", "bin", "python")
SC = os.path.join(ROOT, "sc.py")
STATE_FILE = os.path.join(ROOT, ".chaldal_service_state.json")
MANIFEST = os.path.join(ROOT, "scrape_manifest.json")
DEAD_URLS = os.path.join(ROOT, "scrape_dead_urls.json")
RUNLOG = os.path.join(ROOT, "scrape_all_run.log")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
NOT_FOUND_TITLE = "Page not Found"


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    html = open("/tmp/chaldal_home.html").read()
    s = html.find("window.__serviceState = ") + len("window.__serviceState = ")
    e = html.find("</script>", s)
    state = json.loads(html[s:e].strip().rstrip(";"))
    json.dump(state, open(STATE_FILE, "w"))
    return state


def build_jobs():
    state = load_state()
    cs = state["CategoryService"]["categories"]
    routes = state["RouterService"]["categoryRoutes"]

    by_id = {}
    for parent, cats in cs.items():
        for c in cats:
            by_id.setdefault(c["Id"], c)

    def flatten(d, out=None):
        if out is None:
            out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                flatten(v, out)
            else:
                out[int(k)] = v
        return out

    route_map = flatten(routes)
    parent_of = {c["Id"]: c["ParentCategoryId"] for c in by_id.values()}

    def slug_of(cid):
        return route_map.get(cid)

    def ancestry(cid):
        chain = []
        seen = set()
        while cid != 0 and cid not in seen:
            chain.append(cid)
            seen.add(cid)
            cid = parent_of.get(cid, 0)
        return list(reversed(chain))  # root -> leaf

    # already-scraped slugs (folder name encodes the URL slug)
    scraped = set()
    for d in os.listdir(ROOT):
        m = re.match(r"chaldal_(.+)_scrape$", d)
        if m:
            scraped.add(m.group(1).replace("_", "-"))
    for top in os.listdir(ROOT):
        topdir = os.path.join(ROOT, top)
        if not os.path.isdir(topdir):
            continue
        for d in os.listdir(topdir):
            m = re.match(r"chaldal_(.+)_scrape$", d)
            if m:
                scraped.add(m.group(1).replace("_", "-"))

    jobs = []
    for c in by_id.values():
        if not c.get("ContainsProducts"):
            continue
        slug = slug_of(c["Id"])
        if not slug or slug in scraped:
            continue
        chain = ancestry(c["Id"])
        if len(chain) >= 3:
            folder = slug_of(chain[1]) or slug
        elif len(chain) == 2:
            folder = slug_of(chain[0]) or slug
        else:
            folder = "collections"  # top-level meta/collection categories
        jobs.append(
            {
                "id": c["Id"],
                "name": c["Name"],
                "slug": slug,
                "url": f"https://chaldal.com/{slug}",
                "folder": folder,
                "outdir": os.path.join(ROOT, folder, f"chaldal_{slug}_scrape"),
            }
        )
    jobs.sort(key=lambda j: (j["folder"], j["slug"]))
    return jobs


def url_is_live(job):
    """Server-rendered title check: 404 pages have a distinctive title."""
    try:
        resp = requests.get(job["url"], headers=HEADERS, timeout=25)
        if NOT_FOUND_TITLE in resp.text[:4000]:
            return False
        return True
    except requests.RequestException:
        return None  # transient error -> keep the job, warn


def verify_jobs(jobs, workers=8):
    live, dead, unsure = [], [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(url_is_live, j): j for j in jobs}
        for fut in as_completed(futs):
            job = futs[fut]
            try:
                r = fut.result()
            except Exception:
                r = None
            if r is True:
                live.append(job)
            elif r is False:
                dead.append(job)
            else:
                unsure.append(job)
    return live, dead, unsure


def run_one(job, log_fh):
    outdir = job["outdir"]
    products_json = os.path.join(outdir, "products.json")
    if os.path.exists(products_json):
        log_fh.write(f"SKIP(exists) {job['slug']} -> {outdir}\n")
        log_fh.flush()
        return ("skip", job, None)

    attempts = 3  # 0-product scrapes are retried (site can be flaky)
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        try:
            proc = subprocess.run(
                [PY, SC, job["url"], "--output-dir", outdir],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            log_fh.write(f"WARN(timeout) {job['slug']} attempt {attempt}\n")
            log_fh.flush()
            continue
        try:
            data = json.load(open(products_json))
            count = len(data)
        except Exception:
            count = None
        if count and count > 0:
            log_fh.write(f"OK({count}) {job['slug']} ({time.time()-t0:.0f}s) -> {outdir}\n")
            log_fh.flush()
            return ("ok", job, count)
        log_fh.write(
            f"WARN {job['slug']} attempt {attempt} exit={proc.returncode} count={count} "
            f"stdout={proc.stdout[-200:]!r}\n"
        )
        log_fh.flush()
        time.sleep(5)
    # exhausted retries
    log_fh.write(f"EMPTY/FAIL {job['slug']} -> {outdir}\n")
    log_fh.flush()
    return ("empty", job, count)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-slug", default=None)
    ap.add_argument("--skip-verify", action="store_true", default=False)
    args = ap.parse_args()

    jobs = build_jobs()
    if args.only_slug:
        jobs = [j for j in jobs if j["slug"] == args.only_slug]
    if args.limit:
        jobs = jobs[: args.limit]

    dead = []
    if not args.skip_verify:
        print(f"Verifying {len(jobs)} URLs ...")
        jobs, dead, unsure = verify_jobs(jobs)
        if unsure:
            print(f"  {len(unsure)} URLs had transient errors; keeping them")
            jobs += unsure
        print(f"  live={len(jobs)} dead={len(dead)}")
        json.dump(dead, open(DEAD_URLS, "w"), indent=1)

    json.dump(jobs, open(MANIFEST, "w"), indent=1)
    print(f"Jobs to run: {len(jobs)}")
    for j in jobs[:60]:
        print(f"  {j['slug']:40} -> {j['outdir']}")

    if not jobs:
        print("Nothing to run.")
        return

    log_fh = open(RUNLOG, "a")
    log_fh.write(
        f"\n===== RUN START {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"({len(jobs)} jobs, {args.workers} workers) =====\n"
    )
    log_fh.flush()

    results = {"ok": 0, "empty": 0, "skip": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, j, log_fh): j for j in jobs}
        for fut in as_completed(futs):
            status, job, count = fut.result()
            results[status] += 1
            print(f"[{status}] {job['slug']} count={count}", flush=True)

    log_fh.write(
        f"===== RUN END {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"ok={results['ok']} empty={results['empty']} skip={results['skip']} =====\n"
    )
    log_fh.close()
    print(f"\nDone. ok={results['ok']} empty={results['empty']} skip={results['skip']}")
    print("Dead URLs saved to:", DEAD_URLS)
    print("Full log:", RUNLOG)


if __name__ == "__main__":
    main()
