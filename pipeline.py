import asyncio
import datetime
import os
import re
import time
from typing import Optional

import yt_dlp
from google import genai
from google.genai import types
from google.genai.errors import APIError
from telethon import TelegramClient
from telethon.sessions import StringSession

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
)

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


# ============================================================
# YouTube
# ============================================================

def _youtube_opts(extra=None):
    """
    הגדרות YouTube.
    חשוב:
    אנחנו לא מורידים וידאו.
    extract_flat משמש רק כאשר אנחנו סורקים רשימת סרטונים.
    """

    opts = {
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "noplaylist": True,
    }

    if extra:
        opts.update(extra)

    return opts


def get_target_dates_strings():
    """
    מחשב את יום שישי והשבת האחרונים
    ומחזיר את כל צורות הכתיבה הסבירות שלהם.
    """

    today = datetime.date.today()

    days_to_subtract = (today.weekday() - 4) % 7
    last_friday = today - datetime.timedelta(days=days_to_subtract)
    last_saturday = last_friday + datetime.timedelta(days=1)

    separators = [".", "/", "-"]
    date_formats = set()

    def add_date_combinations(target_date):
        days = [
            str(target_date.day),
            target_date.strftime("%d"),
        ]

        months = [
            str(target_date.month),
            target_date.strftime("%m"),
        ]

        years = [
            str(target_date.year),
            str(target_date.year)[2:],
        ]

        for d in days:
            for m in months:
                for y in years:
                    for sep in separators:
                        date_formats.add(f"{d}{sep}{m}{sep}{y}")

    add_date_combinations(last_friday)
    add_date_combinations(last_saturday)

    return date_formats


def find_latest_friday_video(
    url: str,
    max_videos: int = MAX_VIDEOS_TO_SCAN,
):
    """
    סורק את רשימת הסרטונים.

    עדיפות:
    1. סרטון שמכיל בכותרת את תאריך שישי/שבת האחרונים.
    2. אם לא נמצא כזה — הסרטון הראשון ברשימה.

    כך המערכת לא נעצרת כאשר הדרשה האחרונה
    לא מכילה תאריך בכותרת.
    """

    target_dates = get_target_dates_strings()

    today = datetime.date.today()

    print("=" * 80)
    print("[YOUTUBE] START SEARCH")
    print(f"[YOUTUBE] URL: {url}")
    print(f"[YOUTUBE] Today: {today.strftime('%d.%m.%Y')}")
    print(f"[YOUTUBE] Scanning up to {max_videos} videos")
    print("=" * 80)

    ydl_opts = _youtube_opts(
        {
            "extract_flat": True,
            "skip_download": True,
            "playlistend": max_videos,
        }
    )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            print("[YOUTUBE] Requesting video list...")

            result_dict = ydl.extract_info(
                url,
                download=False,
            )

            if not result_dict:
                print("[YOUTUBE] ERROR: no result returned.")
                return None, None

            entries = result_dict.get("entries") or []

            # במקרה שהקישור הוא סרטון בודד
            if not entries and result_dict.get("id"):
                entries = [result_dict]

            print(f"[YOUTUBE] Received {len(entries)} entries.")

            if not entries:
                print("[YOUTUBE] ERROR: no videos found.")
                return None, None

            first_valid_video = None

            for index, video in enumerate(entries, start=1):

                if not video:
                    continue

                title = video.get("title") or ""

                video_id = (
                    video.get("id")
                    or video.get("video_id")
                )

                if not video_id:
                    print(f"[YOUTUBE] #{index}: missing video ID")
                    continue

                video_url = (
                    f"https://www.youtube.com/watch?v={video_id}"
                )

                print(
                    f"[YOUTUBE] #{index}: {title}"
                )

                # שומרים את הסרטון הראשון כ-fallback
                if first_valid_video is None:
                    first_valid_video = (
                        video_url,
                        title,
                    )

                # בדיקת תאריך
                if any(
                    target_date in title
                    for target_date in target_dates
                ):
                    print()
                    print(
                        "[YOUTUBE] MATCH FOUND - "
                        "Friday/Saturday date detected"
                    )
                    print(f"[YOUTUBE] Title: {title}")
                    print(f"[YOUTUBE] URL: {video_url}")
                    print()

                    return video_url, title

            # ------------------------------------------------
            # FALLBACK
            # ------------------------------------------------

            if first_valid_video:
                video_url, title = first_valid_video

                print()
                print(
                    "[YOUTUBE] No Friday/Saturday date found."
                )
                print(
                    "[YOUTUBE] FALLBACK: using latest video."
                )
                print(
                    f"[YOUTUBE] Title: {title}"
                )
                print(
                    f"[YOUTUBE] URL: {video_url}"
                )
                print()

                return video_url, title

            print(
                "[YOUTUBE] ERROR: no valid video found."
            )

            return None, None

    except Exception as exc:

        print()
        print("[YOUTUBE] SEARCH FAILED")
        print(
            f"[YOUTUBE] Exception type: "
            f"{type(exc).__name__}"
        )
        print(
            f"[YOUTUBE] Exception: {exc}"
        )
        print()

        return None, None


