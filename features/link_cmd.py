# features/link_cmd.py
import re
import asyncio
import itertools
from urllib.parse import quote
import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================
WORKERS = 4
BATCH_SIZE = 2000
SLEEP_PER_BATCH = 1.5  # detik antar batch
MAX_COMBINATIONS = 20000  # batas kombinasi supaya aman

# Conversation state
ASKING_TAGS = 1

# Templates (pakai dl.dir...)
TEMPLATE_GACHA = "https://dl.dir.freefiremobile.com/common/OB[V]/CSH/gacha/[G]_[E]xxx_[T]_TabID_ind.jpg"
TEMPLATE_BANNER_SPLASH = "https://dl.dir.freefiremobile.com/common/OB[V]/ID/[T]_[E]/splash.jpg"
TEMPLATE_BANNER_OVERVIEW = "https://dl.dir.freefiremobile.com/common/OB[V]/ID/[T]_[E]/overview.jpg"

# Overview spelling variants:
OVERVIEW_VARIANTS = ["overview", "viewover", "overivew", "rivervow", "vowover"]

# =========================
# Utilities
# =========================
_tag_re = re.compile(r'\[([A-Za-z0-9]+)\]')

def extract_tags(template: str):
    return _tag_re.findall(template)

def valid_tags(tags):
    return all(re.fullmatch(r'[A-Za-z0-9]+', t) for t in tags)

def expand_gacha_name_base(name: str):
    """
    Expand a single gacha name into case variants.
    Returns unique list (preserve original).
    Also tries to include a camel-ish TokenWheel if name contains 'token' and 'wheel'.
    """
    variants = []
    orig = name
    if orig not in variants:
        variants.append(orig)
    title = orig.title()
    if title not in variants:
        variants.append(title)
    lower = orig.lower()
    if lower not in variants:
        variants.append(lower)
    # special-case to include TokenWheel style if base contains token & wheel
    if "token" in lower and "wheel" in lower:
        # build "TokenWheel" style: capitalize tokens and concat
        parts = re.split(r'[\s_-]+', lower)
        camel = "".join(p.capitalize() for p in parts)
        if camel not in variants:
            variants.append(camel)
    # keep order, unique
    seen = set()
    uniq = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq

def expand_gacha_values(values_list):
    """
    Given list of input gacha names, return expanded list including variants and suffixes 1..6:
    e.g. "Tokenwheel" -> Tokenwheel, Tokenwheel-2, ..., Tokenwheel-6
          and also TokenWheel, TokenWheel-2, ...
    """
    out = []
    for v in values_list:
        bases = expand_gacha_name_base(v)
        for b in bases:
            # variant 1 is the base without suffix
            out.append(b)
            for i in range(2, 7):  # 2..6 inclusive
                out.append(f"{b}-{i}")
    # dedupe preserving order
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq

# =========================
# URL checking + worker
# =========================
async def check_url(session: aiohttp.ClientSession, url: str) -> bool:
    """Try HEAD first, fallback to GET. Return True if status==200."""
    try:
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                return True
    except:
        pass
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return r.status == 200
    except:
        return False

async def worker_task(session, urls_chunk, progress, total, progress_msg, found_event):
    """
    Check batches inside this worker. Return first found URL or None.
    progress: single-element list used as mutable counter
    found_event: asyncio.Event to notify other workers
    """
    for i in range(0, len(urls_chunk), BATCH_SIZE):
        if found_event.is_set():
            return None
        batch = urls_chunk[i:i+BATCH_SIZE]
        # check batch in parallel
        coros = [check_url(session, u) for u in batch]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # update progress
        progress[0] += len(batch)
        # update progress message (minimalist)
        percent = int(progress[0] / total * 100)
        bar_len = 15
        filled = int(bar_len * percent / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        try:
            await progress_msg.edit_text(f"Progress: [{bar}] {percent}% ({progress[0]}/{total})")
        except Exception:
            pass

        for idx, ok in enumerate(results):
            if isinstance(ok, Exception):
                continue
            if ok:
                found_url = batch[idx]
                # notify others
                found_event.set()
                try:
                    await progress_msg.edit_text(f"✅ Anjay Ketemu, GG Juga Lu🗿: {found_url}")
                except Exception:
                    pass
                return found_url

        # sleep between batches to avoid flooding target
        await asyncio.sleep(SLEEP_PER_BATCH)
    return None

async def find_first_live_url(urls, progress_msg):
    """
    Distribute urls across WORKERS, run worker_task, return first found url or None.
    Uses FIRST_COMPLETED loop to stop early.
    """
    total = len(urls)
    if total == 0:
        return None

    # split urls into roughly equal chunks for workers (round-robin)
    chunks = [urls[i::WORKERS] for i in range(WORKERS)]
    progress = [0]
    found_event = asyncio.Event()

    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(worker_task(session, chunk, progress, total, progress_msg, found_event))
                 for chunk in chunks if chunk]
        if not tasks:
            return None

        pending = set(tasks)
        found = None
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    try:
                        res = d.result()
                    except asyncio.CancelledError:
                        res = None
                    except Exception:
                        res = None
                    if res:
                        found = res
                        # cancel pending tasks
                        for p in pending:
                            p.cancel()
                        # wait for cancellation finish
                        await asyncio.gather(*pending, return_exceptions=True)
                        pending = set()
                        break
                # if found, break outer loop
                if found:
                    break
            # if no one found, ensure all tasks finished
            if not found:
                # gather remaining done results to be thorough
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, str) and r:
                        found = r
                        break
        finally:
            # ensure all tasks cancelled/finished
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return found

