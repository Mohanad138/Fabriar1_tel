# -*- coding: utf-8 -*-
import os
import re
import json
import time
import asyncio
import subprocess
import difflib
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage)
import requests

TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION = os.getenv("TG_SESSION")

# استخدام مفتاح Groq
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"

STATE_FILE = "relay_state.json"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

CHANNELS = {
    -1001344466508: {
        "name": "فابريزيو رومانو بالعربي",
        "target": -1002015818715,
        "source_username": "@FabrizioRomanoTG",
        "skip_if_video": True,
        "skip_if_youtube": True,
        # تم إلغاء شرط تجاهل أغلفة الفيديو
    }
}

client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()

def is_duplicate_content(source_id, text):
    if not text or not text.strip(): return False
    key = str(source_id)
    recent = state.setdefault("recent_texts", {}).get(key, [])
    norm = re.sub(r'\s+', ' ', text).strip().lower()
    for prev in recent:
        if difflib.SequenceMatcher(None, norm, prev).ratio() >= 0.82: return True
    return False

def record_published_text(source_id, text):
    if not text or not text.strip(): return
    key = str(source_id)
    norm = re.sub(r'\s+', ' ', text).strip().lower()
    bucket = state.setdefault("recent_texts", {}).setdefault(key, [])
    bucket.append(norm)
    state["recent_texts"][key] = bucket[-15:]
    save_state(state)

def commit_state():
    try:
        subprocess.run(["git", "add", STATE_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "update relay state"], check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False, capture_output=True)
        subprocess.run(["git", "push"], check=False, capture_output=True)
    except Exception: pass

def _is_small_thumbnail_photo(photo):
    try:
        dims = [(getattr(s, "w", 0), getattr(s, "h", 0)) for s in photo.sizes if hasattr(s, "w")]
        if not dims: return False
        max_w = max(w for w, _ in dims)
        max_h = max(h for _, h in dims)
        if max_w < 250 and max_h < 250: return True
        if max_w > 0 and max_h > 0:
            ratio = max_w / max_h
            if 0.85 <= ratio <= 1.18 and max(max_w, max_h) <= 800: return True
        return False
    except Exception: return False

def _is_usable_media_msg(m):
    media = m.media
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)): return True
    if isinstance(media, MessageMediaWebPage):
        wp = getattr(media, "webpage", None)
        photo = getattr(wp, "photo", None)
        if photo and not _is_small_thumbnail_photo(photo): return True
    return False

def _media_payload(m):
    if isinstance(m.media, MessageMediaWebPage): return m.photo if m.photo else None
    return m.media

def _message_has_any_video(m):
    media = m.media
    if isinstance(media, MessageMediaDocument): return bool(m.video) or bool(getattr(m, "gif", None))
    if isinstance(media, MessageMediaWebPage):
        wp = getattr(media, "webpage", None)
        if not wp: return False
        if getattr(wp, "duration", None): return True
        wtype = str(getattr(wp, "type", "") or "").lower()
        if "video" in wtype or "gif" in wtype: return True
        doc = getattr(wp, "document", None)
        if doc and str(getattr(doc, "mime_type", "")).startswith(("video/", "image/gif")): return True
    return False

def contains_youtube_link(text): return bool(re.search(r'(youtube\.com|youtu\.be)', text, re.IGNORECASE)) if text else False