# ============================================================
# Video ID
# ============================================================

def extract_video_id(url_or_id: str) -> str:
    """
    חילוץ Video ID מקישור YouTube.
    """

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

    raise ValueError(
        f"לא ניתן לזהות Video ID מתוך: {url_or_id}"
    )


# ============================================================
# Transcript
# ============================================================

def fetch_transcript(url_or_id: str) -> str:
    """
    מקבל transcript ישירות דרך YouTubeTranscriptApi.

    אין כאן הורדת וידאו.
    """

    video_id = extract_video_id(url_or_id)

    print("=" * 80)
    print("[TRANSCRIPT] START")
    print(f"[TRANSCRIPT] Video ID: {video_id}")
    print(f"[TRANSCRIPT] Languages: {LANGUAGES}")
    print("=" * 80)

    try:

        ytt_api = YouTubeTranscriptApi()

        print(
            "[TRANSCRIPT] Requesting transcript..."
        )

        fetched = ytt_api.fetch(
            video_id,
            languages=LANGUAGES,
        )

        content = "\n".join(
            snippet.text
            for snippet in fetched.snippets
        )

        if not content.strip():
            raise RuntimeError(
                "התקבל transcript ריק."
            )

        print(
            f"[TRANSCRIPT] SUCCESS - "
            f"{len(content):,} characters"
        )

        return content

    except TranscriptsDisabled:
        print(
            "[TRANSCRIPT] ERROR: "
            "Transcripts are disabled."
        )
        raise

    except NoTranscriptFound:
        print(
            "[TRANSCRIPT] ERROR: "
            "No transcript found."
        )
        raise

    except Exception as exc:

        print()
        print("[TRANSCRIPT] FAILED")
        print(
            f"[TRANSCRIPT] Exception type: "
            f"{type(exc).__name__}"
        )
        print(
            f"[TRANSCRIPT] Exception: {exc}"
        )
        print()

        raise


# ============================================================
# Optional title lookup
# ============================================================

def get_video_title(video_url: str) -> str:
    """
    ניסיון לקבל כותרת לסרטון ישיר.

    הכותרת אינה קריטית להמשך התהליך.
    אם YouTube מסרב להחזיר אותה, נחזיר כותרת כללית
    ולא נפיל את כל ה-pipeline.
    """

    try:

        video_id = extract_video_id(video_url)

        opts = _youtube_opts(
            {
                "extract_flat": True,
                "skip_download": True,
            }
        )

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(
                video_url,
                download=False,
            )

            title = (
                (info or {}).get("title")
                or ""
            )

            if title.strip():
                return title.strip()

    except Exception as exc:

        print(
            "[YOUTUBE] Could not retrieve title "
            f"for direct URL: {exc}"
        )

    return "סרטון YouTube"


