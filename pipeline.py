import asyncio
import datetime
import os
import re
import time
import traceback
from typing import Optional

import yt_dlp

from youtube_transcript_api import (
    YouTubeTranscriptApi,
)
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
)

from google import genai
from google.genai import types
from google.genai.errors import APIError

from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# הגדרות
# ============================================================

MAX_TELEGRAM_CHARS = int(
    os.getenv("MAX_TELEGRAM_CHARS", "4000")
)

MAX_VIDEOS_TO_SCAN = int(
    os.getenv("MAX_VIDEOS_TO_SCAN", "20")
)

LANGUAGES = ["ar", "he", "en"]


# ============================================================
# כתובות הדרשנים
# ============================================================

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


# ============================================================
# Cookies עבור YouTube
# ============================================================

def _cookie_file_from_env():
    """
    מחזיר:
        (path, temporary)

    תומך בשתי אפשרויות:
    1. קובץ cookies.txt קיים בשרת
    2. טקסט cookies מתוך Environment Variable
    """

    explicit = os.getenv(
        "YOUTUBE_COOKIES_FILE",
        ""
    ).strip()

    if explicit and os.path.exists(explicit):
        return explicit, False

    text = os.getenv(
        "YOUTUBE_COOKIES_TEXT",
        ""
    )

    if text.strip():

        import tempfile

        f = tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            suffix=".txt",
            encoding="utf-8",
        )

        f.write(text)
        f.close()

        return f.name, True

    return None, False


def _youtube_opts(extra=None):
    """
    הגדרות yt-dlp.

    חשוב:
    yt-dlp משמש כאן רק לצורך איתור הסרטון.
    אין הורדת וידאו.
    """

    opts = {
        "quiet": True,
        "no_warnings": True,

        # לא להפיל את כל הסריקה בגלל סרטון אחד בעייתי
        "ignoreerrors": True,

        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,

        # חשוב מאוד:
        # כשאנחנו סורקים playlist אנחנו לא רוצים
        # להיכנס לכל סרטון ולהוריד אותו.
        "noplaylist": False,

        # איתור metadata בלבד
        "extract_flat": True,
        "skip_download": True,
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


# ============================================================
# חישוב תאריכי שישי ושבת
# ============================================================

def get_target_dates_strings():

    today = datetime.date.today()

    # יום שישי = 4
    days_to_subtract = (
        today.weekday() - 4
    ) % 7

    last_friday = (
        today
        - datetime.timedelta(
            days=days_to_subtract
        )
    )

    last_saturday = (
        last_friday
        + datetime.timedelta(days=1)
    )

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

                        date_formats.add(
                            f"{d}{sep}{m}{sep}{y}"
                        )

    add_date_combinations(last_friday)
    add_date_combinations(last_saturday)

    return list(date_formats)


# ============================================================
# איתור הדרשה האחרונה
# ============================================================

def find_latest_friday_video(
    url,
    max_videos=MAX_VIDEOS_TO_SCAN,
):
    """
    סורק את הערוץ/playlist ומאתר סרטון שהכותרת שלו
    מכילה תאריך של יום שישי או שבת האחרונים.

    חשוב:
    כאן בלבד אנחנו משתמשים ב-yt-dlp.

    אין הורדת וידאו.
    """

    target_dates = get_target_dates_strings()

    print()
    print("=" * 70)
    print("🔎 איתור הדרשה האחרונה")
    print("=" * 70)

    print(
        f"📅 היום: "
        f"{datetime.date.today().strftime('%d.%m.%Y')}"
    )

    print(
        f"🔍 בודק תאריכים של שישי/שבת האחרונים"
    )

    print(
        f"📊 מספר קומבינציות תאריך: "
        f"{len(target_dates)}"
    )

    print(
        f"🔄 סריקה של עד {max_videos} סרטונים"
    )

    print(
        f"🔗 מקור: {url}"
    )

    print("-" * 70)

    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "playlistend": max_videos,
        "ignoreerrors": True,
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
    }

    cookie_file, temporary = _cookie_file_from_env()

    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    try:

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            try:

                result_dict = ydl.extract_info(
                    url,
                    download=False,
                )

            except Exception as e:

                print(
                    f"⚠️ שגיאה בסריקת YouTube: {e}"
                )

                traceback.print_exc()

                return None, None

            if not result_dict:

                print(
                    "❌ לא התקבל מידע מהקישור."
                )

                return None, None

            videos = (
                result_dict.get("entries")
                or []
            )

            if (
                not videos
                and result_dict.get("title")
            ):

                videos = [
                    result_dict
                ]

            print(
                f"📋 נמצאו "
                f"{len(videos)} פריטים לסריקה."
            )

            for index, video in enumerate(
                videos,
                start=1,
            ):

                if not video:
                    continue

                title = (
                    video.get("title")
                    or ""
                )

                video_id = (
                    video.get("id")
                    or video.get("video_id")
                )

                if not video_id:
                    continue

                video_url = (
                    "https://www.youtube.com/watch?v="
                    + video_id
                )

                print()
                print(
                    f"#{index}: {title}"
                )

                if any(
                    target_date in title
                    for target_date in target_dates
                ):

                    print(
                        "🎉 נמצא הסרטון המתאים!"
                    )

                    print(
                        f"   📌 כותרת: {title}"
                    )

                    print(
                        f"   🔗 קישור: {video_url}"
                    )

                    return (
                        video_url,
                        title,
                    )

            print()
            print(
                "❌ לא נמצא סרטון מתאים "
                "ליום שישי או שבת האחרונים."
            )

            return None, None

    finally:

        _cleanup_cookie(
            cookie_file,
            temporary,
        )


