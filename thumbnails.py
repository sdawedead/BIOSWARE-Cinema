"""Small, read-only video frame previews, generated only when requested."""
import hashlib, subprocess, threading, time
from pathlib import Path

SLOTS=threading.BoundedSemaphore(2)
LOCK=threading.Lock()
FAILED={}
FALLBACK=b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180"><rect width="320" height="180" fill="#28151e"/><path d="M145 65L185 90L145 115Z" fill="#c77b8d"/></svg>'

def preview(base,ffmpeg,row,validate):
    source=validate(row['path']);st=source.stat()
    key=f"{row['id']}-"+hashlib.sha256(f'{st.st_mtime_ns}:{st.st_size}'.encode()).hexdigest()[:16]
    directory=base/'cache'/'thumbnails';directory.mkdir(parents=True,exist_ok=True)
    target=directory/(key+'.jpg')
    if target.exists():return target.read_bytes(),'image/jpeg',True
    with LOCK:failed=FAILED.get(key,0)>time.monotonic()
    if failed or not ffmpeg:return FALLBACK,'image/svg+xml',False
    if not SLOTS.acquire(timeout=30):return FALLBACK,'image/svg+xml',False
    try:
        if target.exists():return target.read_bytes(),'image/jpeg',True
        duration=row['duration'] or 0
        for position in [min(120,duration*.1) if duration else 30,0]:
            try:
                result=subprocess.run([ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-ss',str(position),
                  '-i',str(source),'-map','0:v:0','-an','-sn','-threads','1',
                  '-vf','thumbnail=12,scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2',
                  '-frames:v','1','-q:v','5','-f','image2pipe','-c:v','mjpeg','pipe:1'],
                  stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=12,
                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if result.returncode==0 and result.stdout.startswith(b'\xff\xd8'):
                    # Atomic cache publication; media roots are never output targets.
                    import tempfile,os
                    with tempfile.NamedTemporaryFile(dir=directory,suffix='.tmp',delete=False) as f:
                        f.write(result.stdout);temporary=Path(f.name)
                    os.replace(temporary,target)
                    return result.stdout,'image/jpeg',True
            except (OSError,subprocess.SubprocessError):pass
        with LOCK:
            if len(FAILED)>10000:FAILED.clear()
            FAILED[key]=time.monotonic()+3600
        return FALLBACK,'image/svg+xml',False
    finally:SLOTS.release()
