import os
import json
import base64
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
from google.genai import types
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

# ---- Instagram Graph API (무료: 별도 비용 없이 메타 개발자 계정만 있으면 됨) ----
# 인스타그램 비즈니스 계정 + 연결된 페이스북 페이지 + 장기 액세스 토큰 발급 후 아래 두 값만 채우면
# handle_final_approval()에서 자동으로 실제 캐러셀 게시까지 수행합니다.
# 값이 비어 있으면 기존처럼 "수동 게시 후 Done 처리"로 동작합니다 (하위 호환).
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN")
IG_USER_ID = os.environ.get("IG_USER_ID")
GRAPH_API_VERSION = "v21.0"

# ---- Gemini 이미지 생성 (일부 계정만 무료; 이 프로젝트 API 키는 무료 티어 한도가 0으로 확인됨) ----
# Unsplash 랜덤 검색 대신 슬라이드 내용에 맞춘 맞춤 배경을 직접 생성하려는 기능이지만,
# 계정마다 무료 이미지 생성 할당량이 다르고(0인 경우도 있음) 확인 전까지는 매번 429만
# 받고 Unsplash로 폴백하며 시간을 낭비하므로, 기본값은 꺼둠. 본인 Google AI Studio에서
# 무료 이미지 생성 한도가 있는 걸 직접 확인한 뒤에만 AI_BACKGROUND_ENABLED=true로 켤 것.
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
AI_BACKGROUND_ENABLED = os.environ.get("AI_BACKGROUND_ENABLED", "false").lower() == "true"

# ---- 비전(Vision) QA 검수 (무료: 같은 GEMINI_API_KEY) ----
# 렌더링된 스크린샷을 텍스트 생성용과 별개로 멀티모달 모델에게 보여주고
# 텍스트 잘림/겹침 등 육안 문제를 검수. 문제 발견 시 1회 축소 재렌더링.
GEMINI_VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-2.5-flash")
VISION_QA_ENABLED = os.environ.get("VISION_QA_ENABLED", "true").lower() != "false"

client = genai.Client(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)

WAITING_SCENARIO_ACTION = 1
WAITING_SCENARIO_FEEDBACK = 2
WAITING_FINAL_APPROVAL = 3

user_drafts = {}

# 슬라이드 장식용으로 쓸 수 있는 무료 오픈소스 아이콘 세트 (Lucide, MIT 라이선스).
# Gemini는 이 목록 안에서만 icon 값을 고르도록 프롬프트에서 지시받음 —
# 목록 밖 값이 오면 build_html_template()에서 자동으로 무시됨(빈 값 처리).
ICON_LIBRARY = {
    "book-open", "feather", "quote", "landmark", "megaphone", "sparkles",
    "trophy", "compass", "scale", "flame", "shield", "star", "heart",
    "flag", "target", "rocket", "globe", "lightbulb", "map-pin", "clock",
}

# ----------------------------------------------------
# 2. Unsplash 감성 스톡 이미지 URL 가져오기
#    (무료 티어 시간당 50회 제한 보호용 1회 재시도 + 짧은 백오프)
# ----------------------------------------------------
def get_unsplash_bg_url(keyword="history,book,library"):
    if UNSPLASH_ACCESS_KEY:
        for attempt in range(2):
            try:
                # params=로 넘겨서 requests가 공백/특수문자를 자동으로 URL 인코딩하게 함
                # (Gemini가 만드는 image_keyword는 "korean hanbok texture"처럼 공백 포함 가능)
                res = requests.get(
                    "https://api.unsplash.com/photos/random",
                    params={"query": keyword, "orientation": "portrait", "client_id": UNSPLASH_ACCESS_KEY},
                    timeout=5,
                )
                if res.status_code == 200:
                    # 듀오톤 처리 시 원본 해상도가 낮으면 색 번짐이 도드라지므로
                    # regular(1080px) 대신 더 큰 사이즈를 명시적으로 요청
                    raw_url = res.json()["urls"].get("raw")
                    if raw_url:
                        return f"{raw_url}&w=2000&fit=max&q=80"
                    return res.json()["urls"]["regular"]
                if res.status_code == 429:
                    logging.warning("Unsplash 요청 한도 초과 — 기본 배경으로 대체")
                    break
            except Exception as e:
                logging.error(f"Unsplash 이미지 로드 실패(시도 {attempt + 1}/2): {e}")
                import time
                time.sleep(0.5)

    return "https://images.unsplash.com/photo-1457369804613-52c61a468e7d?auto=format&fit=crop&w=1080&q=80"

