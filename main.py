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
GEMINI_KEY = os.getenv("GEMINI_KEY_FABRIZ")

GEMINI_MODEL = "gemini-flash-lite-latest"
STATE_FILE = "relay_state.json"

TEST_MODE = False 

CHANNELS = {
    -1001344466508: {
        "name": "فابريزيو رومانو بالعربي",
        "target": -1002015818715,
        "source_username": "@FabrizioRomanoTG",
        "skip_if_video": True,
        "skip_if_youtube": True,
        "skip_if_play_button": True, # 👈 تم تفعيل فحص أغلفة الفيديو وتجاهلها
    }
}

client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception: return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()
DUP_HISTORY_SIZE = 15
DUP_SIMILARITY_THRESHOLD = 0.82 

def is_duplicate_content(source_id, text):
    if not text or not text.strip(): return False
    key = str(source_id)
    recent = state.setdefault("recent_texts", {}).get(key, [])
    norm = re.sub(r'\s+', ' ', text).strip().lower()
    for prev in recent:
        if difflib.SequenceMatcher(None, norm, prev).ratio() >= DUP_SIMILARITY_THRESHOLD: return True
    return False

def record_published_text(source_id, text):
    if not text or not text.strip(): return
    key = str(source_id)
    norm = re.sub(r'\s+', ' ', text).strip().lower()
    bucket = state.setdefault("recent_texts", {}).setdefault(key, [])
    bucket.append(norm)
    state["recent_texts"][key] = bucket[-DUP_HISTORY_SIZE:]
    save_state(state)

def commit_state():
    try:
        subprocess.run(["git", "add", STATE_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "update relay state"], check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False, capture_output=True)
        subprocess.run(["git", "push"], check=False, capture_output=True)
    except Exception as e:
        print(f"⚠️ git error: {e}")

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

def call_gemini(prompt, api_key, max_retries=3):
    if not api_key: raise RuntimeError("no gemini key")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.strip("`").strip()
                if raw.lower().startswith("json"): raw = raw[4:].strip()
            return json.loads(raw)
        except Exception as e:
            if attempt == max_retries: raise e
            time.sleep(2 ** attempt)

