import os
import re
import time
import asyncio
import datetime
import tempfile
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from google import genai
from google.genai import types
from google.genai.errors import APIError

# ייבוא Telethon עבור התחברות דרך StringSession
from telethon import TelegramClient
from telethon.sessions import StringSession

# הגדרות כלליות
MAX_TELEGRAM_CHARS = 4000
MAX_VIDEOS_TO_SCAN = 15

# קישורים לערוצים/פלייליסטים
SPEAKER_URLS = {
    "salah": "https://www.youtube.com/playlist?list=PLWrMpoT7k1QikW0C0172oQ6HwmODp-ML2",
    "khateb": "https://www.youtube.com/@KamalKhateb/videos"
}


# ==========================================
# שליחה לטלגרם באמצעות Telethon (StringSession)
# ==========================================
async def send_to_telegram(full_text: str, video_title: str, video_url: str):
    api_id_env = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session_str = os.getenv("TELEGRAM_SESSION_STRING")
    target_user = os.getenv("TELEGRAM_TARGET_USER")

    if not all([api_id_env, api_hash, session_str, target_user]):
        print("⚠️ משתני סביבה של טלגרם חסרים. הודעה לא תישלח לטלגרם.")
        return

    api_id = int(api_id_env)
    text_chunks = split_text(full_text, MAX_TELEGRAM_CHARS)

    async with TelegramClient(StringSession(session_str), api_id, api_hash) as client:
        header_msg = f"🎬 **תרגום אוטומטי לדרשה**\n📌 **כותרת:** {video_title}\n\nהתוכן מתחיל מטה 👇"
        await client.send_message(target_user, header_msg)
        await asyncio.sleep(1.0)

        for index, chunk in enumerate(text_chunks, 1):
            await client.send_message(target_user, chunk)
            await asyncio.sleep(1.2)

        footer_msg = f"🔗 **קישור לסרטון המקורי ביוטיוב:**\n{video_url}\n\n✨ המשימה הושלמה בהצלחה!"
        await client.send_message(target_user, footer_msg)


# ==========================================
# איתור סרטונים וחילוץ
# ==========================================
def extract_video_id(url_or_id: str) -> str:
    patterns = [r"(?:v=)([a-zA-Z0-9_-]{11})", r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})", r"^([a-zA-Z0-9_-]{11})$"]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    raise ValueError(f"לא ניתן לחלץ Video ID מ: {url_or_id}")


def fetch_transcript(url_or_id: str) -> str:
    """חילוץ כתוביות יציב ותואם לכל גרסאות youtube-transcript-api בעזרת עוגיות"""
    video_id = extract_video_id(url_or_id)
    cookies_text = os.getenv("YOUTUBE_COOKIES_TEXT")
    cookie_file_path = None

    # יצירת קובץ cookies זמני במידה וקיים
    if cookies_text:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as cookie_file:
            cookie_file.write(cookies_text)
            cookie_file_path = cookie_file.name

    try:
        # שימוש ב-get_transcript הישיר והנתמך בכל הגרסאות לקבלת רשימת הדיקשנריז
        if cookie_file_path:
            transcript_list = YouTubeTranscriptApi.get_transcript(
                video_id, 
                languages=["ar", "he", "en"], 
                cookies=cookie_file_path
            )
        else:
            transcript_list = YouTubeTranscriptApi.get_transcript(
                video_id, 
                languages=["ar", "he", "en"]
            )

        # חילוץ הטקסט מתוך רשימת המילונים
        return "\n".join(item["text"] for item in transcript_list)

    finally:
        if cookie_file_path and os.path.exists(cookie_file_path):
            os.remove(cookie_file_path)


# ==========================================
# Gemini Translator
# ==========================================
class GeminiTranslator:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("❌ מפתח API של Gemini חסר!")
        self.client = genai.Client(api_key=api_key)
        self.primary_model = "gemini-2.5-flash"
        self.fallback_model = "gemini-3.1-flash-lite"

    def translate_large_file(self, input_file_path: str, output_file_path: str) -> bool:
        if not os.path.exists(input_file_path):
            return False

        uploaded_file = self.client.files.upload(file=input_file_path)

        system_instruction = (
            "אתה מתרגם מקצועי ומומחה לשפה הערבית והעברית. תפקידך לתרגם את קובץ הטקסט המצורף מערבית לעברית.\n"
            "מדובר בתמלול של דרשה. טקסט המקור עשוי להכיל שבירות שורה קצרות - חובה להתעלם מהן ולחבר את המשפטים לרצף אחד.\n"
            "דגשים:\n"
            "1. תרגום מלא ומדויק מילה במילה.\n"
            "2. משלב שפה גבוה, מכובד ורהוט.\n"
            "3. פסקאות רציפות. אל תוסיף הערות או הקדמות."
        )

        models_to_try = [self.primary_model, self.fallback_model]
        
        try:
            for current_model in models_to_try:
                for attempt in range(1, 4):
                    try:
                        response = self.client.models.generate_content(
                            model=current_model,
                            contents=[uploaded_file, "אנא תרגם את כל הקובץ המצורף לפי ההנחיות."],
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                temperature=0.3,
                            )
                        )
                        if response.text:
                            with open(output_file_path, "w", encoding="utf-8") as f:
                                f.write(response.text)
                            return True
                    except APIError as e:
                        if e.code in (429, 503) or "RESOURCE_EXHAUSTED" in str(e):
                            time.sleep(20)
                        else:
                            break
                    except Exception:
                        break
            return False
        finally:
            try:
                self.client.files.delete(name=uploaded_file.name)
            except Exception:
                pass


# ==========================================
# הצינור הראשי
# ==========================================
def run_pipeline(video_url: str = None, speaker: str = None):
    gemini_key = os.getenv("GEMINI_API_KEY")
    video_title = "סרטון יוטיוב"

    if not video_url:
        return {"success": False, "message": "לא סופק קישור תקין ליוטיוב."}

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            video_id = extract_video_id(video_url)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        original_file = os.path.join(temp_dir, f"transcript_{video_id}_orig.txt")
        translated_file = os.path.join(temp_dir, f"transcript_{video_id}_trans.txt")

        # 1. חילוץ טרנסקריפט ישיר בעזרת עוגיות
        try:
            transcript_text = fetch_transcript(video_url)
            with open(original_file, "w", encoding="utf-8") as f:
                f.write(transcript_text)
        except (TranscriptsDisabled, NoTranscriptFound):
            return {"success": False, "message": "לא נמצאו כתוביות זמינות לסרטון זה."}
        except Exception as e:
            return {"success": False, "message": f"שגיאה בחילוץ הטרנסקריפט: {e}"}

        # 2. תרגום
        translator = GeminiTranslator(api_key=gemini_key)
        if not translator.translate_large_file(original_file, translated_file):
            return {"success": False, "message": "תרגום הקובץ באמצעות Gemini נכשל."}

        with open(translated_file, "r", encoding="utf-8") as f:
            full_translated_text = f.read()

        # 3. שליחה לטלגרם
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(send_to_telegram(full_translated_text, video_title, video_url))
                else:
                    loop.run_until_complete(send_to_telegram(full_translated_text, video_title, video_url))
            except RuntimeError:
                asyncio.run(send_to_telegram(full_translated_text, video_title, video_url))
        except Exception as e:
            print(f"⚠️ שגיאה במהלך שליחה לטלגרם: {e}")

        return {
            "success": True,
            "message": "הפייפליין הושלם בהצלחה!",
            "video_url": video_url,
            "translation": full_translated_text
        }
