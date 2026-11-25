"""Restore the last publication, refresh each branch, and build the Pages data."""
import gzip
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from import_prices import publish

BASE = 'https://meniviner.github.io/mezol-ashkelon-feed/'
BUNDLED = 'https://me-zol.web.app/ashkelon.json'


def fetch(url):
    with urlopen(Request(url, headers={'User-Agent': 'MeZol-price-import/1.0'}), timeout=45) as r:
        return r.read()


def previous_data():
    try:
        manifest = json.loads(fetch(BASE + 'latest.json'))
    except HTTPError as exc:
        if exc.code != 404:
            raise
        return fetch(BUNDLED)
    digest = manifest['sha256']
    data = fetch(BASE + 'catalog-' + digest + '.json')
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Previous publication failed its checksum')
    return data


def build():
    with TemporaryDirectory() as temp:
        catalog = Path(temp, 'catalog.gz')
        catalog.write_bytes(gzip.compress(previous_data(), mtime=0))
        problems = publish(catalog)
        data = gzip.decompress(catalog.read_bytes())
    digest = hashlib.sha256(data).hexdigest()
    output = Path('site')
    output.mkdir(exist_ok=True)
    (output / ('catalog-' + digest + '.json')).write_bytes(data)
    manifest = {'sha256': digest, 'checkedAt': max(s['checkedAt'] for s in json.loads(data)['stores'])}
    (output / 'latest.json').write_text(json.dumps(manifest))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            print('partial=' + str(bool(problems)).lower(), file=f)
            print('revision=' + digest, file=f)


if __name__ == '__main__':
    build()
