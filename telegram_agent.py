import os
import json
import logging
import threading
import warnings
import requests
import textwrap
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
# 2. Unsplash 고화질 무료 이미지 가져오기
# ----------------------------------------------------
def get_free_stock_image(keyword="reading", width=1080, height=1350):
    try:
        if UNSPLASH_ACCESS_KEY:
            url = f"https://api.unsplash.com/photos/random?query={keyword}&client_id={UNSPLASH_ACCESS_KEY}"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                img_url = res.json()["urls"]["regular"]
                img_res = requests.get(img_url, timeout=5)
                img = Image.open(BytesIO(img_res.content)).convert("RGBA")
                return img.resize((width, height))
    except Exception as e:
        logging.error(f"Unsplash 이미지 로드 실패: {e}")

    fallback_url = f"https://picsum.photos/{width}/{height}"
    res = requests.get(fallback_url, timeout=5)
    return Image.open(BytesIO(res.content)).convert("RGBA")

# ----------------------------------------------------
# 3. 구글 시트 연동
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
                    "aspect_ratio": row[5].strip() if len(row) > 5 and row[5].strip() else "4:5",
                    "worksheet": worksheet
                }
    except Exception as e:
        logging.error(f"구글 시트 연동 에러 발생: {e}")
    return None

# ----------------------------------------------------
# 4. Gemini AI 시나리오 및 포스팅 생성
# ----------------------------------------------------
def generate_scenario_and_draft(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 도서 전문 마케터야. 아래 도서 정보와 요청사항을 바탕으로 인스타그램 카드뉴스 3장 시나리오 및 포스팅 문구를 작성해줘.

    [도서 정보]
    - 도서명: {book_title}
    - 저자: {author}
    - 홍보 주제/요청: {event_info}
    """
    if feedback:
        prompt += f"\n- [사용자 수정 요청사항]: {feedback}"

    prompt += """
    반드시 아래 JSON 포맷으로만 응답해줘. 다른 설명이나 마크다운 없이 순수 JSON만 반환해.

    {
      "card1_sub": "슬라이드1 카테고리/캐치프레이즈 (예: 한글날 기념 특별 추천)",
      "card2_title": "슬라이드2 핵심 질문/주제 (15자 이내)",
      "card2_body": "슬라이드2 핵심 스토리/내용 요약 (60자 이내, 줄바꿈 포함 가능)",
      "card3_title": "슬라이드3 추천 대상/메시지 (20자 이내)",
      "caption": "인스타그램 본문 텍스트 (독자의 흥미를 끄는 문구, 해시태그 포함 600자 이내)"
    }
    """

    chat = client.chats.create(model="gemini-3.5-flash-lite")
    response = chat.send_message(prompt)
    
    text = response.text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
    except Exception:
        data = {
            "card1_sub": "특집 추천 도서",
            "card2_title": f"《{book_title}》을 읽어야 하는 이유",
            "card2_body": f"{author} 저자가 전하는 살아있는 역사 이야기.\n지금 이 순간, 우리가 꼭 기억해야 할 역사적 순간들!",
            "card3_title": "역사에 관심 있는 모든 독자분들께 추천합니다",
            "caption": f"📖 《{book_title}》\n저자: {author}\n\n{event_info}\n\n#도서추천 #한국사 #책스타그램 #허들링북스"
        }
    return data

# ----------------------------------------------------
# 5. 시나리오 기반 카드뉴스 3장 자동 합성
# ----------------------------------------------------
def create_card_news_pack(book_title, author, scenario_data, cover_url=None, aspect_ratio="4:5"):
    image_paths = []
    
    if aspect_ratio == "1:1":
        canvas_w, canvas_h = 1080, 1080
    elif aspect_ratio == "1.91:1":
        canvas_w, canvas_h = 1080, 566
    else:
        canvas_w, canvas_h = 1080, 1350

    font_title = get_font(int(canvas_h * 0.040))
    font_sub = get_font(int(canvas_h * 0.026))
    font_body = get_font(int(canvas_h * 0.023))

    cover_img = None
    if cover_url and cover_url.startswith("http"):
        try:
            res = requests.get(cover_url, timeout=5)
            cover_img = Image.open(BytesIO(res.content)).convert("RGBA")
        except Exception as e:
            logging.error(f"표지 다운로드 실패: {e}")

    bg_img = get_free_stock_image("book,library,history", canvas_w, canvas_h)

    # ===== 1장: 메인 표지 카드뉴스 =====
    c1 = bg_img.copy()
    overlay1 = Image.new("RGBA", (canvas_w, canvas_h), (15, 23, 42, 170))
    c1 = Image.alpha_composite(c1, overlay1)
    d1 = ImageDraw.Draw(c1)

    margin = int(canvas_w * 0.06)
    d1.rectangle([margin, margin, canvas_w-margin, canvas_h-margin], outline=(255, 255, 255, 100), width=2)

    # 상단 서브 카테고리
    d1.text((canvas_w / 2, int(canvas_h * 0.12)), scenario_data.get("card1_sub", "FEATURED BOOK"), font=font_sub, fill=(56, 189, 248), anchor="mm")

    if cover_img:
        img_temp = cover_img.copy()
        max_h = int(canvas_h * 0.50)
        img_temp.thumbnail((int(canvas_w * 0.55), max_h))
        w_size, h_size = img_temp.size
        cover_x = (canvas_w - w_size) // 2
        cover_y = int(canvas_h * 0.18)
        
        d1.rounded_rectangle([cover_x-12, cover_y-12, cover_x+w_size+12, cover_y+h_size+12], radius=16, fill=(255, 255, 255, 40))
        c1.paste(img_temp, (cover_x, cover_y), img_temp)
        text_y = cover_y + h_size + int(canvas_h * 0.06)
    else:
        text_y = canvas_h // 2

    d1.text((canvas_w / 2, text_y), f"《{book_title}》", font=font_title, fill=(255, 255, 255), anchor="mm")
    d1.text((canvas_w / 2, text_y + int(canvas_h * 0.05)), f"{author} 지음", font=font_sub, fill=(203, 213, 225), anchor="mm")
    
    p1_path = "card1.png"
    c1.convert("RGB").save(p1_path, "PNG")
    image_paths.append(p1_path)

    # ===== 2장: 핵심 스토리/내용 카드뉴스 (어두운 가독성 카드 적용) =====
    c2 = bg_img.copy()
    overlay2 = Image.new("RGBA", (canvas_w, canvas_h), (15, 23, 42, 220))
    c2 = Image.alpha_composite(c2, overlay2)
    d2 = ImageDraw.Draw(c2)

    d2.text((canvas_w / 2, int(canvas_h * 0.10)), "INSIGHT STORY", font=font_sub, fill=(56, 189, 248), anchor="mm")
    d2.text((canvas_w / 2, int(canvas_h * 0.16)), scenario_data.get("card2_title", "핵심 스토리"), font=font_title, fill=(255, 255, 255), anchor="mm")

    # 가독성을 확보한 어두운 카드 박스 (배경 대비 100% 명확)
    box_margin = int(canvas_w * 0.08)
    d2.rounded_rectangle([box_margin, int(canvas_h * 0.24), canvas_w-box_margin, int(canvas_h * 0.88)], radius=24, fill=(30, 41, 59, 230), outline=(71, 85, 105), width=2)
    
    # 본문 자동 줄바꿈 및 렌더링
    raw_body = scenario_data.get("card2_body", "")
    lines = []
    for paragraph in raw_body.split("\n"):
        lines.extend(textwrap.wrap(paragraph, width=22))
    
    start_y = int(canvas_h * 0.38)
    line_height = int(canvas_h * 0.045)
    for i, line in enumerate(lines[:10]):
        d2.text((canvas_w / 2, start_y + (i * line_height)), line, font=font_body, fill=(241, 245, 249), anchor="mm")

    p2_path = "card2.png"
    c2.convert("RGB").save(p2_path, "PNG")
    image_paths.append(p2_path)

    # ===== 3장: 추천 대상 & CTA 카드뉴스 =====
    c3 = Image.new("RGBA", (canvas_w, canvas_h), (248, 250, 252))
    d3 = ImageDraw.Draw(c3)

    header_h = int(canvas_h * 0.36)
    header_bg = bg_img.crop((0, 0, canvas_w, header_h))
    overlay3 = Image.new("RGBA", (canvas_w, header_h), (0, 0, 0, 130))
    header_bg = Image.alpha_composite(header_bg, overlay3)
    c3.paste(header_bg, (0, 0))

    d3.text((canvas_w / 2, int(header_h * 0.40)), "RECOMMENDATION", font=font_sub, fill=(56, 189, 248), anchor="mm")
    d3.text((canvas_w / 2, int(header_h * 0.70)), f"《{book_title}》", font=font_title, fill=(255, 255, 255), anchor="mm")

    # 하단 추천 카드
    d3.rounded_rectangle([box_margin, header_h + int(canvas_h * 0.05), canvas_w-box_margin, canvas_h - int(canvas_h * 0.06)], radius=28, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
    
    rec_title = scenario_data.get("card3_title", "지금 온·오프라인 서점에서 만나보세요!")
    rec_lines = textwrap.wrap(rec_title, width=18)
    for idx, l in enumerate(rec_lines[:2]):
        d3.text((canvas_w / 2, header_h + int(canvas_h * 0.16) + (idx * 50)), l, font=font_sub, fill=(30, 41, 59), anchor="mm")

    btn_y = canvas_h - int(canvas_h * 0.18)
    d3.rounded_rectangle([int(canvas_w * 0.16), btn_y, canvas_w - int(canvas_w * 0.16), btn_y + int(canvas_h * 0.07)], radius=50, fill=(14, 165, 233))
    d3.text((canvas_w / 2, btn_y + int(canvas_h * 0.035)), "자세히 보기 & 구매하기 ➔", font=font_sub, fill=(255, 255, 255), anchor="mm")

    p3_path = "card3.png"
    c3.convert("RGB").save(p3_path, "PNG")
    image_paths.append(p3_path)

    return image_paths

# ----------------------------------------------------
# 6. 텔레그램 대화 핸들러
# ----------------------------------------------------
async def send_draft_pack(chat_id, context, data, scenario_data):
    aspect = data.get("aspect_ratio", "4:5")
    img_paths = create_card_news_pack(data["book_title"], data["author"], scenario_data, data["cover_url"], aspect)

    media = [InputMediaPhoto(media=open(p, "rb")) for p in img_paths]
    await context.bot.send_media_group(chat_id=chat_id, media=media)

    caption_text = scenario_data.get("caption", "")
    draft_message = f"📌 **[인스타그램 본문 텍스트 초안]**\n\n{caption_text}"
    if len(draft_message) > 4000:
        draft_message = draft_message[:3900] + "...\n(길이 제한으로 일부 생략)"

    keyboard = [
        [
            InlineKeyboardButton("👍 승인 및 완료", callback_data="approve"),
            InlineKeyboardButton("✏️ 수정 요청", callback_data="request_edit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=chat_id,
        text=draft_message,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    event_data = get_pending_event_from_sheet()
    if not event_data:
        await context.bot.send_message(chat_id=chat_id, text="📌 현재 처리할 [Pending] 상태의 도서 정보가 없습니다.")
        return ConversationHandler.END

    user_drafts[chat_id] = event_data
    
    scenario_data = generate_scenario_and_draft(event_data["book_title"], event_data["author"], event_data["event_info"])
    user_drafts[chat_id]["scenario"] = scenario_data
    
    await send_draft_pack(chat_id, context, event_data, scenario_data)
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

    await update.message.reply_text("🔄 피드백을 반영하여 카드뉴스 시나리오와 3장 이미지를 재생성 중입니다...")

    data = user_drafts[chat_id]
    new_scenario = generate_scenario_and_draft(data["book_title"], data["author"], data["event_info"], feedback=feedback_text)
    user_drafts[chat_id]["scenario"] = new_scenario
    
    await send_draft_pack(chat_id, context, data, new_scenario)
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
