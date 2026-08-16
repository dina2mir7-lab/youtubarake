import os
import asyncio
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ייבוא הפייפליין מהקובץ שפתחנו בשלב הקודם
from pipeline import run_pipeline

app = FastAPI(title="YouTube Sermon Translator")

# אפשור גישה מכל דפדפן (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# מבנה נתונים לבקשה
class PipelineRequest(BaseModel):
    video_url: Optional[str] = None
    speaker: Optional[str] = None

# אחסון תוצאות אחרונות בזיכרון (כדי שנוכל לבדוק סטטוס)
JOBS_STATUS = {}


def execute_pipeline_task(job_id: str, video_url: Optional[str], speaker: Optional[str]):
    """הרצת הפייפליין ברקע ועדכון סטטוס המשימה"""
    JOBS_STATUS[job_id] = {"status": "running", "message": "הפייפליין מופעל ברקע..."}
    
    try:
        result = run_pipeline(video_url=video_url, speaker=speaker)
        if result.get("success"):
            JOBS_STATUS[job_id] = {
                "status": "completed",
                "message": result.get("message"),
                "video_url": result.get("video_url"),
                "translation": result.get("translation")
            }
        else:
            JOBS_STATUS[job_id] = {
                "status": "failed",
                "message": result.get("message")
            }
    except Exception as e:
        JOBS_STATUS[job_id] = {
            "status": "failed",
            "message": f"שגיאה בלתי צפויה: {str(e)}"
        }


@app.post("/api/run")
async def start_pipeline(req: PipelineRequest, background_tasks: BackgroundTasks):
    """נתיב להפעלת הפייפליין"""
    if not req.video_url and not req.speaker:
        raise HTTPException(status_code=400, detail="יש לספק קישור לסרטון או לבחור דרשן.")

    import uuid
    job_id = str(uuid.uuid4())[:8]
    
    # הרצה ברקע כדי שהשרת יחזיר תשובה מיידית ללקוח והדפדפן לא יתנתק
    background_tasks.add_task(execute_pipeline_task, job_id, req.video_url, req.speaker)

    return {"job_id": job_id, "status": "started", "message": "התהליך הותחל בהצלחה ברקע."}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    """נתיב לבדיקת סטטוס ההרצה לפי job_id"""
    job = JOBS_STATUS.get(job_id)
    if not job:
        return {"status": "not_found", "message": "משימה לא נמצאה"}
    return job