# ============================================================
# Gemini Translator
# ============================================================

class GeminiTranslator:

    def __init__(self, api_key: str):

        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY חסר ב-Environment Variables."
            )

        self.client = genai.Client(
            api_key=api_key
        )

        self.models = [
            os.getenv(
                "GEMINI_PRIMARY_MODEL",
                "gemini-2.5-flash",
            ),
            os.getenv(
                "GEMINI_FALLBACK_MODEL",
                "gemini-3.1-flash-lite",
            ),
        ]

    def translate_text(
        self,
        text: str,
    ) -> str:

        print("=" * 80)
        print("[GEMINI] START TRANSLATION")
        print(
            f"[GEMINI] Input characters: "
            f"{len(text):,}"
        )
        print("=" * 80)

        chunk_size = int(
            os.getenv(
                "TRANSLATION_CHUNK_CHARS",
                "45000",
            )
        )

        chunks = split_text(
            text,
            chunk_size,
        )

        print(
            f"[GEMINI] Number of chunks: "
            f"{len(chunks)}"
        )

        translated = []

        system_instruction = (
            "אתה מתרגם מקצועי ומומחה לשפה "
            "הערבית והעברית. "
            "תרגם את התמלול מערבית לעברית. "
            "מדובר בתמלול של דרשה. "
            "התמלול עשוי להכיל שבירות שורה "
            "קצרות שנוצרו מאופי הכתוביות; "
            "התעלם מהן וחבר את המשפטים "
            "לרצף טבעי.\n"
            "כללים מחייבים:\n"
            "1. תרגום מלא ומדויק. אין לסכם, "
            "לקצר, להשמיט או להוסיף תוכן.\n"
            "2. שמור על משמעות המקור, שמות, "
            "מונחים וציטוטים ככל שניתן.\n"
            "3. עברית גבוהה, מכובדת, רהוטה "
            "וטבעית המתאימה לדרשה.\n"
            "4. החזר פסקאות רציפות ולא שורה "
            "חדשה אחרי כל משפט.\n"
            "5. אל תוסיף הערות, הקדמות, "
            "הסברים או כותרות משלך.\n"
            "6. החזר אך ורק את התרגום."
        )

        for index, chunk in enumerate(
            chunks,
            start=1,
        ):

            print(
                f"[GEMINI] Translating "
                f"chunk {index}/{len(chunks)}..."
            )

            prompt = (
                "תרגם את החלק הבא במלואו. "
                "זהו חלק מתוך תמלול ארוך, "
                "לכן אל תסכם ואל תדלג על דבר.\n\n"
                + chunk
            )

            result = None
            last_error = None

            for model in self.models:

                print(
                    f"[GEMINI] Model: {model}"
                )

                for attempt in range(1, 4):

                    try:

                        response = (
                            self.client.models.generate_content(
                                model=model,
                                contents=[prompt],
                                config=(
                                    types.GenerateContentConfig(
                                        system_instruction=(
                                            system_instruction
                                        ),
                                        temperature=0.2,
                                    )
                                ),
                            )
                        )

                        if (
                            response.text
                            and response.text.strip()
                        ):

                            result = (
                                response.text.strip()
                            )

                            print(
                                f"[GEMINI] Chunk "
                                f"{index} completed."
                            )

                            break

                    except APIError as exc:

                        last_error = exc

                        code = getattr(
                            exc,
                            "code",
                            None,
                        )

                        if (
                            code in (
                                429,
                                500,
                                502,
                                503,
                                504,
                            )
                            or any(
                                x in str(exc).upper()
                                for x in (
                                    "RESOURCE_EXHAUSTED",
                                    "UNAVAILABLE",
                                    "TIMEOUT",
                                )
                            )
                        ):

                            wait_time = min(
                                60,
                                5 * (
                                    2 ** (attempt - 1)
                                ),
                            )

                            print(
                                f"[GEMINI] Temporary "
                                f"error {code}. "
                                f"Waiting {wait_time}s..."
                            )

                            time.sleep(
                                wait_time
                            )

                            continue

                        break

                    except Exception as exc:

                        last_error = exc

                        print(
                            "[GEMINI] Unexpected error: "
                            f"{exc}"
                        )

                        break

                if result:
                    break

            if not result:

                raise RuntimeError(
                    f"Gemini נכשל בתרגום "
                    f"חלק {index}/{len(chunks)}: "
                    f"{last_error}"
                )

            translated.append(result)

        final_text = "\n\n".join(
            translated
        )

        print(
            f"[GEMINI] Translation complete. "
            f"Output characters: "
            f"{len(final_text):,}"
        )

        return final_text


