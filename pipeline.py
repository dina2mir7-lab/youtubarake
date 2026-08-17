import asyncio
import os
import re
import tempfile
import time
from typing import Optional
from urllib.request import Request, urlopen
import traceback

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
    Build yt-dlp options.

    Important:
    These options are used only for metadata/subtitle extraction.
    No video format is selected and no video is downloaded.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
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


def find_latest_friday_video(
    url: str,
    max_videos: int = MAX_VIDEOS_TO_SCAN,
):
    """
    Find the newest video published on the configured YouTube
    channel/playlist.

    IMPORTANT:
    This function only retrieves YouTube metadata.
    It does NOT download any video.
    """

    opts, cookie_file, temporary = _youtube_opts(
        {
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": False,
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

            videos = []

            for video in entries:
                if not video:
                    continue

                video_id = video.get("id") or video.get("video_id")

                if not video_id:
                    continue

                title = video.get("title") or "סרטון יוטיוב"

                upload_date = video.get("upload_date") or ""
                timestamp = video.get("timestamp") or 0

                videos.append(
                    {
                        "id": video_id,
                        "title": title,
                        "upload_date": upload_date,
                        "timestamp": timestamp or 0,
                    }
                )

            if not videos:
                return None, None

            # Prefer the actual upload date.
            # timestamp is used as a fallback.
            def sort_key(video):
                upload_date = video["upload_date"]

                if upload_date and re.fullmatch(r"\d{8}", upload_date):
                    try:
                        return (
                            int(upload_date),
                            int(video["timestamp"] or 0),
                        )
                    except ValueError:
                        pass

                return (
                    0,
                    int(video["timestamp"] or 0),
                )

            videos.sort(key=sort_key, reverse=True)

            latest = videos[0]

            video_url = (
                f"https://www.youtube.com/watch?v={latest['id']}"
            )

            return video_url, latest["title"]

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

    raise ValueError(
        "לא ניתן לזהות Video ID מתוך קישור YouTube שסופק."
    )


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

        if line == "WEBVTT":
            continue

        if line.startswith("NOTE"):
            continue

        if re.match(r"^\d+$", line):
            continue

        if "-->" in line:
            continue

        # Remove common VTT markup.
        line = re.sub(
            r"<\d{2}:\d{2}:\d{2}\.\d{3}>",
            "",
            line,
        )

        line = re.sub(
            r"</?c(?:\.[^>]*)?>",
            "",
            line,
        )

        line = re.sub(r"<[^>]+>", "", line)

        line = re.sub(r"&amp;", "&", line)
        line = re.sub(r"&lt;", "<", line)
        line = re.sub(r"&gt;", ">", line)

        line = re.sub(r"\s+", " ", line).strip()

        if not line:
            continue

        # YouTube captions often repeat the previous caption.
        if line != previous:
            result.append(line)
            previous = line

    return "\n".join(result)


def _select_subtitle_track(info, language):
    """
    Select the best subtitle track for a requested language.

    Preference:
    1. manually supplied subtitles
    2. automatic captions
    """

    subtitles = info.get("subtitles") or {}
    automatic_captions = info.get("automatic_captions") or {}

    # Manual subtitles first.
    tracks = subtitles.get(language)

    if tracks:
        return tracks

    # Try regional variants, e.g. ar-SA / he-IL / en-US.
    for lang, lang_tracks in subtitles.items():
        if lang.lower().split("-")[0] == language:
            return lang_tracks

    # Automatic captions.
    tracks = automatic_captions.get(language)

    if tracks:
        return tracks

    # Regional variants for automatic captions.
    for lang, lang_tracks in automatic_captions.items():
        if lang.lower().split("-")[0] == language:
            return lang_tracks

    return None


def _select_vtt_format(tracks):
    """
    Select a VTT subtitle format from a YouTube subtitle track.
    """

    if not tracks:
        return None

    # Prefer VTT.
    for track in tracks:
        ext = (track.get("ext") or "").lower()

        if ext == "vtt" and track.get("url"):
            return track

    # Fall back to any text-based subtitle format.
    preferred_extensions = [
        "srv3",
        "srv2",
        "srv1",
        "ttml",
        "json3",
    ]

    for preferred_ext in preferred_extensions:
        for track in tracks:
            if (
                (track.get("ext") or "").lower() == preferred_ext
                and track.get("url")
            ):
                return track

    # Last resort: first track with a URL.
    for track in tracks:
        if track.get("url"):
            return track

    return None


def _download_subtitle_text(
    subtitle_url: str,
    ydl: yt_dlp.YoutubeDL,
) -> str:
    """
    Download only the subtitle resource.

    This does NOT download the YouTube video.
    """

    try:
        # Use yt-dlp's own HTTP client so cookies/headers configured
        # for YouTube can be reused.
        response = ydl.urlopen(subtitle_url)
        data = response.read()

        return data.decode("utf-8", errors="replace")

    except Exception:
        # Fallback for environments where ydl.urlopen is unavailable.
        request = Request(
            subtitle_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/150 Safari/537.36"
                )
            },
        )

        with urlopen(request, timeout=30) as response:
            return response.read().decode(
                "utf-8",
                errors="replace",
            )


