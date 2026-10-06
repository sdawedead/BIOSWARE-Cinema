/* Extends the existing player without changing its layout. */
let cinemaHls=null,playbackSession=null,playbackGeneration=0,preparing=false;
const quality=$('#quality');
async function releaseSession(sid){if(sid)try{await api('/playback/'+sid+'/stop',{})}catch{}}
async function attachCinema(position=0,paused=false,forcedQuality=null){
 const generation=++playbackGeneration;preparing=true;
 $('#playerMessage').textContent='Preparing cinema stream…';
 const old=playbackSession;playbackSession=null;
 if(cinemaHls){cinemaHls.destroy();cinemaHls=null}
 video.pause();video.removeAttribute('src');video.load();
 const result=await api('/movies/'+current.id+'/playback',{quality:forcedQuality||quality.value,previous:old?.session});
 if(generation!==playbackGeneration){await releaseSession(result.session);return}
 playbackSession=result;current.duration=result.duration||current.duration;
 const label=result.mode==='direct'?'DIRECT PLAY':'TRANSCODING • '+result.quality;
 $('#playbackStatus').textContent=label;
 video.onloadedmetadata=()=>{if(generation!==playbackGeneration)return;video.currentTime=Math.min(position,Math.max(0,(result.duration||video.duration)-.2));preparing=false;if(!paused)video.play().catch(()=>{$('#playerMessage').textContent='Press Play to begin.'});else $('#playerMessage').textContent=''};
 if(result.mode==='direct')video.src=result.url;
 else if(window.Hls&&Hls.isSupported()){
   cinemaHls=new Hls({enableWorker:false,startPosition:position,maxBufferLength:18,maxMaxBufferLength:30,backBufferLength:12,fragLoadingTimeOut:100000,manifestLoadingTimeOut:30000});
   cinemaHls.on(Hls.Events.ERROR,(_event,data)=>{if(data.fatal&&generation===playbackGeneration){preparing=false;$('#playerMessage').textContent='Playback could not continue. Try Auto or a lower quality.';showControls()}});
   cinemaHls.loadSource(result.url);cinemaHls.attachMedia(video);
 }else if(video.canPlayType('application/vnd.apple.mpegurl'))video.src=result.url;
 else{preparing=false;throw Error('This browser does not support HLS playback. Use an up-to-date Chrome or Safari.')}
 showControls();
}
play=async function(id,restart=false){
 try{
  if(current)await closePlayer();
  current=await api('/movies/'+id);$('#detail').close();$('#player').showModal();$('#playingTitle').textContent=current.title;
  video.innerHTML='';quality.value='auto';$('#subs').innerHTML='<option value="-1">Subtitles off</option>';
  const tracks=await api('/movies/'+id+'/subtitles');
  tracks.forEach((t,i)=>{const tr=document.createElement('track');tr.kind='subtitles';tr.label=t.language;tr.srclang=/^[a-z]{2,3}$/.test(t.language)?t.language:'und';tr.src='/api/subtitles/'+t.id;video.append(tr);$('#subs').insertAdjacentHTML('beforeend',`<option value="${i}">${esc(t.language)}</option>`)});
  if(restart)await api('/movies/'+id+'/progress',{position:0});
  await attachCinema(restart?0:current.progress||0);
 }catch(e){preparing=false;$('#playerMessage').textContent=e.message;showControls()}
};
save=async function(){if(!preparing&&current&&playbackSession&&Number.isFinite(video.currentTime)&&video.readyState>=1){try{await api('/movies/'+current.id+'/progress',{position:video.currentTime,duration:current.duration||video.duration||0})}catch(e){toast('Could not save progress: '+e.message)}}};
closePlayer=async function(){
 await save();++playbackGeneration;preparing=true;video.pause();
 if(cinemaHls){cinemaHls.destroy();cinemaHls=null}
 const old=playbackSession;playbackSession=null;video.removeAttribute('src');video.load();$('#player').close();current=null;
 await releaseSession(old?.session);preparing=false;render();
};
$('#back').onclick=()=>closePlayer();
quality.onchange=async()=>{if(!current)return;const pos=video.currentTime,paused=video.paused;await save();try{await attachCinema(pos,paused)}catch(e){preparing=false;$('#playerMessage').textContent=e.message;showControls()}};
video.onplaying=()=>{$('#playerMessage').textContent='';showControls()};
video.onerror=()=>{if(!playbackSession)return;if(playbackSession?.mode==='direct'&&quality.value==='auto'){attachCinema(video.currentTime||current.progress||0,false,'1080p').catch(e=>{$('#playerMessage').textContent=e.message;preparing=false})}else{$('#playerMessage').textContent='Playback failed. Try Auto or a lower quality.';showControls()}};
video.ontimeupdate=()=>{const duration=current?.duration||video.duration;$('#clock').textContent=time(video.currentTime)+' / '+time(duration);$('#seek').value=duration?video.currentTime/duration*100:0;if(Date.now()-saveAt>10000){saveAt=Date.now();save()}};
$('#seek').oninput=e=>{const duration=current?.duration||video.duration;if(Number.isFinite(duration))video.currentTime=Math.min(duration-.2,duration*e.target.value/100)};
setInterval(()=>{if(playbackSession)api('/playback/'+playbackSession.session+'/heartbeat',{}).catch(()=>{})},30000);
window.addEventListener('pagehide',()=>{if(playbackSession)fetch('/api/playback/'+playbackSession.session+'/stop',{method:'POST',keepalive:true,headers:{'X-Cinema-Token':status.token,'Content-Type':'application/json'},body:'{}'}).catch(()=>{})});
