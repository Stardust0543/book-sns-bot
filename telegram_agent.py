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

# 대화 상태 정의 (2단계 프로세스)
WAITING_SCENARIO_ACTION = 1
WAITING_SCENARIO_FEEDBACK = 2
WAITING_FINAL_APPROVAL = 3

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
# 4. Gemini AI 가변 장수(1~6장) 시나리오 생성
# ----------------------------------------------------
def generate_dynamic_scenario(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 인스타그램 감성 출판 기획 마케터야. 아래 도서 정보와 홍보 목적에 맞춰 카드뉴스 시나리오(1장~6장 사이 가변) 및 인스타그램 포스팅 문구를 기획해줘.

    [도서 정보]
    - 도서명: {book_title}
    - 저자: {author}
    - 홍보 주제/요청: {event_info}
    """
    if feedback:
        prompt += f"\n- [사용자 수정 요청사항]: {feedback}"

    prompt += """
    [기획 지침]:
    1. 도서의 홍보 목적과 내용의 깊이에 맞춰 카드뉴스 장수를 최소 1장에서 최대 6장 사이로 자율 구성할 것.
    2. 각 카드별 구체적인 역할(표지, 인용구, 에피소드 스토리, 추천 대상 등)과 텍스트 내용을 명확히 설정할 것.

    반드시 아래 JSON 포맷으로만 응답해줘. 다른 설명 없이 순수 JSON 텍스트만 반환해.

    {
      "concept": "전체 카드뉴스 기획 콘셉트 한 줄 요약",
      "slides": [
        {
          "slide_num": 1,
          "type": "cover",
          "badge": "#카테고리태그",
          "sub_title": "표지 캐치프레이즈",
          "title": "도서 메인 제목"
        },
        {
          "slide_num": 2,
          "type": "quote",
          "main_text": "가슴을 울리는 책 속 한 구절 또는 강렬한 질문",
          "sub_text": "인용구 부연 설명"
        }
      ],
      "caption": "인스타그램 본문 텍스트 (줄바꿈 및 해시태그 포함 600자 이내)"
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
            "concept": "한글날 기념 감성 역사 에세이 추천",
            "slides": [
                {
                    "slide_num": 1,
                    "type": "cover",
                    "badge": "#역사속이야기",
                    "sub_title": "우리가 무심코 쓰는 글자에 담긴 수많은 눈물과 기적",
                    "title": book_title
                },
                {
                    "slide_num": 2,
                    "type": "quote",
                    "main_text": "“세상에서 가장 아름다운 유산, 우리가 숨 쉬듯 지켜온 우리말”",
                    "sub_text": "오늘 하루, 우리의 세종과 그날의 마음을 기억해 주세요"
                },
                {
                    "slide_num": 3,
                    "type": "detail",
                    "main_text": "이 책을 꼭 읽어야 하는 이유",
                    "sub_text": "1. 독도에서 임시정부까지 살아있는 역사 이슈\n2. 서경덕 교수와 전문가들의 명쾌한 해설"
                }
            ],
            "caption": f"📖 《{book_title}》\n저자: {author}\n\n{event_info}\n\n#도서추천 #한국사 #책스타그램 #허들링북스"
        }
    return data

# ----------------------------------------------------
# 5. 확정 시나리오 기반 가변 카드뉴스 이미지 합성
# ----------------------------------------------------
def create_dynamic_card_news_pack(book_title, author, scenario_data, cover_url=None, aspect_ratio="4:5"):
    image_paths = []
    slides = scenario_data.get("slides", [])
    
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

    for slide_idx, slide in enumerate(slides, start=1):
        s_type = slide.get("type", "quote")
        c = bg_img.copy()
        
        # 1) 표지 슬라이드 (cover)
        if s_type == "cover" or slide_idx == 1:
            overlay = Image.new("RGBA", (canvas_w, canvas_h), (10, 15, 30, 180))
            c = Image.alpha_composite(c, overlay)
            d = ImageDraw.Draw(c)

            d.text((canvas_w / 2, int(canvas_h * 0.10)), slide.get("badge", "#FEATURED_BOOK"), font=font_sub, fill=(56, 189, 248), anchor="mm")

            if cover_img:
                img_temp = cover_img.copy()
                max_h = int(canvas_h * 0.48)
                img_temp.thumbnail((int(canvas_w * 0.55), max_h))
                w_size, h_size = img_temp.size
                cover_x = (canvas_w - w_size) // 2
                cover_y = int(canvas_h * 0.16)
                
                d.rounded_rectangle([cover_x-16, cover_y-16, cover_x+w_size+16, cover_y+h_size+16], radius=20, fill=(255, 255, 255, 30))
                c.paste(img_temp, (cover_x, cover_y), img_temp)
                text_y = cover_y + h_size + int(canvas_h * 0.06)
            else:
                text_y = canvas_h // 2

            sub_text = slide.get("sub_title", "")
            d.text((canvas_w / 2, text_y), sub_text, font=font_body, fill=(203, 213, 225), anchor="mm")
            d.text((canvas_w / 2, text_y + int(canvas_h * 0.05)), f"《{book_title}》", font=font_huge, fill=(255, 255, 255), anchor="mm")
            d.text((canvas_w / 2, text_y + int(canvas_h * 0.11)), f"{author} 저", font=font_sub, fill=(148, 163, 184), anchor="mm")

        # 2) 명문장/질문 슬라이드 (quote)
        elif s_type == "quote":
            overlay = Image.new("RGBA", (canvas_w, canvas_h), (15, 23, 42, 230))
            c = Image.alpha_composite(c, overlay)
            d = ImageDraw.Draw(c)

            font_quote = get_font(int(canvas_h * 0.12))
            d.text((canvas_w / 2, int(canvas_h * 0.22)), "“", font=font_quote, fill=(56, 189, 248, 120), anchor="mm")

            quote_text = slide.get("main_text", "")
            q_lines = textwrap.wrap(quote_text, width=16)
            
            start_y = int(canvas_h * 0.38)
            for idx, l in enumerate(q_lines):
                d.text((canvas_w / 2, start_y + (idx * int(canvas_h * 0.06))), l, font=font_huge, fill=(255, 255, 255), anchor="mm")

            sub_q = slide.get("sub_text", "")
            d.text((canvas_w / 2, start_y + (len(q_lines) * int(canvas_h * 0.06)) + int(canvas_h * 0.08)), sub_q, font=font_sub, fill=(148, 163, 184), anchor="mm")

        # 3) 기타 상세/요약 슬라이드 (detail)
        else:
            c = Image.new("RGBA", (canvas_w, canvas_h), (248, 250, 252))
            d = ImageDraw.Draw(c)

            header_h = int(canvas_h * 0.35)
            header_bg = bg_img.crop((0, 0, canvas_w, header_h))
            overlay_h = Image.new("RGBA", (canvas_w, header_h), (15, 23, 42, 140))
            header_bg = Image.alpha_composite(header_bg, overlay_h)
            c.paste(header_bg, (0, 0))

            d.text((canvas_w / 2, int(header_h * 0.35)), f"SLIDE 0{slide_idx}", font=font_sub, fill=(56, 189, 248), anchor="mm")
            
            main_t = slide.get("main_text", "")
            d.text((canvas_w / 2, int(header_h * 0.65)), main_t, font=font_title, fill=(255, 255, 255), anchor="mm")

            box_m = int(canvas_w * 0.08)
            card_y = header_h + int(canvas_h * 0.06)
            card_h = canvas_h - header_h - int(canvas_h * 0.12)
            
            d.rounded_rectangle([box_m, card_y, canvas_w - box_m, card_y + card_h], radius=24, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
            
            sub_body = slide.get("sub_text", "")
            lines = textwrap.wrap(sub_body, width=20)
            for idx, l in enumerate(lines[:8]):
                d.text((canvas_w / 2, card_y + int(card_h * 0.20) + (idx * int(canvas_h * 0.05))), l, font=font_sub, fill=(30, 41, 59), anchor="mm")

        p_path = f"card_{slide_idx}.png"
        c.convert("RGB").save(p_path, "PNG")
        image_paths.append(p_path)

    return image_paths

# ----------------------------------------------------
# 6. 텔레그램 대화 핸들러 (2단계 검토 프로세스)
# ----------------------------------------------------
async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    event_data = get_pending_event_from_sheet()
    if not event_data:
        await context.bot.send_message(chat_id=chat_id, text="📌 현재 처리할 [Pending] 상태의 도서 정보가 없습니다.")
        return ConversationHandler.END

    user_drafts[chat_id] = event_data
    
    await context.bot.send_message(chat_id=chat_id, text="🧠 AI가 도서 홍보 가변 시나리오 및 본문 포스팅을 기획 중입니다...")

    scenario_data = generate_dynamic_scenario(event_data["book_title"], event_data["author"], event_data["event_info"])
    user_drafts[chat_id]["scenario"] = scenario_data

    # Step 1: 시나리오 브리핑 메시지 전송
    slides_info = ""
    for s in scenario_data.get("slides", []):
        slides_info += f"• **{s.get('slide_num')}장 ({s.get('type')})**: {s.get('main_text', s.get('title', ''))}\n"

    scenario_msg = f"""
📌 **[1단계: 카드뉴스 시나리오 기획안]**

• **도서명**: 《{event_data['book_title']}》
• **기획 콘셉트**: {scenario_data.get('concept')}
• **카드뉴스 구성 (총 {len(scenario_data.get('slides', []))}장)**:
{slides_info}

----------------------------------------
📝 **[인스타그램 본문 초안]**:
{scenario_data.get('caption')}

----------------------------------------
👇 아래 버튼을 눌러 시나리오를 승인하고 이미지를 생성하거나, 수정 피드백을 보내주세요!
"""
    keyboard = [
        [
            InlineKeyboardButton("👍 시나리오 승인 & 이미지 생성", callback_data="approve_scenario"),
            InlineKeyboardButton("✏️ 시나리오 수정 요청", callback_data="edit_scenario"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(chat_id=chat_id, text=scenario_msg, parse_mode="Markdown", reply_markup=reply_markup)
    return WAITING_SCENARIO_ACTION

async def handle_scenario_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "approve_scenario":
        await query.edit_message_text(text="🎨 확정된 시나리오로 고화질 카드뉴스 이미지 팩을 생성 중입니다. 잠시만 기다려 주세요...")
        
        data = user_drafts[chat_id]
        scenario = data["scenario"]
        
        # 이미지 생성 및 앨범 전송
        img_paths = create_dynamic_card_news_pack(data["book_title"], data["author"], scenario, data["cover_url"], data.get("aspect_ratio", "4:5"))
        
        media = [InputMediaPhoto(media=open(p, "rb")) for p in img_paths]
        await context.bot.send_media_group(chat_id=chat_id, media=media)

        keyboard = [
            [
                InlineKeyboardButton("✅ 최종 포스팅 완료 (Done 처리)", callback_data="final_done"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(chat_id=chat_id, text="📸 생성된 카드뉴스 이미지 팩입니다. 검토 후 완료 버튼을 눌러주세요.", reply_markup=reply_markup)
        return WAITING_FINAL_APPROVAL

    elif query.data == "edit_scenario":
        await query.edit_message_text(text="✏️ **[시나리오 수정]** 보완할 시나리오 방향이나 메시지를 답장으로 입력해 주세요.")
        return WAITING_SCENARIO_FEEDBACK

async def receive_scenario_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    feedback_text = update.message.text

    await update.message.reply_text("🔄 피드백을 반영하여 시나리오를 재기획 중입니다...")

    data = user_drafts[chat_id]
    new_scenario = generate_dynamic_scenario(data["book_title"], data["author"], data["event_info"], feedback=feedback_text)
    user_drafts[chat_id]["scenario"] = new_scenario

    slides_info = ""
    for s in new_scenario.get("slides", []):
        slides_info += f"• **{s.get('slide_num')}장 ({s.get('type')})**: {s.get('main_text', s.get('title', ''))}\n"

    scenario_msg = f"""
📌 **[수정된 카드뉴스 시나리오 기획안]**

• **도서명**: 《{data['book_title']}》
• **기획 콘셉트**: {new_scenario.get('concept')}
• **카드뉴스 구성 (총 {len(new_scenario.get('slides', []))}장)**:
{slides_info}

----------------------------------------
📝 **[인스타그램 본문 초안]**:
{new_scenario.get('caption')}

----------------------------------------
👇 아래 버튼을 눌러 시나리오를 승인하거나 추가 수정을 요청해 주세요!
"""
    keyboard = [
        [
            InlineKeyboardButton("👍 시나리오 승인 & 이미지 생성", callback_data="approve_scenario"),
            InlineKeyboardButton("✏️ 시나리오 수정 요청", callback_data="edit_scenario"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(chat_id=chat_id, text=scenario_msg, parse_mode="Markdown", reply_markup=reply_markup)
    return WAITING_SCENARIO_ACTION

async def handle_final_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "final_done":
        if chat_id in user_drafts and "worksheet" in user_drafts[chat_id]:
            row_idx = user_drafts[chat_id]["row_index"]
            ws = user_drafts[chat_id]["worksheet"]
            ws.update_cell(row_idx, 5, "Done")

        await query.edit_message_text(text="✅ **[최종 처리 완료]** 구글 시트 상태가 'Done'으로 업로드 업데이트되었습니다!")
        return ConversationHandler.END

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_draft)],
        states={
            WAITING_SCENARIO_ACTION: [
                CallbackQueryHandler(handle_scenario_action)
            ],
            WAITING_SCENARIO_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_scenario_feedback)
            ],
            WAITING_FINAL_APPROVAL: [
                CallbackQueryHandler(handle_final_approval)
            ]
        },
        fallbacks=[],
        per_message=False
    )
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == "__main__":
    main()
