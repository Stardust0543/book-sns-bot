import os
import json
import logging
import threading
import warnings
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
import gspread
from google import genai

# SDK 경고 메시지 로깅 억제
warnings.filterwarnings("ignore", category=UserWarning, module="google_genai")

# ----------------------------------------------------
# 0. Render 포트 스캔 대응용 미니 웹서버
# ----------------------------------------------------
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Telegram Bot Agent is Running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# ----------------------------------------------------
# 1. 환경 변수 및 폰트 설정
# ----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)

WAITING_FOR_FEEDBACK = 1
user_drafts = {}

FONT_PATH = "NanumGothic.ttf"
def get_font(size):
    if not os.path.exists(FONT_PATH):
        font_url = "https://github.com/google/fonts/raw/main/ofl/nanumgothic/NanumGothic-Bold.ttf"
        try:
            res = requests.get(font_url, timeout=10)
            with open(FONT_PATH, "wb") as f:
                f.write(res.content)
            logging.info("한글 폰트(NanumGothic) 다운로드 완료")
        except Exception as e:
            logging.error(f"폰트 다운로드 실패: {e}")
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()

# ----------------------------------------------------
# 2. Unsplash 무료 고화질 실사 이미지 가져오기
# ----------------------------------------------------
def get_free_stock_image(keyword="reading"):
    try:
        if UNSPLASH_ACCESS_KEY:
            url = f"https://api.unsplash.com/photos/random?query={keyword}&orientation=squarish&client_id={UNSPLASH_ACCESS_KEY}"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                img_url = res.json()["urls"]["regular"]
                img_res = requests.get(img_url, timeout=5)
                return Image.open(BytesIO(img_res.content)).convert("RGBA")
    except Exception as e:
        logging.error(f"Unsplash 이미지 로드 실패: {e}")

    fallback_url = "https://picsum.photos/1080/1080"
    res = requests.get(fallback_url, timeout=5)
    return Image.open(BytesIO(res.content)).convert("RGBA")

# ----------------------------------------------------
# 3. 구글 시트 연동 함수
# ----------------------------------------------------
def get_pending_event_from_sheet():
    try:
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            logging.error("GOOGLE_SERVICE_ACCOUNT_JSON 환경변수가 설정되지 않았습니다.")
            return None

        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open("도서_이벤트_마스터")
        worksheet = spreadsheet.worksheet("Events")
        
        rows = worksheet.get_all_values()
        
        if len(rows) <= 1:
            return None

        for idx, row in enumerate(rows[1:], start=2):
            status = row[4].strip() if len(row) > 4 else ""
            if status == "Pending":
                return {
                    "row_index": idx,
                    "book_title": row[0] if len(row) > 0 else "도서명 미정",
                    "author": row[1] if len(row) > 1 else "저자 미정",
                    "event_info": row[2] if len(row) > 2 else "도서 홍보 요청",
                    "cover_url": row[3] if len(row) > 3 else "",
                    "worksheet": worksheet
                }
    except Exception as e:
        logging.error(f"구글 시트 연동 에러 발생: {e}")
    return None

