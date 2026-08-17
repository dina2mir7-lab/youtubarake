import threading
import uuid
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pipeline import run_pipeline

app = FastAPI(title="YouTube Sermon Translator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

class PipelineRequest(BaseModel):
    video_url: Optional[str] = None
    speaker: Optional[str] = None

JOBS_STATUS = {}
JOBS_LOCK = threading.Lock()


def execute_pipeline_task(job_id: str, video_url: Optional[str], speaker: Optional[str]):
    with JOBS_LOCK:
        JOBS_STATUS[job_id] = {"status": "running", "message": "הפייפליין מופעל..."}
    try:
        result = run_pipeline(video_url=video_url, speaker=speaker)
        with JOBS_LOCK:
            JOBS_STATUS[job_id] = {
                "status": "completed" if result.get("success") else "failed",
                **result,
            }
    except Exception as exc:
        with JOBS_LOCK:
            JOBS_STATUS[job_id] = {"status": "failed", "message": f"שגיאה בלתי צפויה: {exc}"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/run")
async def start_pipeline(req: PipelineRequest, background_tasks: BackgroundTasks):
    if req.video_url and req.speaker:
        raise HTTPException(status_code=400, detail="בחר או קישור ישיר או דרשן — לא את שניהם.")
    if not req.video_url and not req.speaker:
        raise HTTPException(status_code=400, detail="יש לספק קישור לסרטון או לבחור דרשן.")
    if req.speaker and req.speaker not in ("salah", "khateb"):
        raise HTTPException(status_code=400, detail="דרשן לא מוכר.")

    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS_STATUS[job_id] = {"status": "queued", "message": "המשימה בתור..."}
    background_tasks.add_task(execute_pipeline_task, job_id, req.video_url, req.speaker)
    return {"job_id": job_id, "status": "started"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS_STATUS.get(job_id)
    if not job:
        return {"status": "not_found", "message": "משימה לא נמצאה"}
    return job


@app.get("/", response_class=HTMLResponse)
async def get_ui():
    return HTMLResponse(HTML)


HTML = r'''<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>תרגום דרשות YouTube</title>
<style>
body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:24px;color:#17202a}.box{max-width:820px;margin:auto;background:white;border-radius:18px;padding:28px;box-shadow:0 8px 30px #00000012}h1{text-align:center;margin-top:0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.btn{border:0;border-radius:12px;padding:16px;font-size:17px;font-weight:700;cursor:pointer;color:white}.salah{background:#087f5b}.khateb{background:#0b7285}.direct{background:#364fc7}.urlrow{display:flex;gap:10px;margin-top:18px}.urlrow input{flex:1;padding:14px;border:1px solid #ccd2d8;border-radius:10px;font-size:16px;direction:ltr}.status{display:none;margin-top:18px;padding:14px;border-radius:10px}.run{display:block;background:#e7f5ff;color:#1864ab}.ok{display:block;background:#ebfbee;color:#2b8a3e}.bad{display:block;background:#fff5f5;color:#c92a2a}.result{display:none;margin-top:22px}.result textarea{width:100%;min-height:420px;box-sizing:border-box;padding:16px;border:1px solid #ccd2d8;border-radius:10px;line-height:1.7;font-size:16px}.small{color:#68737d;text-align:center;margin:10px 0 20px}.actions{display:flex;gap:10px;margin-top:10px}.secondary{background:#495057}.spinner{display:inline-block;width:14px;height:14px;border:2px solid currentColor;border-left-color:transparent;border-radius:50%;animation:spin .7s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:650px){.grid{grid-template-columns:1fr}.urlrow{flex-direction:column}}
</style>
</head>
<body><main class="box">
<h1>🎬 תרגום דרשות וסרטוני YouTube</h1>
<p class="small">בחר דרשה אוטומטית או הדבק קישור ישיר לסרטון.</p>
<div class="grid">
<button class="btn salah" onclick="run({speaker:'salah'})">🕌 דרשה אחרונה — ראאד צלאח</button>
<button class="btn khateb" onclick="run({speaker:'khateb'})">🕌 דרשה אחרונה — כמאל ח'טיב</button>
</div>
<div class="urlrow"><input id="url" placeholder="https://www.youtube.com/watch?v=..."><button class="btn direct" onclick="direct()">תרגם סרטון</button></div>
<div id="status" class="status"></div>
<section id="result" class="result"><h2>📜 התרגום</h2><textarea id="text" readonly></textarea><div class="actions"><button class="btn secondary" onclick="copyText()">📋 העתק</button><button class="btn secondary" onclick="downloadText()">💾 TXT</button></div></section>
</main>
<script>
let timer=null;
function status(msg,kind){let e=document.getElementById('status');e.className='status '+kind;e.innerHTML=kind==='run'?'<span class="spinner"></span> '+msg:msg}
function run(payload){start(payload)}
function direct(){let url=document.getElementById('url').value.trim();if(!url){alert('נא להדביק קישור YouTube');return}start({video_url:url})}
async function start(payload){document.getElementById('result').style.display='none';status('מתחיל את התהליך...','run');try{let r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});let d=await r.json();if(!r.ok){status('❌ '+(d.detail||'שגיאה בשרת'),'bad');return}poll(d.job_id)}catch(e){status('❌ שגיאת תקשורת: '+e.message,'bad')}}
function poll(id){if(timer)clearInterval(timer);timer=setInterval(async()=>{try{let r=await fetch('/api/status/'+id);let j=await r.json();if(j.status==='running'||j.status==='queued'){status(j.message||'הפעולה מתבצעת...','run')}else if(j.status==='completed'){clearInterval(timer);status('✨ התרגום הושלם בהצלחה. '+(j.message||''),'ok');document.getElementById('text').value=j.translation||'';document.getElementById('result').style.display='block'}else if(j.status==='failed'){clearInterval(timer);status('❌ '+(j.message||'הפעולה נכשלה'),'bad')}}catch(e){console.error(e)}},2500)}
async function copyText(){await navigator.clipboard.writeText(document.getElementById('text').value);alert('הטקסט הועתק')}
function downloadText(){let b=new Blob([document.getElementById('text').value],{type:'text/plain;charset=utf-8'});let a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='sermon_translation.txt';a.click();URL.revokeObjectURL(a.href)}
</script></body></html>'''