# ============================================================
# חילוץ Video ID
# ============================================================

def extract_video_id(
    url_or_id: str,
) -> str:

    value = url_or_id.strip()

    patterns = [

        # https://www.youtube.com/watch?v=XXXXXXXXXXX
        # וגם URL עם &list וכו'
        r"(?:[?&]v=)([A-Za-z0-9_-]{11})",

        # https://youtu.be/XXXXXXXXXXX
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",

        # shorts / embed / live
        r"(?:youtube\.com/"
        r"(?:shorts|embed|live)/)"
        r"([A-Za-z0-9_-]{11})",

        # ID ישיר
        r"^([A-Za-z0-9_-]{11})$",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            value,
        )

        if match:

            return match.group(1)

    raise ValueError(
        f"לא ניתן לחלץ Video ID מ: "
        f"{url_or_id}"
    )


# ============================================================
# חילוץ טרנסקריפט
# ============================================================

def fetch_transcript(
    url_or_id: str,
) -> str:
    """
    חילוץ הטרנסקריפט באמצעות
    YouTubeTranscriptApi.

    חשוב:
    כאן אין yt-dlp.
    אין הורדת וידאו.
    """

    video_id = extract_video_id(
        url_or_id
    )

    print()
    print("=" * 70)
    print("📝 חילוץ טרנסקריפט")
    print("=" * 70)

    print(
        f"🎬 Video ID: {video_id}"
    )

    print(
        f"🌐 שפות מבוקשות: "
        f"{', '.join(LANGUAGES)}"
    )

    print(
        "⬇️ מוריד רק את נתוני הטרנסקריפט "
        "ולא את הסרטון."
    )

    try:

        ytt_api = (
            YouTubeTranscriptApi()
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
                "הטרנסקריפט נמצא אך הוא ריק."
            )

        print(
            f"✅ הטרנסקריפט חולץ בהצלחה."
        )

        print(
            f"📊 אורך הטרנסקריפט: "
            f"{len(content):,} תווים"
        )

        return content

    except (
        TranscriptsDisabled,
        NoTranscriptFound,
    ):

        print(
            "❌ לא נמצאו כתוביות "
            "זמינות לסרטון."
        )

        raise

    except Exception as e:

        print(
            f"❌ שגיאה בחילוץ הטרנסקריפט: "
            f"{e}"
        )

        traceback.print_exc()

        raise


# ============================================================
# תרגום באמצעות Gemini
# ============================================================

