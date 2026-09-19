import os
import json
import logging
import threading
import warnings
import asyncio
import requests
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
from playwright.async_api import async_playwright

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
# 1. 환경 변수 및 초기화
# ----------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)

WAITING_FOR_FEEDBACK = 1
user_drafts = {}

# ----------------------------------------------------
# 2. Unsplash 무료 고화질 스톡 이미지 URL 가져오기
# ----------------------------------------------------
def get_unsplash_bg_url(keyword="reading"):
    try:
        if UNSPLASH_ACCESS_KEY:
            url = f"https://api.unsplash.com/photos/random?query={keyword}&client_id={UNSPLASH_ACCESS_KEY}"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                return res.json()["urls"]["regular"]
    except Exception as e:
        logging.error(f"Unsplash 이미지 로드 실패: {e}")

    return "https://images.unsplash.com/photo-1457369804613-52c61a468e7d?auto=format&fit=crop&w=1080&q=80"

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
# 4. Gemini AI 시나리오 생성
# ----------------------------------------------------
def generate_scenario_and_draft(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 도서 전문 트렌디 마케터야. 아래 도서 정보와 요청사항을 바탕으로 인스타그램 카드뉴스 3장 시나리오 및 본문 포스팅을 작성해줘.

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
      "card1_sub": "슬라이드1 카테고리/캐치프레이즈 (예: 한글날 기념 특별 기획)",
      "card2_title": "슬라이드2 메인 주제 제목 (예: 우리가 몰랐던 한글의 역사)",
      "card2_p1": "슬라이드2 핵심 포인트 1 (30자 이내)",
      "card2_p2": "슬라이드2 핵심 포인트 2 (30자 이내)",
      "card2_p3": "슬라이드2 핵심 포인트 3 (30자 이내)",
      "card3_title": "슬라이드3 추천 대상 / 도서의 가치 (예: 이런 분들께 이 책을 추천합니다)",
      "card3_r1": "추천 대상 1 (25자 이내)",
      "card3_r2": "추천 대상 2 (25자 이내)",
      "card3_r3": "추천 대상 3 (25자 이내)",
      "caption": "인스타그램 본문 텍스트 (줄바꿈 및 해시태그 포함, 흥미진진한 도서 소개글 600자 이내)"
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
            "card2_title": f"《{book_title}》 핵심 이야기",
            "card2_p1": "역사 속 숨겨진 감동적인 순간들",
            "card2_p2": "저자가 직접 전하는 생생한 현장 기록",
            "card2_p3": "오늘날 우리가 꼭 기억해야 할 역사적 가치",
            "card3_title": "이런 분들께 추천합니다",
            "card3_r1": "깊이 있는 역사를 쉽게 읽고 싶은 독자",
            "card3_r2": "올바른 역사 의식을 키우고 싶은 청소년",
            "card3_r3": "가슴 따뜻한 이야기를 찾는 모든 분들",
            "caption": f"📖 《{book_title}》\n저자: {author}\n\n{event_info}\n\n#도서추천 #한국사 #책스타그램 #허들링북스"
        }
    return data