# =========================
# URL generation (cartesian product)
# =========================
def generate_urls_from_template(template: str, tag_values: dict):
    """
    template: string with tags like [V], [G], [E], [T], etc.
    tag_values: dict mapping tag -> list of user-supplied strings
    returns list of URLs or raises ValueError if too many combinations or invalid input
    """
    tags = extract_tags(template)
    if not tags:
        return []

    # prepare values per tag, with special handling for G (gacha)
    lists = []
    for t in tags:
        vals = tag_values.get(t, [])
        if not vals:
            return []
        # special expand for G (gacha)
        if t.upper() == "G":
            expanded = expand_gacha_values(vals)
            lists.append(expanded)
        elif t.upper() == "T" and all(v.isdigit() for v in vals):
            # date-like sometimes numeric; keep as-is
            lists.append(vals)
        else:
            # normal: use raw user values
            lists.append(vals)

    # calculate total combos
    total = 1
    for l in lists:
        total *= len(l)
        if total > MAX_COMBINATIONS:
            raise ValueError(f"Jumlah kombinasi ({total}) terlalu besar. Kurangi jumlah nilai per tag.")

    urls = []
    for combo in itertools.product(*lists):
        url = template
        for i, t in enumerate(tags):
            # URL-encode each value (preserve safe chars)
            url = url.replace(f'[{t}]', quote(str(combo[i]), safe=''))
        urls.append(url)
    return urls

# =========================
# Conversation: ask tags then check
# =========================

async def cmdlink_handler(update: Update, context):
    keyboard = [
        [InlineKeyboardButton("Link Gacha❄", callback_data="link_gacha"),
         InlineKeyboardButton("Link Banner🔥", callback_data="link_banner")],
        [InlineKeyboardButton("Link Custom🪐", callback_data="link_custom")]
    ]
    await update.message.reply_text("Pilih Jenis Link Yang Ingin Di Generate:", reply_markup=InlineKeyboardMarkup(keyboard))

async def helplink_handler(update: Update, context):
    keyboard = [
        [InlineKeyboardButton("Gacha❄", callback_data="help_gacha"),
         InlineKeyboardButton("Banner🔥", callback_data="help_banner")],
        [InlineKeyboardButton("Custom🪐", callback_data="help_custom")]
    ]
    await update.message.reply_text("Pilih Tutorial Untuk Mencari Link Yang Kamu Mau🌚:", reply_markup=InlineKeyboardMarkup(keyboard))

async def help_button_callback(update, context):
    query = update.callback_query
    await query.answer()
    key = query.data
    if key == "help_gacha":
        text = "V = Versi Epep\nG = Nama Gacha\nE = Nama Event\nT = Tanggal Rilis\nJika Link Tidak Ketemu Cobalah Acak Nama Event Atau Tanggal"
    elif key == "help_banner":
        text = "V = Versi Epep\nT = Tanggal Event\nE = Nama Event\n Jika Link Tidak Ketemu Cobalah Acak Nama Event Atau Tanggal"
    else:
        text = "Kamu Bisa Menggunakan Link Apa Saja, Tag Yang Kamu Tempatkan Di Link Akan Diubah Oleh Huruf/Angka Yang Kamu Masukan Pada Tag/Kolom Yang Kamu Buat"
    await query.edit_message_text(text)

