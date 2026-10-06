"""BIOSWARE CINEMA: read-only media server, Python 3.11+ standard library."""
import json, mimetypes, os, re, shutil, sqlite3, subprocess, threading, time, secrets, socket
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs
import hls
import thumbnails
from contextlib import contextmanager

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / 'config.json').read_text('utf-8'))
ROOTS = [Path(p).resolve() for p in CONFIG['libraries']]
DATA = BASE / 'database'; DATA.mkdir(exist_ok=True)
DB = DATA / 'cinema.sqlite3'
EXTS = set('.mp4 .mkv .avi .mov .m4v .webm .mpg .mpeg .ts .m2ts .wmv'.split())
FFPROBE = hls.find_tool('ffprobe',BASE,CONFIG.get('ffprobe',''))
FFMPEG = hls.find_tool('ffmpeg',BASE,CONFIG.get('ffmpeg',''))
STREAMS = None
START = time.time(); TOKEN = secrets.token_urlsafe(32)
SCAN = {'running': False, 'new': 0, 'errors': [], 'last': None}
LOCK = threading.Lock()

@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row
    try:
        with c:yield c
    finally:c.close()

def initialize():
    with db() as c:
        c.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS movies (
          id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, title TEXT, year INTEGER,
          filename TEXT, relative_path TEXT, source TEXT, extension TEXT, size INTEGER,
          duration REAL, resolution TEXT, video_codec TEXT, audio_codec TEXT,
          added REAL, scanned REAL, missing INTEGER DEFAULT 0, poster TEXT, backdrop TEXT,
          metadata TEXT DEFAULT '{}', progress REAL DEFAULT 0, watched INTEGER DEFAULT 0,
          last_watched REAL DEFAULT 0, favorite INTEGER DEFAULT 0, mtime REAL);
        CREATE INDEX IF NOT EXISTS movie_title ON movies(title COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS movie_added ON movies(added);
        CREATE INDEX IF NOT EXISTS movie_watched ON movies(last_watched);
        CREATE TABLE IF NOT EXISTS subtitles(id INTEGER PRIMARY KEY, movie_id INTEGER,
          path TEXT UNIQUE, language TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        ''')
        row = c.execute("SELECT value FROM settings WHERE key='last_scan'").fetchone()
        if row: SCAN['last'] = float(row[0])
        columns={r[1] for r in c.execute('PRAGMA table_info(movies)')}
        for name,kind in {'container':'TEXT','bitrate':'INTEGER','pixel_format':'TEXT','video_level':'INTEGER','probed':'REAL'}.items():
            if name not in columns:c.execute(f'ALTER TABLE movies ADD COLUMN {name} {kind}')

def validated(path):
    p = Path(path).resolve(strict=True)
    if not p.is_file() or not any(p.is_relative_to(r) for r in ROOTS):
        raise PermissionError('Outside approved libraries')
    return p

def clean_name(filename):
    s = Path(filename).stem.replace('_', ' ').replace('.', ' ').replace('+', ' ')
    years = list(re.finditer(r'\b(?:19\d{2}|20[0-3]\d)\b', s))
    y = years[-1] if years else None
    title = s[:y.start()] if y and y.start() > 0 else s
    title = re.split(r'\b(?:2160p|1080p|720p|480p|BluRay|WEB-DL|WEBRip|DVDRip|x264|x265)\b', title, flags=re.I)[0]
    return title.strip(' -[]()') or Path(filename).stem, int(y[0]) if y else None

def probe(p):
    return hls.inspect(FFPROBE,p)

def scan():
    if not LOCK.acquire(False): return
    SCAN.update(running=True, new=0, errors=[])
    stamp = time.time()
    try:
        for root in ROOTS:
            if not root.is_dir():
                SCAN['errors'].append(f'Unavailable library: {root}'); continue
            complete = True
            def walk_error(e):
                nonlocal complete
                complete = False
                if len(SCAN['errors']) < 20: SCAN['errors'].append(f'Cannot read a folder in {root}')
            for folder, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                subtitle_names = [n for n in files if Path(n).suffix.lower() in ('.srt','.vtt')]
                dirs[:] = [d for d in dirs if not (Path(folder)/d).is_symlink() and not os.path.isjunction(Path(folder)/d)] if hasattr(os.path,'isjunction') else dirs
                for name in files:
                    p = Path(folder)/name
                    if p.suffix.lower() not in EXTS: continue
                    try:
                        p = validated(p); st = p.stat(); title, year = clean_name(name)
                        with db() as c:
                            old = c.execute('SELECT id,mtime,probed FROM movies WHERE path=?',(str(p),)).fetchone()
                        info = probe(p) if not old or old['mtime'] != st.st_mtime or not old['probed'] else {}
                        with db() as c:
                            c.execute('''INSERT INTO movies(path,title,year,filename,relative_path,source,extension,size,added,scanned,mtime)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,scanned=excluded.scanned,missing=0,mtime=excluded.mtime''',
                              (str(p),title,year,name,str(p.relative_to(root)),str(root),p.suffix.lower(),st.st_size,stamp,stamp,st.st_mtime))
                            mid = c.execute('SELECT id FROM movies WHERE path=?',(str(p),)).fetchone()[0]
                            for key,value in info.items(): c.execute(f'UPDATE movies SET {key}=? WHERE id=?',(value,mid))
                            c.execute('DELETE FROM subtitles WHERE movie_id=?',(mid,))
                            for subname in subtitle_names:
                                sub = Path(folder)/subname
                                if sub.suffix.lower() not in ('.srt','.vtt'): continue
                                if sub.stem.casefold() == p.stem.casefold() or sub.stem.casefold().startswith(p.stem.casefold()+'.'):
                                    validated(sub)
                                    lang = sub.stem[len(p.stem):].strip('.') or 'Default'
                                    c.execute('INSERT OR IGNORE INTO subtitles(movie_id,path,language) VALUES(?,?,?)',(mid,str(sub),lang))
                        if not old: SCAN['new'] += 1
                    except (OSError, ValueError):
                        if len(SCAN['errors']) < 20: SCAN['errors'].append(f'Could not index: {name}')
            if complete:
                with db() as c: c.execute('UPDATE movies SET missing=1 WHERE source=? AND scanned<?',(str(root),stamp))
        SCAN['last'] = stamp
        with db() as c: c.execute("INSERT OR REPLACE INTO settings VALUES('last_scan',?)",(str(stamp),))
    finally:
        SCAN['running'] = False; LOCK.release()

def public(row):
    x = dict(row); x.pop('path'); x.pop('mtime',None)
    try: x['metadata'] = json.loads(x['metadata'])
    except ValueError: x['metadata'] = {}
    x['direct_play_compatible']=hls.compatible(row)
    return x

def byte_range(header, size):
    if not header: return 0, size-1, 200
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not m or not any(m.groups()) or size == 0: raise ValueError()
    a,b = m.groups()
    if not a:
        n = int(b)
        if n <= 0: raise ValueError()
        return max(0,size-n),size-1,206
    start = int(a); end = min(int(b),size-1) if b else size-1
    if start >= size or end < start: raise ValueError()
    return start,end,206

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def send(self, body, status=200, mime='application/json', extra=None):
        if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control',(extra or {}).get('Cache-Control','no-store'))
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        for k,v in (extra or {}).items():
            if k != 'Cache-Control':self.send_header(k,v)
        self.end_headers()
        if self.command != 'HEAD':
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        try: self.get()
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError): pass
        except (FileNotFoundError, PermissionError): self.send({'error':'Media unavailable'},404)
        except hls.StreamError as e:self.send({'error':str(e)},503)
        except Exception: self.send({'error':'Request failed'},500)
    def get(self):
        u=urlsplit(self.path); q=parse_qs(u.query)
        thumb=re.fullmatch(r'/api/movies/(\d+)/thumbnail',u.path)
        if thumb:
            with db() as c:row=c.execute('SELECT * FROM movies WHERE id=?',(thumb[1],)).fetchone()
            if not row:return self.send({'error':'Movie not found'},404)
            body,mime,cached=thumbnails.preview(BASE,FFMPEG,row,validated)
            return self.send(body,mime=mime,extra={'Cache-Control':'private, max-age=86400' if cached else 'no-store'})
        if u.path == '/api/status':
            with db() as c:
                stats=dict(c.execute('SELECT count(*) AS movies,coalesce(sum(size),0) AS size,sum(missing) AS missing,sum(probed IS NOT NULL) AS inspected FROM movies').fetchone())
            return self.send(dict(**stats, **STREAMS.stats(),scan=SCAN, ffmpeg=bool(FFMPEG),ffprobe=bool(FFPROBE),uptime=int(time.time()-START),libraries=[str(r) for r in ROOTS],token=TOKEN))
        stream=re.fullmatch(r'/api/playback/([a-f0-9]{32})/(index.m3u8|\d+\.ts)',u.path)
        if stream:
            if stream[2]=='index.m3u8':return self.send(STREAMS.manifest(stream[1]),mime='application/vnd.apple.mpegurl')
            return self.send(STREAMS.segment(stream[1],int(stream[2][:-3]),validated),mime='video/mp2t')
        if u.path == '/api/movies':
            term=q.get('q',[''])[0][:200]; view=q.get('view',['all'])[0]
            try: offset=max(0,int(q.get('offset',['0'])[0])); limit=min(100,max(1,int(q.get('limit',['48'])[0])))
            except ValueError: return self.send({'error':'Invalid pagination'},400)
            where='missing=0'; params=[]
            if term: where+=' AND (title LIKE ? OR filename LIKE ? OR CAST(year AS TEXT) LIKE ? OR metadata LIKE ?)'; params += ['%'+term+'%']*4
            if view=='favorites': where+=' AND favorite=1'
            if view=='continue': where+=' AND progress>0 AND watched=0'
            order='last_watched DESC' if view=='continue' else 'added DESC,id DESC' if view=='recent' else 'title COLLATE NOCASE'
            with db() as c:
                total=c.execute('SELECT count(*) FROM movies WHERE '+where,params).fetchone()[0]
                rows=c.execute('SELECT * FROM movies WHERE '+where+' ORDER BY '+order+' LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
            return self.send({'items':[public(r) for r in rows], 'total':total})
        m=re.fullmatch(r'/api/movies/(\d+)(?:/(stream|subtitles))?',u.path)
        if m:
            with db() as c: row=c.execute('SELECT * FROM movies WHERE id=?',(m[1],)).fetchone()
            if not row: return self.send({'error':'Movie not found'},404)
            if m[2]=='stream': return self.stream(validated(row['path']))
            if m[2]=='subtitles':
                with db() as c: rows=c.execute('SELECT id,language FROM subtitles WHERE movie_id=?',(m[1],)).fetchall()
                return self.send([dict(r) for r in rows])
            return self.send(public(row))
        m=re.fullmatch(r'/api/subtitles/(\d+)',u.path)
        if m:
            with db() as c: row=c.execute('SELECT path FROM subtitles WHERE id=?',(m[1],)).fetchone()
            if not row: return self.send({'error':'Subtitle not found'},404)
            p=validated(row[0])
            if p.stat().st_size>8*1024*1024: return self.send({'error':'Subtitle too large'},413)
            raw=p.read_bytes()
            try: txt=raw.decode('utf-8-sig')
            except UnicodeDecodeError: txt=raw.decode('cp1250',errors='replace')
            if p.suffix.lower()=='.srt': txt='WEBVTT\n\n'+re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})',r'\1.\2',txt)
            return self.send(txt.encode(),mime='text/vtt; charset=utf-8')
        files={'/':'index.html','/app.js':'app.js','/player.js':'player.js','/vendor/hls.min.js':'vendor/hls.min.js','/style.css':'style.css'}
        if u.path in files:
            p=BASE/'frontend'/files[u.path]
            return self.send(p.read_bytes(),mime={'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css'}[p.suffix[1:]])
        return self.send({'error':'Not found'},404)
    def stream(self,p):
        with p.open('rb') as f:
            size=os.fstat(f.fileno()).st_size
            try: start,end,status=byte_range(self.headers.get('Range'),size)
            except ValueError: return self.send(b'',416,extra={'Content-Range':f'bytes */{size}','Accept-Ranges':'bytes'})
            self.send_response(status)
            mime={'.mkv':'video/x-matroska','.m4v':'video/mp4','.ts':'video/mp2t','.m2ts':'video/mp2t','.avi':'video/x-msvideo'}.get(p.suffix.lower(),mimetypes.guess_type(p)[0] or 'application/octet-stream')
            self.send_header('Content-Type',mime); self.send_header('Accept-Ranges','bytes'); self.send_header('Content-Length',str(max(0,end-start+1)))
            self.send_header('X-Content-Type-Options','nosniff')
            if status==206: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command=='HEAD': return
            f.seek(start); remaining=end-start+1
            while remaining>0:
                chunk=f.read(min(256*1024,remaining))
                if not chunk: break
                self.wfile.write(chunk); remaining-=len(chunk)
    def do_POST(self):
        try:
            if self.headers.get('X-Cinema-Token') != TOKEN: return self.send({'error':'Refresh the app before changing settings'},403)
            length=int(self.headers.get('Content-Length','0'))
            if length<0 or length>4096: self.close_connection=True; return self.send({'error':'Request too large'},413)
            payload=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(payload,dict):raise ValueError()
            playmatch=re.fullmatch(r'/api/movies/(\d+)/playback',self.path)
            if playmatch:
                with db() as c:row=c.execute('SELECT * FROM movies WHERE id=?',(playmatch[1],)).fetchone()
                if not row:return self.send({'error':'Movie not found'},404)
                row=dict(row);p=validated(row['path'])
                if not row.get('probed') or p.stat().st_mtime!=row['mtime']:
                    info=probe(p)
                    with db() as c:
                        for k,v in info.items():c.execute(f'UPDATE movies SET {k}=? WHERE id=?',(v,row['id']))
                    row.update(info)
                return self.send(STREAMS.create(row,payload.get('quality','auto'),payload.get('previous')))
            control=re.fullmatch(r'/api/playback/([a-f0-9]{32})/(stop|heartbeat)',self.path)
            if control:
                if control[2]=='stop':STREAMS.stop(control[1])
                else:STREAMS.get(control[1])
                return self.send({'ok':True})
            if self.path=='/api/scan':
                threading.Thread(target=scan,daemon=True).start(); return self.send({'started':True},202)
            m=re.fullmatch(r'/api/movies/(\d+)/(favorite|progress)',self.path)
            if not m: return self.send({'error':'Not found'},404)
            with db() as c:
                row=c.execute('SELECT duration FROM movies WHERE id=?',(m[1],)).fetchone()
                if not row: return self.send({'error':'Not found'},404)
                if m[2]=='favorite': c.execute('UPDATE movies SET favorite=? WHERE id=?',(int(bool(payload.get('favorite'))),m[1]))
                else:
                    import math
                    pos=float(payload.get('position',0)); duration=float(payload.get('duration') or row[0] or 0)
                    if not math.isfinite(pos) or not math.isfinite(duration) or not 0<=pos<=604800 or not 0<=duration<=604800: raise ValueError()
                    c.execute('UPDATE movies SET progress=?,watched=?,last_watched=? WHERE id=?',(pos,int(duration>0 and pos/duration>=.95),time.time(),m[1]))
            self.send({'ok':True})
        except (ValueError,TypeError): self.send({'error':'Invalid request'},400)
        except (FileNotFoundError,PermissionError):self.send({'error':'Media or session unavailable'},404)
        except hls.StreamError as e:self.send({'error':str(e)},503)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError): pass

def main():
    global STREAMS
    initialize()
    class ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address = False
        def server_bind(self):
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            super().server_bind()
    server=ExclusiveServer((CONFIG.get('host','0.0.0.0'),int(CONFIG.get('port',8090))),Handler)
    server.daemon_threads=True
    STREAMS=hls.Manager(BASE,FFMPEG,FFPROBE,CONFIG)
    threading.Thread(target=scan,daemon=True).start()
    print(f'\n=====================================\n       BIOSWARE CINEMA\n=====================================\nServer: ONLINE\nLAN/VPN Port: {server.server_port}\nOpen: http://localhost:{server.server_port}\nRemote: http://<SERVER-VPN-IP>:{server.server_port}\nStartup library scan running\n=====================================',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: STREAMS.close();server.server_close()

if __name__=='__main__': main()