@app.get("/", response_class=HTMLResponse)
async def get_ui():
    """הממשק הוויזואלי (HTML + CSS + JS)"""
    html_content = """
    <!DOCTYPE html>
    <html lang="he" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>מערכת תרגום דרשות וסרטוני יוטיוב</title>
        <style>
            :root {
                --primary: #10b981;
                --primary-hover: #059669;
                --bg: #f3f4f6;
                --card-bg: #ffffff;
                --text: #1f2937;
            }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background-color: var(--bg);
                color: var(--text);
                margin: 0;
                padding: 20px;
                display: flex;
                justify-content: center;
            }
            .container {
                width: 100%;
                max-width: 800px;
                background: var(--card-bg);
                padding: 30px;
                border-radius: 12px;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            }
            h1 {
                text-align: center;
                color: #111827;
                margin-bottom: 25px;
            }
            .btn-group {
                display: flex;
                gap: 15px;
                margin-bottom: 25px;
                flex-wrap: wrap;
            }
            .btn {
                flex: 1;
                min-width: 200px;
                padding: 14px 20px;
                font-size: 16px;
                font-weight: bold;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                transition: all 0.2s;
                background-color: #3b82f6;
                color: white;
                text-align: center;
            }
            .btn:hover { opacity: 0.9; transform: translateY(-1px); }
            .btn-salah { background-color: #059669; }
            .btn-khateb { background-color: #0d9488; }
            .divider {
                text-align: center;
                margin: 20px 0;
                position: relative;
            }
            .divider::before {
                content: "";
                position: absolute;
                top: 50%;
                left: 0;
                right: 0;
                height: 1px;
                background: #e5e7eb;
                z-index: 1;
            }
            .divider span {
                background: var(--card-bg);
                padding: 0 10px;
                position: relative;
                z-index: 2;
                color: #6b7280;
                font-size: 14px;
            }
            .input-group {
                display: flex;
                gap: 10px;
                margin-bottom: 25px;
            }
            input[type="text"] {
                flex: 1;
                padding: 12px 16px;
                border: 1px solid #d1d5db;
                border-radius: 8px;
                font-size: 15px;
            }
            .status-box {
                padding: 15px;
                border-radius: 8px;
                display: none;
                margin-bottom: 20px;
                font-weight: 500;
            }
            .status-running { background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; }
            .status-success { background: #d1fae5; color: #047857; border: 1px solid #a7f3d0; }
            .status-failed { background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }
            
            .output-box {
                display: none;
                margin-top: 20px;
            }
            textarea {
                width: 100%;
                height: 300px;
                padding: 15px;
                border: 1px solid #d1d5db;
                border-radius: 8px;
                font-size: 15px;
                box-sizing: border-box;
                resize: vertical;
                line-height: 1.6;
            }
            .action-btns {
                display: flex;
                gap: 10px;
                margin-top: 10px;
            }
            .btn-secondary {
                background: #4b5563;
                color: white;
                padding: 8px 16px;
                border-radius: 6px;
                border: none;
                cursor: pointer;
            }
            .spinner {
                display: inline-block;
                width: 16px;
                height: 16px;
                border: 2px solid currentColor;
                border-radius: 50%;
                border-top-color: transparent;
                animation: spin 0.8s linear infinite;
                margin-left: 8px;
                vertical-align: middle;
            }
            @keyframes spin { to { transform: rotate(360deg); } }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 תרגום דרשות וסרטוני יוטיוב</h1>
            
            <!-- כפתורי הגדרות מראש -->
            <div class="btn-group">
                <button class="btn btn-salah" onclick="runForSpeaker('salah')">
                    🕌 דרשה אחרונה - ראאד צלאח
                </button>
                <button class="btn btn-khateb" onclick="runForSpeaker('khateb')">
                    🕌 דרשה אחרונה - כמאל ח'טיב
                </button>
            </div>

            <div class="divider"><span>או הכנס קישור ישיר</span></div>

            <!-- קלט קישור חופשי -->
            <div class="input-group">
                <input type="text" id="youtubeUrl" placeholder="https://www.youtube.com/watch?v=...">
                <button class="btn" style="flex: 0 0 150px;" onclick="runForUrl()">תרגם סרטון</button>
            </div>

            <!-- קופסת סטטוס -->
            <div id="statusBox" class="status-box"></div>

            <!-- תצוגת תוצאות -->
            <div id="outputBox" class="output-box">
                <h3>📜 תוצאת התרגום:</h3>
                <textarea id="resultText" readonly></textarea>
                <div class="action-btns">
                    <button class="btn-secondary" onclick="copyText()">📋 העתק טקסט</button>
                    <button class="btn-secondary" onclick="downloadText()">💾 הורד כקובץ TXT</button>
                </div>
            </div>
        </div>

        <script>
            let currentJobId = null;
            let pollInterval = null;

            function showStatus(message, type) {
                const box = document.getElementById('statusBox');
                box.className = 'status-box status-' + type;
                box.style.display = 'block';
                
                if (type === 'running') {
                    box.innerHTML = '<span class="spinner"></span> ' + message;
                } else {
                    box.innerText = message;
                }
            }

            function hideOutput() {
                document.getElementById('outputBox').style.display = 'none';
            }

            async function triggerPipeline(payload) {
                hideOutput();
                showStatus('שולח בקשה לשרת...', 'running');

                try {
                    const res = await fetch('/api/run', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    
                    const data = await res.json();
                    
                    if (res.ok) {
                        currentJobId = data.job_id;
                        showStatus('הפייפליין רץ ברקע... מוריד טרנסקריפט ומבצע תרגום ב-Gemini', 'running');
                        pollStatus();
                    } else {
                        showStatus('שגיאה: ' + (data.detail || 'אירעה שגיאה בשרת'), 'failed');
                    }
                } catch (e) {
                    showStatus('שגיאת תקשורת עם השרת: ' + e.message, 'failed');
                }
            }

            function runForSpeaker(speaker) {
                triggerPipeline({ speaker: speaker });
            }

            function runForUrl() {
                const url = document.getElementById('youtubeUrl').value.trim();
                if (!url) {
                    alert('נא להזין קישור תקין ליוטיוב');
                    return;
                }
                triggerPipeline({ video_url: url });
            }

            function pollStatus() {
                if (pollInterval) clearInterval(pollInterval);
                
                pollInterval = setInterval(async () => {
                    if (!currentJobId) return;

                    try {
                        const res = await fetch('/api/status/' + currentJobId);
                        const job = await res.json();

                        if (job.status === 'completed') {
                            clearInterval(pollInterval);
                            showStatus('✨ התהליך הושלם בהצלחה! התרגום נשלח גם לטלגרם.', 'success');
                            document.getElementById('resultText').value = job.translation;
                            document.getElementById('outputBox').style.display = 'block';
                        } else if (job.status === 'failed') {
                            clearInterval(pollInterval);
                            showStatus('❌ הפעולה נכשלה: ' + job.message, 'failed');
                        }
                    } catch (e) {
                        console.error('Polling error:', e);
                    }
                }, 3000); // בדיקה כל 3 שניות
            }

            function copyText() {
                const text = document.getElementById('resultText');
                text.select();
                document.execCommand('copy');
                alert('הטקסט הועתק ללוח!');
            }

            function downloadText() {
                const text = document.getElementById('resultText').value;
                const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'sermon_translation.txt';
                a.click();
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