# ----------------------------------------------------
# 4. 실사 배경 합성 카드뉴스 3장 생성 함수
# ----------------------------------------------------
def create_card_news_pack(book_title, author, event_info, cover_url=None):
    image_paths = []
    canvas_w, canvas_h = 1080, 1080

    font_title = get_font(52)
    font_sub = get_font(36)
    font_body = get_font(30)

    cover_img = None
    if cover_url and cover_url.startswith("http"):
        try:
            res = requests.get(cover_url, timeout=5)
            cover_img = Image.open(BytesIO(res.content)).convert("RGBA")
        except Exception as e:
            logging.error(f"표지 다운로드 실패: {e}")

    bg_img = get_free_stock_image("book,library,reading")
    bg_img = bg_img.resize((canvas_w, canvas_h))

    # 1장: 메인 표지 + 실사 감성 배경
    c1 = bg_img.copy()
    overlay1 = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 140))
    c1 = Image.alpha_composite(c1, overlay1)
    d1 = ImageDraw.Draw(c1)

    if cover_img:
        img_temp = cover_img.copy()
        img_temp.thumbnail((380, 540))
        w_size, h_size = img_temp.size
        cover_x = (canvas_w - w_size) // 2
        cover_y = 120
        d1.rounded_rectangle([cover_x-10, cover_y-10, cover_x+w_size+10, cover_y+h_size+10], radius=12, fill=(255, 255, 255, 40))
        c1.paste(img_temp, (cover_x, cover_y), img_temp)
        text_y = cover_y + h_size + 70
    else:
        text_y = 480

    d1.text((canvas_w / 2, text_y), f"《{book_title}》", font=font_title, fill=(255, 255, 255), anchor="mm")
    d1.text((canvas_w / 2, text_y + 80), f"저자: {author}", font=font_sub, fill=(220, 220, 220), anchor="mm")
    
    p1_path = "card1.png"
    c1.convert("RGB").save(p1_path, "PNG")
    image_paths.append(p1_path)

    # 2장: 홍보 핵심 내용 / 저자 이야기
    c2 = bg_img.copy()
    overlay2 = Image.new("RGBA", (canvas_w, canvas_h), (15, 23, 42, 210))
    c2 = Image.alpha_composite(c2, overlay2)
    d2 = ImageDraw.Draw(c2)

    d2.text((canvas_w / 2, 160), "BOOK HIGHLIGHT", font=font_sub, fill=(56, 189, 248), anchor="mm")
    d2.text((canvas_w / 2, 250), f"《{book_title}》", font=font_title, fill=(255, 255, 255), anchor="mm")

    d2.rounded_rectangle([100, 360, canvas_w-100, 820], radius=24, fill=(255, 255, 255, 25), outline=(255, 255, 255, 60), width=2)
    d2.text((canvas_w / 2, 450), "📌 이 책의 핵심 홍보 포인트", font=font_sub, fill=(255, 255, 255), anchor="mm")
    d2.text((canvas_w / 2, 590), event_info, font=font_body, fill=(226, 232, 240), anchor="mm")

    p2_path = "card2.png"
    c2.convert("RGB").save(p2_path, "PNG")
    image_paths.append(p2_path)

    # 3장: 안내 & CTA
    c3 = Image.new("RGBA", (canvas_w, canvas_h), (248, 250, 252))
    d3 = ImageDraw.Draw(c3)

    header_bg = bg_img.crop((0, 0, canvas_w, 450))
    overlay3 = Image.new("RGBA", (canvas_w, 450), (0, 0, 0, 100))
    header_bg = Image.alpha_composite(header_bg, overlay3)
    c3.paste(header_bg, (0, 0))

    d3.text((canvas_w / 2, 200), f"《{book_title}》", font=font_title, fill=(255, 255, 255), anchor="mm")
    d3.text((canvas_w / 2, 290), event_info, font=font_sub, fill=(226, 232, 240), anchor="mm")

    d3.rounded_rectangle([120, 520, canvas_w-120, 880], radius=30, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
    d3.text((canvas_w / 2, 620), "📖 지금 온·오프라인 서점에서 만나보세요!", font=font_sub, fill=(30, 41, 59), anchor="mm")
    
    d3.rounded_rectangle([200, 720, canvas_w-200, 820], radius=50, fill=(14, 165, 233))
    d3.text((canvas_w / 2, 770), "자세히 보기 & 구매하기 ➔", font=font_sub, fill=(255, 255, 255), anchor="mm")

    p3_path = "card3.png"
    c3.convert("RGB").save(p3_path, "PNG")
    image_paths.append(p3_path)

    return image_paths

# ----------------------------------------------------
# 5. Gemini 홍보 목적별 맞춤 문구 생성 (권장 Chat API 적용)
# ----------------------------------------------------
def generate_draft(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 도서 전문 마케터야. 아래 도서 정보와 요청사항에 맞춰 인스타그램 포스팅 문구를 작성해줘.
    
    - 도서명: {book_title}
    - 저자: {author}
    - 홍보 요청사항/내용: {event_info}

    [작성 가이드]:
    1. 서평 이벤트에만 국한하지 말고, 요청사항 내용({event_info})에 맞춰 도서 추천, 저자 소개, 책 속의 한 문장, 북토크/홍보 이슈 등 목적에 딱 맞는 매력적인 톤으로 작성할 것.
    2. 독자의 흥미를 유발하는 독창적인 캐치프레이즈로 시작할 것.
    3. 텔레그램 메세지 제한을 준수하기 위해 관련 해시태그 포함 전체 길이는 800자 이내로 작성할 것.
    """
    if feedback:
        prompt += f"\n\n[사용자 수정 요청사항]: {feedback}\n위 요구사항을 적극 반영해서 800자 이내로 재생성해줘."

    # Chat API 호출 방식으로 변경하여 SDK 경고 제거
    chat = client.chats.create(model="gemini-3.5-flash-lite")
    response = chat.send_message(prompt)
    
    return response.text

# ----------------------------------------------------
# 6. 텔레그램 핸들러
# ----------------------------------------------------
async def send_draft_pack(chat_id, context, data, text_prompt):
    img_paths = create_card_news_pack(data["book_title"], data["author"], data["event_info"], data["cover_url"])

    caption_text = f"📌 **[도서 맞춤 홍보 초안 검토]**\n\n{text_prompt}"
    if len(caption_text) > 1000:
        caption_text = caption_text[:990] + "...\n(글자 수 제한으로 일부 생략)"

    media = []
    for i, p in enumerate(img_paths):
        if i == 0:
            media.append(InputMediaPhoto(media=open(p, "rb"), caption=caption_text, parse_mode="Markdown"))
        else:
            media.append(InputMediaPhoto(media=open(p, "rb")))

    await context.bot.send_media_group(chat_id=chat_id, media=media)

    keyboard = [
        [
            InlineKeyboardButton("👍 승인 및 완료", callback_data="approve"),
            InlineKeyboardButton("✏️ 수정 요청", callback_data="request_edit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=chat_id, text="👇 아래 버튼을 눌러 승인하거나 수정을 요청해 주세요.", reply_markup=reply_markup)

async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    event_data = get_pending_event_from_sheet()
    if not event_data:
        await context.bot.send_message(chat_id=chat_id, text="📌 현재 처리할 [Pending] 상태의 도서 정보가 없습니다.")
        return ConversationHandler.END

    user_drafts[chat_id] = event_data
    
    draft_text = generate_draft(event_data["book_title"], event_data["author"], event_data["event_info"])
    user_drafts[chat_id]["current_text"] = draft_text
    
    await send_draft_pack(chat_id, context, event_data, draft_text)
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "approve":
        if chat_id in user_drafts and "worksheet" in user_drafts[chat_id]:
            row_idx = user_drafts[chat_id]["row_index"]
            ws = user_drafts[chat_id]["worksheet"]
            ws.update_cell(row_idx, 5, "Done")

        await query.edit_message_text(text="✅ **[승인 완료]** 포스팅이 승인되었으며 구글 시트 상태가 'Done'으로 변경되었습니다!")
        return ConversationHandler.END

    elif query.data == "request_edit":
        await query.edit_message_text(text="✏️ **[수정 요청]** 보완할 요청 사항을 메시지로 입력해 주세요.")
        return WAITING_FOR_FEEDBACK

async def receive_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    feedback_text = update.message.text

    await update.message.reply_text("🔄 피드백과 실사 배경을 반영하여 초안과 3장 카드뉴스를 재생성 중입니다...")

    data = user_drafts[chat_id]
    new_draft = generate_draft(data["book_title"], data["author"], data["event_info"], feedback=feedback_text)
    user_drafts[chat_id]["current_text"] = new_draft
    
    await send_draft_pack(chat_id, context, data, new_draft)
    return ConversationHandler.END

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_draft),
            CallbackQueryHandler(button_handler)
        ],
        states={
            WAITING_FOR_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_feedback)
            ]
        },
        fallbacks=[],
        per_message=False
    )
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == "__main__":
    main()
