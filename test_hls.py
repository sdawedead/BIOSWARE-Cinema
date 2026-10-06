"""Actual media/VPN integration tests. CINEMA_TEST_URL overrides target address."""
import hashlib,json,os,time,unittest,urllib.request,urllib.error
from pathlib import Path
import server,hls
BASE=os.environ.get('CINEMA_TEST_URL','http://localhost:8091')
class HLSTests(unittest.TestCase):
    def setUp(self):
        self.token=self.call('/status')['token'];self.sessions=[]
    def call(self,path,payload=None,raw=False):
        req=urllib.request.Request(BASE+'/api'+path,json.dumps(payload).encode() if payload is not None else None,
           {'Content-Type':'application/json','X-Cinema-Token':getattr(self,'token','')})
        with urllib.request.urlopen(req,timeout=120) as r:data=r.read()
        return data if raw else json.loads(data)
    def start(self,mid,quality='auto'):
        s=self.call(f'/movies/{mid}/playback',{'quality':quality});self.sessions.append(s['session']);return s
    def tearDown(self):
        for sid in self.sessions:self.call(f'/playback/{sid}/stop',{})
    def test_brian_transcode_seek_cleanup(self):
        with server.db() as c:r=c.execute("SELECT * FROM movies WHERE title='Zivot-briana'").fetchone()
        self.assertIsNotNone(r);source=server.validated(r['path']);before=source.stat()
        # Hash original content before and after transcoding, not just timestamps.
        def digest():
            with source.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
        sha=digest();s=self.start(r['id']);self.assertEqual(s['mode'],'hls')
        manifest=self.call(f"/playback/{s['session']}/index.m3u8",raw=True)
        self.assertIn(b'#EXT-X-ENDLIST',manifest)
        for index in [0,300,1]:
            segment=self.call(f"/playback/{s['session']}/{index}.ts",raw=True)
            self.assertGreater(len(segment),1000)
            path=server.BASE/'cache'/'test-segment.ts';path.write_bytes(segment)
            info=hls.inspect(server.FFPROBE,path);path.unlink()
            self.assertEqual(info['video_codec'],'h264');self.assertEqual(info['audio_codec'],'aac')
            self.assertEqual(info['resolution'],'720×540')
        self.assertEqual(digest(),sha);self.assertEqual(source.stat().st_mtime_ns,before.st_mtime_ns)
        self.call(f"/playback/{s['session']}/stop",{});self.sessions.remove(s['session'])
        self.assertFalse((server.BASE/'cache'/'hls'/s['session']).exists())
    def test_direct_play_no_encoder(self):
        with server.db() as c:rows=c.execute("SELECT * FROM movies WHERE extension='.mp4' AND video_codec='h264' AND probed IS NOT NULL LIMIT 100").fetchall()
        row=next(r for r in rows if hls.compatible(r));s=self.start(row['id'])
        self.assertEqual(s['mode'],'direct')
        cache=server.BASE/'cache'/'hls'/s['session'];self.assertEqual(list(cache.iterdir()),[])
        self.assertTrue(s['url'].endswith('/stream'))
    def test_invalid_requests(self):
        for path,payload in [('/movies/0/playback',{}),('/movies/6350/playback',{'quality':'../../Windows'}),('/playback/'+('0'*32)+'/0.ts',None)]:
            with self.assertRaises(urllib.error.HTTPError) as cm:self.call(path,payload)
            self.assertIn(cm.exception.code,(400,404))
    def test_original_rejected_for_incompatible(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:self.start(6350,'original')
        self.assertEqual(cm.exception.code,503)
    def test_session_expiry(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            manager=hls.Manager(Path(folder),None,None,{})
            row=dict(id=1,extension='.mp4',video_codec='h264',pixel_format='yuv420p',audio_codec='aac',video_level=31,duration=100)
            s=manager.create(row,'auto');directory=manager.root/s['session']
            manager.sessions[s['session']]['last']-=121
            manager.reap_expired()
            self.assertFalse(directory.exists());self.assertEqual(manager.stats()['active_streams'],0)
if __name__=='__main__':unittest.main(verbosity=2)