def fetch_transcript(video_url: str) -> str:
    """
    Fetch YouTube transcript WITHOUT downloading the video.
    Includes detailed diagnostics for yt-dlp failures.
    """

    video_id = extract_video_id(video_url)

    print("=" * 80)
    print("[TRANSCRIPT] Starting transcript extraction")
    print(f"[TRANSCRIPT] Video URL: {video_url}")
    print(f"[TRANSCRIPT] Video ID: {video_id}")
    print("[TRANSCRIPT] IMPORTANT: video download is disabled")
    print("=" * 80)

    opts, cookie_file, temporary = _youtube_opts(
        {
            "skip_download": True,
            "noplaylist": True,
            "writesubtitles": False,
            "writeautomaticsub": False,
        }
    )

    print("[TRANSCRIPT] yt-dlp options:")
    print(
        {
            key: value
            for key, value in opts.items()
            if key != "cookiefile"
        }
    )
    print(
        f"[TRANSCRIPT] Cookies enabled: "
        f"{bool(cookie_file)}"
    )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:

            print(
                "[TRANSCRIPT] Calling "
                "extract_info(download=False)..."
            )

            try:
                info = ydl.extract_info(
                    video_url,
                    download=False,
                )

                print(
                    "[TRANSCRIPT] extract_info() completed successfully"
                )

            except Exception as exc:
                print("=" * 80)
                print("[TRANSCRIPT] !!! extract_info() FAILED !!!")
                print(f"[TRANSCRIPT] Exception type: {type(exc).__name__}")
                print(f"[TRANSCRIPT] Exception: {exc}")
                print("[TRANSCRIPT] Full traceback:")
                traceback.print_exc()
                print("=" * 80)

                raise

            if not info:
                raise RuntimeError(
                    "YouTube לא החזיר מידע על הסרטון."
                )

            print("[TRANSCRIPT] Metadata received")
            print(
                f"[TRANSCRIPT] Title: "
                f"{info.get('title')}"
            )
            print(
                f"[TRANSCRIPT] Upload date: "
                f"{info.get('upload_date')}"
            )
            print(
                f"[TRANSCRIPT] Duration: "
                f"{info.get('duration')}"
            )

            # --------------------------------------------------
            # Inspect subtitles
            # --------------------------------------------------

            subtitles = info.get("subtitles") or {}
            automatic_captions = (
                info.get("automatic_captions") or {}
            )

            print(
                "[TRANSCRIPT] Manual subtitle languages:"
            )
            print(
                list(subtitles.keys())
            )

            print(
                "[TRANSCRIPT] Automatic caption languages:"
            )
            print(
                list(automatic_captions.keys())
            )

            print(
                "[TRANSCRIPT] Requested language priority:"
            )
            print(LANGUAGES)

            subtitle_track = None
            selected_language = None
            selected_source = None

            # --------------------------------------------------
            # Search manual subtitles first
            # --------------------------------------------------

            for language in LANGUAGES:

                print(
                    f"[TRANSCRIPT] Checking manual subtitles "
                    f"for language: {language}"
                )

                tracks = _select_subtitle_track(
                    {
                        "subtitles": subtitles,
                        "automatic_captions": {},
                    },
                    language,
                )

                if tracks:
                    print(
                        f"[TRANSCRIPT] Found manual track "
                        f"for {language}"
                    )

                    subtitle_track = _select_vtt_format(
                        tracks
                    )

                    if subtitle_track:
                        selected_language = language
                        selected_source = "manual"
                        break

            # --------------------------------------------------
            # If no manual subtitles, search automatic captions
            # --------------------------------------------------

            if not subtitle_track:

                for language in LANGUAGES:

                    print(
                        f"[TRANSCRIPT] Checking automatic captions "
                        f"for language: {language}"
                    )

                    tracks = _select_subtitle_track(
                        {
                            "subtitles": {},
                            "automatic_captions": automatic_captions,
                        },
                        language,
                    )

                    if tracks:
                        print(
                            f"[TRANSCRIPT] Found automatic track "
                            f"for {language}"
                        )

                        subtitle_track = _select_vtt_format(
                            tracks
                        )

                        if subtitle_track:
                            selected_language = language
                            selected_source = "automatic"
                            break

            # --------------------------------------------------
            # No transcript
            # --------------------------------------------------

            if not subtitle_track:

                print("=" * 80)
                print(
                    "[TRANSCRIPT] !!! NO SUITABLE TRANSCRIPT FOUND !!!"
                )
                print(
                    f"[TRANSCRIPT] Manual languages: "
                    f"{list(subtitles.keys())}"
                )
                print(
                    f"[TRANSCRIPT] Automatic languages: "
                    f"{list(automatic_captions.keys())}"
                )
                print("=" * 80)

                raise RuntimeError(
                    "לא נמצאו כתוביות/טרנסקריפט "
                    "בשפות המבוקשות."
                )

            print("=" * 80)
            print("[TRANSCRIPT] SELECTED SUBTITLE")
            print(
                f"[TRANSCRIPT] Language: "
                f"{selected_language}"
            )
            print(
                f"[TRANSCRIPT] Source: "
                f"{selected_source}"
            )
            print(
                f"[TRANSCRIPT] Extension: "
                f"{subtitle_track.get('ext')}"
            )
            print(
                f"[TRANSCRIPT] Name: "
                f"{subtitle_track.get('name')}"
            )
            print(
                f"[TRANSCRIPT] URL exists: "
                f"{bool(subtitle_track.get('url'))}"
            )
            print("=" * 80)

            subtitle_url = subtitle_track.get("url")

            if not subtitle_url:
                raise RuntimeError(
                    "נמצא track לכתוביות אך אין לו URL."
                )

            print(
                "[TRANSCRIPT] Downloading SUBTITLE RESOURCE ONLY"
            )
            print(
                "[TRANSCRIPT] No video download is being performed"
            )

            try:
                subtitle_text = _download_subtitle_text(
                    subtitle_url,
                    ydl,
                )

            except Exception as exc:

                print("=" * 80)
                print(
                    "[TRANSCRIPT] !!! SUBTITLE DOWNLOAD FAILED !!!"
                )
                print(
                    f"[TRANSCRIPT] Exception type: "
                    f"{type(exc).__name__}"
                )
                print(
                    f"[TRANSCRIPT] Exception: {exc}"
                )
                print(
                    "[TRANSCRIPT] Full traceback:"
                )
                traceback.print_exc()
                print("=" * 80)

                raise

            print(
                f"[TRANSCRIPT] Subtitle response size: "
                f"{len(subtitle_text)} characters"
            )

            if not subtitle_text.strip():
                raise RuntimeError(
                    "קובץ הכתוביות התקבל אך הוא ריק."
                )

            transcript = _clean_vtt(
                subtitle_text
            )

            print(
                f"[TRANSCRIPT] Clean transcript size: "
                f"{len(transcript)} characters"
            )

            if not transcript.strip():
                raise RuntimeError(
                    "הכתוביות התקבלו אך לא ניתן "
                    "היה לחלץ מהן טקסט."
                )

            print("=" * 80)
            print(
                "[TRANSCRIPT] SUCCESS - transcript extracted"
            )
            print("=" * 80)

            return transcript

    finally:
        _cleanup_cookie(
            cookie_file,
            temporary,
        )

