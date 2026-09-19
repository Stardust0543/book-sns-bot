import os
import json
import logging
import threading
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
# 1. 환경 변수 및 한글 폰트 자동 다운로드
# ----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

client = genai.Client(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)

WAITING_FOR_FEEDBACK = 1
user_drafts = {}

FONT_PATH = "NanumGothic.ttf"
def get_font(size):
    """나눔고딕 폰트 파일이 없으면 자동으로 다운로드하여 적용합니다."""
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
# 2. 구글 시트 연동 함수
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
            logging.info("시트에 데이터 행이 존재하지 않습니다.")
            return None

        for idx, row in enumerate(rows[1:], start=2):
            status = row[4].strip() if len(row) > 4 else ""
            if status == "Pending":
                return {
                    "row_index": idx,
                    "book_title": row[0] if len(row) > 0 else "도서명 미정",
                    "author": row[1] if len(row) > 1 else "저자 미정",
                    "event_info": row[2] if len(row) > 2 else "이벤트 내용 없음",
                    "cover_url": row[3] if len(row) > 3 else "",
                    "worksheet": worksheet
                }
    except Exception as e:
        logging.error(f"구글 시트 연동 에러 발생: {e}")
    return None

# ----------------------------------------------------
# 3. Pillow 카드뉴스 3장 자동 합성 함수
# ----------------------------------------------------
def create_card_news_pack(book_title, author, event_info, cover_url=None):
    image_paths = []
    canvas_w, canvas_h = 1080, 1080

    font_title = get_font(52)
    font_sub = get_font(36)
    font_body = get_font(30)

    # [표지 이미지 다운로드]
    cover_img = None
    if cover_url and cover_url.startswith("http"):
        try:
            res = requests.get(cover_url, timeout=5)
            cover_img = Image.open(BytesIO(res.content)).convert("RGBA")
        except Exception as e:
            logging.error(f"표지 이미지 다운로드 실패: {e}")

    # ===== 1장: 메인 표지 카드뉴스 =====
    c1 = Image.new("RGBA", (canvas_w, canvas_h), (245, 247, 250))
    d1 = ImageDraw.Draw(c1)
    
    if cover_img:
        img_temp = cover_img.copy()
        img_temp.thumbnail((440, 600))
        w_size, h_size = img_temp.size
        cover_x = (canvas_w - w_size) // 2
        cover_y = 100
        d1.rounded_rectangle([cover_x+12, cover_y+12, cover_x+w_size+12, cover_y+h_size+12], radius=16, fill=(210, 215, 222))
        c1.paste(img_temp, (cover_x, cover_y), img_temp)
        text_y = cover_y + h_size + 60
    else:
        text_y = 450

    d1.text((canvas_w / 2, text_y), f"《{book_title}》", font=font_title, fill=(20, 20, 20), anchor="mm")
    d1.text((canvas_w / 2, text_y + 80), f"저자: {author}", font=font_sub, fill=(80, 80, 80), anchor="mm")
    
    p1_path = "card1.png"
    c1.convert("RGB").save(p1_path, "PNG")
    image_paths.append(p1_path)

    # ===== 2장: 핵심 포인트 카드뉴스 =====
    c2 = Image.new("RGBA", (canvas_w, canvas_h), (250, 252, 255))
    d2 = ImageDraw.Draw(c2)
    d2.rectangle([80, 80, canvas_w-80, canvas_h-80], outline=(220, 225, 230), width=4)
    
    d2.text((canvas_w / 2, 200), "BOOK HIGHLIGHT", font=font_sub, fill=(0, 102, 204), anchor="mm")
    d2.text((canvas_w / 2, 300), f"《{book_title}》", font=font_title, fill=(20, 20, 20), anchor="mm")
    
    # 중앙 설명 상자
    d2.rounded_rectangle([140, 420, canvas_w-140, 780], radius=20, fill=(235, 242, 250))
    d2.text((canvas_w / 2, 520), "📌 주요 내용 및 핵심 포인트", font=font_sub, fill=(30, 30, 30), anchor="mm")
    d2.text((canvas_w / 2, 620), event_info, font=font_body, fill=(60, 60, 60), anchor="mm")

    p2_path = "card2.png"
    c2.convert("RGB").save(p2_path, "PNG")
    image_paths.append(p2_path)

    # ===== 3장: 이벤트 & CTA 카드뉴스 =====
    c3 = Image.new("RGBA", (canvas_w, canvas_h), (240, 244, 248))
    d3 = ImageDraw.Draw(c3)
    
    d3.rounded_rectangle([100, 150, canvas_w-100, canvas_h-150], radius=30, fill=(255, 255, 255))
    d3.text((canvas_w / 2, 280), "SPECIAL EVENT", font=font_sub, fill=(220, 50, 50), anchor="mm")
    d3.text((canvas_w / 2, 400), "🎉 도서 출간 기념 이벤트", font=font_title, fill=(20, 20, 20), anchor="mm")
    d3.text((canvas_w / 2, 520), event_info, font=font_sub, fill=(50, 50, 50), anchor="mm")
    
    # CTA 버튼 박스
    d3.rounded_rectangle([200, 680, canvas_w-200, 780], radius=50, fill=(0, 102, 204))
    d3.text((canvas_w / 2, 730), "프로필 링크에서 참여하기 ➔", font=font_sub, fill=(255, 255, 255), anchor="mm")

    p3_path = "card3.png"
    c3.convert("RGB").save(p3_path, "PNG")
    image_paths.append(p3_path)

    return image_paths

