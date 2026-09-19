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
# 2. Unsplash 감성 이미지 가져오기
# ----------------------------------------------------
def get_free_stock_image(keyword="reading,book,library", width=1080, height=1350):
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
# 4. Gemini AI 감성 시나리오 생성
# ----------------------------------------------------
def generate_scenario_and_draft(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 인스타그램 감성 도서 마케터야. 아래 도서 정보와 요청사항을 바탕으로 독자의 가슴을 울리는 감성 카드뉴스 시나리오 및 본문 포스팅을 작성해줘. 이미지 장수는 시나리오에 맞게 3~6장으로 해줘.

    [도서 정보]
    - 도서명: {book_title}
    - 저자: {author}
    - 홍보 주제/요청: {event_info}
    """
    if feedback:
        prompt += f"\n- [사용자 수정 요청사항]: {feedback}"

    prompt += """
    반드시 아래 JSON 포맷으로만 응답해줘. 다른 설명 없이 순수 JSON 텍스트만 반환해.

    {
      "card1_badge": "슬라이드1 상단 뱃지 (예: #가슴을울리는역사)",
      "card1_sub": "슬라이드1 캐치프레이즈 (예: 역사의 거센 파도 속, 우리가 지켜낸 이름)",
      "card2_quote": "슬라이드2 책 속 명문장 또는 핵심 질문 (40자 이내, 강렬하고 감성적인 인용구)",
      "card2_sub": "슬라이드2 인용구 부연 설명 (35자 이내)",
      "card3_title": "슬라이드3 메인 질문/주제 (예: 오늘, 당신이 지키고 싶은 가치는 무엇인가요?)",
      "card3_point1": "슬라이드3 주요 포인트 1 (25자 이내)",
      "card3_point2": "슬라이드3 주요 포인트 2 (25자 이내)",
      "caption": "인스타그램 본문 텍스트 (감성적인 문체, 해시태그 포함 600자 이내)"
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
            "card1_badge": "#오늘의추천도서",
            "card1_sub": "역사의 순간 속에서 찾아낸 우리의 이야기",
            "card2_quote": "“기억하지 않는 역사는 되풀이된다.”",
            "card2_sub": "우리가 반드시 알아야 할 잊혀진 선조들의 숨결",
            "card3_title": "이 책이 당신의 마음에 전하는 깊은 울림",
            "card3_point1": "역사적 사실 너머의 가슴 뜨거운 감동",
            "card3_point2": "지금 온·오프라인 서점에서 만나보세요",
            "caption": f"📖 《{book_title}》\n저자: {author}\n\n{event_info}\n\n#도서추천 #한국사 #책스타그램 #허들링북스"
        }
    return data

# ----------------------------------------------------
# 5. 동적 감성 디자인 카드뉴스 3장 생성
# ----------------------------------------------------
def create_card_news_pack(book_title, author, scenario_data, cover_url=None, aspect_ratio="4:5"):
    image_paths = []
    
    if aspect_ratio == "1:1":
        canvas_w, canvas_h = 1080, 1080
    elif aspect_ratio == "1.91:1":
        canvas_w, canvas_h = 1080, 566
    else:
        canvas_w, canvas_h = 1080, 1350

    font_huge = get_font(int(canvas_h * 0.045))
    font_title = get_font(int(canvas_h * 0.036))
    font_sub = get_font(int(canvas_h * 0.026))
    font_body = get_font(int(canvas_h * 0.022))

    cover_img = None
    if cover_url and cover_url.startswith("http"):
        try:
            res = requests.get(cover_url, timeout=5)
            cover_img = Image.open(BytesIO(res.content)).convert("RGBA")
        except Exception as e:
            logging.error(f"표지 다운로드 실패: {e}")

    bg_img = get_free_stock_image("book,library,history,emotional", canvas_w, canvas_h)

    # ===== 1장: 대형 포커스 표지 + 그라데이션 후광 템플릿 =====
    c1 = bg_img.copy()
    overlay1 = Image.new("RGBA", (canvas_w, canvas_h), (10, 15, 30, 180))
    c1 = Image.alpha_composite(c1, overlay1)
    d1 = ImageDraw.Draw(c1)

    # 상단 감성 뱃지
    d1.text((canvas_w / 2, int(canvas_h * 0.10)), scenario_data.get("card1_badge", "#FEATURED_BOOK"), font=font_sub, fill=(56, 189, 248), anchor="mm")

    if cover_img:
        img_temp = cover_img.copy()
        max_h = int(canvas_h * 0.48)
        img_temp.thumbnail((int(canvas_w * 0.55), max_h))
        w_size, h_size = img_temp.size
        cover_x = (canvas_w - w_size) // 2
        cover_y = int(canvas_h * 0.16)
        
        # 은은한 글로우 후광 박스
        d1.rounded_rectangle([cover_x-16, cover_y-16, cover_x+w_size+16, cover_y+h_size+16], radius=20, fill=(255, 255, 255, 30))
        c1.paste(img_temp, (cover_x, cover_y), img_temp)
        text_y = cover_y + h_size + int(canvas_h * 0.06)
    else:
        text_y = canvas_h // 2

    # 캐치프레이즈 및 타이틀
    sub_text = scenario_data.get("card1_sub", "")
    d1.text((canvas_w / 2, text_y), sub_text, font=font_body, fill=(203, 213, 225), anchor="mm")
    d1.text((canvas_w / 2, text_y + int(canvas_h * 0.05)), f"《{book_title}》", font=font_huge, fill=(255, 255, 255), anchor="mm")
    d1.text((canvas_w / 2, text_y + int(canvas_h * 0.11)), f"{author} 지음", font=font_sub, fill=(148, 163, 184), anchor="mm")
    
    p1_path = "card1.png"
    c1.convert("RGB").save(p1_path, "PNG")
    image_paths.append(p1_path)

    # ===== 2장: 책 속 명문장/질문 인용 템플릿 (인용구 타이포그래피) =====
    c2 = bg_img.copy()
    overlay2 = Image.new("RGBA", (canvas_w, canvas_h), (15, 23, 42, 230))
    c2 = Image.alpha_composite(c2, overlay2)
    d2 = ImageDraw.Draw(c2)

    # 대형 큰따옴표 장식
    font_quote = get_font(int(canvas_h * 0.12))
    d2.text((canvas_w / 2, int(canvas_h * 0.22)), "“", font=font_quote, fill=(56, 189, 248, 120), anchor="mm")

    quote_text = scenario_data.get("card2_quote", "")
    q_lines = textwrap.wrap(quote_text, width=16)
    
    start_y = int(canvas_h * 0.38)
    for idx, l in enumerate(q_lines):
        d2.text((canvas_w / 2, start_y + (idx * int(canvas_h * 0.06))), l, font=font_huge, fill=(255, 255, 255), anchor="mm")

    sub_q = scenario_data.get("card2_sub", "")
    d2.text((canvas_w / 2, start_y + (len(q_lines) * int(canvas_h * 0.06)) + int(canvas_h * 0.08)), sub_q, font=font_sub, fill=(148, 163, 184), anchor="mm")

    p2_path = "card2.png"
    c2.convert("RGB").save(p2_path, "PNG")
    image_paths.append(p2_path)

    # ===== 3장: 비대칭 감성 레이아웃 & 추천 인사이트 템플릿 =====
    c3 = Image.new("RGBA", (canvas_w, canvas_h), (248, 250, 252))
    d3 = ImageDraw.Draw(c3)

    # 상단 스톡 비주얼 헤더
    header_h = int(canvas_h * 0.40)
    header_bg = bg_img.crop((0, 0, canvas_w, header_h))
    overlay3 = Image.new("RGBA", (canvas_w, header_h), (15, 23, 42, 140))
    header_bg = Image.alpha_composite(header_bg, overlay3)
    c3.paste(header_bg, (0, 0))

    d3.text((canvas_w / 2, int(header_h * 0.35)), "BOOK INSIGHT", font=font_sub, fill=(56, 189, 248), anchor="mm")
    
    title_3 = scenario_data.get("card3_title", f"《{book_title}》")
    t3_lines = textwrap.wrap(title_3, width=16)
    for idx, l in enumerate(t3_lines[:2]):
        d3.text((canvas_w / 2, int(header_h * 0.60) + (idx * int(canvas_h * 0.05))), l, font=font_title, fill=(255, 255, 255), anchor="mm")

    # 하단 2개 핵심 포인트 카드 (비대칭 카드 스타일)
    box_m = int(canvas_w * 0.08)
    card_y1 = header_h + int(canvas_h * 0.06)
    
    p1 = scenario_data.get("card3_point1", "")
    if p1:
        d3.rounded_rectangle([box_m, card_y1, canvas_w - box_m, card_y1 + int(canvas_h * 0.16)], radius=20, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
        d3.text((box_m + 40, card_y1 + 35), "POINT 01", font=font_body, fill=(14, 165, 233))
        d3.text((box_m + 40, card_y1 + 85), p1, font=font_sub, fill=(30, 41, 59))

    p2 = scenario_data.get("card3_point2", "")
    card_y2 = card_y1 + int(canvas_h * 0.20)
    if p2:
        d3.rounded_rectangle([box_m, card_y2, canvas_w - box_m, card_y2 + int(canvas_h * 0.16)], radius=20, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
        d3.text((box_m + 40, card_y2 + 35), "POINT 02", font=font_body, fill=(14, 165, 233))
        d3.text((box_m + 40, card_y2 + 85), p2, font=font_sub, fill=(30, 41, 59))

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

    await update.message.reply_text("🔄 피드백을 반영하여 감성 카드뉴스 시나리오와 3장 이미지를 재생성 중입니다...")

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