# ============================================================
# Text splitting
# ============================================================

def split_text(
    text: str,
    max_chars: int,
):

    paragraphs = [
        p.strip()
        for p in re.split(
            r"\n\s*\n",
            text,
        )
        if p.strip()
    ]

    chunks = []
    current = ""

    for paragraph in paragraphs:

        if len(paragraph) > max_chars:

            if current:
                chunks.append(
                    current.strip()
                )
                current = ""

            for i in range(
                0,
                len(paragraph),
                max_chars,
            ):

                chunks.append(
                    paragraph[
                        i:i + max_chars
                    ].strip()
                )

            continue

        candidate = (
            f"{current}\n\n{paragraph}"
            if current
            else paragraph
        )

        if len(candidate) > max_chars:

            if current.strip():
                chunks.append(
                    current.strip()
                )

            current = paragraph

        else:
            current = candidate

    if current.strip():
        chunks.append(
            current.strip()
        )

    return chunks


# ============================================================
# Telegram
# ============================================================

async def send_to_telegram(
    full_text: str,
    video_title: str,
    video_url: str,
):

    api_id_env = os.getenv(
        "TELEGRAM_API_ID"
    )

    api_hash = os.getenv(
        "TELEGRAM_API_HASH"
    )

    session_str = os.getenv(
        "TELEGRAM_SESSION_STRING"
    )

    target_user = os.getenv(
        "TELEGRAM_TARGET_USER"
    )

    if not all(
        [
            api_id_env,
            api_hash,
            session_str,
            target_user,
        ]
    ):

        return (
            False,
            "משתני טלגרם חסרים; "
            "התרגום הושלם אך לא נשלח לטלגרם.",
        )

    api_id = int(api_id_env)

    chunks = split_text(
        full_text,
        MAX_TELEGRAM_CHARS,
    )

    print(
        f"[TELEGRAM] Sending "
        f"{len(chunks)} chunks..."
    )

    async with TelegramClient(
        StringSession(session_str),
        api_id,
        api_hash,
    ) as client:

        await client.send_message(
            target_user,
            (
                "🎬 **תרגום אוטומטי לדרשה**\n"
                f"📌 **כותרת:** {video_title}\n\n"
                "התוכן מתחיל מטה 👇"
            ),
        )

        for index, chunk in enumerate(
            chunks,
            start=1,
        ):

            print(
                f"[TELEGRAM] Sending "
                f"{index}/{len(chunks)}"
            )

            await client.send_message(
                target_user,
                chunk,
            )

            await asyncio.sleep(
                1.1
            )

        await client.send_message(
            target_user,
            (
                "🔗 **קישור לסרטון "
                "המקורי ביוטיוב:**\n"
                f"{video_url}\n\n"
                "✨ המשימה הושלמה בהצלחה!"
            ),
        )

    return (
        True,
        "נשלח לטלגרם בהצלחה.",
    )


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_pipeline(
    video_url: Optional[str] = None,
    speaker: Optional[str] = None,
):

    print()
    print("=" * 80)
    print("[PIPELINE] START")
    print(f"[PIPELINE] video_url={video_url}")
    print(f"[PIPELINE] speaker={speaker}")
    print("=" * 80)

    try:

        # ====================================================
        # 1. Direct URL
        # ====================================================

        if video_url and video_url.strip():

            video_url = video_url.strip()

            print(
                "[PIPELINE] Direct YouTube URL supplied"
            )

            video_id = extract_video_id(
                video_url
            )

            print(
                f"[PIPELINE] Video ID: {video_id}"
            )

            # ------------------------------------------------
            # חשוב:
            # לא מפילים את התהליך בגלל title.
            # קודם transcript.
            # ------------------------------------------------

            title = get_video_title(
                video_url
            )

            print(
                f"[PIPELINE] Title: {title}"
            )

        # ====================================================
        # 2. Speaker / automatic discovery
        # ====================================================

        elif speaker in SPEAKER_URLS:

            print(
                f"[PIPELINE] Speaker selected: "
                f"{speaker}"
            )

            video_url, title = (
                find_latest_friday_video(
                    SPEAKER_URLS[speaker],
                    max_videos=MAX_VIDEOS_TO_SCAN,
                )
            )

            if not video_url:

                return {
                    "success": False,
                    "message": (
                        f"לא נמצאה דרשה מתאימה "
                        f"עבור {speaker}."
                    ),
                }

        else:

            return {
                "success": False,
                "message": (
                    "יש לבחור דרשן או להזין "
                    "קישור YouTube."
                ),
            }

        # ====================================================
        # 3. Transcript
        # ====================================================

        print()
        print(
            "[PIPELINE] Fetching transcript..."
        )

        transcript = fetch_transcript(
            video_url
        )

        if not transcript.strip():

            raise RuntimeError(
                "התקבל transcript ריק."
            )

        print(
            f"[PIPELINE] Transcript received: "
            f"{len(transcript):,} chars"
        )

        # ====================================================
        # 4. Gemini
        # ====================================================

        print()
        print(
            "[PIPELINE] Starting Gemini translation..."
        )

        translator = GeminiTranslator(
            os.getenv(
                "GEMINI_API_KEY",
                "",
            )
        )

        translation = (
            translator.translate_text(
                transcript
            )
        )

        # ====================================================
        # 5. Telegram
        # ====================================================

        telegram_ok = False
        telegram_message = ""

        try:

            telegram_ok, telegram_message = (
                asyncio.run(
                    send_to_telegram(
                        translation,
                        title,
                        video_url,
                    )
                )
            )

        except Exception as exc:

            telegram_message = (
                f"שגיאה בטלגרם: {exc}"
            )

            print(
                f"[PIPELINE] Telegram error: "
                f"{exc}"
            )

        # ====================================================
        # SUCCESS
        # ====================================================

        print()
        print("=" * 80)
        print("[PIPELINE] SUCCESS")
        print("=" * 80)

        return {
            "success": True,
            "message": (
                f"התרגום הושלם. "
                f"{telegram_message}"
            ),
            "video_url": video_url,
            "video_title": title,
            "translation": translation,
            "telegram_sent": telegram_ok,
        }

    except (
        TranscriptsDisabled,
        NoTranscriptFound,
    ) as exc:

        print()
        print(
            "[PIPELINE] TRANSCRIPT ERROR"
        )
        print(
            f"[PIPELINE] {type(exc).__name__}: "
            f"{exc}"
        )

        return {
            "success": False,
            "message": (
                "לא נמצאו כתוביות/Transcript "
                "עבור הסרטון."
            ),
        }

    except Exception as exc:

        print()
        print("=" * 80)
        print("[PIPELINE] !!! FAILED !!!")
        print("=" * 80)
        print(
            f"[PIPELINE] Exception type: "
            f"{type(exc).__name__}"
        )
        print(
            f"[PIPELINE] Exception: {exc}"
        )
        print("=" * 80)

        return {
            "success": False,
            "message": str(exc),
        }