def clean_text(text, source_username=None):
    if not text: return ""
    cleaned = text.replace('📊', '')
    if source_username: cleaned = re.sub(re.escape(source_username), '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'(https?://)?t\.me/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'https?://t\.co/\S+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'(?i)forwarded from.*', '', cleaned)
    cleaned = re.sub(r'(?i)read\s*more\.?\s*$', '', cleaned.rstrip())
    cleaned = re.sub(r'اقرأ\s*المزيد\.?\s*$', '', cleaned.rstrip())
    cleaned = re.sub(r'أعيد التوجيه من.*', '', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    lines = cleaned.split('\n')
    while lines and not re.search(r'[A-Za-z\u0600-\u06FF0-9]', lines[-1]): lines.pop()
    return '\n'.join(lines).rstrip()

def call_groq(prompt, max_retries=3):
    if not GROQ_API_KEY: raise RuntimeError("no groq key")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(2 ** attempt); continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if attempt == max_retries: raise e
            time.sleep(2 ** attempt)

def call_fabrizio(text):
    prompt = f"""أنت محرر لصفحة عربية تنقل كل منشورات حساب فابريزيو رومانو الرسمي بالعربية.

معايير النشر (فلتر معتدل — مو صارم ومو مفتوح بالكامل):
- انشر (should_post = true) أي منشور فيه **معلومة كروية ملموسة وجديدة** عن لاعب أو مدرب أو نادي محدد.
- **أخبار الجوائز الفردية دايماً تُنشر**.
- أي منشور يحتوي على عبارة "Here we go" يُنشر دائماً بلا استثناء.
- اجعل should_post = false للإعلانات المدفوعة، الترويج لبرامجه، التشويق بلا معلومة، أو المنشورات الترفيهية.
- عند الشك انشر.

الترجمة (فقط إذا should_post = true):
- **أمانة كاملة للنص الأصلي:** اكتب الخبر بنفس معناه ونفس ترتيب أفكاره وفقراته وأسطره وإيموجياته بالضبط كما كتبه فابريزيو، بس بعربي طبيعي.
- **ممنوع إضافة عبارات غير موجودة بالنص الأصلي** مثل "أؤكد لكم"، "أبشركم". اكتب الخبر بشكل اعتيادي مباشر.
- **الصيغة تبقى نفس صيغة الأصل:** إذا الأصل خبر بصيغة الغائب، فالترجمة بصيغة الغائب. وإذا كان بصيغة المتكلم، ترجمه بصيغة المتكلم.
- **المصطلحات الثابتة:** "OFFICIAL" ← "رسمياً"، "BREAKING" ← "عاجل"، "Here we go" تبقى بالإنجليزي.
- **لا تُبقِ أي رابط إطلاقاً بالنص النهائي**.

النص:
{text}

أعد النتيجة بصيغة JSON فقط: {{"should_post": true/false, "text": "النص المُعالج"}}"""
    try:
        res_raw = call_groq(prompt)
        if res_raw.startswith("```"):
            res_raw = res_raw.strip("`").strip()
            if res_raw.lower().startswith("json"): res_raw = res_raw[4:].strip()
        res = json.loads(res_raw)
        out_text = (res.get("text") or text).strip()
        out_text = re.sub(r'https?://\S+', '', out_text)
        if not re.search(r"\bI(?:'m| am)? (?:can )?(?:confirm|announce)|\bI(?:'m| am) (?:delighted|happy|excited) to", text, re.IGNORECASE):
            out_text = re.sub(r'(?:أؤكد|أؤكّد|أبشركم|أعلن|أخبركم|أبلغكم)\s+لكم\s*(?:بأن|أن|أنّ|بأنّ)?[\s،:]*', '', out_text)
        out_text = re.sub(r'[ \t]{2,}', ' ', out_text)
        out_text = re.sub(r'\n{3,}', '\n\n', out_text).strip()
        return {"should_post": bool(res.get("should_post", True)), "text": out_text}
    except Exception as e:
        print(f"❌ خطأ معالجة فابريزيو: {e}")
        return {"should_post": False, "text": ""}

async def process_batch(source_id, messages):
    config = CHANNELS.get(source_id)
    if not config: return

    raw_text = next((m.message for m in messages if m.message), "")
    cleaned = clean_text(raw_text, config.get("source_username"))

    if is_duplicate_content(source_id, cleaned): return

    force = bool(re.search(r'here\s*we\s*go', cleaned, re.IGNORECASE)) if cleaned else False
    res = call_fabrizio(cleaned)
    should_post = (bool(res["text"]) and (res["should_post"] or force))
    
    if not should_post: return

    record_published_text(source_id, cleaned)
    final_text = res["text"]
    
    media_msgs = [m for m in messages if _is_usable_media_msg(m)]

    if config.get("skip_if_video") and any(_message_has_any_video(m) for m in messages) and not force: return
    if config.get("skip_if_youtube") and contains_youtube_link(raw_text) and not force: return

    try:
        if media_msgs:
            valid_payloads = [_media_payload(m) for m in media_msgs if _media_payload(m) is not None]
            if valid_payloads:
                to_send = valid_payloads[0] if len(valid_payloads) == 1 else valid_payloads
                await client.send_file(config["target"], to_send, caption=final_text or None, parse_mode="html")
                print(f"✅ نُشر (صورة + نص) — فابريزيو")
                commit_state()
                return
                
        if final_text:
            await client.send_message(config["target"], final_text, parse_mode="html", link_preview=False)
            print(f"✅ نُشر (نص فقط) — فابريزيو")
            commit_state()
    except Exception as e:
        print(f"❌ خطأ نشر: {e}")

async def catch_up_channel(source_id):
    config = CHANNELS.get(source_id, {})
    entity_ref = config.get("source_username") or source_id
    key = str(source_id)
    
    last_id = state.get(key, 0)
    if last_id == 0:
        latest = await client.get_messages(entity_ref, limit=1)
        if latest: last_id = max(latest[0].id - 5, 0)
        state[key] = last_id
        save_state(state)
        
    messages = await client.get_messages(entity_ref, min_id=last_id, limit=200)
    if not messages: return

    messages = list(reversed(messages))
    processed_ids = set()

    for msg in messages:
        if msg.id in processed_ids: continue
        if msg.grouped_id:
            group = [m for m in messages if m.grouped_id == msg.grouped_id and m.id not in processed_ids]
            group.sort(key=lambda m: m.id)
            await process_batch(source_id, group)
            processed_ids.update(m.id for m in group)
            new_last = group[-1].id
        else:
            await process_batch(source_id, [msg])
            processed_ids.add(msg.id)
            new_last = msg.id
        
        state[key] = new_last
        save_state(state)

async def main():
    await client.start()
    print("🚀 بدء فحص فابريزيو (سريع)...")
    await catch_up_channel(-1001344466508)
    await client.disconnect()
    commit_state()
    print("✅ انتهت التشغيلة.")

if __name__ == "__main__":
    asyncio.run(main())
