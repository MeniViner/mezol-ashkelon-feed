"""Read only the Carrefour XML files needed from the public Over archive."""
import io
import json
from pathlib import PurePosixPath
from urllib.request import Request, urlopen
from zipfile import ZipFile

API = 'https://www.over.org.il/api/'
DATASET = '2e0a7e7b-2bbb-4bbe-8e13-d87c90bca124'


class RangeReader(io.RawIOBase):
    """Seekable view of an immutable ZIP on an HTTP range server."""
    def __init__(self, url):
        with urlopen(Request(url, headers={'Range': 'bytes=0-0'}), timeout=25) as res:
            if res.status != 206:
                raise OSError('Archive does not serve ranges')
            self.length = int(res.headers['Content-Range'].split('/')[-1])
            self.url = res.geturl()
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        position = (0, self.pos, self.length)[whence] + offset
        if position < 0:
            raise OSError('Invalid seek')
        self.pos = position
        return self.pos

    def readinto(self, buffer):
        n = min(len(buffer), self.length - self.pos)
        if n <= 0:
            return 0
        end = self.pos + n - 1
        with urlopen(Request(self.url, headers={'Range': f'bytes={self.pos}-{end}'}), timeout=40) as res:
            if res.status != 206 or res.headers.get('Content-Range') != f'bytes {self.pos}-{end}/{self.length}':
                raise OSError('Incomplete archive range')
            data = res.read(n)
        if len(data) != n:
            raise EOFError('Incomplete archive bytes')
        buffer[:n] = data
        self.pos += n
        return n


def archived_files():
    with urlopen(API + 'v1/datasets/' + DATASET + '/versions/latest', timeout=25) as res:
        version = json.load(res)
    handles = {}
    for index, resource in enumerate(r for r in version['resources'] if r['name'] == '_zip_parts'):
        stream = RangeReader(API + 'versions/' + version['id'] + f'/download/_zip_parts?index={index}')
        archive = ZipFile(io.BufferedReader(stream, buffer_size=64 * 1024))
        for info in archive.infolist():
            name = PurePosixPath(info.filename).name
            if name.startswith(('PriceFull7290055700007-', 'Stores7290055700007-')):
                handles[name] = (archive, info)
    def read(name):
        archive, entry = handles[name]
        return archive.read(entry)
    return list(handles), read
