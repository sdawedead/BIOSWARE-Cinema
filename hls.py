"""Bounded on-demand HLS: virtual VOD playlist, serialized short segment jobs."""
import math, re, secrets, shutil, subprocess, threading, time, json
from pathlib import Path
SEGMENT=6
FLAGS=getattr(subprocess,'CREATE_NO_WINDOW',0)

def find_tool(name,base,configured=''):
    if configured and Path(configured).is_file():return str(Path(configured).resolve())
    return shutil.which(configured or name) or next((str(p) for p in (base/'tools').glob(f'**/{name}.exe')),None)

def compatible(row):
    r=dict(row)
    return (r.get('extension') in ('.mp4','.m4v','.mov') and r.get('video_codec')=='h264' and
      r.get('pixel_format') in ('yuv420p','yuvj420p') and r.get('audio_codec') in (None,'aac','mp3') and
      (r.get('video_level') or 999)<=51)

def inspect(ffprobe,path):
    if not ffprobe:return {}
    try:
        r=subprocess.run([ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(path)],capture_output=True,timeout=30,creationflags=FLAGS)
        d=json.loads(r.stdout);streams=d.get('streams',[])
        v=next((s for s in streams if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')), {})
        a=next((s for s in streams if s.get('codec_type')=='audio'),{});f=d.get('format',{})
        if not v:return {}
        return dict(container=f.get('format_name'),video_codec=v.get('codec_name'),audio_codec=a.get('codec_name'),
          resolution=f"{v.get('width',0)}×{v.get('height',0)}",duration=float(f.get('duration') or v.get('duration') or 0),
          bitrate=int(f.get('bit_rate') or 0),pixel_format=v.get('pix_fmt'),video_level=v.get('level'),probed=time.time())
    except (ValueError,OSError,subprocess.SubprocessError):return {}

class StreamError(Exception):pass

class Manager:
    def __init__(self,base,ffmpeg,ffprobe,config):
        self.root=(base/'cache'/'hls').resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.ffmpeg=ffmpeg;self.ffprobe=ffprobe;self.sessions={};self.lock=threading.RLock()
        self.slots=threading.BoundedSemaphore(2);self.hardware=[];self.backend='libx264'
        if ffmpeg:
            for enc in ['h264_nvenc','h264_qsv','h264_amf']:
                try:
                    r=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i','color=s=128x128:d=0.1','-frames:v','1','-c:v',enc,'-f','null','-'],capture_output=True,timeout=12,creationflags=FLAGS)
                    if r.returncode==0:self.hardware.append(enc)
                except (OSError,subprocess.SubprocessError):pass
        if config.get('encoder') in self.hardware:self.backend=config['encoder']
        for p in self.root.iterdir():
            if re.fullmatch(r'[a-f0-9]{32}',p.name):self.remove_dir(p)
        threading.Thread(target=self.reaper,daemon=True).start()
    def remove_dir(self,p):
        p=p.resolve()
        if p.parent==self.root and re.fullmatch(r'[a-f0-9]{32}',p.name):shutil.rmtree(p,ignore_errors=True)
    def stats(self):
        with self.lock:return dict(transcoding_ready=bool(self.ffmpeg and self.ffprobe),hardware_encoders=self.hardware,encoder=self.backend,
            active_streams=len(self.sessions),active_transcodes=sum(bool(s.get('process') and s['process'].poll() is None) for s in self.sessions.values()))
    def create(self,row,quality,previous=None):
        if quality not in ('auto','original','1080p','720p','480p'):raise ValueError('Invalid quality')
        if previous:self.stop(previous)
        direct=compatible(row) and quality in ('auto','original')
        if quality=='original' and not direct:raise StreamError('Original is incompatible. Choose Auto for this film.')
        if not direct and not (self.ffmpeg and self.ffprobe):raise StreamError('Install FFmpeg and FFprobe to play this film.')
        duration=float(row.get('duration') or 0)
        if not direct and not 0<duration<=604800:raise StreamError('Could not determine film duration. Rescan this film.')
        with self.lock:
            if len(self.sessions)>=8:raise StreamError('Cinema is busy. Close another playback session.')
            sid=secrets.token_hex(16);directory=self.root/sid;directory.mkdir();chosen='original' if direct else '1080p' if quality=='auto' else quality
            self.sessions[sid]=dict(row=dict(row),quality=chosen,mode='direct' if direct else 'hls',last=time.monotonic(),directory=directory,lock=threading.Lock(),process=None,stopped=False)
        return dict(session=sid,mode='direct' if direct else 'hls',quality=chosen,duration=duration,
          url=f"/api/movies/{row['id']}/stream" if direct else f'/api/playback/{sid}/index.m3u8')
    def get(self,sid):
        with self.lock:
            s=self.sessions.get(sid)
            if not s or s['stopped']:raise FileNotFoundError()
            s['last']=time.monotonic();return s
    def manifest(self,sid):
        s=self.get(sid);duration=s['row']['duration'];count=math.ceil(duration/SEGMENT)
        lines=['#EXTM3U','#EXT-X-VERSION:3',f'#EXT-X-TARGETDURATION:{SEGMENT}','#EXT-X-MEDIA-SEQUENCE:0','#EXT-X-PLAYLIST-TYPE:VOD','#EXT-X-INDEPENDENT-SEGMENTS']
        for i in range(count):
            if i:lines.append('#EXT-X-DISCONTINUITY')
            lines.extend([f'#EXTINF:{min(SEGMENT,duration-i*SEGMENT):.6f},',f'{i}.ts'])
        return ('\n'.join(lines+['#EXT-X-ENDLIST',''])).encode()
    def segment(self,sid,index,validate):
        s=self.get(sid)
        if s['mode']!='hls' or not 0<=index<math.ceil(s['row']['duration']/SEGMENT):raise FileNotFoundError()
        with s['lock']:
            if s['stopped']:raise FileNotFoundError()
            out=s['directory']/f'{index}.ts'
            if out.exists():return out.read_bytes()
            if not self.slots.acquire(timeout=60):raise StreamError('Transcoding is busy. Try again shortly.')
            try:
                source=validate(s['row']['path']);height=int(s['quality'].removesuffix('p'));temp=s['directory']/f'{index}.tmp'
                cmd=[self.ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-ss',str(index*SEGMENT),'-i',str(source),
                  '-map','0:v:0','-map','0:a:0?','-sn','-dn','-t',str(min(SEGMENT,s['row']['duration']-index*SEGMENT)),
                  '-vf',f'scale=w=-2:h=min(ih\\,{height}):force_divisible_by=2,format=yuv420p','-r','30',
                  '-c:v',self.backend,'-threads','2','-g','180','-keyint_min','180','-sc_threshold','0','-maxrate','6M','-bufsize','12M',
                  '-c:a','aac','-ac','2','-ar','48000','-b:a','160k','-af','apad','-max_muxing_queue_size','2048']
                if self.backend=='libx264':cmd+=['-preset','veryfast','-crf','23']
                cmd+=['-mpegts_flags','+resend_headers','-muxdelay','0','-f','mpegts','-y',str(temp)]
                with (s['directory']/'encoder.log').open('wb') as log:
                    with self.lock:
                        if s['stopped']:raise FileNotFoundError()
                        s['process']=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=log,creationflags=FLAGS)
                    try:code=s['process'].wait(timeout=90)
                    except subprocess.TimeoutExpired:s['process'].kill();s['process'].wait();raise StreamError('Encoding timed out. Try lower quality.')
                if code or not temp.exists() or not temp.stat().st_size:raise StreamError('Could not encode this film. See the local encoder log.')
                if s['stopped']:raise FileNotFoundError()
                temp.replace(out)
                cached=sorted(s['directory'].glob('*.ts'),key=lambda p:p.stat().st_mtime)
                for old in cached[:-12]:old.unlink(missing_ok=True)
                return out.read_bytes()
            finally:s['process']=None;self.slots.release()
    def stop(self,sid):
        with self.lock:
            s=self.sessions.pop(sid,None)
            if not s:return
            s['stopped']=True;p=s.get('process')
            if p and p.poll() is None:p.terminate()
        with s['lock']:
            if p:
                try:p.wait(timeout=5)
                except subprocess.TimeoutExpired:p.kill();p.wait()
            self.remove_dir(s['directory'])
    def reaper(self):
        while True:
            time.sleep(15)
            self.reap_expired()
    def reap_expired(self):
        with self.lock:expired=[sid for sid,s in self.sessions.items() if time.monotonic()-s['last']>120]
        for sid in expired:self.stop(sid)
    def close(self):
        for sid in list(self.sessions):self.stop(sid)