# ----------------------------------------------------
# 4. Gemini 문구 생성 (800자 이하 제약)
# ----------------------------------------------------
def generate_draft(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 도서 마케팅 전문가야. 아래 정보로 인스타그램 홍보 포스팅 문구를 작성해줘.
    - 도서명: {book_title}
    - 저자: {author}
    - 이벤트 내용: {event_info}
    
    [주의사항]: 텔레그램 메시지 길이 제한을 준수하기 위해 해시태그 포함 전체 문구 길이는 800자 이내로 간결하고 매력적으로 작성해줘.
    """
    if feedback:
        prompt += f"\n\n[사용자 수정 요청사항]: {feedback}\n위 요구사항을 적극 반영해서 800자 이내로 재생성해줘."

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )
    return response.text

# ----------------------------------------------------
# 5. 텔레그램 대화 핸들러 (3장 앨범 전송)
# ----------------------------------------------------
async def send_draft_pack(chat_id, context, data, text_prompt):
    img_paths = create_card_news_pack(data["book_title"], data["author"], data["event_info"], data["cover_url"])

    caption_text = f"📌 **[도서 포스팅 초안 검토 요청]**\n\n{text_prompt}"
    if len(caption_text) > 1000:
        caption_text = caption_text[:990] + "...\n(글자 수 제한으로 일부 생략)"

    # 3장 이미지를 앨범(MediaGroup)으로 구성
    media = []
    for i, p in enumerate(img_paths):
        if i == 0:
            media.append(InputMediaPhoto(media=open(p, "rb"), caption=caption_text, parse_mode="Markdown"))
        else:
            media.append(InputMediaPhoto(media=open(p, "rb")))

    await context.bot.send_media_group(chat_id=chat_id, media=media)

    keyboard = [
        [
            InlineKeyboardButton("👍 승인 및 업로드", callback_data="approve"),
            InlineKeyboardButton("✏️ 수정 요청", callback_data="request_edit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=chat_id, text="👇 아래 버튼을 눌러 승인하거나 수정을 요청해 주세요.", reply_markup=reply_markup)

async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    event_data = get_pending_event_from_sheet()
    if not event_data:
        await context.bot.send_message(chat_id=chat_id, text="📌 현재 처리할 [Pending] 상태의 도서 이벤트가 없습니다.")
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
        await query.edit_message_text(text="✏️ **[수정 요청]** 수정 및 보완할 사항을 메시지로 입력해 주세요.")
        return WAITING_FOR_FEEDBACK

async def receive_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    feedback_text = update.message.text

    await update.message.reply_text("🔄 피드백을 반영하여 초안과 3장의 카드뉴스 이미지를 재생성 중입니다...")

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
