import os
import json
import asyncio

from aiohttp import web
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS",
    "gemini-3.5-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-latest",
).split(",") if m.strip()]
BOT_MODE = os.getenv("BOT_MODE", "polling")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
PORT = int(os.getenv("PORT", "10000"))
SECRET_TOKEN = os.getenv("SECRET_TOKEN", "change-me")

client = genai.Client(api_key=GEMINI_API_KEY)
MODES = {"both", "en", "tr"}

RULES = (
    "Rules:\n- The input is Tajik (Cyrillic script). Do not confuse it with Persian/Dari.\n"
    "- Preserve names, numbers, tone and meaning.\n- Return JSON only."
)


def keys_for(mode: str) -> str:
    if mode == "en":
        return '"english"'
    if mode == "tr":
        return '"turkish"'
    return '"english" and "turkish"'


async def ask_gemini(parts) -> dict:
    """Try each model; on overload/rate-limit errors move to the next one."""
    cfg = types.GenerateContentConfig(temperature=0, response_mime_type="application/json")
    last_err = None
    for attempt in range(2):
        for model in MODELS:
            try:
                resp = await client.aio.models.generate_content(model=model, contents=parts, config=cfg)
                return json.loads(resp.text)
            except Exception as e:
                msg = str(e)
                last_err = e
                if any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND", "500", "INTERNAL")):
                    print(f"{model} failed: {msg[:120]}")
                    continue
                raise
        await asyncio.sleep(3)
    raise RuntimeError("Gemini şu an çok yoğun, lütfen biraz sonra tekrar deneyin.") from last_err


async def translate_text(text: str, mode: str) -> dict:
    prompt = f"Translate this Tajik text. Return a JSON object with keys {keys_for(mode)}.\n{RULES}\n\nTajik text:\n{text}"
    return await ask_gemini([prompt])


async def translate_audio(audio: bytes, mode: str) -> dict:
    prompt = (
        "This is a Tajik voice message. Transcribe it exactly in Tajik (Cyrillic) under key \"tajik\", "
        f"then translate it, returning a JSON object with keys \"tajik\" and {keys_for(mode)}. "
        "If the audio has no understandable speech, return {\"tajik\": \"\"}.\n" + RULES
    )
    return await ask_gemini([types.Part.from_bytes(data=audio, mime_type="audio/ogg"), prompt])


def fmt(tajik: str, res: dict) -> str:
    parts = [f"🇹🇯 Tajik:\n{tajik}"]
    if res.get("english"):
        parts.append(f"🇬🇧 English:\n{res['english']}")
    if res.get("turkish"):
        parts.append(f"🇹🇷 Türkçe:\n{res['turkish']}")
    return "\n\n".join(parts)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Tacikçe yazı veya sesli mesaj gönderin, çevireyim.\n\n"
        "/both — İngilizce + Türkçe (varsayılan)\n/en — sadece İngilizce\n/tr — sadece Türkçe"
    )


async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = update.message.text.strip().lstrip("/").split("@")[0].lower()
    context.user_data["mode"] = mode
    label = {"both": "İngilizce + Türkçe", "en": "Sadece İngilizce", "tr": "Sadece Türkçe"}[mode]
    await update.message.reply_text(f"✅ Mod: {label}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return
    try:
        res = await translate_text(text, context.user_data.get("mode", "both"))
        await update.message.reply_text(fmt(text, res))
    except Exception as e:
        await update.message.reply_text(f"Çeviri hatası: {e}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if voice is None:
        return
    status = await update.message.reply_text("🎙️ Ses çevriliyor…")
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        audio = bytes(await tg_file.download_as_bytearray())
        res = await translate_audio(audio, context.user_data.get("mode", "both"))
        tajik = (res.get("tajik") or "").strip()
        if not tajik:
            await status.edit_text("Ses anlaşılamadı, daha net tekrar deneyin.")
            return
        await status.edit_text(fmt(tajik, res))
    except Exception as e:
        await status.edit_text(f"Hata: {e}")


def build_app(webhook: bool) -> Application:
    b = Application.builder().token(BOT_TOKEN)
    if webhook:
        b = b.updater(None)
    app = b.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["both", "en", "tr"], set_mode))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    return app


def run_webhook():
    ptb = build_app(webhook=True)
    path = "/webhook"

    async def on_startup(_):
        await ptb.initialize()
        await ptb.start()
        await ptb.bot.set_webhook(url=f"{WEBHOOK_URL.rstrip('/')}{path}", secret_token=SECRET_TOKEN)
        print("Webhook set", flush=True)

    async def on_shutdown(_):
        await ptb.stop()
        await ptb.shutdown()

    async def receive(request: web.Request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET_TOKEN:
            return web.Response(status=403)
        await ptb.update_queue.put(Update.de_json(await request.json(), ptb.bot))
        return web.Response(text="ok")

    async def health(_):
        return web.Response(text="ok")

    aio = web.Application()
    aio.on_startup.append(on_startup)
    aio.on_shutdown.append(on_shutdown)
    aio.router.add_post(path, receive)
    aio.router.add_get("/", health)
    aio.router.add_get("/healthz", health)
    web.run_app(aio, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    if BOT_MODE == "webhook":
        if not WEBHOOK_URL:
            raise SystemExit("WEBHOOK_URL gerekli")
        run_webhook()
    else:
        build_app(webhook=False).run_polling()
