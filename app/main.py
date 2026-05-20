from flask import Flask, request, jsonify, session
from flask_cors import CORS
from passlib.hash import bcrypt
from openai import OpenAI
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime
import stripe
import secrets
import os
import base64
import traceback
import sys

# ---------------------------------------
# Load environment variables
# ---------------------------------------
load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

# ---------------------------------------
# Helper: Generate API Key
# ---------------------------------------
def generate_api_key():
    return secrets.token_hex(32)

# ---------------------------------------
# Database Setup
# ---------------------------------------
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ---------------------------------------
# User Model
# ---------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    api_key_hash = Column(String)
    plan = Column(String, default="free")
    free_uses = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ---------------------------------------
# Flask App Setup
# ---------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this")

# Required for Render
# app.config["SERVER_NAME"] = "replyhero-backend.onrender.com"

# Session cookie settings
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_DOMAIN="replyhero-backend.onrender.com"
)

app.config["SESSION_TYPE"] = "filesystem"

# ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# CORS
CORS(app,
     supports_credentials=True,
     resources={r"/*": {"origins": [
         "https://cute-melomakarona-3312b6.netlify.app",
         "http://localhost:5500"
     ]}})

# OpenAI client
client = OpenAI(api_key=OPENAI_KEY)

# ---------------------------------------
# Helper: Get Current User
# ---------------------------------------
def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    user = db.get(User, user_id)
    db.close()
    return user

# ---------------------------------------
# Helper: Generate reply from extracted text
# ---------------------------------------
def generate_reply(text):
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Rewrite the extracted text into a clean, professional message."},
            {"role": "user", "content": text}
        ],
        temperature=0.7,
        n=3  # ⭐ generate 3 replies
    )
    return [choice.message.content.strip() for choice in completion.choices]

# ---------------------------------------
# Signup
# ---------------------------------------
@app.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    db = SessionLocal()
    existing = db.query(User).filter_by(email=email).first()
    if existing:
        db.close()
        return jsonify({"error": "Email already registered"}), 400

    user = User(
        email=email,
        password_hash=bcrypt.hash(password),
        plan="free",
        free_uses=0
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()

    session["user_id"] = user.id
    return jsonify({"message": "Signup successful"})

# ---------------------------------------
# Login
# ---------------------------------------
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    email = data.get("email")
    password = data.get("password")

    db = SessionLocal()
    user = db.query(User).filter_by(email=email).first()

    if not user or not bcrypt.verify(password, user.password_hash):
        db.close()
        return jsonify({"error": "Invalid credentials"}), 401

    session["user_id"] = user.id
    db.close()
    return jsonify({"message": "Login successful"})

# ---------------------------------------
# Logout
# ---------------------------------------
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})

# ---------------------------------------
# OCR + Reply
# ---------------------------------------
@app.route("/reply-image", methods=["POST"])
def reply_image():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not logged in"}), 401

    FREE_LIMIT = 15

    if user.plan == "free" and user.free_uses >= FREE_LIMIT:
        return jsonify({
            "error": "limit_reached",
            "message": "You’ve reached your free reply limit."
        }), 402

    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    # ⭐ Receive tone, rewrite mode, and length from frontend
    tone = request.form.get("tone", "Professional")
    rewrite_mode = request.form.get("rewrite", "false") == "true"
    length = request.form.get("length", "Medium")

    # ⭐ Same length instruction logic as /reply
    length_instruction = {
        "Short": "Keep the reply to 1 short sentence.",
        "Medium": "Write a reply that is 2–3 sentences long.",
        "Long": "Write a detailed reply that is 4–6 sentences long."
    }.get(length, "Write a concise reply.")

    # ⭐ Same rewrite vs generate logic as /reply
    if rewrite_mode:
        user_instruction = (
            "Rewrite the user's draft reply using the selected tone. "
            "Keep the meaning the same but improve clarity, tone, and professionalism."
        )
    else:
        user_instruction = (
            "Generate a polished reply to the conversation using the selected tone. "
            "Respond as if you are the user, writing a single reply message."
        )

    # ⭐ Build system prompt (same as /reply)
    system_prompt = f"""
You are ReplyHero, an AI assistant that helps users write professional, clear, and context-aware replies.

Tone to use: {tone}
Length style: {length_instruction}

Instruction:
{user_instruction}

Rules:
- Do not include explanations.
- Do not mention that you are an AI.
- Return only the reply text.
"""

    # ⭐ OCR handling
    image_file = request.files["image"]
    image_bytes = image_file.read()

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    image_data_url = f"data:image/jpeg;base64,{image_b64}"

    try:
        vision_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all readable text from this image."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_data_url,
                                "detail": "auto"
                            }
                        }
                    ]
                }
            ]
        )
        extracted_text = vision_response.choices[0].message.content

    except Exception as e:
        print("OCR ERROR:", e, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": "OCR failed", "details": str(e)}), 500

    # ⭐ Build messages for reply generation
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": extracted_text})

    # ⭐ Generate 3 replies (same as /reply)
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            n=3
        )
        replies = [c.message.content.strip() for c in completion.choices]

    except Exception as e:
        return jsonify({"error": "Reply generation failed", "details": str(e)}), 500

    # ⭐ Update free usage
    db = SessionLocal()
    user = db.get(User, user.id)
    if user.plan == "free":
        user.free_uses += 1
        db.commit()
    db.close()

    return jsonify({"replies": replies})

