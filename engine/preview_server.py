"""The local preview server behind `./publish.sh --serve`.

Python's own `http.server` would do, except for one thing: it answers a Range
request with `200 OK` and the whole file. Browsers treat that as "this server
cannot do partial content" and refuse to seek within audio or video - so an
episode page previewed through it has a play button that works and a timeline
that does nothing. The player looks broken while being perfectly correct, and
the bug appears to be in the page rather than in the server serving it.

Apache honours Range, so this only ever bites locally, which is worse: it bites
exactly when someone is checking whether their player works.

Everything else is stdlib `SimpleHTTPRequestHandler`. Bound to 127.0.0.1 by the
caller - see publish.sh - because an unpublished site is nobody else's business.
"""

import os
import re
import shutil
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')


class _Bounded:
    """A read-only view of `length` bytes of an already-positioned file.

    shutil.copyfileobj reads to EOF, which for a range response would send the
    rest of the file after the part that was asked for. Rather than reimplement
    the copy, hand it something that reports EOF at the end of the range.
    """

    def __init__(self, stream, length):
        self._stream = stream
        self._left = length

    def read(self, size=-1):
        if self._left <= 0:
            return b''
        if size is None or size < 0 or size > self._left:
            size = self._left
        data = self._stream.read(size)
        self._left -= len(data)
        return data

    def close(self):
        self._stream.close()


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that actually answers Range requests."""

    server_version = "UnpromptedPreview/1.0"

    def send_head(self):
        header = self.headers.get('Range')
        if not header:
            return super().send_head()

        match = _RANGE_RE.match(header.strip())
        if not match:
            # A multi-range or malformed request. Serving the whole file is the
            # spec's own advice for a range you will not honour, and it is what
            # every client copes with.
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            stream = open(path, 'rb')
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        try:
            size = os.fstat(stream.fileno()).st_size
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:
                # bytes=-N - the final N bytes. Players use this to read a
                # trailing metadata atom before deciding how to stream.
                start = max(0, size - int(last))
                end = size - 1
            else:
                return super().send_head()

            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                stream.close()
                return None

            stream.seek(start)
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header('Content-Type', self.guess_type(path))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', str(length))
            self.send_header('Last-Modified', self.date_time_string(
                int(os.fstat(stream.fileno()).st_mtime)))
            self.end_headers()
            return _Bounded(stream, length)
        except Exception:
            stream.close()
            raise

    def end_headers(self):
        # Advertise the capability on every response, so a player knows it can
        # seek before it tries.
        if 'Accept-Ranges' not in self._headers_buffer_names():
            self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def _headers_buffer_names(self):
        return b''.join(getattr(self, '_headers_buffer', [])).decode(
            'latin-1', 'replace')

    def copyfile(self, source, outputfile):
        shutil.copyfileobj(source, outputfile)

    def log_message(self, fmt, *args):
        # One line per request is noise when a page pulls forty assets; keep
        # errors only, which is what someone previewing actually wants to see.
        status = args[1] if len(args) > 1 else ''
        if str(status).startswith(('4', '5')):
            sys.stderr.write("   %s %s\n" % (self.address_string(), fmt % args))


def serve(directory, port, host='127.0.0.1'):
    handler = partial(RangeRequestHandler, directory=directory)
    with ThreadingHTTPServer((host, port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python3 engine/preview_server.py <port> <directory>")
        sys.exit(1)
    serve(sys.argv[2], int(sys.argv[1]))