def get_video_title(video_url: str) -> str:
    """
    Retrieve video title only.
    No video is downloaded.
    """

    opts, cookie_file, temporary = _youtube_opts(
        {
            "skip_download": True,
            "noplaylist": True,
        }
    )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                video_url,
                download=False,
            )

            return (
                (info or {}).get("title")
                or "סרטון יוטיוב"
            )

    finally:
        _cleanup_cookie(
            cookie_file,
            temporary,
        )


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

    def translate_text(self, text: str) -> str:
        """Translate the complete transcript."""

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

        translated = []

        system_instruction = (
            "אתה מתרגם מקצועי ומומחה לשפה הערבית והעברית. "
            "תרגם את התמלול מערבית לעברית. מדובר בתמלול של דרשה. "
            "התמלול עשוי להכיל שבירות שורה קצרות שנוצרו מאופי הכתוביות; "
            "התעלם מהן וחבר את המשפטים לרצף טבעי.\n"
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
                "זהו חלק מתוך תמלול ארוך, לכן אל תסכם "
                "ואל תדלג על דבר.\n\n"
                + chunk
            )

            result = None
            last_error = None

            for model in self.models:

                for attempt in range(1, 4):

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
                            result = response.text.strip()
                            break

                    except APIError as exc:

                        last_error = exc

                        code = getattr(
                            exc,
                            "code",
                            None,
                        )

                        if (
                            code
                            in (
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
                            time_sleep = min(
                                60,
                                5 * (2 ** (attempt - 1)),
                            )

                            time.sleep(
                                time_sleep
                            )

                            continue

                        break

                    except Exception as exc:
                        last_error = exc
                        break

                if result:
                    break

            if not result:
                raise RuntimeError(
                    "Gemini נכשל בתרגום חלק "
                    f"{index + 1}/{len(chunks)}: "
                    f"{last_error}"
                )

            translated.append(result)

        return "\n\n".join(translated)


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
                        i : i + max_chars
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

    async with TelegramClient(
        StringSession(session_str),
        api_id,
        api_hash,
    ) as client:

        await client.send_message(
            target_user,
            (
                f"🎬 **תרגום אוטומטי לדרשה**\n"
                f"📌 **כותרת:** {video_title}\n\n"
                "התוכן מתחיל מטה 👇"
            ),
        )

        for chunk in chunks:
            await client.send_message(
                target_user,
                chunk,
            )

            await asyncio.sleep(1.1)

        await client.send_message(
            target_user,
            (
                "🔗 **קישור לסרטון המקורי ביוטיוב:**\n"
                f"{video_url}\n\n"
                "✨ המשימה הושלמה בהצלחה!"
            ),
        )

    return (
        True,
        "נשלח לטלגרם בהצלחה.",
    )


def run_pipeline(
    video_url: Optional[str] = None,
    speaker: Optional[str] = None,
):
    """
    Main pipeline with diagnostic logging.
    """

    print("=" * 80)
    print("[PIPELINE] START")
    print(f"[PIPELINE] video_url={video_url}")
    print(f"[PIPELINE] speaker={speaker}")
    print("=" * 80)

    try:

        if video_url and video_url.strip():

            video_url = video_url.strip()

            print(
                "[PIPELINE] Direct YouTube URL supplied"
            )

            extract_video_id(
                video_url
            )

            print(
                f"[PIPELINE] Video ID: "
                f"{extract_video_id(video_url)}"
            )

            print(
                "[PIPELINE] Getting video title..."
            )

            title = get_video_title(
                video_url
            )

            print(
                f"[PIPELINE] Title: {title}"
            )

        elif speaker in SPEAKER_URLS:

            print(
                f"[PIPELINE] Finding latest video "
                f"for speaker: {speaker}"
            )

            video_url, title = (
                find_latest_friday_video(
                    SPEAKER_URLS[speaker]
                )
            )

            print(
                f"[PIPELINE] Selected URL: "
                f"{video_url}"
            )

            print(
                f"[PIPELINE] Selected title: "
                f"{title}"
            )

            if not video_url:

                return {
                    "success": False,
                    "message": (
                        "לא נמצא סרטון מתאים "
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

        print(
            "[PIPELINE] Starting transcript extraction..."
        )

        transcript = fetch_transcript(
            video_url
        )

        print(
            f"[PIPELINE] Transcript received: "
            f"{len(transcript)} characters"
        )

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

        print(
            f"[PIPELINE] Translation complete: "
            f"{len(translation)} characters"
        )

        telegram_ok = False
        telegram_message = ""

        try:

            print(
                "[PIPELINE] Sending translation to Telegram..."
            )

            telegram_ok, telegram_message = (
                asyncio.run(
                    send_to_telegram(
                        translation,
                        title,
                        video_url,
                    )
                )
            )

            print(
                f"[PIPELINE] Telegram result: "
                f"{telegram_message}"
            )

        except Exception as exc:

            print(
                f"[PIPELINE] Telegram error: {exc}"
            )

            telegram_message = (
                f"שגיאה בטלגרם: {exc}"
            )

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

    except Exception as exc:

        print("=" * 80)
        print("[PIPELINE] !!! FAILED !!!")
        print(
            f"[PIPELINE] Exception type: "
            f"{type(exc).__name__}"
        )
        print(
            f"[PIPELINE] Exception: {exc}"
        )
        print("[PIPELINE] Full traceback:")
        traceback.print_exc()
        print("=" * 80)

        return {
            "success": False,
            "message": str(exc),
        }
