import os
import json
import logging
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# 1. 환경 변수 설정
# ----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

client = genai.Client(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)

WAITING_FOR_FEEDBACK = 1
user_drafts = {}

# ----------------------------------------------------
# 2. 구글 시트 연동 함수 (안정화 적용)
# ----------------------------------------------------
def get_pending_event_from_sheet():
    """구글 시트에서 Status가 Pending인 첫 번째 이벤트를 읽어옵니다."""
    try:
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            logging.error("GOOGLE_SERVICE_ACCOUNT_JSON 환경변수가 설정되지 않았습니다.")
            return None

        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        gc = gspread.service_account_from_dict(creds_dict)
        spreadsheet = gc.open("도서_이벤트_마스터")
        worksheet = spreadsheet.worksheet("Events")
        
        # get_all_values()를 사용하여 모든 행을 리스트로 읽음
        rows = worksheet.get_all_values()
        
        if len(rows) <= 1:
            logging.info("시트에 데이터 행이 존재하지 않습니다.")
            return None

        # 2행(index 1)부터 데이터 검사
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
# 3. Pillow 카드뉴스 이미지 합성 함수
# ----------------------------------------------------
def create_card_news(book_title, event_info, cover_url=None, output_path="cardnews.png"):
    canvas_w, canvas_h = 1080, 1080
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (250, 252, 255))
    draw = ImageDraw.Draw(canvas)

    # 표지 다운로드 및 배치
    cover_y = 120
    h_size = 400
    if cover_url and cover_url.startswith("http"):
        try:
            res = requests.get(cover_url, timeout=5)
            cover_img = Image.open(BytesIO(res.content)).convert("RGBA")
            cover_img.thumbnail((380, 500))
            w_size, h_size = cover_img.size
            cover_x = (canvas_w - w_size) // 2
            
            # 그림자 효과
            draw.rounded_rectangle([cover_x+10, cover_y+10, cover_x+w_size+10, cover_y+h_size+10], radius=12, fill=(220, 225, 230))
            canvas.paste(cover_img, (cover_x, cover_y))
        except Exception as e:
            logging.error(f"표지 이미지 로드 실패: {e}")

    # 텍스트 배치
    font = ImageFont.load_default()
    text_start_y = cover_y + h_size + 60
    draw.text((canvas_w / 2, text_start_y), f"《{book_title}》", font=font, fill=(30, 30, 30), anchor="mm")
    draw.text((canvas_w / 2, text_start_y + 80), event_info, font=font, fill=(70, 70, 70), anchor="mm")

    final_img = canvas.convert("RGB")
    final_img.save(output_path, "PNG")
    return output_path

# ----------------------------------------------------
# 4. Gemini 문구 생성
# ----------------------------------------------------
def generate_draft(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 도서 마케팅 전문가야. 아래 정보로 인스타그램 홍보 포스팅 문구를 작성해줘.
    - 도서명: {book_title}
    - 저자: {author}
    - 이벤트 내용: {event_info}
    """
    if feedback:
        prompt += f"\n\n[사용자 수정 요청사항]: {feedback}\n위 요구사항을 적극 반영해서 재생성해줘."

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )
    return response.text

# ----------------------------------------------------
# 5. 텔레그램 대화 핸들러
# ----------------------------------------------------
async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # 구글 시트에서 Pending 데이터 조회
    event_data = get_pending_event_from_sheet()
    if not event_data:
        await context.bot.send_message(chat_id=chat_id, text="📌 현재 처리할 [Pending] 상태의 도서 이벤트가 없습니다.")
        return ConversationHandler.END

    user_drafts[chat_id] = event_data
    
    # AI 문구 생성 및 카드뉴스 제작
    draft_text = generate_draft(event_data["book_title"], event_data["author"], event_data["event_info"])
    user_drafts[chat_id]["current_text"] = draft_text
    
    img_path = create_card_news(event_data["book_title"], event_data["event_info"], event_data["cover_url"])

    keyboard = [
        [
            InlineKeyboardButton("👍 승인 및 업로드", callback_data="approve"),
            InlineKeyboardButton("✏️ 수정 요청", callback_data="request_edit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 이미지와 문구를 함께 전송
    with open(img_path, "rb") as photo:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=f"📌 **[도서 포스팅 초안 검토 요청]**\n\n{draft_text}",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "approve":
        # 승인 시 구글 시트 Status를 Done으로 변경
        if chat_id in user_drafts and "worksheet" in user_drafts[chat_id]:
            row_idx = user_drafts[chat_id]["row_index"]
            ws = user_drafts[chat_id]["worksheet"]
            ws.update_cell(row_idx, 5, "Done")

        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n✅ **[승인 완료]** 포스팅이 승인되었으며 시트 상태가 'Done'으로 변경되었습니다!"
        )
        return ConversationHandler.END

    elif query.data == "request_edit":
        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n✏️ **[수정 요청]** 수정 사항을 메시지로 입력해 주세요."
        )
        return WAITING_FOR_FEEDBACK

async def receive_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    feedback_text = update.message.text

    await update.message.reply_text("🔄 피드백을 반영하여 초안과 이미지를 재생성 중입니다...")

    data = user_drafts[chat_id]
    new_draft = generate_draft(data["book_title"], data["author"], data["event_info"], feedback=feedback_text)
    user_drafts[chat_id]["current_text"] = new_draft
    
    img_path = create_card_news(data["book_title"], data["event_info"], data["cover_url"])

    keyboard = [
        [
            InlineKeyboardButton("👍 승인 및 업로드", callback_data="approve"),
            InlineKeyboardButton("✏️ 수정 요청", callback_data="request_edit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    with open(img_path, "rb") as photo:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=f"📌 **[수정된 포스팅 초안]**\n\n{new_draft}",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    return ConversationHandler.END

def main():
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
