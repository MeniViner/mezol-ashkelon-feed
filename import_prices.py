"""Build the list of Ashkelon branches and their packaged product prices."""
import argparse
import gzip
import html
import json
import math
import re
import time
from urllib.error import URLError
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from shufersal_source import CHAIN, PORTAL, TZ, latest_file

CARREFOUR = '7290055700007'
BASE = 'https://prices.carrefour.co.il/'
TARGET = Path('ashkelon.json.gz')


def retry(call, arg):
    for attempt in range(3):
        try:
            return call(arg)
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def download(url):
    def read(address):
        with urlopen(address, timeout=20) as response:
            return response.read()
    return retry(read, url)


def shufersal_directory():
    listing = download(PORTAL + '?catID=5&storeId=0').decode('utf-8-sig')
    urls = [html.unescape(url) for url in re.findall(r'href="([^"]+)"', listing)
            if url.startswith('https://pricesprodpublic.blob.core.windows.net/stores/Stores' + CHAIN)]
    return ET.fromstring(gzip.decompress(download(max(urls)))), latest_file


def carrefour_directory():

    page = download(BASE).decode('utf-8-sig')
    day = re.search(r"const path = '(\d{8})'", page)[1]
    files = json.JSONDecoder().raw_decode(page.split('const files = ', 1)[1])[0]
    names = [f['name'] for f in files]
    root_name = max(n for n in names if re.fullmatch(r'Stores' + CARREFOUR + r'-\d+-\d{8}-\d{6}\.xml', n))
    def price_file(number):
        names_for_store = [n for n in names if re.fullmatch(rf'PriceFull{CARREFOUR}-\d+-{number:03d}-\d{{8}}-\d{{6}}\.gz', n)]
        name = max(names_for_store, key=lambda n: n[-18:-3])
        return name, BASE + day + '/' + name
    return ET.fromstring(download(BASE + day + '/' + root_name)), price_file


def retail_code(code):
    if len(code) not in (8, 12, 13, 14):
        return False
    # Locally assigned variable-weight and retailer codes must stay chain-specific.
    if code.startswith(('02', '2')):
        return False
    return (sum(int(ch) * (3 if i % 2 == 0 else 1)
                for i, ch in enumerate(reversed(code[:-1]))) + int(code[-1])) % 10 == 0


def branch_directory(chain, root):
    if root.findtext('ChainID') != chain:
        raise ValueError('Unexpected chain in directory')
    branches = {}
    for sub in root.findall('.//SubChain'):
        for entry in sub.findall('.//Store'):
            if entry.findtext('City', '').strip() not in ('7100', 'אשקלון'):
                continue
            number = int(entry.findtext('StoreID'))
            store_id = (100000 if chain == CHAIN else 200000) + number
            parent = 'שופרסל' if chain == CHAIN else 'קרפור'
            if number in branches:
                raise ValueError('Duplicate branch identity')
            branches[number] = dict(id=store_id, name=' '.join(entry.findtext('StoreName').split()),
                                    chain=(sub.findtext('SubChainName') if chain == CHAIN else parent) or parent,
                                    parent=parent, city='אשקלון', address=(entry.findtext('Address') or '').strip() + ', אשקלון')
    if not branches:
        raise ValueError('Empty Ashkelon directory')
    return branches


class IdentityMismatch(ValueError):
    pass


def price_rows(chain, number, url):
    root = ET.fromstring(gzip.decompress(download(url)))
    if root.findtext('ChainID') != chain or int(root.findtext('StoreID')) != number:
        raise IdentityMismatch('Unexpected price file identity')
    items = {}
    for item in root.iter('Item'):
        def field(key):
            return (item.findtext(key) or '').strip()
        code, title = field('ItemCode'), field('ItemName')
        if not code.isdigit() or not title or field('bIsWeighted') != '0':
            continue
        price = float(field('ItemPrice'))
        if not math.isfinite(price) or price <= 0:
            continue
        key = code if field('ItemType') == '1' and retail_code(code) else chain + '-' + code
        if key in items:
            raise ValueError('Duplicate price')
        items[key] = [title, float(field('Quantity') or 0), field('UnitQty'), field('ManufactureName'),
                      round(price * 100), field('PriceUpdateTime')[:10]]
    if not items:
        raise ValueError('Empty full price file')
    return items


