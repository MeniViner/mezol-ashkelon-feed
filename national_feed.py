"""Publish a national branch directory and an emergency basket of public shelf prices."""
import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE = "https://www.over.org.il/api/prices/"
PAGES = "https://meniviner.github.io/mezol-ashkelon-feed/"
DAY = datetime.now(ZoneInfo("Asia/Jerusalem")).date().isoformat()
# Public grocery GTINs. No shopper lists or account information enter this job.
CODES = """7290004131074 7290004127329 7290000048185 7290116932033 7290004125776
7290000043814 7290116935409 7290000408316 7296082000134 7290116936116 7290003643004
7290000060880 7290000060903 7290000064758 7296073021568 7290000211503 7290003643752
7290003643714 7290001216040 7290019603498 7290018989333 7290005287206 7290000208329
7290000072623 8711000601587 7290000345284 7290000174723 7290002130567 7290116936291
7290000048307""".split()


def get(url):
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "MeZol-national-price-import/1.0"})
            with urlopen(req, timeout=50) as response:
                return json.load(response)
        except (OSError, ValueError) as error:
            if attempt == 2:
                raise
            time.sleep(5 if getattr(error, "code", None) != 429 else 62)


def pages(table, **params):
    offset = 0
    while True:
        query = urlencode(dict(limit=1000, offset=offset, **params))
        page = get(SOURCE + "table/" + table + "?" + query)
        yield from page["rows"]
        if not page["has_more"] or not page["rows"]:
            break
        offset += len(page["rows"])


def key(row):
    return "|".join(str(row[field]) for field in ("chain", "sub_chain_id", "store_id"))


def text(value):
    return re.sub(r"[^א-תa-z0-9]", "", str(value or "").lower())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def build_directory(out):
    directory = {}
    columns = "chain,sub_chain_id,store_id,chain_name,store_name,address,city,city_code,store_type"
    entries = list(pages("prices_stores", order="chain,sub_chain_id,store_id", columns=columns))
    cities = {r["city"] for r in entries if r["city_code"] and r["city"] and re.search("[א-ת]",r["city"])}
    def words(value):
        return " ".join(re.findall(r"[א-תa-z0-9]+", (value or "").lower()))
    aliases = {words(c): c for c in cities}
    for city in cities:
        if words(city) == "תל אביב יפו":
            aliases["תל אביב"] = city
    for row in entries:
        store = [row[k] or "" for k in columns.split(",")[3:]]
        inferred = False
        if not store[3] or not re.search("[א-תa-zA-Z]",store[3]):
            label = " " + words(store[1]) + " "
            matches = {city for alias,city in aliases.items() if len(alias) >= 3 and
                       (" " + alias + " ") in label}
            store[3] = matches.pop() if len(matches) == 1 else "ללא עיר מזוהה"
            inferred = store[3] != "ללא עיר מזוהה"
        directory[key(row)] = store + ["", "", inferred]
    if len(directory) < 500:
        raise RuntimeError("Incomplete national branch directory")
    # The newest coverage row can contain a promo file alone. Only attach actual price files.
    seen_dates = set()
    for row in pages("prices_coverage", order="-snapshot_date,chain,store_id",
                     columns="chain,sub_chain_id,store_id,snapshot_date,price_file,price_file_time,price_items"):
        seen_dates.add(row["snapshot_date"])
        if len(seen_dates) > 5:
            break
        store = directory.get(key(row))
        filename = row["price_file"] or ""
        match = re.search(r"PriceFull(\d{13})-", filename, re.I)
        if store and not store[7] and match and (row["price_items"] or 0) > 0:
            store[6:8] = [row["price_file_time"], match[1]]
    write(out / "national-directory.json", {"day": DAY, "stores": directory})
    print("National directory:", len(directory), flush=True)

    return directory


def build(out):
    out.mkdir(exist_ok=True, parents=True)
    directory = build_directory(out)
    backup = defaultdict(dict)
    products = {}
    for code in CODES:
        for row in pages("prices_market", item_code=code, order="chain,sub_chain_id,store_id",
                         columns="chain,sub_chain_id,store_id,item_code,item_name,item_price,as_of,is_weighted"):
            sid = key(row)
            if str(row["item_code"]) != code or sid not in directory or str(row["is_weighted"]).lower() not in ("0","false"):
                continue
            date = str(row["as_of"] or "")[:10]
            cents = round(float(row["item_price"] or 0)*100)
            if cents > 0 and date:
                products[code] = row["item_name"]
                backup[sid][code] = [cents, date, "over"]
    # PriceIL is a separate supplier. Map its private numeric IDs via official
    # chain/store numbers AND an identical nonempty street address.
    by_address = defaultdict(list)
    for sid, store in directory.items():
        if store[7] and text(store[2]) and sid.split("|")[2].isdigit():
            by_address[(store[7], int(sid.split("|")[2]), text(store[2]))].append(sid)
    priceil_ids = {}
    def priceil(path):
        time.sleep(3.2)  # anonymous public allowance is 20 requests per minute
        return get("https://api.priceil.dev" + path)["data"]
    try:
        page = 1
        while True:
            data = priceil("/stores?" + urlencode(dict(page=page, limit=100)))
            for store in data["items"]:
                if not str(store.get("storeId")).isdigit():
                    continue
                matches = by_address.get((str(store.get("chainId")), int(store["storeId"]), text(store["address"])), [])
                if len(matches) == 1:
                    priceil_ids[store["id"]] = matches[0]
            if page * data["limit"] >= data["total"] or not data["items"]:
                break
            page += 1
        for code in CODES:
            data = priceil("/products/" + code + "/prices")
            if str((data.get("product") or {}).get("itemCode")) != code:
                continue
            for row in data["prices"]:
                sid = priceil_ids.get(row.get("storeId"))
                date = str(row.get("priceUpdateDate") or "")[:10]
                if not sid or code in backup[sid] or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    continue
                cents = round(float(row["price"] or 0)*100)
                if cents > 0:
                    products.setdefault(code, data["product"]["itemName"])
                    backup[sid][code] = [cents, date, "priceil"]
    except (OSError, ValueError, KeyError) as error:
        print("PriceIL temporarily unavailable:", type(error).__name__, flush=True)
    if not products or not backup:
        raise RuntimeError("Empty national backup")
    write(out / "national-backup.json", {"day": DAY, "products": products, "stores": backup})
    print("Backup:",len(products),"products;",len(backup),"branches;",sum(len(b) for b in backup.values()),"quotes",flush=True)


if __name__ == "__main__":
    try:
        build(Path("site"))
    except (OSError, ValueError, RuntimeError) as error:
        print("Retaining previous national publication:", type(error).__name__, flush=True)
        for file in ("national-directory.json", "national-backup.json"):
            previous = get(PAGES + file)
            if not previous.get("day") or not previous.get("stores"):
                raise RuntimeError("Missing previous national publication")
            write(Path("site", file), previous)
