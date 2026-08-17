import asyncio
import datetime
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp
from google import genai
from google.genai import types
from google.genai.errors import APIError
from telethon import TelegramClient
from telethon.sessions import StringSession

MAX_TELEGRAM_CHARS = int(os.getenv("MAX_TELEGRAM_CHARS", "4000"))
MAX_VIDEOS_TO_SCAN = int(os.getenv("MAX_VIDEOS_TO_SCAN", "20"))

SPEAKER_URLS = {
    "salah": os.getenv(
        "SALAH_URL",
        "https://www.youtube.com/playlist?list=PLWrMpoT7k1QikW0C0172oQ6HwmODp-ML2",
    ),
    "khateb": os.getenv(
        "KHATEB_URL",
        "https://www.youtube.com/@KamalKhateb/videos",
    ),
}

LANGUAGES = ["ar", "he", "en"]


def _cookie_file_from_env():
    """Return (path, temporary). Supports a server file or Netscape cookies text."""
    explicit = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if explicit and os.path.exists(explicit):
        return explicit, False

    text = os.getenv("YOUTUBE_COOKIES_TEXT", "")
    if text.strip():
        f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8")
        f.write(text)
        f.close()
        return f.name, True

    return None, False


def _youtube_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "noplaylist": True,
    }
    cookie_file, temporary = _cookie_file_from_env()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if extra:
        opts.update(extra)
    return opts, cookie_file, temporary


def _cleanup_cookie(cookie_file, temporary):
    if temporary and cookie_file:
        try:
            os.remove(cookie_file)
        except OSError:
            pass


def get_target_dates_strings():
    today = datetime.date.today()
    days_to_subtract = (today.weekday() - 4) % 7
    last_friday = today - datetime.timedelta(days=days_to_subtract)
    last_saturday = last_friday + datetime.timedelta(days=1)

    separators = [".", "/", "-"]
    date_formats = set()

    for target_date in (last_friday, last_saturday):
        days = [str(target_date.day), target_date.strftime("%d")]
        months = [str(target_date.month), target_date.strftime("%m")]
        years = [str(target_date.year), str(target_date.year)[2:]]
        for d in days:
            for m in months:
                for y in years:
                    for sep in separators:
                        date_formats.add(f"{d}{sep}{m}{sep}{y}")
    return date_formats


def find_latest_friday_video(url: str, max_videos: int = MAX_VIDEOS_TO_SCAN):
    """Scan the configured channel/playlist and select a Friday/Saturday video by title."""
    target_dates = get_target_dates_strings()
    opts, cookie_file, temporary = _youtube_opts(
        {
            "extract_flat": True,
            "skip_download": True,
            "playlistend": max_videos,
        }
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None, None

            entries = info.get("entries") or []
            if not entries and info.get("id"):
                entries = [info]

            for video in entries:
                if not video:
                    continue
                title = video.get("title") or ""
                video_id = video.get("id") or video.get("video_id")
                if not video_id:
                    continue
                if any(d in title for d in target_dates):
                    return f"https://www.youtube.com/watch?v={video_id}", title

            return None, None
    finally:
        _cleanup_cookie(cookie_file, temporary)


def extract_video_id(url_or_id: str) -> str:
    value = url_or_id.strip()
    patterns = [
        r"(?:v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/(?:shorts|embed|live)/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise ValueError("לא ניתן לזהות Video ID מתוך קישור YouTube שסופק.")


def _clean_vtt(vtt_text: str) -> str:
    """Convert VTT into readable text and remove duplicate caption lines."""
    text = vtt_text.replace("\ufeff", "")
    lines = text.splitlines()
    result = []
    previous = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line == "WEBVTT" or line.startswith("NOTE"):
            continue
        if re.match(r"^\d+$", line):
            continue
        if "-->" in line:
            continue
        # Remove common VTT markup.
        line = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", line)
        line = re.sub(r"</?c(?:\.[^>]*)?>", "", line)
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"&amp;", "&", line)
        line = re.sub(r"&lt;", "<", line)
        line = re.sub(r"&gt;", ">", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if line != previous:
            result.append(line)
            previous = line

    return "\n".join(result)


def fetch_transcript(video_url: str) -> str:
    """Download YouTube captions through yt-dlp, using cookies when supplied."""
    video_id = extract_video_id(video_url)
    with tempfile.TemporaryDirectory(prefix=f"yt_{video_id}_") as workdir:
        outtmpl = str(Path(workdir) / "caption.%(ext)s")
        opts, cookie_file, temporary = _youtube_opts(
            {
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": LANGUAGES,
                "subtitlesformat": "vtt",
                "outtmpl": outtmpl,
            }
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
                if not info:
                    raise RuntimeError("YouTube לא החזיר מידע על הסרטון.")

                # download subtitles only; no video is downloaded
                ydl.download([video_url])

            candidates = sorted(Path(workdir).glob("*.vtt"))
            if not candidates:
                raise RuntimeError(
                    "לא נמצאו כתוביות/טרנסקריפט לסרטון. אם הסרטון מוגן, ודא שקובץ cookies.txt תקין ומעודכן."
                )

            # Prefer Arabic, then Hebrew, then English.
            def score(path):
                name = path.name.lower()
                return next((i for i, lang in enumerate(LANGUAGES) if f".{lang}." in name), 99)

            candidates.sort(key=score)
            transcript = _clean_vtt(candidates[0].read_text(encoding="utf-8", errors="replace"))
            if not transcript.strip():
                raise RuntimeError("קובץ הכתוביות נמצא אך הוא ריק.")
            return transcript
        finally:
            _cleanup_cookie(cookie_file, temporary)


def get_video_title(video_url: str) -> str:
    opts, cookie_file, temporary = _youtube_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            return (info or {}).get("title") or "סרטון יוטיוב"
    finally:
        _cleanup_cookie(cookie_file, temporary)


class GeminiTranslator:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY חסר ב-Environment Variables.")
        self.client = genai.Client(api_key=api_key)
        self.models = [
            os.getenv("GEMINI_PRIMARY_MODEL", "gemini-2.5-flash"),
            os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"),
        ]

    def translate_text(self, text: str) -> str:
        """Translate the complete transcript. For long transcripts, process safe chunks."""
        # Keep chunks comfortably below common request limits.
        chunk_size = int(os.getenv("TRANSLATION_CHUNK_CHARS", "45000"))
        chunks = split_text(text, chunk_size)
        translated = []

        system_instruction = (
            "אתה מתרגם מקצועי ומומחה לשפה הערבית והעברית. "
            "תרגם את התמלול מערבית לעברית. מדובר בתמלול של דרשה. "
            "התמלול עשוי להכיל שבירות שורה קצרות שנוצרו מאופי הכתוביות; התעלם מהן וחבר את המשפטים לרצף טבעי.\n"
            "כללים מחייבים:\n"
            "1. תרגום מלא ומדויק. אין לסכם, לקצר, להשמיט או להוסיף תוכן.\n"
            "2. שמור על משמעות המקור, שמות, מונחים וציטוטים ככל שניתן.\n"
            "3. עברית גבוהה, מכובדת, רהוטה וטבעית המתאימה לדרשה.\n"
            "4. החזר פסקאות רציפות ולא שורה חדשה אחרי כל משפט.\n"
            "5. אל תוסיף הערות, הקדמות, הסברים או כותרות משלך.\n"
            "6. החזר אך ורק את התרגום."
        )

        for index, chunk in enumerate(chunks):
            prompt = (
                "תרגם את החלק הבא במלואו. "
                "זהו חלק מתוך תמלול ארוך, לכן אל תסכם ואל תדלג על דבר.\n\n" + chunk
            )
            result = None
            last_error = None
            for model in self.models:
                for attempt in range(1, 4):
                    try:
                        response = self.client.models.generate_content(
                            model=model,
                            contents=[prompt],
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                temperature=0.2,
                            ),
                        )
                        if response.text and response.text.strip():
                            result = response.text.strip()
                            break
                    except APIError as exc:
                        last_error = exc
                        code = getattr(exc, "code", None)
                        if code in (429, 500, 502, 503, 504) or any(
                            x in str(exc).upper() for x in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "TIMEOUT")
                        ):
                            time_sleep = min(60, 5 * (2 ** (attempt - 1)))
                            import time
                            time.sleep(time_sleep)
                            continue
                        break
                    except Exception as exc:
                        last_error = exc
                        break
                if result:
                    break

            if not result:
                raise RuntimeError(f"Gemini נכשל בתרגום חלק {index + 1}/{len(chunks)}: {last_error}")
            translated.append(result)

        return "\n\n".join(translated)


def split_text(text: str, max_chars: int):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for i in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[i:i + max_chars].strip())
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > max_chars:
            chunks.append(current.strip())
            current = paragraph
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


