import os
import re
import time
import shutil
import tempfile
import logging
import requests
import telebot
from dotenv import load_dotenv
from telebot import apihelper
from telebot.types import InputMediaPhoto
import yt_dlp

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN is missing. Add it to your .env file.")

apihelper.CONNECT_TIMEOUT = 30
apihelper.READ_TIMEOUT = 300

bot = telebot.TeleBot(TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Keep third-party libraries quiet; only our own lines + real problems show
for noisy in ("TeleBot", "telebot", "urllib3", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
FB_COOKIES_FILE = os.path.join(BASE_DIR, "facebook_cookies.txt")  # optional
MAX_SIZE = 50 * 1024 * 1024  # Telegram bot upload limit
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SUPPORTED = ["facebook.com", "fb.watch", "fb.com", "tiktok.com"]
FB_DOMAINS = ["facebook.com", "fb.watch", "fb.com"]
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def clean_error(e):
    return ANSI_RE.sub("", str(e)).replace("ERROR:", "").strip()[:250]


class QuietLogger:
    """Stops yt-dlp from printing its own errors in the terminal."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# ---------- Method: yt-dlp ----------
def ytdlp_download(url, out_template, cookie_copy=None, fmt='b[ext=mp4]/b', merge=False):
    opts = {
        'format': fmt,
        'outtmpl': out_template,
        'restrictfilenames': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 2,
        'extractor_retries': 1,
        'max_filesize': MAX_SIZE,
        'quiet': True,
        'no_warnings': True,
        'logger': QuietLogger(),
    }
    if merge:
        opts['merge_output_format'] = 'mp4'
    if cookie_copy:
        opts['cookiefile'] = cookie_copy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    downloads = info.get("requested_downloads") or []
    path = downloads[0].get("filepath") if downloads else None
    if not path or not os.path.exists(path):
        raise yt_dlp.utils.DownloadError("File is larger than max-filesize or was not downloaded")
    return path


# ---------- Method: tikwm (TikTok only) ----------
def download_images(image_urls, base_path):
    """Download photo-post images. Returns a list of file paths."""
    paths = []
    try:
        for i, img_url in enumerate(image_urls):
            if img_url.startswith("/"):
                img_url = "https://www.tikwm.com" + img_url
            ext = os.path.splitext(img_url.split("?")[0])[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            p = f"{base_path}_img{i + 1}{ext}"
            with requests.get(img_url, stream=True, timeout=60,
                              headers={"User-Agent": "Mozilla/5.0"}) as resp:
                resp.raise_for_status()
                with open(p, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        f.write(chunk)
            paths.append(p)
        return paths
    except Exception:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)
        raise


def tikwm_download(url, base_path):
    r = requests.get(
        "https://www.tikwm.com/api/",
        params={"url": url, "hd": 0},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0 or not data.get("data"):
        raise RuntimeError(f"tikwm: {data.get('msg', 'no data')}")

    # Photo post: return a LIST of image files instead of a video path
    images = data["data"].get("images")
    if images:
        logging.info(f"Photo post detected: {len(images)} images")
        return download_images(images, base_path)

    video_url = data["data"].get("play")  # "play" = no watermark
    if not video_url:
        raise RuntimeError("tikwm: no video url")
    if video_url.startswith("/"):
        video_url = "https://www.tikwm.com" + video_url

    path = base_path + ".mp4"
    size = 0
    with requests.get(video_url, stream=True, timeout=60,
                      headers={"User-Agent": "Mozilla/5.0"}) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                size += len(chunk)
                if size > MAX_SIZE:
                    f.close()
                    os.remove(path)
                    raise RuntimeError("File is larger than max-filesize")
                f.write(chunk)
    return path


# ---------- Facebook (video only) ----------
def facebook_download(url, user_id, out_template):
    """Facebook-only flow. TikTok links never come through here."""
    # HD merge only if ffmpeg exists; always keep the simple format as fallback
    formats = []
    if HAS_FFMPEG:
        formats.append(("bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b", True))
    formats.append(("b[ext=mp4]/b", False))

    def try_formats(cookie_copy=None):
        err = None
        for fmt, merge in formats:
            try:
                return ytdlp_download(url, out_template, cookie_copy, fmt=fmt, merge=merge)
            except yt_dlp.utils.DownloadError as e:
                err = e
                msg = str(e).lower()
                if "larger than" in msg or "max-filesize" in msg:
                    break  # a different format won't make it smaller enough
        raise err

    cookie_copy = None
    try:
        t = time.time()
        logging.info(f"[{user_id}] Facebook: trying yt-dlp...")
        try:
            path = try_formats()
            logging.info(f"[{user_id}] Facebook OK in {time.time() - t:.1f}s")
            return path
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            logging.warning(f"[{user_id}] Facebook failed after {time.time() - t:.1f}s: {clean_error(e)}")
            if "larger than" in str(e).lower():
                raise

        # Retry with Facebook cookies (any failure, not just "log in")
        if os.path.exists(FB_COOKIES_FILE):
            t = time.time()
            logging.info(f"[{user_id}] Facebook: retrying with cookies...")
            cookie_copy = os.path.join(tempfile.gettempdir(), f"fbcookies_{user_id}.txt")
            shutil.copyfile(FB_COOKIES_FILE, cookie_copy)
            try:
                path = try_formats(cookie_copy)
                logging.info(f"[{user_id}] Facebook (cookies) OK in {time.time() - t:.1f}s")
                return path
            except yt_dlp.utils.DownloadError as e2:
                last_error = e2
                logging.warning(f"[{user_id}] Facebook (cookies) failed: {clean_error(e2)}")

        raise last_error
    finally:
        if cookie_copy and os.path.exists(cookie_copy):
            os.remove(cookie_copy)


# ---------- Orchestration ----------
def get_video(url, user_id):
    out_template = os.path.join(BASE_DIR, f"video_{user_id}.%(ext)s")
    base_path = os.path.join(BASE_DIR, f"video_{user_id}")
    cookie_copy = None
    last_error = None

    # Facebook: separate flow, video only
    if any(d in url for d in FB_DOMAINS) and "tiktok.com" not in url:
        return facebook_download(url, user_id, out_template)

    try:
        # 1) TikTok: fast route first (tikwm)
        if "tiktok.com" in url:
            t = time.time()
            logging.info(f"[{user_id}] TikTok: trying tikwm...")
            try:
                path = tikwm_download(url, base_path)
                logging.info(f"[{user_id}] tikwm OK in {time.time() - t:.1f}s")
                return path
            except Exception as e:
                last_error = e
                logging.warning(f"[{user_id}] tikwm failed after {time.time() - t:.1f}s: {clean_error(e)}")
                if os.path.exists(base_path + ".mp4"):
                    os.remove(base_path + ".mp4")

        # 2) yt-dlp WITHOUT cookies
        t = time.time()
        logging.info(f"[{user_id}] Trying yt-dlp...")
        try:
            path = ytdlp_download(url, out_template)
            logging.info(f"[{user_id}] yt-dlp OK in {time.time() - t:.1f}s")
            return path
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            logging.warning(f"[{user_id}] yt-dlp failed after {time.time() - t:.1f}s: {clean_error(e)}")
            msg = str(e).lower()

            # 3) Age-restricted? retry with a throwaway copy of the cookies
            if ("log in" in msg or "comfortable" in msg) and os.path.exists(COOKIES_FILE):
                t = time.time()
                logging.info(f"[{user_id}] Retrying yt-dlp with cookies...")
                cookie_copy = os.path.join(tempfile.gettempdir(), f"cookies_{user_id}.txt")
                shutil.copyfile(COOKIES_FILE, cookie_copy)
                try:
                    path = ytdlp_download(url, out_template, cookie_copy)
                    logging.info(f"[{user_id}] yt-dlp (cookies) OK in {time.time() - t:.1f}s")
                    return path
                except yt_dlp.utils.DownloadError as e2:
                    last_error = e2
                    logging.warning(f"[{user_id}] yt-dlp (cookies) failed: {clean_error(e2)}")

        raise last_error
    finally:
        if cookie_copy and os.path.exists(cookie_copy):
            os.remove(cookie_copy)


def upload_video(chat_id, path):
    size_mb = os.path.getsize(path) / (1024 * 1024)
    t = time.time()
    logging.info(f"[{chat_id}] Uploading {size_mb:.1f} MB to Telegram...")
    for attempt in range(3):
        try:
            with open(path, 'rb') as f:
                bot.send_video(chat_id, f, caption="Here is your video! 📥",
                               supports_streaming=True, timeout=300)
            logging.info(f"[{chat_id}] Upload OK in {time.time() - t:.1f}s")
            return
        except Exception as e:
            logging.warning(f"[{chat_id}] Upload attempt {attempt + 1} failed: {e}")
            time.sleep(2)
    logging.info(f"[{chat_id}] Sending as document instead...")
    with open(path, 'rb') as f:
        bot.send_document(chat_id, f, caption="Here is your video! 📥", timeout=300)


def upload_photos(chat_id, paths):
    """Send photo-post images as albums (Telegram allows 10 per album)."""
    t = time.time()
    logging.info(f"[{chat_id}] Uploading {len(paths)} photos to Telegram...")

    for start in range(0, len(paths), 10):
        chunk = paths[start:start + 10]
        sent = False
        for attempt in range(3):
            handles = []
            try:
                media = []
                for i, p in enumerate(chunk):
                    fh = open(p, 'rb')
                    handles.append(fh)
                    caption = "Here are your photos! 🖼️" if (start == 0 and i == 0) else None
                    media.append(InputMediaPhoto(fh, caption=caption))
                if len(media) == 1:
                    handles[0].seek(0)
                    bot.send_photo(chat_id, handles[0],
                                   caption="Here are your photos! 🖼️" if start == 0 else None,
                                   timeout=300)
                else:
                    bot.send_media_group(chat_id, media, timeout=300)
                sent = True
                break
            except Exception as e:
                logging.warning(f"[{chat_id}] Photo upload attempt {attempt + 1} failed: {e}")
                time.sleep(2)
            finally:
                for fh in handles:
                    fh.close()

        if not sent:
            logging.info(f"[{chat_id}] Sending photos as documents instead...")
            for p in chunk:
                with open(p, 'rb') as f:
                    bot.send_document(chat_id, f, timeout=300)

    logging.info(f"[{chat_id}] Photo upload OK in {time.time() - t:.1f}s")


@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 Hello! Send me a Facebook or TikTok link and I'll send you the video.")


@bot.message_handler(func=lambda message: True)
def handle_media_link(message):
    url = (message.text or "").strip()

    if not any(d in url for d in SUPPORTED):
        bot.reply_to(message, "❌ Please send a valid Facebook or TikTok link.")
        return

    start = time.time()
    user_id = message.from_user.id
    logging.info(f"[{user_id}] New request: {url}")

    processing_msg = bot.reply_to(message, "⏳ Downloading media... Please wait.")
    file_path = None
    try:
        file_path = get_video(url, user_id)
        if isinstance(file_path, list):
            upload_photos(message.chat.id, file_path)
        else:
            upload_video(message.chat.id, file_path)
        try:
            bot.delete_message(message.chat.id, processing_msg.message_id)
        except Exception:
            pass
        logging.info(f"[{user_id}] Done. Total {time.time() - start:.1f}s")

    except Exception as e:
        logging.error(f"[{user_id}] FAILED after {time.time() - start:.1f}s: {clean_error(e)}")
        msg = str(e).lower()
        is_fb = any(d in url for d in FB_DOMAINS) and "tiktok.com" not in url
        if "larger than" in msg:
            text = "📦 This video is over 50 MB, which is Telegram's bot limit."
        elif is_fb and any(k in msg for k in ("log in", "login", "cookies", "parse data", "private", "registered users")):
            text = "🔒 This Facebook video is private or requires login, so it can't be downloaded."
        elif "log in" in msg or "comfortable" in msg:
            text = "🔒 This post is age-restricted. Cookies are missing or expired."
        elif "403" in msg or "forbidden" in msg:
            text = "🚫 TikTok is blocking requests right now. Please try again later."
        else:
            text = f"⚠️ Couldn't download: {clean_error(e)}"
        bot.reply_to(message, text)

    finally:
        if file_path:
            for p in (file_path if isinstance(file_path, list) else [file_path]):
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    print("🤖 Bot is up and running...")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)