# ==== فحص زر التشغيل في الصورة بالذكاء الاصطناعي ====
def check_image_has_play_button(image_path, api_key, max_retries=3):
    if not api_key: return False
    try:
        import base64
        with open(image_path, "rb") as f: img_b64 = base64.b64encode(f.read()).decode()
    except Exception: return False
    mime = "image/png" if image_path.lower().endswith("png") else "image/jpeg"
    prompt = 'هل يوجد في هذه الصورة زر تشغيل فيديو (مثلث ▶ غالباً داخل دائرة في المنتصف) يدل على أنها غلاف فيديو؟ JSON: {"has_play_button": true/false}'
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": img_b64}}, {"text": prompt}]}]}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.strip("`").strip()
                if raw.lower().startswith("json"): raw = raw[4:].strip()
            return bool(json.loads(raw).get("has_play_button", False))
        except Exception:
            if attempt == max_retries: return False
            time.sleep(2 ** attempt)

def call_fabrizio(text, api_key):
    prompt = f"""أنت محرر لصفحة عربية تنقل كل منشورات حساب فابريزيو رومانو الرسمي بالعربية.

معايير النشر (فلتر معتدل — مو صارم ومو مفتوح بالكامل):
- انشر (should_post = true) أي منشور فيه **معلومة كروية ملموسة وجديدة** عن لاعب أو مدرب أو نادي محدد:
  انتقال أو صفقة أو تجديد عقد (مع مرحلة واضحة: مفاوضات متقدمة، اتفاق، رسمي)، إصابة أو غياب لاعب مهم،
  تشكيلة أو نتيجة مباراة أو حدث مهم بمباراة، قرار أو إعلان رسمي، تصريح له قيمة إخبارية فعلية،
  إحصائية لافتة، أو تغيير مدرب/إدارة.
- **أخبار الجوائز الفردية دايماً تُنشر**: الكرة الذهبية (Ballon d'Or)، أفضل لاعب، أي جائزة فردية —
  فوز، ترشيح، توقع، تحليل — حتى لو كان النص قصير أو بصيغة "BREAKING" أو بأحرف كبيرة.
- أي منشور يحتوي على عبارة "Here we go" يُنشر دائماً بلا استثناء.
- اجعل should_post = false للأنواع التالية فقط (هامشية أو بلا قيمة إخبارية):
  * إعلان تجاري مدفوع أو ترويج لمنتج/تطبيق/خدمة/رعاية.
  * ترويج لبودكاست/برنامج فابريزيو نفسه (زي "New pod on"، دعوة للاستماع).
  * تشويق بلا معلومة فعلية ("more to follow"، "big news coming"، "talks ongoing" بدون أي تفصيل
    أو مرحلة أو اسم محدد).
  * منشورات تفاعلية/ترفيهية بلا خبر: تهنئة، عيد ميلاد، احتفال، ردود فعل جماهير، دردشة، طرائف.
  * رأي أو تحليل عام بلا حقيقة جديدة، أو تكرار لمعلومة معروفة سبق نشرها بدون أي جديد.
  * أي محتوى بعيد عن كرة القدم أو شخصي.
- قرار الفيديو والصور يتخذ بشكل منفصل بالكود، مو من عندك — لا تجعل should_post = false بسبب إن
  المنشور "يشبه فيديو" أو قصير. خبر قصير حقيقي (فوز بجائزة، نتيجة، صفقة) يبقى should_post = true.
- عند الشك بين خبر حقيقي متوسط الأهمية وخبر هامشي: إذا فيه اسم محدد + تطور فعلي → انشر، وإذا ما فيه
  إلا انطباع أو تشويق عام → لا تنشر.

الترجمة (فقط إذا should_post = true):
- **أمانة كاملة للنص الأصلي:** اكتب الخبر بنفس معناه ونفس ترتيب أفكاره وفقراته وأسطره وإيموجياته
  بالضبط كما كتبه فابريزيو بحسابه الأصلي، بس بعربي طبيعي كأن شخص عربي كتبه (مو كلمة بكلمة ومو إعادة
  سرد). لا تحذف أي معلومة ولا تضيف أي معلومة أو عبارة من عندك.
- **ممنوع إضافة عبارات غير موجودة بالنص الأصلي** مثل "أؤكد لكم"، "أبشركم"، "أعلن لكم"، "خبر حصري لكم".
  اكتب الخبر بشكل اعتيادي مباشر.
- **الصيغة تبقى نفس صيغة الأصل:** إذا الأصل خبر بصيغة الغائب ("OFFICIAL: Rafa Nadal confirms he's
  entering Real Madrid Board") فالترجمة خبر عادي بصيغة الغائب: "رسمياً: رافا نادال يؤكد انضمامه إلى مجلس
  إدارة ريال مدريد". وإذا الأصل فعلاً بصيغة المتكلم ("I can confirm...") ترجمه بصيغة المتكلم. وبكل
  الأحوال لا تتكلم عن فابريزيو بصيغة الغائب ("قال فابريزيو"، "ينفي فابريزيو").
- **أمثلة (اتبعها بالضبط):**
  الأصل: "🚨 OFFICIAL: Rafa Nadal confirms he's entering Real Madrid Board of Directors."
  صح: "🚨 رسمياً: رافا نادال يؤكد انضمامه إلى مجلس إدارة ريال مدريد."
  غلط: "🚨 رسمياً: أؤكد لكم انضمام رافا نادال إلى مجلس إدارة ريال مدريد." (أضاف "أؤكد لكم" وغيّر الفاعل)
  الأصل: "🚨🏴 Harry Kane, set to sign a new deal at FC Bayern soon — valid until June 2029."
  صح: "🚨🏴 هاري كين على وشك توقيع عقد جديد مع بايرن ميونخ قريباً — ساري حتى يونيو 2029."
  الأصل: "Here we go! Done deal."  ← صح: "Here we go! الصفقة أُنجزت."
- **المصطلحات الثابتة:** "OFFICIAL" ← "رسمياً"، "BREAKING" ← "عاجل"، "Here we go" تبقى بالإنجليزي
  حرفياً بدون ترجمة. الاقتباسات تبقى بين علامتي تنصيص وبنفس ترتيبها، وأسماء اللاعبين والأندية بالشكل
  العربي المتداول.
- احذف أي رابط أو إشارة لقناة المصدر نفسها (بما فيها روابط تويتر/X وإشارات "Fabrizio Romano on X").
  **لا تُبقِ أي رابط إطلاقاً بالنص النهائي** — لا روابط يوتيوب، لا روابط تويتر/X، لا أي رابط آخر.
  لا تضيف تعليقات من عندك.

النص:
{text}

أعد النتيجة بصيغة JSON فقط: {{"should_post": true/false, "text": "النص المُعالج"}}"""
    try:
        res = call_gemini(prompt, api_key)
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

    if is_duplicate_content(source_id, cleaned):
        print(f"⏭️ تجاهل — تكرار")
        return

    force = bool(re.search(r'here\s*we\s*go', cleaned, re.IGNORECASE)) if cleaned else False
    res = call_fabrizio(cleaned, GEMINI_KEY)
    should_post = (bool(res["text"]) and (res["should_post"] or force))
    
    if not should_post:
        print("⏭️ تجاهل — فلترة فابريزيو")
        return

    record_published_text(source_id, cleaned)
    final_text = res["text"]
    
    media_msgs = [m for m in messages if _is_usable_media_msg(m)]

    if config.get("skip_if_video") and any(_message_has_any_video(m) for m in messages) and not force:
        print("⏭️ تجاهل — فيديو")
        return
    if config.get("skip_if_youtube") and contains_youtube_link(raw_text) and not force:
        print("⏭️ تجاهل — يوتيوب")
        return

    # ==== فحص زر التشغيل في الصور ====
    if config.get("skip_if_play_button") and not force:
        first_photo_msg = next((m for m in media_msgs if m.photo), None)
        if first_photo_msg:
            tmp_path = None
            has_play = False
            try:
                tmp_path = await client.download_media(first_photo_msg, file="downloads/")
                if tmp_path:
                    has_play = check_image_has_play_button(tmp_path, GEMINI_KEY)
            except Exception as e:
                print(f"⚠️ خطأ تحميل صورة لفحص زر التشغيل: {e}")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try: os.remove(tmp_path)
                    except OSError: pass
            if has_play:
                print(f"⏭️ تجاهل — الصورة غلاف فيديو (تحتوي زر تشغيل)")
                return

    try:
        if media_msgs:
            valid_payloads = [_media_payload(m) for m in media_msgs if _media_payload(m) is not None]
            if valid_payloads:
                to_send = valid_payloads[0] if len(valid_payloads) == 1 else valid_payloads
                await client.send_file(config["target"], to_send, caption=final_text or None, parse_mode="html")
                print(f"✅ نُشر (صورة + نص) — فابريزيو")
                return
                
        if final_text:
            await client.send_message(config["target"], final_text, parse_mode="html", link_preview=False)
            print(f"✅ نُشر (نص فقط) — فابريزيو")
    except Exception as e:
        print(f"❌ خطأ نشر: {e}")

async def catch_up_channel(source_id):
    config = CHANNELS.get(source_id, {})
    entity_ref = config.get("source_username") or source_id
    key = str(source_id)
    
    if TEST_MODE:
        print("🧪 وضع التجربة (TEST_MODE) يعمل: سيتم فحص آخر 5 منشورات ونشرها.")
        last_id = 0 
    else:
        last_id = state.get(key, 0)

    if last_id == 0:
        latest = await client.get_messages(entity_ref, limit=1)
        if latest: 
            last_id = max(latest[0].id - 5, 0) if not TEST_MODE else 0
        
    limit = 5 if TEST_MODE else 200
    messages = await client.get_messages(entity_ref, min_id=last_id, limit=limit)
    
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
        
        if not TEST_MODE:
            state[key] = new_last
            save_state(state)

async def main():
    await client.start()
    print("🚀 بدء فحص فابريزيو...")
    await catch_up_channel(-1001344466508)
    await client.disconnect()
    commit_state()
    print("✅ انتهت التشغيلة.")

if __name__ == "__main__":
    asyncio.run(main())