# ----------------------------------------------------
# 2-1. Gemini 이미지 생성 — 슬라이드 내용에 맞춘 맞춤 배경 아트
#      (Unsplash 랜덤 스톡사진 대신 사용. 실패 시 호출부에서 Unsplash로 폴백)
# ----------------------------------------------------
def generate_ai_background(image_keyword, accent_color):
    if not AI_BACKGROUND_ENABLED or not image_keyword:
        return None
    try:
        prompt = (
            f"A tasteful editorial background image for a Korean book-marketing social "
            f"media card. Subject/scene: {image_keyword}. Cinematic, muted and slightly "
            f"desaturated tones that will work underneath a {accent_color} color-tint "
            f"overlay and white text. No text, no watermark, no logo, no people's faces "
            f"close-up. Leave clear negative space (top or bottom third) for text overlay. "
            f"Professional magazine-quality photography or illustration, portrait orientation."
        )
        response = client.models.generate_content(
            model=GEMINI_IMAGE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="4:5"),  # 1080x1350과 동일 비율
            ),
        )
        for part in response.parts:
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                raw = inline.data
                mime = inline.mime_type or "image/png"
                # SDK 버전에 따라 raw bytes로 오거나 이미 base64 문자열로 올 수 있어 둘 다 처리
                if isinstance(raw, (bytes, bytearray)):
                    b64 = base64.b64encode(raw).decode("ascii")
                else:
                    b64 = raw
                return f"data:{mime};base64,{b64}"
    except Exception as e:
        logging.error(f"Gemini 이미지 생성 실패, Unsplash로 폴백: {e}")
    return None