# ---------------------------------------
# Reply from Text
# ---------------------------------------
@app.route("/reply", methods=["POST"])
def reply():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    FREE_LIMIT = 15

    db = SessionLocal()
    user = db.get(User, user.id)

    if user.plan == "free" and user.free_uses >= FREE_LIMIT:
        db.close()
        return jsonify({
            "error": "limit_reached",
            "message": "You’ve used your 15 free replies. Upgrade to continue."
        }), 402

    data = request.get_json()
    if not data:
        db.close()
        return jsonify({"error": "Invalid JSON"}), 400

    conversation = data.get("conversation", [])
    tone = data.get("tone", "Professional")
    rewrite_mode = data.get("rewrite", False)
    length = data.get("length", "Medium")

    length_instruction = {
        "Short": "Keep the reply to 1 short sentence.",
        "Medium": "Write a reply that is 2–3 sentences long.",
        "Long": "Write a detailed reply that is 4–6 sentences long."
    }.get(length, "Write a concise reply.")

    if rewrite_mode:
        user_instruction = (
            "Rewrite the user's draft reply using the selected tone. "
            "Keep the meaning the same but improve clarity, tone, and professionalism."
        )
    else:
        user_instruction = (
            "Generate a polished reply to the conversation using the selected tone. "
            "Respond as if you are the user, writing a single reply message."
        )

    system_prompt = f"""
You are ReplyHero, an AI assistant that helps users write professional, clear, and context-aware replies.

Tone to use: {tone}
Length style: {length_instruction}

Instruction:
{user_instruction}

Rules:
- Do not include explanations.
- Do not mention that you are an AI.
- Return only the reply text.
"""

    messages = [{"role": "system", "content": system_prompt}]

    for turn in conversation:
        messages.append(turn)

    messages.append({"role": "user", "content": data.get("message", "")})

    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.7,
        n=3  # ⭐ generate 3 replies
    )

    replies = [c.message.content.strip() for c in completion.choices]
    if user.plan == "free":
        user.free_uses += 1
        db.commit()

    db.close()
    return jsonify({"replies": replies})

# ---------------------------------------
# Stripe Webhook Endpoint
# ---------------------------------------
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except Exception as e:
        print("Webhook signature verification failed:", e)
        return jsonify({"error": "Invalid signature"}), 400

    event_type = event["type"]
    data = event["data"]["object"]

    # -------------------------
    # Handle subscription events
    # -------------------------

    if event_type == "checkout.session.completed":
        print("Checkout completed:", data.get("id"))

    elif event_type == "invoice.paid":
        print("Invoice paid:", data.get("id"))

    elif event_type == "invoice.payment_failed":
        print("Payment failed:", data.get("id"))

    elif event_type == "customer.subscription.deleted":
        print("Subscription canceled:", data.get("id"))

    return "", 200

# ---------------------------------------
# Run App
# ---------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)