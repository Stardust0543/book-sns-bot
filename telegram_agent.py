import os
import logging
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
from google import genai

# ==========================================
# [수정 영역] 발급받은 토큰과 키를 입력하세요!
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# ==========================================

client = genai.Client(api_key=GEMINI_API_KEY)
logging.basicConfig(level=logging.INFO)

WAITING_FOR_FEEDBACK = 1
user_drafts = {}

def generate_draft(book_title, event_info, feedback=None):
    prompt = f"""
    너는 도서 전문 마케터야. 아래 정보를 바탕으로 인스타그램 홍보 문구를 작성해줘.
    - 도서명: {book_title}
    - 이벤트 내용: {event_info}
    """
    if feedback:
        prompt += f"\n\n[사용자 추가 수정 요청사항]: {feedback}\n위 수정 요청사항을 적극 반영해서 다시 작성해줘."

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )
    return response.text

async def start_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    user_drafts[chat_id] = {
        "book_title": "우리가 지켜야 할 한국사",
        "event_info": "출간 기념 서평 이벤트 및 할인 행사",
        "current_text": ""
    }
    
    draft_text = generate_draft(
        user_drafts[chat_id]["book_title"], 
        user_drafts[chat_id]["event_info"]
    )
    user_drafts[chat_id]["current_text"] = draft_text

    keyboard = [
        [
            InlineKeyboardButton("👍 승인 및 업로드", callback_data="approve"),
            InlineKeyboardButton("✏️ 수정 요청", callback_data="request_edit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📌 **[도서 포스팅 초안 검토 요청]**\n\n{draft_text}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return ConversationHandler.END

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "approve":
        await query.edit_message_text(
            text=f"{query.message.text}\n\n✅ **[승인 완료]** 포스팅이 최종 승인되었습니다!"
        )
        return ConversationHandler.END

    elif query.data == "request_edit":
        await query.edit_message_text(
            text=f"{query.message.text}\n\n✏️ **[수정 요청]** 수정하고자 하는 내용을 메시지로 입력해 주세요."
        )
        return WAITING_FOR_FEEDBACK

async def receive_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    feedback_text = update.message.text

    await update.message.reply_text("🔄 피드백을 반영하여 초안을 다시 작성하고 있습니다...")

    new_draft = generate_draft(
        user_drafts[chat_id]["book_title"],
        user_drafts[chat_id]["event_info"],
        feedback=feedback_text
    )
    user_drafts[chat_id]["current_text"] = new_draft

    keyboard = [
        [
            InlineKeyboardButton("👍 승인 및 업로드", callback_data="approve"),
            InlineKeyboardButton("✏️ 수정 요청", callback_data="request_edit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text=f"📌 **[수정된 포스팅 초안]**\n\n{new_draft}",
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
    print("텔레그램 에이전트 봇이 성공적으로 실행되었습니다! 텔레그램에서 /start 를 입력해보세요.")
    application.run_polling()

if __name__ == "__main__":
    main()