import os
import logging
import json
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, TextSendMessage, 
    ImageSendMessage, QuickReply, QuickReplyButton, MessageAction
)
import openai
from dotenv import load_dotenv
import requests
import uuid
import firebase_admin
from firebase_admin import credentials, firestore

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
line_secret = os.getenv("LINE_CHANNEL_SECRET")
line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
firebase_config = os.getenv("FIREBASE_CONFIG") # JSON string of service account

# Initialize Firebase
if firebase_config:
    try:
        cred_dict = json.loads(firebase_config)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase initialized successfully.")
    except Exception as e:
        logger.error(f"Firebase initialization failed: {e}")
        db = None
else:
    logger.warning("FIREBASE_CONFIG not found. Running without persistence.")
    db = None

app = Flask(__name__)

line_bot_api = LineBotApi(line_token)
handler = WebhookHandler(line_secret)
client = openai.OpenAI(api_key=api_key)

UPLOAD_FOLDER = 'static/uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def get_user_data(user_id):
    default_data = {
        "fashion": "High Fashion",
        "lens": "50mm Standard",
        "lighting": "Natural",
        "action": "Editorial Pose",
        "credits": 3 # Initial free credits
    }
    
    if db:
        doc_ref = db.collection('users').document(user_id)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
        else:
            doc_ref.set(default_data)
            return default_data
    return default_data

def update_user_data(user_id, data):
    if db:
        db.collection('users').document(user_id).set(data, merge=True)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    user_data = get_user_data(user_id)
    
    # Credit check
    if user_data.get("credits", 0) <= 0:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="無料枠の上限に達しました。継続して利用するにはプランのアップグレードが必要です。")
        )
        return

    message_id = event.message.id
    message_content = line_bot_api.get_message_content(message_id)
    input_filename = f"{uuid.uuid4()}.jpg"
    input_path = os.path.join(UPLOAD_FOLDER, input_filename)
    
    with open(input_path, 'wb') as f:
        for chunk in message_content.iter_content():
            f.write(chunk)
    
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=f"GENEPORTが生成を開始しました。\n現在の残りクレジット: {user_data['credits']}",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="ファッション変更", text="ファッション設定")),
                QuickReplyButton(action=MessageAction(label="レンズ変更", text="レンズ設定")),
            ])
        )
    )

    try:
        full_prompt = (
            "Persona: You are a legendary master of portrait photography.\n"
            f"Artistic Mandate:\n"
            f"- Model Casting: Diverse professional model\n"
            f"- Fashion Style: {user_data['fashion']}\n"
            f"- Action/Pose: {user_data['action']}\n"
            f"- Lighting Style: {user_data['lighting']}\n"
            f"- Optical Specs: Shot with a {user_data['lens']} lens.\n"
            "Technical Style: Shot on Kodak Portra 400, Leica glass.\n"
            "Requirements: Match lighting and shadows perfectly."
        )

        with open(input_path, "rb") as img_file:
            response = client.images.edit(
                model="gpt-image-2",
                image=[img_file],
                prompt=full_prompt
            )
            
        generated_url = response.data[0].url
        
        # Deduct credit
        user_data["credits"] -= 1
        update_user_data(user_id, user_data)
        
        line_bot_api.push_message(
            user_id,
            ImageSendMessage(
                original_content_url=generated_url,
                preview_image_url=generated_url
            )
        )
        
    except Exception as e:
        logger.error(f"Generation error: {e}")
        line_bot_api.push_message(user_id, TextSendMessage(text=f"エラー: {str(e)}"))

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    text = event.message.text
    user_data = get_user_data(user_id)
    
    if text == "ファッション設定":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="ファッションスタイルを選択:",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="High Fashion", text="SET_FASHION:High Fashion")),
                    QuickReplyButton(action=MessageAction(label="Streetwear", text="SET_FASHION:Streetwear")),
                    QuickReplyButton(action=MessageAction(label="Suit", text="SET_FASHION:Suit")),
                ])
            )
        )
    elif text == "レンズ設定":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="レンズを選択:",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="35mm Wide", text="SET_LENS:35mm Wide")),
                    QuickReplyButton(action=MessageAction(label="50mm Standard", text="SET_LENS:50mm Standard")),
                    QuickReplyButton(action=MessageAction(label="85mm Portrait", text="SET_LENS:85mm Portrait")),
                ])
            )
        )
    elif text == "ライティング設定":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="ライティングを選択:",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="Natural", text="SET_LIGHT:Natural")),
                    QuickReplyButton(action=MessageAction(label="Studio", text="SET_LIGHT:Studio")),
                    QuickReplyButton(action=MessageAction(label="Cinematic", text="SET_LIGHT:Cinematic")),
                ])
            )
        )
    elif text == "マイステータス":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"【現在のステータス】\n残りクレジット: {user_data['credits']}\nファッション: {user_data['fashion']}\nレンズ: {user_data['lens']}\nライティング: {user_data['lighting']}")
        )
    elif text.startswith("SET_LIGHT:"):
        new_val = text.split(":")[1]
        user_data["lighting"] = new_val
        update_user_data(user_id, user_data)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ライティングを {new_val} に保存しました。"))
    elif text.startswith("SET_FASHION:"):
        new_val = text.split(":")[1]
        user_data["fashion"] = new_val
        update_user_data(user_id, user_data)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ファッションを {new_val} に保存しました。"))
    elif text.startswith("SET_LENS:"):
        new_val = text.split(":")[1]
        user_data["lens"] = new_val
        update_user_data(user_id, user_data)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"レンズを {new_val} に保存しました。"))
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"GENEPORTへようこそ！写真を送ると人物を合成します。\n現在の残りクレジット: {user_data['credits']}")
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