# ----------------------------------------------------
# 5. HTML/CSS 기반 웹 렌더링 카드뉴스 생성 엔진
# ----------------------------------------------------
def build_html_template(card_num, book_title, author, scenario_data, cover_url, bg_url):
    """트렌디한 웹 디자이너 스타일의 HTML/CSS 템플릿 코드 생성"""
    
    cover_img_html = f'<img src="{cover_url}" class="book-cover">' if cover_url else ''
    
    css_common = """
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Pretendard', sans-serif; }
    body { width: 1080px; height: 1350px; overflow: hidden; background: #0f172a; position: relative; }
    .bg-image {
        position: absolute; width: 100%; height: 100%;
        background-image: url('""" + bg_url + """');
        background-size: cover; background-position: center;
        filter: blur(8px) brightness(0.45); transform: scale(1.05);
    }
    .overlay {
        position: absolute; width: 100%; height: 100%;
        background: linear-gradient(180deg, rgba(15,23,42,0.4) 0%, rgba(15,23,42,0.85) 100%);
    }
    .container {
        position: relative; z-index: 10; width: 100%; height: 100%;
        padding: 80px 70px; display: flex; flex-direction: column;
        justify-content: space-between; align-items: center; color: #fff;
    }
    .tag {
        background: rgba(56, 189, 248, 0.2); border: 1px solid rgba(56, 189, 248, 0.5);
        color: #38bdf8; padding: 12px 28px; border-radius: 30px; font-size: 22px;
        font-weight: 700; letter-spacing: 2px; text-transform: uppercase;
    }
    .glass-card {
        background: rgba(255, 255, 255, 0.08); backdrop-filter: blur(16px);
        border: 1px solid rgba(255, 255, 255, 0.18); border-radius: 32px;
        box-shadow: 0 30px 60px rgba(0,0,0,0.4); width: 100%; padding: 50px 40px;
    }
    """

    if card_num == 1:
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .book-cover {{
            width: 380px; height: 540px; object-fit: cover; border-radius: 16px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7); border: 1px solid rgba(255,255,255,0.2);
        }}
        .title-box {{ text-align: center; margin-top: 30px; }}
        .title {{ font-size: 52px; font-weight: 800; color: #ffffff; line-height: 1.3; text-shadow: 0 4px 12px rgba(0,0,0,0.5); }}
        .author {{ font-size: 28px; color: #cbd5e1; font-weight: 500; margin-top: 16px; }}
        </style></head><body>
        <div class="bg-image"></div><div class="overlay"></div>
        <div class="container">
            <div class="tag">{scenario_data.get("card1_sub", "NEW BOOK")}</div>
            <div style="margin-top: 40px;">{cover_img_html}</div>
            <div class="title-box">
                <div class="title">《{book_title}》</div>
                <div class="author">{author} 저</div>
            </div>
        </div></body></html>
        """
    elif card_num == 2:
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .header {{ text-align: center; margin-bottom: 40px; }}
        .main-title {{ font-size: 46px; font-weight: 800; color: #ffffff; margin-top: 20px; }}
        .point-item {{
            display: flex; align-items: center; background: rgba(30, 41, 59, 0.7);
            border-left: 5px solid #38bdf8; padding: 28px 32px; border-radius: 16px;
            margin-bottom: 24px; font-size: 26px; font-weight: 600; color: #f1f5f9; line-height: 1.4;
        }}
        </style></head><body>
        <div class="bg-image"></div><div class="overlay"></div>
        <div class="container" style="justify-content: center;">
            <div class="header">
                <div class="tag">INSIGHT STORY</div>
                <div class="main-title">{scenario_data.get("card2_title", "핵심 이야기")}</div>
            </div>
            <div class="glass-card">
                <div class="point-item">01. {scenario_data.get("card2_p1", "")}</div>
                <div class="point-item">02. {scenario_data.get("card2_p2", "")}</div>
                <div class="point-item">03. {scenario_data.get("card2_p3", "")}</div>
            </div>
        </div></body></html>
        """
    else:
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .rec-header {{ text-align: center; margin-bottom: 40px; }}
        .rec-title {{ font-size: 42px; font-weight: 800; color: #ffffff; margin-top: 16px; }}
        .rec-box {{
            background: #ffffff; border-radius: 28px; padding: 40px 30px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.3); color: #0f172a; width: 100%;
        }}
        .rec-item {{
            display: flex; align-items: center; background: #f8fafc;
            padding: 24px 28px; border-radius: 16px; margin-bottom: 20px;
            font-size: 25px; font-weight: 700; color: #334155; border: 1px solid #e2e8f0;
        }}
        .check-icon {{ color: #0284c7; margin-right: 16px; font-weight: 900; }}
        </style></head><body>
        <div class="bg-image"></div><div class="overlay"></div>
        <div class="container" style="justify-content: center;">
            <div class="rec-header">
                <div class="tag">RECOMMENDATION</div>
                <div class="rec-title">《{book_title}》</div>
            </div>
            <div class="rec-box">
                <div style="font-size: 28px; font-weight: 800; color: #0f172a; margin-bottom: 30px; text-align: center;">
                    {scenario_data.get("card3_title", "이런 분들께 강력 추천합니다")}
                </div>
                <div class="rec-item"><span class="check-icon">✓</span> {scenario_data.get("card3_r1", "")}</div>
                <div class="rec-item"><span class="check-icon">✓</span> {scenario_data.get("card3_r2", "")}</div>
                <div class="rec-item"><span class="check-icon">✓</span> {scenario_data.get("card3_r3", "")}</div>
            </div>
        </div></body></html>
        """
    return html

async def render_html_to_images(book_title, author, scenario_data, cover_url):
    """Playwright를 이용해 HTML 코드를 고화질 인스타그램 카드뉴스 PNG로 변환"""
    bg_url = get_unsplash_bg_url("book,library,history")
    img_paths = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1080, "height": 1350})

        for card_num in range(1, 4):
            html_content = build_html_template(card_num, book_title, author, scenario_data, cover_url, bg_url)
            await page.set_content(html_content)
            await page.wait_for_timeout(300) # 스타일 및 폰트 렌더링 완료 대기
            
            output_path = f"card_{card_num}.png"
            await page.screenshot(path=output_path)
            img_paths.append(output_path)

        await browser.close()
    return img_paths

# ----------------------------------------------------
# 6. 텔레그램 핸들러
# ----------------------------------------------------
async def send_draft_pack(chat_id, context, data, scenario_data):
    img_paths = await render_html_to_images(
        data["book_title"], data["author"], scenario_data, data["cover_url"]
    )

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

    await update.message.reply_text("🔄 피드백을 반영하여 HTML/CSS 카드뉴스와 초안을 재생성 중입니다...")

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
