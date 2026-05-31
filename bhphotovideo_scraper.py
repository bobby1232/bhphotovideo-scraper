cat > bhphotovideo_scraper.py <<'PY'
import argparse
import gzip
import json
import re
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SITEMAP_INDEX_URLS = [
    "https://www.bhphotovideo.com/SiteMapIndex.xml",
    "https://www.bhphotovideo.com/sitemap.xml",
]

PRODUCT_URL_RE = re.compile(r"https://www\.bhphotovideo\.com/c/product/[^<\s\"']+")


def get_text(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()

    content = r.content

    if url.endswith(".gz"):
        try:
            content = gzip.decompress(content)
        except Exception:
            content = gzip.GzipFile(fileobj=BytesIO(content)).read()

    return content.decode("utf-8", errors="replace")


def parse_xml_locations(xml_text: str) -> list[str]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    for elem in root.iter():
        if elem.tag.endswith("loc") and elem.text:
            out.append(elem.text.strip())

    return out


def collect_sitemap_urls(limit_sitemaps: int | None = None) -> list[str]:
    sitemaps = []

    for index_url in SITEMAP_INDEX_URLS:
        try:
            txt = get_text(index_url)
            locs = parse_xml_locations(txt)
            sitemaps.extend(locs)
        except Exception as e:
            print(f"[WARN] sitemap index failed: {index_url} | {e}")

    # На случай если индекс не отдаст loc, добавим сам индекс.
    if not sitemaps:
        sitemaps = SITEMAP_INDEX_URLS[:1]

    # Приоритизируем sitemap, где вероятнее есть товары.
    preferred = []
    other = []
    for u in sitemaps:
        low = u.lower()
        if any(x in low for x in ["product", "products", "catalog", "sitemap"]):
            preferred.append(u)
        else:
            other.append(u)

    result = []
    seen = set()
    for u in preferred + other:
        if u not in seen:
            seen.add(u)
            result.append(u)

    if limit_sitemaps:
        result = result[:limit_sitemaps]

    print(f"[INFO] sitemap files found: {len(result)}")
    return result


def collect_product_urls(target_urls: int = 3000, limit_sitemaps: int | None = None) -> list[str]:
    sitemap_urls = collect_sitemap_urls(limit_sitemaps=limit_sitemaps)

    product_urls = []
    seen = set()

    for sm_url in tqdm(sitemap_urls, desc="Reading sitemaps"):
        if len(product_urls) >= target_urls:
            break

        try:
            txt = get_text(sm_url)
        except Exception as e:
            print(f"[WARN] sitemap read failed: {sm_url} | {e}")
            continue

        locs = parse_xml_locations(txt)
        candidates = []

        for loc in locs:
            if "/c/product/" in loc:
                candidates.append(loc)

        # fallback regex
        candidates += PRODUCT_URL_RE.findall(txt)

        for url in candidates:
            url = url.strip()
            url = url.split("?")[0]
            if "/c/product/" not in url:
                continue
            if url in seen:
                continue

            seen.add(url)
            product_urls.append(url)

            if len(product_urls) >= target_urls:
                break

        print(f"[INFO] products collected so far: {len(product_urls)}")

    return product_urls


def clean_price(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    s = str(value)
    s = s.replace(",", "")
    m = re.search(r"(\d+(?:\.\d{1,2})?)", s)
    if not m:
        return None

    try:
        return float(m.group(1))
    except Exception:
        return None


def flatten_jsonld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from flatten_jsonld(x)
    elif isinstance(obj, dict):
        yield obj
        for v in obj.values():
            if isinstance(v, (list, dict)):
                yield from flatten_jsonld(v)


def extract_from_jsonld(soup: BeautifulSoup):
    name = None
    brand = None
    price = None
    currency = None
    availability = None
    sku = None
    breadcrumbs = []

    for tag in soup.find_all("script", type=lambda x: x and "ld+json" in x):
        raw = tag.string or tag.get_text(" ", strip=True)
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        for obj in flatten_jsonld(data):
            typ = obj.get("@type")

            if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
                name = name or obj.get("name")
                sku = sku or obj.get("sku") or obj.get("mpn")

                b = obj.get("brand")
                if isinstance(b, dict):
                    brand = brand or b.get("name")
                elif isinstance(b, str):
                    brand = brand or b

                offers = obj.get("offers")
                if isinstance(offers, dict):
                    price = price or clean_price(offers.get("price") or offers.get("lowPrice"))
                    currency = currency or offers.get("priceCurrency")
                    availability = availability or offers.get("availability")
                elif isinstance(offers, list):
                    for off in offers:
                        if isinstance(off, dict):
                            price = price or clean_price(off.get("price") or off.get("lowPrice"))
                            currency = currency or off.get("priceCurrency")
                            availability = availability or off.get("availability")

            if typ == "BreadcrumbList" or (isinstance(typ, list) and "BreadcrumbList" in typ):
                items = obj.get("itemListElement") or []
                for it in items:
                    if isinstance(it, dict):
                        item = it.get("item")
                        if isinstance(item, dict) and item.get("name"):
                            breadcrumbs.append(item.get("name"))
                        elif it.get("name"):
                            breadcrumbs.append(it.get("name"))

    return {
        "name": name,
        "brand": brand,
        "price_usd": price,
        "currency": currency,
        "availability": availability,
        "sku": sku,
        "breadcrumbs": breadcrumbs,
    }


def extract_price_fallback(html: str):
    patterns = [
        r'"price"\s*:\s*"?([0-9,]+(?:\.[0-9]{1,2})?)"?',
        r'"salePrice"\s*:\s*"?([0-9,]+(?:\.[0-9]{1,2})?)"?',
        r'"finalPrice"\s*:\s*"?([0-9,]+(?:\.[0-9]{1,2})?)"?',
        r'\$\s*([0-9,]+(?:\.[0-9]{2})?)',
    ]

    for p in patterns:
        m = re.search(p, html)
        if m:
            return clean_price(m.group(1))

    return None


def infer_category(url: str, breadcrumbs: list[str], title: str | None):
    if breadcrumbs:
        useful = [x for x in breadcrumbs if x and x.lower() not in ["home", "bh photo"]]
        if useful:
            return " > ".join(useful[:4])

    text = f"{url} {title or ''}".lower()

    rules = [
        ("Cameras & Drones", ["camera", "mirrorless", "dslr", "camcorder", "cinema camera", "drone"]),
        ("Lenses", [" lens", "mm f/", "zoom lens", "prime lens"]),
        ("Lighting", [" light", "led", "strobe", "softbox", "flash"]),
        ("Audio", ["microphone", "headphone", "speaker", "audio", "mixer", "recorder"]),
        ("Computers & Storage", ["laptop", "desktop", "ssd", "hard drive", "nas", "memory card", "tablet"]),
        ("Monitors & Displays", ["monitor", "display", "projector"]),
        ("Tripods & Support", ["tripod", "monopod", "gimbal", "head"]),
        ("Printers & Scanners", ["printer", "scanner"]),
        ("Bags & Cases", ["bag", "case", "backpack"]),
        ("Video Production", ["switcher", "video", "capture", "streaming"]),
    ]

    for cat, keys in rules:
        if any(k in text for k in keys):
            return cat

    return "Other"


def scrape_product(url: str, session: requests.Session, min_price: float):
    try:
        r = session.get(url, headers=HEADERS, timeout=35)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)

    html = r.text
    soup = BeautifulSoup(html, "lxml")

    meta = extract_from_jsonld(soup)

    title = meta.get("name")
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    if not title:
        if soup.title:
            title = soup.title.get_text(" ", strip=True)

    price = meta.get("price_usd")
    if price is None:
        price = extract_price_fallback(html)

    if price is None:
        return None, "NO_PRICE"

    if price <= min_price:
        return None, "BELOW_MIN_PRICE"

    currency = meta.get("currency") or "USD"
    if currency and currency.upper() != "USD":
        return None, f"NON_USD_{currency}"

    category = infer_category(url, meta.get("breadcrumbs") or [], title)

    row = {
        "category": category,
        "product_name": title,
        "brand": meta.get("brand"),
        "sku_or_mpn": meta.get("sku"),
        "price_usd": price,
        "currency": "USD",
        "availability": meta.get("availability"),
        "url": url,
        "source_domain": urlparse(url).netloc,
        "scraped_at": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    return row, "OK"


def safe_sheet_name(name: str):
    invalid = r'[]:*?/\\'
    for ch in invalid:
        name = name.replace(ch, " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:31] or "Sheet"


def write_excel(df: pd.DataFrame, out_file: str):
    df = df.copy()

    summary = (
        df.groupby("category", dropna=False)
        .agg(
            items=("product_name", "count"),
            min_price_usd=("price_usd", "min"),
            avg_price_usd=("price_usd", "mean"),
            max_price_usd=("price_usd", "max"),
            total_value_usd=("price_usd", "sum"),
        )
        .reset_index()
        .sort_values("items", ascending=False)
    )

    readme = pd.DataFrame(
        {
            "field": [
                "source",
                "filter",
                "method",
                "important_caveat",
            ],
            "value": [
                "B&H Photo Video public product pages and sitemaps",
                "Only products with price_usd > selected min price",
                "Sitemap discovery -> product page fetch -> JSON-LD/HTML price extraction -> Excel export",
                "Prices and availability can change; verify final procurement prices on product URLs.",
            ],
        }
    )

    with pd.ExcelWriter(out_file, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="All Products")
        summary.to_excel(writer, index=False, sheet_name="Category Summary")
        readme.to_excel(writer, index=False, sheet_name="Readme")

        for cat, part in df.groupby("category"):
            part.sort_values("price_usd", ascending=False).to_excel(
                writer, index=False, sheet_name=safe_sheet_name(str(cat))
            )

    print(f"[DONE] Excel written: {out_file}")
    print(f"[DONE] Rows: {len(df)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=500)
    parser.add_argument("--min-price", type=float, default=150)
    parser.add_argument("--out", type=str, default="bhphotovideo_products_over_150_500.xlsx")
    parser.add_argument("--target-urls", type=int, default=6000)
    parser.add_argument("--sleep", type=float, default=0.35)
    parser.add_argument("--limit-sitemaps", type=int, default=None)
    args = parser.parse_args()

    print("[INFO] Starting B&H scraper")
    print(f"[INFO] Target rows: {args.target}")
    print(f"[INFO] Min price: {args.min_price}")

    urls = collect_product_urls(target_urls=args.target_urls, limit_sitemaps=args.limit_sitemaps)

    print(f"[INFO] Product URLs collected: {len(urls)}")

    rows = []
    errors = {}

    session = requests.Session()

    for url in tqdm(urls, desc="Scraping products"):
        if len(rows) >= args.target:
            break

        row, status = scrape_product(url, session, min_price=args.min_price)

        if row:
            rows.append(row)
            print(f"[OK] {len(rows)}/{args.target} | ${row['price_usd']} | {row['product_name'][:90]}")
        else:
            errors[status] = errors.get(status, 0) + 1

        time.sleep(args.sleep)

    print("[INFO] Scrape status summary:")
    for k, v in sorted(errors.items(), key=lambda x: x[1], reverse=True):
        print(f"  {k}: {v}")

    if not rows:
        raise SystemExit("No rows collected. B&H may be blocking requests or sitemap parsing failed.")

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["url"])
    df = df.sort_values(["category", "price_usd"], ascending=[True, False])

    write_excel(df, args.out)

    if len(df) < args.target:
        print(f"[WARN] Collected only {len(df)} rows, less than target {args.target}.")
        print("[WARN] Try increasing --target-urls 12000 or reducing --sleep if requests are stable.")


if __name__ == "__main__":
    main()
PY
