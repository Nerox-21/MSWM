"""
musicid: a tiny service for the Telegram bot.

  POST /recognize     body = raw audio bytes   -> {"found": true, "title", "artist", "cover", "url"}   (uses ShazamIO, free, no key)
  GET  /find?q=...                             -> an mp3 file (X-Title / X-Artist headers)               (uses yt-dlp, SoundCloud search)
  GET  /health                                 -> ok

Set the variable KEY (any long random text) and give the same text to the bot as MUSICID_KEY.
Only the Python standard library is used for the web part, so there is nothing else to install except shazamio and yt-dlp.
"""
import asyncio, glob, json, os, re, shutil, tempfile, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = os.environ.get('KEY', '').strip()
PORT = int(os.environ.get('PORT', '8080'))
MAX_REC = 8 * 1024 * 1024            # audio sent for recognition
MAX_DL = 25 * 1024 * 1024            # biggest mp3 we will hand back
MAX_SEC = 12 * 60                    # longest track we accept (mixes and podcasts are skipped)
MIN_SEC = 60
SOURCES = [s.strip() for s in os.environ.get('SOURCES', 'scsearch').split(',') if s.strip()]
SLOT = threading.Semaphore(int(os.environ.get('PARALLEL', '2')))   # protects the small free server from too many jobs at once

BAD = ('remix', 'cover', 'slowed', 'sped up', 'speed up', 'reverb', 'karaoke', 'instrumental', 'live', 'nightcore', '8d', 'mashup', 'bass boosted', 'lofi', 'lo-fi', 'edit')
words = lambda s: [w for w in re.split(r'[^\w]+', (s or '').lower(), flags=re.UNICODE) if len(w) > 1]


def pick(entries, q):
    """Choose the best search result for the query (pure function, easy to test)."""
    qw = set(words(q)); qlow = (q or '').lower(); best, best_score = None, -1e9
    for e in entries or []:
        dur = e.get('duration') or 0
        if dur and (dur < MIN_SEC or dur > MAX_SEC):
            continue
        text = ((e.get('title') or '') + ' ' + (e.get('uploader') or '')).lower()
        hit = sum(1 for w in qw if w in text)
        score = hit * 10 - (len(qw) - hit) * 4
        for b in BAD:
            if b in text and b not in qlow:
                score -= 6
        if 150 <= dur <= 330:
            score += 2
        if score > best_score:
            best, best_score = e, score
    if best is not None and qw and best_score < len(qw) * 3:      # nothing really matches the words
        return None
    return best


# ---------- recognition ----------
def recognize_bytes(data):
    from shazamio import Shazam

    async def run():
        sh = Shazam()
        fn = getattr(sh, 'recognize', None) or getattr(sh, 'recognize_song')
        return await fn(data)
    out = asyncio.run(run())
    tr = (out or {}).get('track')
    if not tr:
        return {'found': False}
    return {
        'found': True, 'title': tr.get('title') or '', 'artist': tr.get('subtitle') or '',
        'cover': ((tr.get('images') or {}).get('coverart') or ''), 'url': tr.get('url') or ''
    }


# ---------- full song ----------
def find_audio(q):
    import yt_dlp
    tmp = tempfile.mkdtemp(prefix='mid_')
    try:
        chosen = None
        for src in SOURCES:
            try:
                with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True, 'extract_flat': True, 'socket_timeout': 15}) as y:
                    info = y.extract_info(src + '8:' + q, download=False)
                chosen = pick((info or {}).get('entries'), q)
            except Exception:
                chosen = None
            if chosen:
                break
        if not chosen:
            return None
        url = chosen.get('url') or chosen.get('webpage_url')
        opts = {
            'quiet': True, 'noplaylist': True, 'socket_timeout': 20, 'format': 'bestaudio/best', 'max_filesize': MAX_DL,
            'outtmpl': os.path.join(tmp, 'a.%(ext)s'),
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'}]
        }
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
        files = glob.glob(os.path.join(tmp, 'a.mp3'))
        if not files or os.path.getsize(files[0]) < 20000 or os.path.getsize(files[0]) > MAX_DL:
            return None
        with open(files[0], 'rb') as f:
            data = f.read()
        return {'data': data, 'title': chosen.get('title') or '', 'artist': chosen.get('uploader') or '', 'duration': chosen.get('duration') or 0}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- web ----------
class H(BaseHTTPRequestHandler):
    server_version = 'musicid'

    def log_message(self, *a):
        pass

    def send(self, code, body=b'', ctype='application/json', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        return (not KEY) or self.headers.get('X-Key', '') == KEY

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ('/', '/health'):
            return self.send(200, b'ok', 'text/plain')
        if not self.authed():
            return self.send(401, {'error': 'key'})
        if u.path == '/find':
            q = (urllib.parse.parse_qs(u.query).get('q') or [''])[0].strip()[:120]
            if len(q) < 2:
                return self.send(400, {'error': 'q'})
            if not SLOT.acquire(timeout=60):
                return self.send(503, {'error': 'busy'})
            try:
                r = find_audio(q)
            except Exception as e:
                return self.send(500, {'error': str(e)[:120]})
            finally:
                SLOT.release()
            if not r:
                return self.send(404, {'error': 'none'})
            return self.send(200, r['data'], 'audio/mpeg', {
                'X-Title': urllib.parse.quote(r['title']), 'X-Artist': urllib.parse.quote(r['artist']), 'X-Duration': str(r['duration'])})
        return self.send(404, {'error': 'not found'})

    def do_POST(self):
        if not self.authed():
            return self.send(401, {'error': 'key'})
        if self.path.split('?')[0] != '/recognize':
            return self.send(404, {'error': 'not found'})
        n = int(self.headers.get('Content-Length') or 0)
        if n < 2000 or n > MAX_REC:
            return self.send(400, {'error': 'size'})
        data = self.rfile.read(n)
        if not SLOT.acquire(timeout=60):
            return self.send(503, {'error': 'busy'})
        try:
            return self.send(200, recognize_bytes(data))
        except Exception as e:
            return self.send(500, {'error': str(e)[:120]})
        finally:
            SLOT.release()


if __name__ == '__main__':
    print('musicid on :%d (key %s)' % (PORT, 'set' if KEY else 'NOT set'), flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
