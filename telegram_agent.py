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

# 대화 상태 정의
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
# 2. Unsplash 감성 스톡 이미지 가져오기
# ----------------------------------------------------
def get_free_stock_image(keyword="history,reading,book", width=1080, height=1350):
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
# 4. Gemini AI 가변 슬라이드(1~6장) 시나리오 기획
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
    [슬라이드 구성 규칙]:
    1. 슬라이드 수(slide_count)는 내용에 따라 1장부터 6장 사이로 자유롭게 결정해줘.
    2. type은 'cover', 'quote', 'background', 'detail', 'cta' 중 하나로 지정할 것.

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
    except Exception as e:
        logging.error(f"JSON 파싱 에러: {e}")
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
# 5. 가변 슬라이드(1~6장) 감성 카드뉴스 합성
# ----------------------------------------------------
def create_card_news_from_scenario(book_title, author, scenario_data, cover_url=None, aspect_ratio="4:5"):
    image_paths = []
    slides = scenario_data.get("slides", [])
    
    if aspect_ratio == "1:1":
        canvas_w, canvas_h = 1080, 1080
    elif aspect_ratio == "1.91:1":
        canvas_w, canvas_h = 1080, 566
    else:
        canvas_w, canvas_h = 1080, 1350

    font_huge = get_font(int(canvas_h * 0.042))
    font_title = get_font(int(canvas_h * 0.034))
    font_sub = get_font(int(canvas_h * 0.025))
    font_body = get_font(int(canvas_h * 0.022))

    cover_img = None
    if cover_url and cover_url.startswith("http"):
        try:
            res = requests.get(cover_url, timeout=5)
            cover_img = Image.open(BytesIO(res.content)).convert("RGBA")
        except Exception as e:
            logging.error(f"표지 다운로드 실패: {e}")

    bg_img = get_free_stock_image("history,book,library,monument", canvas_w, canvas_h)

    for idx, slide in enumerate(slides, start=1):
        s_type = slide.get("type", "detail")
        c = bg_img.copy()
        
        # 1) 표지 슬라이드 (cover)
        if s_type == "cover" or idx == 1:
            overlay = Image.new("RGBA", (canvas_w, canvas_h), (10, 15, 30, 180))
            c = Image.alpha_composite(c, overlay)
            d = ImageDraw.Draw(c)

            d.text((canvas_w / 2, int(canvas_h * 0.09)), f"#도서추천", font=font_sub, fill=(56, 189, 248), anchor="mm")

            if cover_img:
                img_temp = cover_img.copy()
                max_h = int(canvas_h * 0.45)
                img_temp.thumbnail((int(canvas_w * 0.55), max_h))
                w_size, h_size = img_temp.size
                cover_x = (canvas_w - w_size) // 2
                cover_y = int(canvas_h * 0.15)
                
                d.rounded_rectangle([cover_x-14, cover_y-14, cover_x+w_size+14, cover_y+h_size+14], radius=18, fill=(255, 255, 255, 30))
                c.paste(img_temp, (cover_x, cover_y), img_temp)
                text_y = cover_y + h_size + int(canvas_h * 0.06)
            else:
                text_y = canvas_h // 2

            head = slide.get("head_copy", "")
            h_lines = textwrap.wrap(head, width=18)
            for i, l in enumerate(h_lines[:2]):
                d.text((canvas_w / 2, text_y + (i * int(canvas_h * 0.05))), l, font=font_huge, fill=(255, 255, 255), anchor="mm")

            sub = slide.get("sub_copy", "")
            s_lines = textwrap.wrap(sub, width=24)
            start_sub_y = text_y + (len(h_lines[:2]) * int(canvas_h * 0.05)) + int(canvas_h * 0.04)
            for i, l in enumerate(s_lines[:2]):
                d.text((canvas_w / 2, start_sub_y + (i * int(canvas_h * 0.035))), l, font=font_sub, fill=(203, 213, 225), anchor="mm")

            d.text((canvas_w / 2, canvas_h - int(canvas_h * 0.06)), f"《{book_title}》 {author} 저", font=font_body, fill=(148, 163, 184), anchor="mm")

        # 2) 명문장 인용구 슬라이드 (quote)
        elif s_type == "quote":
            overlay = Image.new("RGBA", (canvas_w, canvas_h), (15, 23, 42, 230))
            c = Image.alpha_composite(c, overlay)
            d = ImageDraw.Draw(c)

            font_q = get_font(int(canvas_h * 0.12))
            d.text((canvas_w / 2, int(canvas_h * 0.20)), "“", font=font_q, fill=(56, 189, 248, 120), anchor="mm")

            head = slide.get("head_copy", "")
            h_lines = textwrap.wrap(head, width=16)
            start_y = int(canvas_h * 0.35)
            for i, l in enumerate(h_lines):
                d.text((canvas_w / 2, start_y + (i * int(canvas_h * 0.06))), l, font=font_huge, fill=(255, 255, 255), anchor="mm")

            body = slide.get("body", "")
            b_lines = textwrap.wrap(body, width=22)
            b_start_y = start_y + (len(h_lines) * int(canvas_h * 0.06)) + int(canvas_h * 0.08)
            for i, l in enumerate(b_lines[:3]):
                d.text((canvas_w / 2, b_start_y + (i * int(canvas_h * 0.04))), l, font=font_sub, fill=(148, 163, 184), anchor="mm")

        # 3) 마무리 CTA 슬라이드 (cta)
        elif s_type == "cta" or idx == len(slides):
            c = Image.new("RGBA", (canvas_w, canvas_h), (248, 250, 252))
            d = ImageDraw.Draw(c)

            header_h = int(canvas_h * 0.35)
            header_bg = bg_img.crop((0, 0, canvas_w, header_h))
            overlay_h = Image.new("RGBA", (canvas_w, header_h), (15, 23, 42, 140))
            header_bg = Image.alpha_composite(header_bg, overlay_h)
            c.paste(header_bg, (0, 0))

            d.text((canvas_w / 2, int(header_h * 0.35)), "SPECIAL RECOMMENDATION", font=font_sub, fill=(56, 189, 248), anchor="mm")
            
            head = slide.get("head_copy", "")
            h_lines = textwrap.wrap(head, width=16)
            for i, l in enumerate(h_lines[:2]):
                d.text((canvas_w / 2, int(header_h * 0.60) + (i * int(canvas_h * 0.05))), l, font=font_title, fill=(255, 255, 255), anchor="mm")

            box_m = int(canvas_w * 0.08)
            card_y = header_h + int(canvas_h * 0.06)
            card_h = canvas_h - header_h - int(canvas_h * 0.12)
            
            d.rounded_rectangle([box_m, card_y, canvas_w - box_m, card_y + card_h], radius=28, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
            
            sub = slide.get("sub_copy", slide.get("body", ""))
            s_lines = textwrap.wrap(sub, width=20)
            for i, l in enumerate(s_lines[:4]):
                d.text((canvas_w / 2, card_y + int(card_h * 0.25) + (i * int(canvas_h * 0.045))), l, font=font_sub, fill=(30, 41, 59), anchor="mm")

            d.text((canvas_w / 2, card_y + card_h - int(canvas_h * 0.10)), "전국 온·오프라인 서점에서 만나보실 수 있습니다.", font=font_body, fill=(100, 116, 139), anchor="mm")

        # 4) 일반 스토리/상세 슬라이드 (background / detail)
        else:
            overlay = Image.new("RGBA", (canvas_w, canvas_h), (15, 23, 42, 225))
            c = Image.alpha_composite(c, overlay)
            d = ImageDraw.Draw(c)

            d.text((canvas_w / 2, int(canvas_h * 0.12)), f"SLIDE 0{idx}", font=font_sub, fill=(56, 189, 248), anchor="mm")

            head = slide.get("head_copy", "")
            h_lines = textwrap.wrap(head, width=16)
            for i, l in enumerate(h_lines[:2]):
                d.text((canvas_w / 2, int(canvas_h * 0.22) + (i * int(canvas_h * 0.05))), l, font=font_huge, fill=(255, 255, 255), anchor="mm")

            box_m = int(canvas_w * 0.08)
            box_y = int(canvas_h * 0.38)
            box_h = int(canvas_h * 0.48)
            d.rounded_rectangle([box_m, box_y, canvas_w - box_m, box_y + box_h], radius=24, fill=(30, 41, 59, 230), outline=(71, 85, 105), width=2)

            body = slide.get("body", "")
            b_lines = []
            for para in body.split("\n"):
                b_lines.extend(textwrap.wrap(para, width=20))

            for i, l in enumerate(b_lines[:8]):
                d.text((canvas_w / 2, box_y + int(box_h * 0.20) + (i * int(canvas_h * 0.045))), l, font=font_sub, fill=(241, 245, 249), anchor="mm")

        p_path = f"card_{idx}.png"
        c.convert("RGB").save(p_path, "PNG")
        image_paths.append(p_path)

    return image_paths

# ----------------------------------------------------
# 6. 텔레그램 대화 핸들러 (2단계 검토)
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

    # 시나리오 브리핑 전송
    slides_info = ""
    for s in scenario_data.get("slides", []):
        content = s.get('head_copy', '')
        slides_info += f"• **Slide {s.get('slide_num')} ({s.get('type')})**: {content}\n"

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
        
        await query.edit_message_text(text=f"🎨 확정된 시나리오로 고화질 카드뉴스 이미지 {slide_count}장을 생성 중입니다. 잠시만 기다려 주세요...")
        
        img_paths = create_card_news_from_scenario(data["book_title"], data["author"], scenario, data["cover_url"], data.get("aspect_ratio", "4:5"))
        
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
        slides_info += f"• **Slide {s.get('slide_num')} ({s.get('type')})**: {content}\n"

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