def publish(target=TARGET):
    previous = json.loads(gzip.decompress(target.read_bytes())) if target.exists() else {'stores': [], 'rows': []}
    old = {s['id']: s for s in previous['stores']}
    by_branch = {sid: {} for sid in old}
    for key, name, qty, unit, maker, quotes in previous['rows']:
        for sid, price, date in quotes:
            by_branch[sid][key] = [name, qty, unit, maker, price, date]

    stores = {}
    problems = []
    checked = datetime.now(TZ).isoformat(timespec='seconds')
    errors = (URLError, OSError, TimeoutError, EOFError, ValueError, TypeError, KeyError, IndexError, ET.ParseError)
    def retain(branch, reason):
        sid = branch['id']
        store = {**old.get(sid, {}), **branch, 'checkedAt': checked}
        if store.get('verifiedAt'):
            store['refreshWarning'] = reason
        else:
            store['unavailable'] = reason
        stores[sid] = store

    for chain, directory in [(CHAIN, shufersal_directory), (CARREFOUR, carrefour_directory)]:
        parent = 'שופרסל' if chain == CHAIN else 'קרפור'
        try:
            root, locate = directory()
            branches = branch_directory(chain, root)
        except errors as exc:
            problems.append(f'{parent} directory ({type(exc).__name__})')
            for branch in old.values():
                if branch['parent'] == parent:
                    retain(branch, 'מדריך הרשת לא היה זמין בבדיקה האחרונה')
            continue
        observed = {b['id'] for b in branches.values()}
        for branch in old.values():
            if branch['parent'] == parent and branch['id'] not in observed:
                retain(branch, 'הסניף חסר במדריך הרשת האחרון')
                problems.append(f"{branch['id']} absent from directory")
        for number, branch in branches.items():
            sid = branch['id']
            prior = old.get(sid, {})
            try:
                filename, url = retry(locate, number)
                stamp = datetime.strptime(filename[-18:-3], '%Y%m%d-%H%M%S').replace(tzinfo=TZ).isoformat()
                if stamp < prior.get('verifiedAt', ''):
                    raise ValueError('Older full file')
                if prior.get('file') != filename or not by_branch.get(sid):
                    by_branch[sid] = price_rows(chain, number, url)
                stores[sid] = {**branch, 'verifiedAt': stamp, 'file': filename,
                               'productCount': len(by_branch[sid]), 'checkedAt': checked}
            except IdentityMismatch:
                retain(branch, 'מזהה הסניף במחירון שונה מהמזהה במדריך הרשת')
                if prior.get('verifiedAt'):
                    problems.append(f'{sid} identity mismatch')
            except errors as exc:
                retain(branch, 'לא ניתן היה לאמת מחירון בבדיקה האחרונה')
                problems.append(f'{sid} ({type(exc).__name__})')
    if {s['parent'] for s in stores.values() if s.get('verifiedAt')} != {'שופרסל', 'קרפור'}:
        raise ValueError('No usable baseline for both retailers')
    rows = {}
    for sid, store in stores.items():
        if not store.get('verifiedAt'):
            continue
        for key, (title, qty, unit, maker, price, date) in by_branch[sid].items():
            row = rows.setdefault(key, [key, title, qty, unit, maker, []])
            row[-1].append([sid, price, date])
    payload = dict(stores=list(stores.values()), rows=list(rows.values()))
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, delete=False) as output:
        output.write(gzip.compress(data, mtime=0))
    Path(output.name).replace(target)
    print(f'{len(stores)} branches, {len(rows)} codes. Checked {checked}', flush=True)
    for problem in problems:
        print('Using previous data where available:', problem)
    return problems


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=TARGET)
    args = parser.parse_args()
    raise SystemExit(2 if publish(args.output) else 0)