async def option_button_callback(update, context):
    """
    Entry point when user presses one of the 3 option buttons.
    We set context.user_data['template'] and start asking tags.
    For custom, we ask user to *send template* first.
    """
    query = update.callback_query
    await query.answer()
    opt = query.data
    context.user_data.clear()
    context.user_data['busy'] = False

    if opt == "link_gacha":
        template = TEMPLATE_GACHA
        context.user_data['template'] = template
        await query.edit_message_text(f"Template Digunakan:\n{template}")
        # start by asking first tag
        tags = extract_tags(template)
        context.user_data['tags'] = tags
        context.user_data['current_index'] = 0
        context.user_data['values'] = {}
        first = tags[0]
        await query.message.reply_text(f"Masukan Huruf/Angka Untuk Kolom [{first}] Jika Banyak Huruf/Angka, Maka Pisahkan Dengan Koma😼")
        return ASKING_TAGS

    elif opt == "link_banner":
        # For banner, we'll generate splash and overview variants internally after we collect tags
        # Use overview template for tag extraction (both share same tags)
        template = TEMPLATE_BANNER_OVERVIEW
        context.user_data['template'] = "BANNER"  # marker to later generate both splash & overview variants
        context.user_data['banner_tag_template'] = TEMPLATE_BANNER_OVERVIEW
        context.user_data['tags'] = extract_tags(TEMPLATE_BANNER_OVERVIEW)
        context.user_data['current_index'] = 0
        context.user_data['values'] = {}
        first = context.user_data['tags'][0]
        await query.edit_message_text(f"Template Banner Digunakan.\nMasukkan Huruf/Angka Di Kolom [{first}] Jika Banyak Maka Pisahkan Dengan Koma")
        return ASKING_TAGS

    else:  # custom
        context.user_data['expecting_template'] = True
        await query.edit_message_text("Kirim Template Link Custom Menggunakan Tag: Huruf/Angka Contoh:\nhttps://dl.dir.freefiremobile.com/common/OB[7]/CSH/[b]/[Z].png")
        return ASKING_TAGS

async def handle_tag_input(update: Update, context):
    """
    Handles either:
    - receiving the custom template (if expecting_template True)
    - or receiving the value(s) for the current tag in the ongoing flow
    """
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Template Link Tidak Ada. Kirim Ulang.")
        return ASKING_TAGS

    # If expecting a custom template, parse tags from it
    if context.user_data.get('expecting_template', False):
        template = text
        tags = extract_tags(template)
        if not tags:
            await update.message.reply_text("Template Link Tidak Valid, Tidak Ada Tag Seperti [A] Atau [2]. Kirim Ulang Template Link.")
            return ASKING_TAGS
        if not valid_tags(tags):
            await update.message.reply_text("Tag Hanya Boleh Huruf/Angka. Ganti Tag Dan Kirim Ulang.")
            return ASKING_TAGS
        context.user_data['expecting_template'] = False
        context.user_data['template'] = template
        context.user_data['tags'] = tags
        context.user_data['current_index'] = 0
        context.user_data['values'] = {}
        first = tags[0]
        await update.message.reply_text(f"Template Diterima. Masukkan Huruf/Angka Untuk Kolom [{first}] Jika Banyak Maka Pisahkan Dengan Koma.")
        return ASKING_TAGS

    # Else we are receiving values for the current tag
    tags = context.user_data.get('tags', [])
    idx = context.user_data.get('current_index', 0)
    if idx >= len(tags):
        await update.message.reply_text("Terjadi Error (index tag). Mulai Ulang Script /cmdlink.")
        return ConversationHandler.END

    tag = tags[idx]
    # split values by comma
    vals = [v.strip() for v in text.split(',') if v.strip()]
    if not vals:
        await update.message.reply_text("Tidak Terdeteksi Huruf/Angka Yang Valid. Kirim Ulang Huruf/Angka Untuk Kolom Ini.")
        return ASKING_TAGS

    context.user_data['values'][tag] = vals

    # move to next tag or finish
    if idx + 1 < len(tags):
        context.user_data['current_index'] = idx + 1
        nxt = tags[idx + 1]
        await update.message.reply_text(f"Masukkan Huruf/Angka Untuk Kolom [{nxt}] Jika Banyak Maka Pisahkan Dengan Koma.")
        return ASKING_TAGS

    # all tags collected -> build urls and check
    template_marker = context.user_data.get('template')
    tag_values = context.user_data.get('values', {})

    # Build urls list according to template marker
    urls = []
    try:
        if template_marker == "BANNER":
            # need to produce both splash and overview variants,
            # overview has 5 spelling variants; for each event & date combination
            # We'll use banner_tag_template for replacing tags
            tag_template = context.user_data.get('banner_tag_template')
            # For splash:
            splash_template = TEMPLATE_BANNER_SPLASH
            # For overview variants:
            overview_template = TEMPLATE_BANNER_OVERVIEW

            # For splash: generate urls for each combination
            splash_urls = generate_urls_from_template(splash_template, tag_values)
            # For overview: generate for each variant name replacement of "overview" token in URL
            # We'll generate overview urls by replacing '/overview.jpg' with '/{variant}.jpg' after generation
            ov_urls = []
            base_ov_urls = generate_urls_from_template(overview_template, tag_values)
            for base in base_ov_urls:
                for v in OVERVIEW_VARIANTS:
                    ov_urls.append(base.replace("/overview.jpg", f"/{v}.jpg"))
            urls = splash_urls + ov_urls

        else:
            # normal template (GACHA or CUSTOM)
            template = context.user_data.get('template')
            urls = generate_urls_from_template(template, tag_values)

    except ValueError as e:
        await update.message.reply_text(str(e))
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text("Terjadi Error Saat Generate Link, Coba Lagi.")
        return ConversationHandler.END

    # safety: if nothing generated
    if not urls:
        await update.message.reply_text("Tidak Ada Link Yang Di Generate. Pastikan Semua Kolom Terisi.")
        return ConversationHandler.END

    # Inform user total count
    total = len(urls)
    await update.message.reply_text(f"Total Link Yang Di Generate: {total}. Memulai Pengecekan😈...")

    # start checking and show progress via a dedicated message
    progress_msg = await update.message.reply_text("Progress: [░░░░░░░░░░░░░░░] 0% (0/{})".format(total))

    found = await find_first_live_url(urls, progress_msg)

    if not found:
        # edit final message already handled in worker; ensure final text
        await progress_msg.edit_text("❌ Aowkwok Gada Jir😂")
    # else (found) already edited by worker to the success message

    return ConversationHandler.END