class GeminiTranslator:

    def __init__(
        self,
        api_key: str,
    ):

        if not api_key:

            raise ValueError(
                "GEMINI_API_KEY חסר "
                "ב-Environment Variables."
            )

        self.client = genai.Client(
            api_key=api_key
        )

        self.primary_model = os.getenv(
            "GEMINI_PRIMARY_MODEL",
            "gemini-2.5-flash",
        )

        self.fallback_model = os.getenv(
            "GEMINI_FALLBACK_MODEL",
            "gemini-3.1-flash-lite",
        )

    def translate_large_text(
        self,
        text: str,
    ) -> str:
        """
        מתרגם את הטרנסקריפט.
        """

        if not text.strip():

            raise ValueError(
                "הטרנסקריפט ריק."
            )

        print()
        print("=" * 70)
        print("🌍 תרגום באמצעות Gemini")
        print("=" * 70)

        # חלוקה כדי לא להיתקע על מגבלת גודל
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
            f"📊 הטרנסקריפט חולק "
            f"ל-{len(chunks)} חלקים."
        )

        system_instruction = (
            "אתה מתרגם מקצועי ומומחה "
            "לשפה הערבית והעברית. "
            "תרגם את התמלול מערבית לעברית. "
            "מדובר בתמלול של דרשה. "
            "התמלול עשוי להכיל שבירות שורה "
            "קצרות שנוצרו מאופי הכתוביות; "
            "התעלם מהן וחבר את המשפטים "
            "לרצף טבעי.\n"

            "כללים מחייבים:\n"

            "1. תרגום מלא ומדויק. "
            "אין לסכם, לקצר, להשמיט "
            "או להוסיף תוכן.\n"

            "2. שמור על משמעות המקור, "
            "שמות, מונחים וציטוטים ככל שניתן.\n"

            "3. עברית גבוהה, מכובדת, "
            "רהוטה וטבעית המתאימה לדרשה.\n"

            "4. החזר פסקאות רציפות "
            "ולא שורה חדשה אחרי כל משפט.\n"

            "5. אל תוסיף הערות, הקדמות, "
            "הסברים או כותרות משלך.\n"

            "6. החזר אך ורק את התרגום."
        )

        translated_chunks = []

        models_to_try = [
            self.primary_model,
            self.fallback_model,
        ]

        for index, chunk in enumerate(
            chunks,
            start=1,
        ):

            print(
                f"🤖 מתרגם חלק "
                f"{index}/{len(chunks)}"
            )

            prompt = (
                "תרגם את החלק הבא במלואו. "
                "זהו חלק מתוך תמלול ארוך, "
                "לכן אל תסכם ואל תדלג על דבר.\n\n"
                + chunk
            )

            result = None

            last_error = None

            for model in models_to_try:

                print(
                    f"   🔹 מודל: {model}"
                )

                for attempt in range(
                    1,
                    4,
                ):

                    try:

                        response = (
                            self.client.models.generate_content(
                                model=model,
                                contents=[prompt],
                                config=types.GenerateContentConfig(
                                    system_instruction=system_instruction,
                                    temperature=0.2,
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
                                "   ✅ החלק תורגם."
                            )

                            break

                    except APIError as exc:

                        last_error = exc

                        code = getattr(
                            exc,
                            "code",
                            None,
                        )

                        print(
                            f"   ⚠️ Gemini API error "
                            f"{code}: {exc}"
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
                                    2 ** (
                                        attempt - 1
                                    )
                                ),
                            )

                            print(
                                f"   ⏳ ממתין "
                                f"{wait_time} שניות..."
                            )

                            time.sleep(
                                wait_time
                            )

                            continue

                        break

                    except Exception as exc:

                        last_error = exc

                        print(
                            f"   ❌ שגיאה: "
                            f"{exc}"
                        )

                        break

                if result:

                    break

            if not result:

                raise RuntimeError(
                    f"Gemini נכשל בתרגום "
                    f"חלק {index}/"
                    f"{len(chunks)}: "
                    f"{last_error}"
                )

            translated_chunks.append(
                result
            )

        translation = "\n\n".join(
            translated_chunks
        )

        print(
            f"✅ התרגום הסתיים. "
            f"{len(translation):,} תווים."
        )

        return translation


# ============================================================
# פיצול טקסט
# ============================================================

def split_text(
    text: str,
    max_chars: int,
):
    """
    מפצל טקסט לפי פסקאות ככל האפשר.
    """

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

            if current:

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
# שליחה לטלגרם
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
            "התרגום הושלם אך "
            "לא נשלח לטלגרם.",
        )

    api_id = int(
        api_id_env
    )

    chunks = split_text(
        full_text,
        MAX_TELEGRAM_CHARS,
    )

    print()
    print("=" * 70)
    print("📨 שליחה לטלגרם")
    print("=" * 70)

    print(
        f"📊 מספר הודעות: "
        f"{len(chunks)}"
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
                f"📤 שולח הודעה "
                f"{index}/{len(chunks)}..."
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
                "🔗 **קישור לסרטון המקורי "
                "ביוטיוב:**\n"
                f"{video_url}\n\n"
                "✨ המשימה הושלמה בהצלחה!"
            ),
        )

    print(
        "✅ התרגום נשלח לטלגרם בהצלחה."
    )

    return (
        True,
        "נשלח לטלגרם בהצלחה.",
    )


