import html
import re
from urllib.parse import urlsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

PORTAL = 'https://prices.shufersal.co.il/FileObject/UpdateCategory'
CHAIN = '7290027600007'
TZ = ZoneInfo('Asia/Jerusalem')


def latest_file(store):
    with urlopen(f'{PORTAL}?catID=2&storeId={store}', timeout=30) as response:
        page = response.read().decode('utf-8-sig')
    candidates = {}
    for link in re.findall(r'href="([^"]+)"', page):
        link = html.unescape(link)
        url = urlsplit(link)
        match = re.fullmatch(rf'/pricefull/(PriceFull{CHAIN}-\d+-{store:03d}-(\d{{8}}-\d{{6}})\.gz)', url.path, re.I)
        if match and url.scheme == 'https' and url.hostname == 'pricesprodpublic.blob.core.windows.net':
            candidates[match[2]] = (match[1], link)
    if not candidates:
        raise ValueError(f'No full price file listed for store {store}')
    return candidates[max(candidates)]