async def send_to_telegram(full_text: str, video_title: str, video_url: str):
    api_id_env = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session_str = os.getenv("TELEGRAM_SESSION_STRING")
    target_user = os.getenv("TELEGRAM_TARGET_USER")
    if not all([api_id_env, api_hash, session_str, target_user]):
        return False, "משתני טלגרם חסרים; התרגום הושלם אך לא נשלח לטלגרם."

    api_id = int(api_id_env)
    chunks = split_text(full_text, MAX_TELEGRAM_CHARS)
    async with TelegramClient(StringSession(session_str), api_id, api_hash) as client:
        await client.send_message(
            target_user,
            f"🎬 **תרגום אוטומטי לדרשה**\n📌 **כותרת:** {video_title}\n\nהתוכן מתחיל מטה 👇",
        )
        for chunk in chunks:
            await client.send_message(target_user, chunk)
            await asyncio.sleep(1.1)
        await client.send_message(
            target_user,
            f"🔗 **קישור לסרטון המקורי ביוטיוב:**\n{video_url}\n\n✨ המשימה הושלמה בהצלחה!",
        )
    return True, "נשלח לטלגרם בהצלחה."


def run_pipeline(video_url: Optional[str] = None, speaker: Optional[str] = None):
    """Main pipeline. Direct URL always bypasses speaker/date discovery."""
    try:
        if video_url and video_url.strip():
            video_url = video_url.strip()
            extract_video_id(video_url)
            title = get_video_title(video_url)
        elif speaker in SPEAKER_URLS:
            video_url, title = find_latest_friday_video(SPEAKER_URLS[speaker])
            if not video_url:
                return {"success": False, "message": f"לא נמצאה דרשה מתאימה עבור {speaker}."}
        else:
            return {"success": False, "message": "יש לבחור דרשן או להזין קישור YouTube."}

        transcript = fetch_transcript(video_url)
        translator = GeminiTranslator(os.getenv("GEMINI_API_KEY", ""))
        translation = translator.translate_text(transcript)

        telegram_ok = False
        telegram_message = ""
        try:
            telegram_ok, telegram_message = asyncio.run(
                send_to_telegram(translation, title, video_url)
            )
        except Exception as exc:
            telegram_message = f"שגיאה בטלגרם: {exc}"

        return {
            "success": True,
            "message": f"התרגום הושלם. {telegram_message}",
            "video_url": video_url,
            "video_title": title,
            "translation": translation,
            "telegram_sent": telegram_ok,
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}