# ============================================================
# Pipeline
# ============================================================

def run_pipeline(
    video_url: Optional[str] = None,
    speaker: Optional[str] = None,
):
    """
    Pipeline ראשי.

    אפשרות 1:
        speaker="salah"

    אפשרות 2:
        speaker="khateb"

    אפשרות 3:
        video_url="https://www.youtube.com/watch?v=..."

    במקרה של URL ישיר:
    yt-dlp אינו מופעל בכלל.
    """

    print()
    print("=" * 80)
    print("🚀 PIPELINE START")
    print("=" * 80)

    print(
        f"[PIPELINE] video_url={video_url}"
    )

    print(
        f"[PIPELINE] speaker={speaker}"
    )

    try:

        # ====================================================
        # שלב 1 — קביעת הסרטון
        # ====================================================

        if (
            video_url
            and video_url.strip()
        ):

            video_url = (
                video_url.strip()
            )

            print()
            print(
                "🔗 התקבל URL ישיר."
            )

            video_id = extract_video_id(
                video_url
            )

            print(
                f"🎬 Video ID: {video_id}"
            )

            # בכוונה לא קוראים כאן ל-yt-dlp.
            #
            # זה חשוב:
            # אם YouTube/yt-dlp מחזיר
            # Requested format is not available,
            # זה לא אמור לעצור URL ישיר.
            #
            video_title = (
                f"YouTube video {video_id}"
            )

        elif speaker in SPEAKER_URLS:

            print()
            print(
                f"🎤 נבחר דרשן: {speaker}"
            )

            video_url, video_title = (
                find_latest_friday_video(
                    SPEAKER_URLS[speaker]
                )
            )

            if not video_url:

                return {
                    "success": False,
                    "message": (
                        "לא נמצאה דרשה מתאימה "
                        f"עבור {speaker}."
                    ),
                }

            video_id = extract_video_id(
                video_url
            )

        else:

            return {
                "success": False,
                "message": (
                    "יש לבחור דרשן או להזין "
                    "קישור YouTube."
                ),
            }

        print()
        print(
            f"🎬 סרטון: {video_url}"
        )

        print(
            f"📌 כותרת: {video_title}"
        )

        # ====================================================
        # שלב 2 — טרנסקריפט
        # ====================================================

        print()
        print(
            "📝 מתחיל חילוץ טרנסקריפט..."
        )

        transcript = fetch_transcript(
            video_url
        )

        print(
            f"✅ טרנסקריפט התקבל: "
            f"{len(transcript):,} תווים"
        )

        # ====================================================
        # שלב 3 — Gemini
        # ====================================================

        translator = GeminiTranslator(
            os.getenv(
                "GEMINI_API_KEY",
                "",
            )
        )

        translation = (
            translator.translate_large_text(
                transcript
            )
        )

        # ====================================================
        # שלב 4 — Telegram
        # ====================================================

        telegram_ok = False

        telegram_message = ""

        try:

            telegram_ok, telegram_message = (
                asyncio.run(
                    send_to_telegram(
                        translation,
                        video_title,
                        video_url,
                    )
                )
            )

        except Exception as exc:

            print(
                f"⚠️ שגיאה בטלגרם: "
                f"{exc}"
            )

            traceback.print_exc()

            telegram_message = (
                f"שגיאה בטלגרם: {exc}"
            )

        # ====================================================
        # הצלחה
        # ====================================================

        print()
        print("=" * 80)
        print("🎉 PIPELINE SUCCESS")
        print("=" * 80)

        return {
            "success": True,

            "message": (
                f"התרגום הושלם. "
                f"{telegram_message}"
            ),

            "video_url": video_url,

            "video_title": video_title,

            "translation": translation,

            "telegram_sent": telegram_ok,
        }

    except Exception as exc:

        print()
        print("=" * 80)
        print("❌ PIPELINE FAILED")
        print("=" * 80)

        print(
            f"Exception type: "
            f"{type(exc).__name__}"
        )

        print(
            f"Exception: {exc}"
        )

        print()
        print(
            "Full traceback:"
        )

        traceback.print_exc()

        print("=" * 80)

        return {
            "success": False,
            "message": str(exc),
        }


# ============================================================
# נקודת כניסה
# ============================================================

