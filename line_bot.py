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
import base64
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
OUTPUT_FOLDER = 'static/outputs'
for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

def get_user_data(user_id):
    default_data = {
        "fashion": "High Fashion",
        "lens": "50mm Standard",
        "lighting": "Natural",
        "pose": "Editorial Pose",
        "style": "Kodak Portra 400",
        "casting": "Diverse Professional",
        "credits": 100
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

        with Image.open(input_path) as img:
            # Ensure image is RGBA
            img = img.convert("RGBA")
            # Save as temporary PNGs for OpenAI
            img.save(input_path, "PNG")
            
        with open(input_path, "rb") as img_file:
            response = client.images.edit(
                model="gpt-image-2",
                image=img_file,
                mask=img_file,
                prompt=full_prompt,
                n=4,
                size="1024x1024",
                quality="standard"  # Set to standard to reduce cost
            )
            
        logger.debug(f"OpenAI Response received with 4 images.")
        
        # Download and composite 4 images into a 2x2 grid
        output_filename = f"{uuid.uuid4()}.png"
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)
        
        # Create a blank canvas for 2x2 grid (2048x2048)
        grid_img = Image.new('RGB', (2048, 2048))
        
        for i, img_obj in enumerate(response.data):
            # Get image data (could be url or b64_json)
            if img_obj.url:
                img_resp = requests.get(img_obj.url)
                img_data = img_resp.content
            else:
                img_data = base64.b64decode(img_obj.b64_json)
            
            # Load into Pillow
            from io import BytesIO
            temp_img = Image.open(BytesIO(img_data)).resize((1024, 1024))
            
            # Paste into grid
            x = (i % 2) * 1024
            y = (i // 2) * 1024
            grid_img.paste(temp_img, (x, y))
            
        # Save final grid image
        grid_img.save(output_path, "PNG")
        
        # Construct public URL
        public_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "web-production-ab5d3.up.railway.app")
        generated_url = f"https://{public_domain}/static/outputs/{output_filename}"

        logger.info(f"Generated Grid Image URL: {generated_url}")
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
    # Credit check
    if user_data["credits"] <= 0:
        # Auto-unlock for the new 100-limit policy
        user_data["credits"] = 100
        update_user_data(user_id, user_data)
        logger.info(f"Auto-unlocked user {user_id} to 100 credits.")

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
