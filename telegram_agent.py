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

WAITING_SCENARIO_ACTION = 1
WAITING_SCENARIO_FEEDBACK = 2
WAITING_FINAL_APPROVAL = 3

user_drafts = {}

# ----------------------------------------------------
# 2. Unsplash 감성 스톡 이미지 URL 가져오기
# ----------------------------------------------------
def get_unsplash_bg_url(keyword="history,book,library"):
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
        logging.error(f"구글 시트 연동 에러: {e}")
    return None

# ----------------------------------------------------
# 4. Gemini AI 전문 시나리오 기획 생성
# ----------------------------------------------------
def generate_pro_scenario(book_title, author, event_info, feedback=None):
    prompt = f"""
    너는 베스트셀러 출판사의 인스타그램 전문 콘텐츠 마케팅 디렉터야.
    아래 도서 정보와 요청사항을 바탕으로 도서의 목적과 내용 깊이에 맞춰 카드뉴스 슬라이드 장수(최소 1장 ~ 최대 6장)를 자율 결정하여 최고 수준의 시나리오 및 본문 포스팅을 기획해줘.

    [도서 정보]
    - 도서명: 《{book_title}》
    - 저자: {author}
    - 홍보 주제/이슈: {event_info}
    """
    if feedback:
        prompt += f"\n- [사용자 피드백 반영 요청]: {feedback}"

    prompt += """
    반드시 아래 JSON 포맷으로만 응답해줘. 다른 설명 없이 순수 JSON 텍스트만 반환해.

    {
      "intent": "기획 의도 (1-2줄)",
      "target": "주요 타깃 독자층",
      "tone": "톤앤매너",
      "slide_count": 5,
      "slides": [
        {
          "slide_num": 1,
          "type": "cover",
          "head_copy": "메인 카피 (강렬한 질문/화두)",
          "sub_copy": "서브 카피"
        },
        {
          "slide_num": 2,
          "type": "quote",
          "head_copy": "가슴을 울리는 책 속 한 구절 또는 인용구",
          "body": "부연 설명"
        },
        {
          "slide_num": 3,
          "type": "detail",
          "head_copy": "핵심 배경/스토리",
          "body": "본문 설명 (줄바꿈 포함 가능)"
        },
        {
          "slide_num": 4,
          "type": "cta",
          "head_copy": "메인 카피 (도서 메시지 & CTA)",
          "sub_copy": "하단 안내 문구"
        }
      ],
      "caption": "인스타그램 본문 텍스트 (줄바꿈, 이모지, 본문글, 해시태그 포함 600자 이내)"
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
            "intent": "역사적 수난 속에서 우리말과 글을 지켜낸 선조들의 노력 부각",
            "target": "한글날의 의미를 새기고 싶은 독자",
            "tone": "진정성 있고 감동적인 톤",
            "slide_count": 4,
            "slides": [
                {
                    "slide_num": 1,
                    "type": "cover",
                    "head_copy": "“만약 일제강점기에 우리말과 글이 완전히 사라졌다면?”",
                    "sub_copy": "우리가 세종대왕 뒤에 꼭 기억해야 할 또 다른 영웅들의 이야기."
                },
                {
                    "slide_num": 2,
                    "type": "quote",
                    "head_copy": "“말은 민족의 정신이요, 글은 민족의 생명이다”",
                    "body": "수많은 학자들이 희생당하면서도 끝까지 지켜낸 것은 바로 '우리의 정체성'이었습니다."
                },
                {
                    "slide_num": 3,
                    "type": "detail",
                    "head_copy": "오늘 당연하게 쓰는 한글, 당연하게 지켜진 것은 없습니다.",
                    "body": "세종대왕의 애민정신부터 독립운동가들의 피와 땀까지.\n역사는 매일 읽고 쓰는 이 글자 하나하나에 살아 숨 쉬고 있습니다."
                },
                {
                    "slide_num": 4,
                    "type": "cta",
                    "head_copy": "더 깊이 알고, 끝까지 기억해야 할 우리 역사 이야기",
                    "sub_copy": "📘 《우리가 지켜야 할 한국사》\n전국 온·오프라인 서점에서 만나보세요."
                }
            ],
            "caption": f"🇰🇷 《{book_title}》\n저자: {author}\n\n{event_info}\n\n#한글날 #우리가지켜야할한국사 #한국사 #책스타그램 #허들링북스"
        }
    return data

# ----------------------------------------------------
# 5. HTML/CSS 기반 전문 디자인 템플릿 생성 엔진
# ----------------------------------------------------
def build_html_template(slide, book_title, author, cover_url, bg_url):
    s_type = slide.get("type", "detail")
    head = slide.get("head_copy", "")
    sub = slide.get("sub_copy", "")
    body = slide.get("body", "").replace("\n", "<br>")

    css_common = f"""
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Pretendard', sans-serif; }}
    body {{ width: 1080px; height: 1350px; overflow: hidden; background: #0f172a; position: relative; }}
    .bg-image {{
        position: absolute; width: 100%; height: 100%;
        background-image: url('{bg_url}');
        background-size: cover; background-position: center;
        filter: blur(8px) brightness(0.35); transform: scale(1.05);
    }}
    .overlay {{
        position: absolute; width: 100%; height: 100%;
        background: linear-gradient(180deg, rgba(15,23,42,0.3) 0%, rgba(15,23,42,0.88) 100%);
    }}
    .container {{
        position: relative; z-index: 10; width: 100%; height: 100%;
        padding: 90px 75px; display: flex; flex-direction: column;
        justify-content: center; align-items: center; color: #fff; text-align: center;
    }}
    .glass-card {{
        background: rgba(255, 255, 255, 0.07); backdrop-filter: blur(20px);
        border: 1px solid rgba(255, 255, 255, 0.16); border-radius: 36px;
        box-shadow: 0 30px 60px rgba(0,0,0,0.5); width: 100%; padding: 60px 50px;
    }}
    """

    if s_type == "cover":
        cover_img_html = f'<img src="{cover_url}" class="book-cover">' if cover_url else ''
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .book-cover {{
            width: 390px; height: 550px; object-fit: cover; border-radius: 20px;
            box-shadow: 0 30px 60px rgba(0,0,0,0.8); border: 1px solid rgba(255,255,255,0.25);
            margin-bottom: 40px;
        }}
        .head-title {{ font-size: 52px; font-weight: 800; color: #ffffff; line-height: 1.35; word-break: keep-all; text-shadow: 0 4px 20px rgba(0,0,0,0.6); }}
        .sub-title {{ font-size: 28px; color: #cbd5e1; font-weight: 500; margin-top: 24px; word-break: keep-all; line-height: 1.4; }}
        .book-meta {{ font-size: 24px; color: #94a3b8; font-weight: 600; margin-top: 36px; }}
        </style></head><body>
        <div class="bg-image"></div><div class="overlay"></div>
        <div class="container">
            {cover_img_html}
            <div class="head-title">{head}</div>
            <div class="sub-title">{sub}</div>
            <div class="book-meta">《{book_title}》 {author} 저</div>
        </div></body></html>
        """
    elif s_type == "quote":
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .quote-icon {{ font-size: 140px; color: #38bdf8; opacity: 0.8; font-family: Georgia, serif; line-height: 0.8; margin-bottom: 20px; }}
        .quote-text {{ font-size: 54px; font-weight: 800; color: #ffffff; line-height: 1.4; word-break: keep-all; margin-bottom: 30px; text-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
        .quote-sub {{ font-size: 28px; color: #cbd5e1; font-weight: 500; word-break: keep-all; line-height: 1.5; }}
        </style></head><body>
        <div class="bg-image"></div><div class="overlay"></div>
        <div class="container">
            <div class="glass-card">
                <div class="quote-icon">“</div>
                <div class="quote-text">{head}</div>
                <div class="quote-sub">{body}</div>
            </div>
        </div></body></html>
        """
    elif s_type == "cta":
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .cta-box {{ background: #ffffff; border-radius: 36px; padding: 70px 50px; color: #0f172a; box-shadow: 0 30px 60px rgba(0,0,0,0.4); width: 100%; }}
        .cta-head {{ font-size: 48px; font-weight: 800; color: #0f172a; line-height: 1.35; margin-bottom: 30px; word-break: keep-all; }}
        .cta-sub {{ font-size: 30px; font-weight: 600; color: #334155; line-height: 1.5; word-break: keep-all; margin-bottom: 40px; }}
        .cta-footer {{ font-size: 24px; font-weight: 700; color: #0284c7; background: #e0f2fe; padding: 20px 30px; border-radius: 50px; display: inline-block; }}
        </style></head><body>
        <div class="bg-image"></div><div class="overlay"></div>
        <div class="container">
            <div class="cta-box">
                <div class="cta-head">{head}</div>
                <div class="cta-sub">{sub}</div>
                <div class="cta-footer">전국 온·오프라인 서점에서 만나보실 수 있습니다</div>
            </div>
        </div></body></html>
        """
    else: # detail / background
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .detail-head {{ font-size: 46px; font-weight: 800; color: #ffffff; margin-bottom: 40px; line-height: 1.35; word-break: keep-all; text-shadow: 0 4px 15px rgba(0,0,0,0.5); }}
        .detail-body {{ font-size: 30px; font-weight: 500; color: #f1f5f9; line-height: 1.7; word-break: keep-all; text-align: left; }}
        </style></head><body>
        <div class="bg-image"></div><div class="overlay"></div>
        <div class="container">
            <div class="detail-head">{head}</div>
            <div class="glass-card">
                <div class="detail-body">{body}</div>
            </div>
        </div></body></html>
        """
    return html

async def render_html_to_images(book_title, author, scenario_data, cover_url):
    bg_url = get_unsplash_bg_url("history,book,library")
    slides = scenario_data.get("slides", [])
    img_paths = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1080, "height": 1350})

        for idx, slide in enumerate(slides, start=1):
            html_content = build_html_template(slide, book_title, author, cover_url, bg_url)
            await page.set_content(html_content)
            await page.wait_for_timeout(300)
            
            output_path = f"card_{idx}.png"
            await page.screenshot(path=output_path)
            img_paths.append(output_path)

        await browser.close()
    return img_paths

# ----------------------------------------------------
# 6. 텔레그램 핸들러
# ----------------------------------------------------
async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    event_data = get_pending_event_from_sheet()
    if not event_data:
        await context.bot.send_message(chat_id=chat_id, text="📌 현재 처리할 [Pending] 상태의 도서 정보가 없습니다.")
        return ConversationHandler.END

    user_drafts[chat_id] = event_data
    
    await context.bot.send_message(chat_id=chat_id, text="🧠 전문 기획 마케터 톤으로 가변 카드뉴스 시나리오 및 포스팅 초안을 기획 중입니다...")

    scenario_data = generate_pro_scenario(event_data["book_title"], event_data["author"], event_data["event_info"])
    user_drafts[chat_id]["scenario"] = scenario_data

    slides_info = ""
    for s in scenario_data.get("slides", []):
        content = s.get('head_copy', '')
        slides_info += f"• **Slide {s.get('slide_num')}**: {content}\n"

    scenario_msg = f"""
📱 **도서 맞춤 SNS 홍보 포스팅 시나리오**

[콘텐츠 개요]
• **도서명**: 《{event_data['book_title']}》 ({event_data['author']} 저)
• **기획 의도**: {scenario_data.get('intent')}
• **타깃**: {scenario_data.get('target')}
• **톤앤매너**: {scenario_data.get('tone')}

🖼️ **카드뉴스 시나리오 (총 {len(scenario_data.get('slides', []))}장)**
{slides_info}

----------------------------------------
📝 **인스타그램/페이스북 게시글 본문 텍스트 (Caption)**
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
        data = user_drafts[chat_id]
        scenario = data["scenario"]
        slide_count = len(scenario.get("slides", []))
        
        await query.edit_message_text(text=f"🎨 HTML/CSS 엔진으로 고화질 카드뉴스 이미지 {slide_count}장을 생성 중입니다. 잠시만 기다려 주세요...")
        
        img_paths = await render_html_to_images(data["book_title"], data["author"], scenario, data["cover_url"])
        
        media = [InputMediaPhoto(media=open(p, "rb")) for p in img_paths]
        await context.bot.send_media_group(chat_id=chat_id, media=media)

        keyboard = [
            [
                InlineKeyboardButton("✅ 최종 포스팅 완료 (Done 처리)", callback_data="final_done"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(chat_id=chat_id, text=f"📸 생성된 {slide_count}장 카드뉴스 이미지 팩입니다. 검토 후 완료 버튼을 눌러주세요.", reply_markup=reply_markup)
        return WAITING_FINAL_APPROVAL

    elif query.data == "edit_scenario":
        await query.edit_message_text(text="✏️ **[시나리오 수정]** 보완할 시나리오 방향이나 피드백을 메시지로 입력해 주세요.")
        return WAITING_SCENARIO_FEEDBACK

async def receive_scenario_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    feedback_text = update.message.text

    await update.message.reply_text("🔄 피드백을 반영하여 시나리오를 재기획 중입니다...")

    data = user_drafts[chat_id]
    new_scenario = generate_pro_scenario(data["book_title"], data["author"], data["event_info"], feedback=feedback_text)
    user_drafts[chat_id]["scenario"] = new_scenario

    slides_info = ""
    for s in new_scenario.get("slides", []):
        content = s.get('head_copy', '')
        slides_info += f"• **Slide {s.get('slide_num')}**: {content}\n"

    scenario_msg = f"""
📱 **[수정된 홍보 포스팅 시나리오]**

[콘텐츠 개요]
• **도서명**: 《{data['book_title']}》 ({data['author']} 저)
• **기획 의도**: {new_scenario.get('intent')}
• **타깃**: {new_scenario.get('target')}
• **톤앤매너**: {new_scenario.get('tone')}

🖼️ **카드뉴스 시나리오 (총 {len(new_scenario.get('slides', []))}장)**
{slides_info}

----------------------------------------
📝 **인스타그램/페이스북 게시글 본문 텍스트 (Caption)**
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

        await query.edit_message_text(text="✅ **[최종 처리 완료]** 구글 시트 상태가 'Done'으로 업데이트되었습니다!")
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