# =========================
# Helper: generate URLs (cartesian)
# =========================
def generate_urls_from_template(template: str, tag_values: dict):
    """
    Thin wrapper that uses generate_urls_from_template defined above
    (here we want to detect G tag expansion etc).
    """
    # reuse function defined earlier in module (avoid name collision)
    # We will call the internal generator implemented above.
    # Already defined: generate_urls_from_template (same name) -> so just call it.
    # But to avoid recursion mistake, we call the lower-level generator function implemented earlier:
    return __generate_urls_from_template_impl(template, tag_values)

def __generate_urls_from_template_impl(template: str, tag_values: dict):
    # same logic as previously defined generate_urls_from_template
    tags = extract_tags(template)
    if not tags:
        return []
    lists = []
    for t in tags:
        vals = tag_values.get(t, [])
        if not vals:
            return []
        if t.upper() == "G":
            lists.append(expand_gacha_values(vals))
        else:
            lists.append(vals)
    total = 1
    for l in lists:
        total *= len(l)
        if total > MAX_COMBINATIONS:
            raise ValueError(f"Jumlah Kombinasi ({total}) Terlalu Banyak. Kurangi Jumlah Huruf/Angka Di Tag.")
    urls = []
    for combo in itertools.product(*lists):
        u = template
        for i, t in enumerate(tags):
            u = u.replace(f'[{t}]', quote(str(combo[i]), safe=''))
        urls.append(u)
    return urls

# =========================
# Register handlers helper
# =========================
def register_handlers(application):
    """
    Call this from bot.py:
      from features.link_cmd import register_handlers
      register_handlers(application)
    """
    # main commands
    application.add_handler(CommandHandler("cmdlink", cmdlink_handler))
    application.add_handler(CommandHandler("helplink", helplink_handler))
    # help button callbacks
    application.add_handler(CallbackQueryHandler(help_button_callback, pattern="^help_"))
    # button callbacks for options (entry to conversation)
    # Conversation starts with pressing a link_ callback
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(option_button_callback, pattern="^link_")],
        states={
            ASKING_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tag_input)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )
    application.add_handler(conv)
    # also direct callback handler for the buttons (so option_button_callback catches them)
    application.add_handler(CallbackQueryHandler(option_button_callback, pattern="^link_"))
