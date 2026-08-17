import os
import re
import time
import asyncio
import datetime
import tempfile
import requests
import yt_dlp
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
        # הודעת כותרת
        header_msg = f"🎬 **תרגום אוטומטי לדרשה**\n📌 **כותרת:** {video_title}\n\nהתוכן מתחיל מטה 👇"
        await client.send_message(target_user, header_msg)
        await asyncio.sleep(1.0)

        # שליחת חלקי הטקסט
        for index, chunk in enumerate(text_chunks, 1):
            await client.send_message(target_user, chunk)
            await asyncio.sleep(1.2)

        # הודעת סגירה
        footer_msg = f"🔗 **קישור לסרטון המקורי ביוטיוב:**\n{video_url}\n\n✨ המשימה הושלמה בהצלחה!"
        await client.send_message(target_user, footer_msg)


# ==========================================
# איתור סרטונים וחילוץ
# ==========================================
def get_target_dates_strings():
    today = datetime.date.today()
    days_to_subtract = (today.weekday() - 4) % 7
    last_friday = today - datetime.timedelta(days=days_to_subtract)
    last_saturday = last_friday + datetime.timedelta(days=1)

    separators = ['.', '/', '-']
    date_formats = set()

    def add_date_combinations(target_date):
        days = [str(target_date.day), target_date.strftime('%d')]
        months = [str(target_date.month), target_date.strftime('%m')]
        years = [str(target_date.year), str(target_date.year)[2:]]
        for d in days:
            for m in months:
                for y in years:
                    for sep in separators:
                        date_formats.add(f"{d}{sep}{m}{sep}{y}")

    add_date_combinations(last_friday)
    add_date_combinations(last_saturday)
    return list(date_formats)


def find_latest_friday_video(url, max_videos=15):
    target_dates = get_target_dates_strings()
    ydl_opts = {
        'extract_flat': True,
        'skip_download': True,
        'playlistend': max_videos,
        'ignoreerrors': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            result_dict = ydl.extract_info(url, download=False)
            if not result_dict:
                return None, None

            videos = result_dict.get('entries', [])
            if not videos and 'title' in result_dict:
                videos = [result_dict]

            for video in videos:
                if not video:
                    continue
                title = video.get('title') or ''
                v_id = video.get('id') or video.get('video_id')
                if not v_id:
                    continue

                video_url = f"https://www.youtube.com/watch?v={v_id}"
                if any(target_date in title for target_date in target_dates):
                    return video_url, title

            return None, None
        except Exception as e:
            print(f"⚠️ שגיאה בסריקת יוטיוב: {e}")
            return None, None


def extract_video_id(url_or_id: str) -> str:
    patterns = [r"(?:v=)([a-zA-Z0-9_-]{11})", r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})", r"^([a-zA-Z0-9_-]{11})$"]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    raise ValueError(f"לא ניתן לחלץ Video ID מ: {url_or_id}")


def fetch_transcript(url_or_id: str) -> str:
    video_id = extract_video_id(url_or_id)
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # הגדרות yt-dlp להורדת כתוביות בלבד ללא הסרטון עצמו
ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['ar', 'he', 'en'],
        'subtitlesformat': 'json3',
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        # תוספת מומלצת למניעת חסימות בשרתי ענן:
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        'geo_bypass': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
        subtitles = info.get('subtitles') or info.get('automatic_captions')

        if not subtitles:
            raise NoTranscriptFound(video_id, ['ar', 'he', 'en'], None)

        # בחירת השפה הראשונה הזמינה מתוך הרשימה
        target_lang = None
        for lang in ['ar', 'he', 'en']:
            if lang in subtitles:
                target_lang = lang
                break

        if not target_lang:
            # אם לא מצאנו מהרשימה, ניקח את השפה הראשונה שישנה
            target_lang = list(subtitles.keys())[0]

        # שליפת קישור הכתוביות בפורמט json3 או vtt
        sub_info = subtitles[target_lang]
        sub_url = None
        for fmt in sub_info:
            if fmt.get('ext') == 'json3':
                sub_url = fmt.get('url')
                break
        if not sub_url and sub_info:
            sub_url = sub_info[0].get('url')

        if not sub_url:
            raise NoTranscriptFound(video_id, ['ar', 'he', 'en'], None)

        # הורדת תוכן הכתוביות מיוטיוב
        resp = requests.get(sub_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        data = resp.json()

        # חילוץ הטקסט מתוך מבנה ה-JSON של יוטיוב
        transcript_lines = []
        for event in data.get('events', []):
            segs = event.get('segs')
            if segs:
                line_text = "".join(s.get('utf8', '') for s in segs if 'utf8' in s).strip()
                if line_text and line_text != '\n':
                    transcript_lines.append(line_text)

        return "\n".join(transcript_lines)


def split_text(text, max_chars):
    chunks = []
    current_chunk = ""
    lines = text.split("\n")

    for line in lines:
        if len(line) > max_chars:
            for i in range(0, len(line), max_chars):
                chunks.append(line[i: i + max_chars])
            continue

        if len(current_chunk) + len(line) + 1 > max_chars:
            chunks.append(current_chunk.strip())
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


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
# הצינור הראשי שיופעל מרחוק
# ==========================================
def run_pipeline(video_url: str = None, speaker: str = None):
    gemini_key = os.getenv("GEMINI_API_KEY")
    video_title = "סרטון יוטיוב"

    # איתור קישור לפי דרשן במידת הצורך
    if not video_url and speaker in SPEAKER_URLS:
        target_channel_url = SPEAKER_URLS[speaker]
        found_url, found_title = find_latest_friday_video(target_channel_url, max_videos=MAX_VIDEOS_TO_SCAN)
        if not found_url:
            return {"success": False, "message": f"לא נמצאה דרשה מתאימה ליום שישי האחרון עבור {speaker}."}
        video_url = found_url
        video_title = found_title

    if not video_url:
        return {"success": False, "message": "לא סופק קישור תקין ליוטיוב."}

    # שימוש בתיקייה זמנית של מערכת ההפעלה (מתאים לענן)
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            video_id = extract_video_id(video_url)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        original_file = os.path.join(temp_dir, f"transcript_{video_id}_orig.txt")
        translated_file = os.path.join(temp_dir, f"transcript_{video_id}_trans.txt")

        # 1. חילוץ טרנסקריפט
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

        # 3. שליחה לטלגרם דרך Telethon
        try:
            asyncio.run(send_to_telegram(full_translated_text, video_title, video_url))
        except Exception as e:
            print(f"⚠️ שגיאה במהלך שליחה לטלגרם: {e}")

        return {
            "success": True,
            "message": "הפייפליין הושלם בהצלחה!",
            "video_url": video_url,
            "translation": full_translated_text
        }
