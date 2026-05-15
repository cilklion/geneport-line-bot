import os
import logging
import json
import threading
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
from PIL import Image

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
line_secret = os.getenv("LINE_CHANNEL_SECRET")
line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
firebase_config = os.getenv("FIREBASE_CONFIG")

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
        "pose": "Editorial Pose",
        "style": "Kodak Portra 400",
        "casting": "Diverse Professional",
        "credits": 3
    }
    
    if db:
        doc_ref = db.collection('users').document(user_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            for key, val in default_data.items():
                if key not in data:
                    data[key] = val
            return data
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

def process_ai_generation(user_id, input_path, user_data):
    """Heavy AI Task running in a background thread."""
    try:
        full_prompt = (
            "Persona: You are a legendary master of portrait photography.\n"
            f"Artistic Mandate:\n"
            f"- Model Casting: {user_data['casting']}\n"
            f"- Fashion Style: {user_data['fashion']}\n"
            f"- Action/Pose: {user_data['pose']}\n"
            f"- Lighting Style: {user_data['lighting']}\n"
            f"- Optical Specs: Shot with a {user_data['lens']} lens.\n"
            f"Technical Style: Shot on {user_data['style']}, Leica glass.\n"
            "Requirements: Match lighting and shadows perfectly."
        )

        with open(input_path, "rb") as img_file:
            response = client.images.edit(
                model="gpt-image-2",
                image=[img_file],
                prompt=full_prompt
            )
            
        logger.debug(f"OpenAI Response: {response}")
        generated_url = response.data[0].url
        
        if not generated_url:
            raise ValueError("OpenAI returned an empty image URL.")

        logger.info(f"Generated Image URL: {generated_url}")
        user_data["credits"] -= 1
        update_user_data(user_id, user_data)
        
        line_bot_api.push_message(
            user_id,
            ImageSendMessage(original_content_url=generated_url, preview_image_url=generated_url)
        )
        
    except Exception as e:
        logger.error(f"Generation error: {e}")
        line_bot_api.push_message(user_id, TextSendMessage(text=f"エラー: {str(e)}"))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    user_data = get_user_data(user_id)
    
    if user_data.get("credits", 0) <= 0:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="無料枠の上限です。アップグレードしてください。"))
        return

    message_id = event.message.id
    message_content = line_bot_api.get_message_content(message_id)
    input_filename = f"{uuid.uuid4()}.jpg"
    input_path = os.path.join(UPLOAD_FOLDER, input_filename)
    
    with open(input_path, 'wb') as f:
        for chunk in message_content.iter_content():
            f.write(chunk)
    
    # Pre-process image for OpenAI: Convert to square PNG
    try:
        with Image.open(input_path) as img:
            # Create a square background (transparent)
            size = max(img.size)
            square_img = Image.new('RGBA', (size, size), (255, 255, 255, 0))
            # Paste original image onto square background
            offset = ((size - img.width) // 2, (size - img.height) // 2)
            square_img.paste(img, offset)
            # Save as PNG
            png_path = input_path.replace(".jpg", ".png")
            square_img.save(png_path, "PNG")
            input_path = png_path
    except Exception as e:
        logger.error(f"Image processing failed: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画像処理に失敗しました。"))
        return

    # 1. Reply immediately to LINE to satisfy the 5-second rule
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=f"GENEPORTが生成を開始しました。\n【現在の設定】\nファッション: {user_data['fashion']}\nレンズ: {user_data['lens']}\n残りクレジット: {user_data['credits']}"
        )
    )

    # 2. Start heavy task in background thread
    thread = threading.Thread(target=process_ai_generation, args=(user_id, input_path, user_data))
    thread.start()

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    text = event.message.text
    user_data = get_user_data(user_id)
    
    # Menus
    if text in ["ファッション", "ファッション設定"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="ファッションスタイルを選択:",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="High Fashion", text="SET_FASHION:High Fashion")),
                QuickReplyButton(action=MessageAction(label="Streetwear", text="SET_FASHION:Streetwear")),
                QuickReplyButton(action=MessageAction(label="Suit", text="SET_FASHION:Suit")),
                QuickReplyButton(action=MessageAction(label="Activewear", text="SET_FASHION:Activewear")),
            ])
        ))
    elif text in ["レンズ", "レンズ設定"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="レンズを選択:",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="35mm Wide", text="SET_LENS:35mm Wide")),
                QuickReplyButton(action=MessageAction(label="50mm Standard", text="SET_LENS:50mm Standard")),
                QuickReplyButton(action=MessageAction(label="85mm Portrait", text="SET_LENS:85mm Portrait")),
            ])
        ))
    elif text in ["ライティング", "ライティング設定"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="ライティングを選択:",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="Natural", text="SET_LIGHT:Natural")),
                QuickReplyButton(action=MessageAction(label="Cinematic", text="SET_LIGHT:Cinematic")),
                QuickReplyButton(action=MessageAction(label="Golden Hour", text="SET_LIGHT:Golden Hour")),
                QuickReplyButton(action=MessageAction(label="Neon Night", text="SET_LIGHT:Neon Night")),
            ])
        ))
    elif text in ["ポーズ", "ポーズ設定"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="ポーズを選択:",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="Editorial", text="SET_POSE:Editorial Pose")),
                QuickReplyButton(action=MessageAction(label="Walking", text="SET_POSE:Walking")),
                QuickReplyButton(action=MessageAction(label="Seated", text="SET_POSE:Seated")),
                QuickReplyButton(action=MessageAction(label="Dynamic", text="SET_POSE:Dynamic")),
            ])
        ))
    elif text in ["スタイル", "質感設定"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="写真の質感を選択:",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="Kodak 400", text="SET_STYLE:Kodak Portra 400")),
                QuickReplyButton(action=MessageAction(label="Fuji Color", text="SET_STYLE:Fuji Superia")),
                QuickReplyButton(action=MessageAction(label="Monochrome", text="SET_STYLE:Black and White")),
                QuickReplyButton(action=MessageAction(label="Polaroid", text="SET_STYLE:Polaroid")),
            ])
        ))
    elif text in ["ステータス", "マイページ"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=(f"【GENEPORT ステータス】\n残りクレジット: {user_data['credits']}\n"
                  f"ファッション: {user_data['fashion']}\nレンズ: {user_data['lens']}\n"
                  f"ライティング: {user_data['lighting']}\nポーズ: {user_data['pose']}\n"
                  f"質感: {user_data['style']}")
        ))
    
    # Setters
    elif text.startswith("SET_"):
        key_map = {
            "FASHION": "fashion", "LENS": "lens", "LIGHT": "lighting",
            "POSE": "pose", "STYLE": "style", "MODEL": "casting"
        }
        prefix, val = text.split(":", 1)
        key = key_map.get(prefix.replace("SET_", ""))
        if key:
            user_data[key] = val
            update_user_data(user_id, user_data)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{key} を {val} に設定しました。"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