# ----------------------------------------------------
# 2-2. 비전(Vision) QA 검수 — 렌더링 결과물을 멀티모달로 육안 검수
#      (검수 자체가 실패하면 파이프라인을 막지 않도록 항상 통과 처리)
# ----------------------------------------------------
def vision_qa_check(image_path):
    if not VISION_QA_ENABLED:
        return True, ""
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        response = client.models.generate_content(
            model=GEMINI_VISION_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                "이 이미지는 인스타그램 카드뉴스 슬라이드야. 텍스트가 화면 밖으로 잘렸거나, "
                "다른 요소와 심하게 겹치거나, 가독성이 크게 떨어지는 명백한 문제가 있으면 "
                "'FAIL: <한 줄 이유>' 형식으로만 답해. 그런 문제가 없으면 'OK'라고만 답해. "
                "사소한 미학적 취향 차이는 문제로 보지 말고, 명백한 결함만 지적해.",
            ],
        )
        text = (response.text or "").strip()
        if text.upper().startswith("OK"):
            return True, ""
        logging.warning(f"비전 QA 결함 발견: {text}")
        return False, text
    except Exception as e:
        # 검수 실패는 렌더링 실패가 아니므로 통과 처리 — QA는 있으면 좋은 안전장치일 뿐,
        # 이것 때문에 전체 파이프라인이 멈추면 안 됨
        logging.error(f"비전 QA 검수 자체가 실패함(통과 처리): {e}")
        return True, ""

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
#    (accent_color 필드 추가: 도서 장르에 맞는 포인트 컬러를 함께 생성)
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
    또한 이 책의 장르/분위기에 가장 잘 어울리는 포인트 컬러(HEX 코드)를 하나 골라줘.
    예: 역사/인문 → 톤 다운된 블루/브론즈 계열, 자기계발 → 밝은 하늘색/그린 계열, 에세이 → 웜톤 계열 등
    다크 배경 위에서도 잘 보이는 채도 높은 색을 선택해.

    첫 번째 슬라이드(cover)에는 시선을 끄는 짧은 캠페인/날짜 태그(badge)를 반드시 넣어줘.
    (예: "10월 9일 한글날", "이 주의 신간", "출간 기념 이벤트" 등, 5~12자 이내)

    ⚠️ 매우 중요: 슬라이드마다 배경 사진과 포인트 아이콘이 절대 겹치지 않고 그 슬라이드의
    구체적인 내용에 맞게 완전히 달라져야 해. "표지/인용구/본문/CTA" 같은 슬라이드 타입 단위로
    뭉뚱그리지 말고, 각 슬라이드가 실제로 말하는 내용(등장 인물, 사건, 장소, 감정, 소재)을
    반영해서 슬라이드마다 개별적으로 정해줘:
    - "image_keyword": 그 슬라이드 내용에 맞는 Unsplash 검색어 (영어 2~4단어, 쉼표로 구분,
      예: "korean hanbok texture", "old newspaper archive", "night city lights").
      슬라이드마다 서로 다른 장면/소재를 검색하도록 다양하게 지정할 것.
    - "icon": 아래 목록 중 그 슬라이드 내용과 가장 잘 어울리는 것 하나를 영문 이름 그대로
      정확히 적어줘 (다른 단어로 바꾸지 말 것): book-open, feather, quote, landmark,
      megaphone, sparkles, trophy, compass, scale, flame, shield, star, heart, flag,
      target, rocket, globe, lightbulb, map-pin, clock. 슬라이드마다 다르게 고를 것.

    반드시 아래 JSON 포맷으로만 응답해줘. 다른 설명 없이 순수 JSON 텍스트만 반환해.

    {
      "intent": "기획 의도 (1-2줄)",
      "target": "주요 타깃 독자층",
      "tone": "톤앤매너",
      "accent_color": "#38bdf8",
      "slide_count": 5,
      "slides": [
        {
          "slide_num": 1,
          "type": "cover",
          "badge": "짧은 캠페인/날짜 태그 (예: 10월 9일 한글날, 신간 출간, 이 주의 추천 등 5~12자)",
          "head_copy": "메인 카피 (강렬한 질문/화두)",
          "sub_copy": "서브 카피",
          "image_keyword": "이 슬라이드 내용에 맞는 영어 Unsplash 검색어",
          "icon": "ICON_LIBRARY 목록 중 이 슬라이드 내용에 맞는 이름 1개"
        },
        {
          "slide_num": 2,
          "type": "quote",
          "head_copy": "가슴을 울리는 책 속 한 구절 또는 인용구",
          "body": "부연 설명",
          "image_keyword": "이 인용구의 장면/소재에 맞는 영어 검색어",
          "icon": "ICON_LIBRARY 목록 중 이 인용구에 맞는 이름 1개"
        },
        {
          "slide_num": 3,
          "type": "detail",
          "head_copy": "핵심 배경/스토리",
          "body": "본문 설명 (줄바꿈 포함 가능)",
          "image_keyword": "이 스토리의 구체적 장면/소재에 맞는 영어 검색어",
          "icon": "ICON_LIBRARY 목록 중 이 스토리에 맞는 이름 1개"
        },
        {
          "slide_num": 4,
          "type": "cta",
          "head_copy": "메인 카피 (도서 메시지 & CTA)",
          "sub_copy": "하단 안내 문구",
          "image_keyword": "마무리 분위기에 맞는 영어 검색어",
          "icon": "ICON_LIBRARY 목록 중 마무리 분위기에 맞는 이름 1개"
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
            "accent_color": "#38bdf8",
            "slide_count": 4,
            "slides": [
                {
                    "slide_num": 1,
                    "type": "cover",
                    "badge": "10월 9일 한글날",
                    "head_copy": "“만약 일제강점기에 우리말과 글이 완전히 사라졌다면?”",
                    "sub_copy": "우리가 세종대왕 뒤에 꼭 기억해야 할 또 다른 영웅들의 이야기.",
                    "image_keyword": "korean hanbok traditional texture",
                    "icon": "landmark"
                },
                {
                    "slide_num": 2,
                    "type": "quote",
                    "head_copy": "“말은 민족의 정신이요, 글은 민족의 생명이다”",
                    "body": "수많은 학자들이 희생당하면서도 끝까지 지켜낸 것은 바로 '우리의 정체성'이었습니다.",
                    "image_keyword": "old handwritten letter ink",
                    "icon": "feather"
                },
                {
                    "slide_num": 3,
                    "type": "detail",
                    "head_copy": "오늘 당연하게 쓰는 한글, 당연하게 지켜진 것은 없습니다.",
                    "body": "세종대왕의 애민정신부터 독립운동가들의 피와 땀까지.\n역사는 매일 읽고 쓰는 이 글자 하나하나에 살아 숨 쉬고 있습니다.",
                    "image_keyword": "independence movement archive photo",
                    "icon": "flag"
                },
                {
                    "slide_num": 4,
                    "type": "cta",
                    "head_copy": "더 깊이 알고, 끝까지 기억해야 할 우리 역사 이야기",
                    "sub_copy": "📘 《우리가 지켜야 할 한국사》\n전국 온·오프라인 서점에서 만나보세요.",
                    "image_keyword": "bookstore warm light shelf",
                    "icon": "book-open"
                }
            ],
            "caption": f"🇰🇷 《{book_title}》\n저자: {author}\n\n{event_info}\n\n#한글날 #우리가지켜야할한국사 #한국사 #책스타그램 #허들링북스"
        }
    return data

# ----------------------------------------------------
# 5. HTML/CSS 기반 전문 디자인 템플릿 엔진 (레이아웃 다양화 버전)
#    - 슬라이드 타입별 2~3개 그리드 패턴을 순환 배치
#    - 페이지 인디케이터 / 시리즈 브랜드마크 추가
#    - accent_color 주입으로 도서마다 다른 포인트 컬러
# ----------------------------------------------------
def build_html_template(slide, book_title, author, cover_url, bg_url, accent_color="#38bdf8",
                         slide_num=1, total_slides=1, series_name="HUDDLING BOOKS", shrink=False):
    s_type = slide.get("type", "detail")
    head = slide.get("head_copy", "")
    sub = slide.get("sub_copy", "")
    body = slide.get("body", "").replace("\n", "<br>")
    badge = slide.get("badge", "").strip()
    badge_html = f'<div class="tag-badge">{badge}</div>' if badge else ""

    # 슬라이드 내용에 맞는 무료 오픈소스 아이콘(Lucide, MIT 라이선스)을 우측 상단에
    # 옅게 띄워서 슬라이드마다 시각적 포인트를 만듦. 이모지 대신 SVG를 CSS mask-image로
    # 얹는 방식이라 플랫폼에 상관없이 항상 동일하고 정제된 모양으로 나옴.
    icon_name = slide.get("icon", "").strip().lower()
    if icon_name not in ICON_LIBRARY:
        icon_name = ""
    icon_html = ""
    if icon_name:
        icon_url = f"https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/{icon_name}.svg"
        icon_html = (
            f'<div class="deco-icon" style="'
            f"-webkit-mask-image:url('{icon_url}'); mask-image:url('{icon_url}');"
            f'"></div>'
        )

    # 비전 QA에서 결함(텍스트 잘림/겹침)이 발견된 슬라이드를 1회 재렌더링할 때
    # 모든 요소를 중앙 기준으로 살짝 축소해서 여백을 확보하는 안전장치
    shrink_rule = ".container { transform: scale(0.86); transform-origin: center center; }" if shrink else ""

    css_common = f"""
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Pretendard', sans-serif; }}
    :root {{ --accent: {accent_color}; }}
    body {{ width: 1080px; height: 1350px; overflow: hidden; background: #0b1220; position: relative; }}

    /* ---- 배경 이미지: 듀오톤 처리 ----
       사진을 흑백+콘트라스트로 눌러둔 뒤(.bg-image), 위에 accent 컬러 그라데이션을
       mix-blend-mode: color 로 얹어서(.duotone-layer) 어떤 스톡사진이 오더라도
       항상 브랜드 컬러 톤의 "디자인된" 이미지처럼 보이게 만듦. 예전처럼 사진을
       blur+brightness로 짓눌러 안 보이게 하는 대신, 사진의 명암/질감은 그대로 살리고
       색감만 넘겨받는 방식이라 배경이 훨씬 입체적이고 풍성해짐. */
    .bg-image {{
        position: absolute; width: 100%; height: 100%;
        background-image: url('{bg_url}');
        background-size: cover; background-position: center;
        filter: grayscale(1) contrast(1.2) brightness(0.7);
        transform: scale(1.08);
    }}
    .duotone-layer {{
        position: absolute; width: 100%; height: 100%;
        background: linear-gradient(150deg, var(--accent) 0%, #0b1220 78%);
        mix-blend-mode: color;
    }}
    .shade-layer {{
        /* 텍스트 가독성을 위한 최소한의 음영 — 예전처럼 화면 전체를 덮어서
           사진을 지워버리지 않고, 아래쪽으로만 자연스럽게 짙어짐 */
        position: absolute; width: 100%; height: 100%;
        background: linear-gradient(180deg, rgba(11,18,32,0.12) 0%, rgba(11,18,32,0.55) 60%, rgba(11,18,32,0.88) 100%);
    }}

    /* ---- 장식 요소: 빈 공간에 색과 입체감을 채워 "허공" 느낌 제거 ---- */
    .top-bar {{
        position: absolute; top: 0; left: 0; width: 100%; height: 14px;
        background: var(--accent); z-index: 25;
    }}
    .deco-orb {{
        position: absolute; width: 620px; height: 620px; border-radius: 50%;
        background: var(--accent); opacity: 0.32; filter: blur(150px); z-index: 1;
    }}
    .deco-orb.pos-a {{ top: -180px; right: -200px; }}
    .deco-orb.pos-b {{ bottom: -200px; left: -180px; }}

    .brand-mark {{
        position: absolute; top: 48px; left: 60px; z-index: 20;
        font-size: 20px; font-weight: 700; letter-spacing: 0.08em;
        color: rgba(255,255,255,0.6); text-transform: uppercase;
    }}
    .page-indicator {{
        position: absolute; bottom: 56px; right: 60px; z-index: 20;
        font-size: 22px; font-weight: 700; color: rgba(255,255,255,0.6);
        display: flex; align-items: baseline; gap: 4px;
    }}
    .page-indicator .current {{ color: var(--accent); font-size: 30px; }}
    .glass-card {{
        background: rgba(255, 255, 255, 0.08); backdrop-filter: blur(24px);
        border: 1px solid rgba(255, 255, 255, 0.18); border-radius: 36px;
        box-shadow: 0 30px 70px rgba(0,0,0,0.55);
    }}
    .tag-badge {{
        display: inline-block; background: var(--accent); color: #0b1220;
        font-weight: 800; padding: 10px 24px; border-radius: 999px;
        font-size: 21px; letter-spacing: -0.01em; margin-bottom: 24px;
    }}
    .deco-icon {{
        position: absolute; top: 60px; right: 66px; z-index: 2;
        width: 130px; height: 130px;
        -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
        -webkit-mask-size: contain; mask-size: contain;
        -webkit-mask-position: center; mask-position: center;
        background-color: rgba(255,255,255,0.32);
        filter: drop-shadow(0 8px 20px rgba(0,0,0,0.35));
    }}
    {shrink_rule}
    """

    orb_pos = "pos-a" if slide_num % 2 == 0 else "pos-b"
    deco_html = (
        '<div class="bg-image"></div>'
        '<div class="duotone-layer"></div>'
        '<div class="shade-layer"></div>'
        '<div class="top-bar"></div>'
        f'<div class="deco-orb {orb_pos}"></div>'
        f'{icon_html}'
    )

    brand_html = f'<div class="brand-mark">{series_name}</div>'
    page_html = (
        f'<div class="page-indicator"><span class="current">{slide_num}</span>'
        f'<span>/ {total_slides}</span></div>'
    )

    # ---------------------------------------------------------
    # COVER: 2가지 패턴 순환 (중앙 집중형 / 좌우 분할 에디토리얼형)
    # ---------------------------------------------------------
    if s_type == "cover":
        variant = slide_num % 2
        cover_img_html = f'<img src="{cover_url}" class="book-cover">' if cover_url else ''

        if variant == 0:
            html = f"""
            <!DOCTYPE html><html><head><style>{css_common}
            .container {{
                position: relative; z-index: 10; width: 100%; height: 100%;
                padding: 90px 75px; display: flex; flex-direction: column;
                justify-content: center; align-items: center; color: #fff; text-align: center;
            }}
            .book-cover {{
                width: 380px; height: 540px; object-fit: cover; border-radius: 16px;
                box-shadow: 0 30px 60px rgba(0,0,0,0.8); border: 1px solid rgba(255,255,255,0.25);
                margin-bottom: 36px;
            }}
            .accent-line {{ width: 56px; height: 5px; background: var(--accent); border-radius: 3px; margin-bottom: 28px; }}
            .head-title {{ font-size: 52px; font-weight: 800; color: #ffffff; line-height: 1.35; letter-spacing: -0.02em; word-break: keep-all; text-shadow: 0 4px 20px rgba(0,0,0,0.6); }}
            .sub-title {{ font-size: 27px; color: #cbd5e1; font-weight: 500; margin-top: 22px; word-break: keep-all; line-height: 1.4; }}
            .book-meta {{ font-size: 22px; color: var(--accent); font-weight: 700; margin-top: 32px; letter-spacing: 0.02em; }}
            </style></head><body>
            {deco_html}
            {brand_html}{page_html}
            <div class="container">
                {cover_img_html}
                {badge_html}
                <div class="accent-line"></div>
                <div class="head-title">{head}</div>
                <div class="sub-title">{sub}</div>
                <div class="book-meta">《{book_title}》 {author} 저</div>
            </div></body></html>
            """
        else:
            html = f"""
            <!DOCTYPE html><html><head><style>{css_common}
            .container {{
                position: relative; z-index: 10; width: 100%; height: 100%;
                display: flex; align-items: center; padding: 0 70px; color: #fff;
            }}
            .left-col {{ flex: 1.15; text-align: left; padding-right: 36px; }}
            .right-col {{ flex: 0.85; display: flex; justify-content: center; }}
            .book-cover {{
                width: 380px; height: 536px; object-fit: cover; border-radius: 14px;
                box-shadow: 0 40px 90px rgba(0,0,0,0.85); border: 1px solid rgba(255,255,255,0.25);
                transform: rotate(3deg);
            }}
            .head-title {{ font-size: 58px; font-weight: 800; color: #fff; line-height: 1.25; letter-spacing: -0.03em; word-break: keep-all; text-shadow: 0 6px 24px rgba(0,0,0,0.5); }}
            .sub-title {{ font-size: 25px; color: #dbe4f0; font-weight: 500; margin-top: 22px; line-height: 1.5; word-break: keep-all; }}
            .book-meta {{ font-size: 21px; color: var(--accent); font-weight: 700; margin-top: 32px; }}
            </style></head><body>
            {deco_html}
            {brand_html}{page_html}
            <div class="container">
                <div class="left-col">
                    {badge_html}
                    <div class="head-title">{head}</div>
                    <div class="sub-title">{sub}</div>
                    <div class="book-meta">《{book_title}》 {author} 저</div>
                </div>
                <div class="right-col">{cover_img_html}</div>
            </div></body></html>
            """

    # ---------------------------------------------------------
    # QUOTE: 2가지 패턴 (글래스카드형 / 배경 위 대형 타이포 노카드형)
    # ---------------------------------------------------------
    elif s_type == "quote":
        variant = slide_num % 2
        if variant == 0:
            html = f"""
            <!DOCTYPE html><html><head><style>{css_common}
            .container {{
                position: relative; z-index: 10; width: 100%; height: 100%;
                padding: 90px 75px; display: flex; flex-direction: column;
                justify-content: center; align-items: center; color: #fff; text-align: center;
            }}
            .glass-card {{ padding: 64px 52px; width: 100%; }}
            .quote-icon {{ font-size: 130px; color: var(--accent); opacity: 0.85; font-family: Georgia, serif; line-height: 0.7; margin-bottom: 16px; }}
            .quote-text {{ font-size: 52px; font-weight: 800; color: #ffffff; line-height: 1.4; letter-spacing: -0.01em; word-break: keep-all; margin-bottom: 26px; }}
            .quote-sub {{ font-size: 27px; color: #cbd5e1; font-weight: 500; word-break: keep-all; line-height: 1.5; }}
            </style></head><body>
            {deco_html}
            {brand_html}{page_html}
            <div class="container">
                <div class="glass-card">
                    <div class="quote-icon">&ldquo;</div>
                    <div class="quote-text">{head}</div>
                    <div class="quote-sub">{body}</div>
                </div>
            </div></body></html>
            """
        else:
            html = f"""
            <!DOCTYPE html><html><head><style>{css_common}
            .container {{
                position: relative; z-index: 10; width: 100%; height: 100%;
                padding: 140px 80px; display: flex; flex-direction: column;
                justify-content: center; color: #fff; text-align: left;
            }}
            .quote-mark {{ font-size: 100px; color: var(--accent); font-family: Georgia, serif; line-height: 0.6; margin-bottom: 8px; }}
            .quote-text {{ font-size: 58px; font-weight: 800; color: #ffffff; line-height: 1.35; letter-spacing: -0.02em; word-break: keep-all; text-shadow: 0 6px 24px rgba(0,0,0,0.6); }}
            .divider {{ width: 100%; height: 1px; background: rgba(255,255,255,0.2); margin: 36px 0; }}
            .quote-sub {{ font-size: 26px; color: #cbd5e1; font-weight: 500; word-break: keep-all; line-height: 1.6; }}
            </style></head><body>
            {deco_html}
            {brand_html}{page_html}
            <div class="container">
                <div class="quote-mark">&ldquo;</div>
                <div class="quote-text">{head}</div>
                <div class="divider"></div>
                <div class="quote-sub">{body}</div>
            </div></body></html>
            """

    # ---------------------------------------------------------
    # CTA: 화이트 카드 고정, accent 컬러만 주입
    # ---------------------------------------------------------
    elif s_type == "cta":
        html = f"""
        <!DOCTYPE html><html><head><style>{css_common}
        .container {{
            position: relative; z-index: 10; width: 100%; height: 100%;
            padding: 90px 75px; display: flex; flex-direction: column;
            justify-content: center; align-items: center; color: #fff; text-align: center;
        }}
        .cta-box {{ background: #ffffff; border-radius: 32px; padding: 68px 50px; color: #0f172a; box-shadow: 0 30px 70px rgba(0,0,0,0.45); width: 100%; }}
        .cta-accent {{ width: 56px; height: 5px; background: var(--accent); border-radius: 3px; margin: 0 auto 28px; }}
        .cta-head {{ font-size: 46px; font-weight: 800; color: #0f172a; line-height: 1.35; letter-spacing: -0.02em; margin-bottom: 26px; word-break: keep-all; }}
        .cta-sub {{ font-size: 28px; font-weight: 600; color: #334155; line-height: 1.55; word-break: keep-all; margin-bottom: 36px; }}
        .cta-footer {{ font-size: 22px; font-weight: 700; color: var(--accent); background: rgba(56,189,248,0.12); padding: 18px 28px; border-radius: 50px; display: inline-block; }}
        </style></head><body>
        {deco_html}
        {brand_html}{page_html}
        <div class="container">
            <div class="cta-box">
                <div class="cta-accent"></div>
                <div class="cta-head">{head}</div>
                <div class="cta-sub">{sub}</div>
                <div class="cta-footer">전국 온·오프라인 서점에서 만나보실 수 있습니다</div>
            </div>
        </div></body></html>
        """

    # ---------------------------------------------------------
    # DETAIL / 기타: 3가지 패턴 순환 (좌측정렬 카드형 / 넘버링 강조형 / 풀블리드 여백형)
    # ---------------------------------------------------------
    else:
        variant = slide_num % 3
        if variant == 0:
            html = f"""
            <!DOCTYPE html><html><head><style>{css_common}
            .container {{
                position: relative; z-index: 10; width: 100%; height: 100%;
                padding: 100px 75px; display: flex; flex-direction: column;
                justify-content: center; color: #fff;
            }}
            .detail-head {{ font-size: 44px; font-weight: 800; color: #ffffff; margin-bottom: 36px; line-height: 1.35; letter-spacing: -0.02em; word-break: keep-all; text-align: left; }}
            .glass-card {{ padding: 52px 44px; }}
            .detail-body {{ font-size: 29px; font-weight: 500; color: #f1f5f9; line-height: 1.75; word-break: keep-all; text-align: left; }}
            </style></head><body>
            {deco_html}
            {brand_html}{page_html}
            <div class="container">
                <div class="detail-head">{head}</div>
                <div class="glass-card"><div class="detail-body">{body}</div></div>
            </div></body></html>
            """
        elif variant == 1:
            html = f"""
            <!DOCTYPE html><html><head><style>{css_common}
            .container {{
                position: relative; z-index: 10; width: 100%; height: 100%;
                display: flex; padding: 100px 70px; color: #fff; align-items: center;
            }}
            .num-col {{ flex: 0 0 200px; }}
            .big-num {{ font-size: 220px; font-weight: 800; color: var(--accent); opacity: 0.35; line-height: 1; font-family: Georgia, serif; }}
            .text-col {{ flex: 1; text-align: left; }}
            .detail-head {{ font-size: 42px; font-weight: 800; color: #ffffff; margin-bottom: 30px; line-height: 1.35; letter-spacing: -0.02em; word-break: keep-all; }}
            .detail-body {{ font-size: 28px; font-weight: 500; color: #f1f5f9; line-height: 1.75; word-break: keep-all; }}
            </style></head><body>
            {deco_html}
            {brand_html}{page_html}
            <div class="container">
                <div class="num-col"><div class="big-num">{slide_num:02d}</div></div>
                <div class="text-col">
                    <div class="detail-head">{head}</div>
                    <div class="detail-body">{body}</div>
                </div>
            </div></body></html>
            """
        else:
            html = f"""
            <!DOCTYPE html><html><head><style>{css_common}
            .container {{
                position: relative; z-index: 10; width: 100%; height: 100%;
                padding: 120px 90px; display: flex; flex-direction: column;
                justify-content: flex-end; color: #fff;
            }}
            .accent-dot {{ width: 14px; height: 14px; border-radius: 50%; background: var(--accent); margin-bottom: 24px; }}
            .detail-head {{ font-size: 46px; font-weight: 800; color: #ffffff; margin-bottom: 28px; line-height: 1.4; letter-spacing: -0.02em; word-break: keep-all; }}
            .detail-body {{ font-size: 27px; font-weight: 500; color: #cbd5e1; line-height: 1.8; word-break: keep-all; }}
            </style></head><body>
            {deco_html}
            {brand_html}{page_html}
            <div class="container">
                <div class="accent-dot"></div>
                <div class="detail-head">{head}</div>
                <div class="detail-body">{body}</div>
            </div></body></html>
            """

    return html

# ----------------------------------------------------
# 6. 메모리 최적화 + 타임아웃 안전장치 + 해상도/배경 다양화 반영 렌더링 함수
# ----------------------------------------------------
async def render_html_to_images(book_title, author, scenario_data, cover_url):
    slides = scenario_data.get("slides", [])
    total = len(slides)
    accent_color = scenario_data.get("accent_color", "#38bdf8")

    # 슬라이드 "타입"이 아니라 슬라이드 "개별 내용"에 맞춰 배경을 가져옴.
    # Gemini가 각 슬라이드마다 만들어준 image_keyword(그 슬라이드의 실제 내용에 맞는
    # 검색어)를 그대로 사용 — 같은 타입(quote, detail 등)이어도 슬라이드마다 완전히
    # 다른 사진이 나오게 됨. image_keyword가 비어 있는 예외 상황에서만 타입별
    # 기본값으로 폴백.
    type_fallback_keywords = {
        "cover": f"{book_title},book,atmosphere",
        "quote": "paper,texture,minimal,light",
        "detail": "library,archive,vintage",
        "cta": "bookstore,shelf,warm light",
    }
    bg_cache = {}
    def get_bg_for_slide(slide):
        keyword = (slide.get("image_keyword") or "").strip()
        if not keyword:
            keyword = type_fallback_keywords.get(slide.get("type", "detail"), "books,library")
        if keyword in bg_cache:
            return bg_cache[keyword]

        # 1순위: Gemini가 이 키워드에 맞춰 직접 그린 맞춤 배경 (무료 티어 하루 약 500장)
        ai_bg = generate_ai_background(keyword, accent_color)
        bg_cache[keyword] = ai_bg if ai_bg else get_unsplash_bg_url(keyword)
        return bg_cache[keyword]

    img_paths = []

    async with async_playwright() as p:
        # Render 무료 플랜 메모리(512MB) 초과 방지 옵션 적용
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process"
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1080, "height": 1350},
            device_scale_factor=2,  # 해상도 2배 → 텍스트/엣지 선명도 대폭 개선
        )
        page = await context.new_page()

        async def set_content_and_wait(html_content):
            # ⚠️ set_content()의 기본 wait_until="load"는 배경 이미지(Unsplash/AI),
            # Lucide 아이콘(jsdelivr CDN), Pretendard 웹폰트까지 "모든" 외부 리소스가
            # 전부 다운로드될 때까지 블로킹한다. Render 무료 플랜은 CPU/대역폭이 매우
            # 제한적이라 이 전체 로딩이 10초를 넘기기 쉬움(특히 아이콘 CDN 요청이
            # 슬라이드마다 추가된 뒤로는 더욱 그렇다) → "Timeout 10000ms exceeded" 발생.
            # domcontentloaded로 빠르게 넘어간 뒤, 이미지 로딩은 별도의 여유 있는
            # 타임아웃으로 기다리되 실패해도 예외를 던지지 않고 있는 그대로 캡처한다.
            await page.set_content(html_content, timeout=20000, wait_until="domcontentloaded")
            try:
                await page.wait_for_function(
                    """() => Array.from(document.images).every(img => img.complete)""",
                    timeout=12000,
                )
            except Exception:
                pass  # 이미지가 느려도 캡처는 진행 (배경 없는 것보다 지연이 더 나쁨)
            try:
                await page.evaluate("document.fonts.ready")
                await page.wait_for_function("document.fonts.status === 'loaded'", timeout=3000)
            except Exception:
                await page.wait_for_timeout(300)  # 폰트 로딩 확인 실패 시 최소 대기로 폴백

        for idx, slide in enumerate(slides, start=1):
            try:
                bg_url = get_bg_for_slide(slide)
                html_content = build_html_template(
                    slide, book_title, author, cover_url, bg_url,
                    accent_color=accent_color,
                    slide_num=idx,
                    total_slides=total,
                )
                await set_content_and_wait(html_content)

                output_path = f"card_{idx}.png"
                await page.screenshot(path=output_path, timeout=15000)

                # 비전 QA: 실제로 렌더링된 결과물을 멀티모달로 육안 검수.
                # 명백한 결함(텍스트 잘림/겹침)이 발견되면 전체 요소를 살짝 축소해서
                # 딱 1번만 재렌더링 (무한 루프 방지, 검수 자체 실패는 통과 처리)
                ok, issue = vision_qa_check(output_path)
                if not ok:
                    logging.warning(f"Slide {idx} 비전 QA 결함으로 축소 재렌더링: {issue}")
                    retry_html = build_html_template(
                        slide, book_title, author, cover_url, bg_url,
                        accent_color=accent_color,
                        slide_num=idx,
                        total_slides=total,
                        shrink=True,
                    )
                    await set_content_and_wait(retry_html)
                    await page.screenshot(path=output_path, timeout=15000)

                img_paths.append(output_path)
            except Exception as e:
                logging.error(f"Slide {idx} 렌더링 에러/타임아웃 발생: {e}")

        await context.close()
        await browser.close()

    return img_paths

