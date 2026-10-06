"""Run against a running server: python tests.py. Never changes media."""
import json, urllib.request, urllib.error, sqlite3, hashlib, unittest
import server
BASE='http://localhost:'+str(server.CONFIG['port'])
def get(path,headers=None):
    return urllib.request.urlopen(urllib.request.Request(BASE+path,headers=headers or {}),timeout=20)
def post(path,data,token):
    return urllib.request.urlopen(urllib.request.Request(BASE+path,json.dumps(data).encode(),{'Content-Type':'application/json','X-Cinema-Token':token}),timeout=20)
class CinemaTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(server.clean_name('Interstellar.2014.1080p.BluRay.x264.mkv'),('Interstellar',2014))
        self.assertEqual(server.clean_name('Blade_Runner_2049_2017.mkv'),('Blade Runner 2049',2017))
    def test_ranges(self):
        for bad in ['bytes=','bytes=-0','bytes=10-9','bytes=100-','bytes=0-1,3-4','items=0-1']:
            with self.assertRaises(ValueError):server.byte_range(bad,100)
        self.assertEqual(server.byte_range('bytes=-10',100),(90,99,206))
    def test_boundaries(self):
        with self.assertRaises((PermissionError,FileNotFoundError)):server.validated(server.BASE/'server.py')
        for path in ['/api/movies/0/stream','/stream?path=C:/Windows/win.ini','/../config.json']:
            with self.assertRaises(urllib.error.HTTPError) as cm:get(path)
            self.assertEqual(cm.exception.code,404)
    def test_real_stream_and_seek(self):
        with server.db() as c: row=c.execute('SELECT * FROM movies WHERE missing=0 AND size>1048576 LIMIT 1').fetchone()
        self.assertIsNotNone(row,'Need an indexed real movie')
        p=server.validated(row['path']); endpoint=f"/api/movies/{row['id']}/stream"
        with p.open('rb') as f:
            for pos in [0,row['size']//2,row['size']-1024,1024]:
                r=get(endpoint,{'Range':f'bytes={pos}-{pos+1023}'})
                self.assertEqual(r.status,206); self.assertEqual(r.headers['Content-Length'],'1024')
                self.assertEqual(r.headers['Content-Range'],f"bytes {pos}-{pos+1023}/{row['size']}")
                f.seek(pos);self.assertEqual(r.read(),f.read(1024))
        with self.assertRaises(urllib.error.HTTPError) as cm:get(endpoint,{'Range':'bytes=0-1,4-5'})
        self.assertEqual(cm.exception.code,416)
    def test_persistence(self):
        token=json.load(get('/api/status'))['token']
        with server.db() as c: original=dict(c.execute('SELECT * FROM movies WHERE missing=0 LIMIT 1').fetchone())
        mid=original['id']
        try:
            post(f'/api/movies/{mid}/favorite',{'favorite':True},token).close()
            post(f'/api/movies/{mid}/progress',{'position':123,'duration':7200},token).close()
            with server.db() as c:r=c.execute('SELECT favorite,progress FROM movies WHERE id=?',(mid,)).fetchone()
            self.assertEqual(tuple(r),(1,123))
            self.assertEqual(json.load(get(f'/api/movies/{mid}'))['progress'],123)
        finally:
            with server.db() as c:c.execute('UPDATE movies SET favorite=?,progress=?,watched=?,last_watched=? WHERE id=?',(original['favorite'],original['progress'],original['watched'],original['last_watched'],mid))
    def test_subtitles(self):
        with server.db() as c:rows=c.execute('SELECT id FROM subtitles LIMIT 5').fetchall()
        if not rows:self.skipTest('No matching sidecar subtitles in actual libraries')
        for row in rows:self.assertTrue(get('/api/subtitles/'+str(row[0])).read().startswith(b'WEBVTT'))
if __name__=='__main__':unittest.main(verbosity=2)