# ----------------------------------------------------
# 7. 텔레그램 핸들러
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

        if img_paths:
            media = [InputMediaPhoto(media=open(p, "rb")) for p in img_paths]
            sent_messages = await context.bot.send_media_group(chat_id=chat_id, media=media)

            # 텔레그램에 올라간 이미지의 file_id를 저장해둠 — Instagram 게시 시
            # 별도 스토리지(S3 등, 유료) 없이 텔레그램 파일의 공개 URL을 그대로 재사용하기 위함
            user_drafts[chat_id]["telegram_file_ids"] = [
                msg.photo[-1].file_id for msg in sent_messages if msg.photo
            ]
            user_drafts[chat_id]["caption"] = scenario.get("caption", "")

            keyboard = [
                [
                    InlineKeyboardButton("✅ 최종 포스팅 완료 (Done 처리)", callback_data="final_done"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=chat_id, text=f"📸 생성된 {len(img_paths)}장 카드뉴스 이미지 팩입니다. 검토 후 완료 버튼을 눌러주세요.", reply_markup=reply_markup)
            return WAITING_FINAL_APPROVAL
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ 이미지 렌더링 중 오류가 발생했습니다. 다시 시도해 주세요.")
            return WAITING_SCENARIO_ACTION

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

# ----------------------------------------------------
# 8. Instagram Graph API 실제 게시 (무료)
#    - 별도 스토리지(S3 등) 없이 텔레그램에 이미 올라간 파일의 공개 URL을
#      그대로 Instagram에 넘겨서 이미지 호스팅 비용을 0으로 만듦
#    - IG_ACCESS_TOKEN / IG_USER_ID 둘 다 없으면 이 함수는 호출되지 않고
#      기존처럼 "수동 게시 후 Done" 흐름으로 자동 폴백
# ----------------------------------------------------
async def get_telegram_file_public_urls(bot, file_ids):
    urls = []
    for file_id in file_ids:
        tg_file = await bot.get_file(file_id)
        # python-telegram-bot이 만들어주는 file_path는 이미 완전한 공개 다운로드 URL
        urls.append(tg_file.file_path)
    return urls


def post_carousel_to_instagram(image_urls, caption):
    """
    Instagram Graph API로 캐러셀 게시.
    1) 각 이미지를 is_carousel_item=true로 미디어 컨테이너 생성
    2) 모든 컨테이너 id를 모아 media_type=CAROUSEL 컨테이너 생성
    3) 발행
    성공 시 (True, ig_media_id) / 실패 시 (False, 에러메시지) 반환.
    """
    graph_base = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
    try:
        children = []
        for img_url in image_urls:
            resp = requests.post(
                f"{graph_base}/{IG_USER_ID}/media",
                data={
                    "image_url": img_url,
                    "is_carousel_item": "true",
                    "access_token": IG_ACCESS_TOKEN,
                },
                timeout=20,
            )
            resp.raise_for_status()
            children.append(resp.json()["id"])

        carousel_resp = requests.post(
            f"{graph_base}/{IG_USER_ID}/media",
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
                "access_token": IG_ACCESS_TOKEN,
            },
            timeout=20,
        )
        carousel_resp.raise_for_status()
        creation_id = carousel_resp.json()["id"]

        publish_resp = requests.post(
            f"{graph_base}/{IG_USER_ID}/media_publish",
            data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
            timeout=20,
        )
        publish_resp.raise_for_status()
        return True, publish_resp.json().get("id")
    except Exception as e:
        logging.error(f"Instagram 게시 실패: {e}")
        return False, str(e)


async def handle_final_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "final_done":
        data = user_drafts.get(chat_id, {})

        # IG 자격 증명이 설정돼 있으면 실제로 인스타그램에 캐러셀을 게시
        if IG_ACCESS_TOKEN and IG_USER_ID and data.get("telegram_file_ids"):
            await query.edit_message_text(text="📤 Instagram에 캐러셀 게시 중입니다...")
            try:
                image_urls = await get_telegram_file_public_urls(context.bot, data["telegram_file_ids"])
                success, result = post_carousel_to_instagram(image_urls, data.get("caption", ""))
            except Exception as e:
                success, result = False, str(e)

            if not success:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Instagram 게시에 실패했습니다: {result}\n수동으로 게시 후 다시 완료 버튼을 눌러주세요.",
                )
                return WAITING_FINAL_APPROVAL

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
