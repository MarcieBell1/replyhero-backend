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
import re
import subprocess

# Email parsing
import email
import email.policy
from email import policy
from email.parser import BytesParser

# File parsing
import extract_msg
import pdfplumber

# OCR
import pytesseract
from PIL import Image

# HTML parsing
from bs4 import BeautifulSoup


import logging

# Configure logging once (top of file)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

def extract_email_content(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    logging.info(f"[extract_email_content] Starting parse. File: {file_path}, Ext: {ext}")

    # -----------------------------
    # .MSG (Outlook desktop)
    # -----------------------------
    if ext == ".msg":
        logging.info("[extract_email_content] Detected .msg file")
        try:
            msg = extract_msg.Message(file_path)

            msg_subject = msg.subject or ""
            msg_body = ""

            if msg.htmlBody:
                logging.info("[extract_email_content] Using HTML body from .msg")
                msg_body = html_to_text(msg.htmlBody)

            elif msg.body:
                logging.info("[extract_email_content] Using plain text body from .msg")
                msg_body = msg.body

            elif msg.rtfBody:
                logging.info("[extract_email_content] RTF body detected (ignored for Render safety)")
                msg_body = ""

            else:
                logging.warning("[extract_email_content] .msg file has no readable body")
                msg_body = ""

            msg_body = clean_forwarded(msg_body)

            return f"Subject: {msg_subject}\n\n{msg_body}".strip()

        except Exception as e:
            logging.error(f"[extract_email_content] MSG parsing error: {e}")
            return f"[MSG parsing error: {str(e)}]"

    # -----------------------------
    # .EML
    # -----------------------------
    if ext == ".eml":
        logging.info("[extract_email_content] Detected .eml file")
        try:
            with open(file_path, "rb") as f:
                msg = email.message_from_binary_file(f, policy=email.policy.default)

            subject = msg.get("subject", "")
            body = extract_eml_body(msg)
            body = clean_forwarded(body)

            return f"Subject: {subject}\n\n{body}".strip()

        except Exception as e:
            logging.error(f"[extract_email_content] EML parsing error: {e}")
            return f"[EML parsing error: {str(e)}]"

    # -----------------------------
    # .PDF
    # -----------------------------
    if ext == ".pdf":
        logging.info("[extract_email_content] Detected .pdf file")
        try:
            with pdfplumber.open(file_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            return text.strip()
        except Exception as e:
            logging.error(f"[extract_email_content] PDF extraction error: {e}")
            return f"[PDF extraction error: {str(e)}]"

    # -----------------------------
    # .TXT
    # -----------------------------
    if ext == ".txt":
        logging.info("[extract_email_content] Detected .txt file")
        try:
            with open(file_path, "r", errors="ignore") as f:
                return f.read().strip()
        except Exception as e:
            logging.error(f"[extract_email_content] TXT read error: {e}")
            return f"[TXT read error: {str(e)}]"

    # -----------------------------
    # Images (Render-safe)
    # -----------------------------
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
        logging.info("[extract_email_content] Detected image file — OCR disabled")
        return "[Image OCR not supported in file uploads]"

    # -----------------------------
    # Unsupported
    # -----------------------------
    logging.warning(f"[extract_email_content] Unsupported file type: {ext}")
    return "[Unsupported file type]"


# -----------------------------
# Helper: HTML → Text
# -----------------------------
def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n").strip()


# -----------------------------
# Helper: RTF → Text
# -----------------------------
def rtf_to_text(rtf):
    return ""


# -----------------------------
# Helper: Extract EML Body
# -----------------------------
def extract_eml_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                return part.get_content().strip()
            if ctype == "text/html":
                return html_to_text(part.get_content())
    else:
        if msg.get_content_type() == "text/html":
            return html_to_text(msg.get_content())
        return msg.get_content().strip()


# -----------------------------
# Helper: Clean Forwarded Headers
# -----------------------------
def clean_forwarded(text):
    text = re.sub(r"From:.*?\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Sent:.*?\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"To:.*?\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Subject:.*?\n", "", text, flags=re.IGNORECASE)
    return text.strip()

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
    stripe_customer_id = Column(String)
    stripe_subscription_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ---------------------------------------
# Flask App Setup
# ---------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this")

app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_DOMAIN="reply-hero.com"
)

app.config["SESSION_TYPE"] = "filesystem"

# ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# CORS
CORS(
    app,
    supports_credentials=True,
    resources={
        r"/*": {
            "origins": [
                "https://reply-hero.com",
                "https://api.reply-hero.com",
                "http://localhost:5500"
            ]
        }
    }
)

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
# Current User Info
# ---------------------------------------
@app.route("/me", methods=["GET"])
def me():
    user = get_current_user()
    if not user:
        return jsonify({"logged_in": False})

    return jsonify({
        "logged_in": True,
        "email": user.email,
        "plan": user.plan,
        "free_uses": user.free_uses
    })

# ---------------------------------------
# TONE DICTIONARY
# ---------------------------------------
TONES = {

    # ---------------------------------------
    # CORE TONES (Merged + Standard)
    # ---------------------------------------

    "Professional-Executive": {
        "model_should": [
            "Use polished, structured, workplace-appropriate language.",
            "Maintain clarity, confidence, and professionalism.",
            "Avoid slang or emotional phrasing.",
            "Focus on solutions, strategy, and clear outcomes."
        ]
    },

    "Warm-Empathetic": {
        "model_should": [
            "Validate the sender's feelings.",
            "Use gentle, supportive, emotionally warm language.",
            "Show understanding without being overly casual.",
            "Avoid harsh or overly direct phrasing."
        ]
    },

    "Romantic-Flirty": {
        "model_should": [
            "Use playful, affectionate language.",
            "Maintain light, safe romantic energy.",
            "Avoid explicit or suggestive content.",
            "Keep the tone warm, fun, and emotionally open."
        ]
    },

    "Intimate X": {
        "model_should": [
            "Use emotionally deep, mature language.",
            "Express vulnerability and sincerity.",
            "Maintain warmth without being sexual.",
            "Focus on emotional connection, trust, and honesty."
        ]
    },

    "Direct": {
        "model_should": [
            "Get to the point quickly.",
            "Use short, clear sentences.",
            "Avoid unnecessary details or softening language."
        ]
    },

    "Assertive": {
        "model_should": [
            "Set boundaries clearly and confidently.",
            "Use firm but respectful language.",
            "Avoid apologizing unnecessarily.",
            "Maintain a tone of self-assurance."
        ]
    },

    "Casual": {
        "model_should": [
            "Use relaxed, everyday language.",
            "Avoid formality.",
            "Keep the tone light, friendly, and conversational."
        ]
    },

    "Apologetic": {
        "model_should": [
            "Take responsibility where appropriate.",
            "Express regret clearly and sincerely.",
            "Offer a path to repair or improvement."
        ]
    },

    "Enthusiastic": {
        "model_should": [
            "Use energetic, upbeat language.",
            "Maintain a positive, excited tone.",
            "Show genuine interest or excitement."
        ]
    },

    "Supportive": {
        "model_should": [
            "Offer reassurance and encouragement.",
            "Highlight strengths and positive outcomes.",
            "Use caring, uplifting language."
        ]
    },

    "Sarcastic": {
        "model_should": [
            "Use subtle irony or dry humor.",
            "Avoid cruelty or hostility.",
            "Keep the tone clever and light."
        ]
    },

    "Humorous": {
        "model_should": [
            "Use light, safe humor.",
            "Keep the tone fun and playful.",
            "Avoid offensive or edgy jokes."
        ]
    },

    "Motivational": {
        "model_should": [
            "Encourage confidence and action.",
            "Use uplifting, inspiring language.",
            "Focus on growth and positive momentum."
        ]
    },

    "Stern": {
        "model_should": [
            "Use firm, corrective language.",
            "Avoid emotional softness.",
            "Maintain authority and seriousness."
        ]
    },

    # ---------------------------------------
    # POLITICAL TONES
    # ---------------------------------------

    "Politics-Democrat": {
        "model_should": [
            "Use inclusive, socially conscious framing.",
            "Avoid inflammatory or divisive language.",
            "Maintain respectful political tone."
        ]
    },

    "Politics-Republican": {
        "model_should": [
            "Emphasize tradition and personal responsibility.",
            "Avoid inflammatory or divisive language.",
            "Maintain respectful political tone."
        ]
    },

    "Politics-Neutral": {
        "model_should": [
            "Avoid ideological language.",
            "Stick to facts and balanced framing.",
            "Maintain neutrality and clarity."
        ]
    },

    # ---------------------------------------
    # SPECIALTY TONES
    # ---------------------------------------

    "Debate-Oriented": {
        "model_should": [
            "Use structured arguments.",
            "Present point-counterpoint clearly.",
            "Maintain logical clarity and reasoning."
        ]
    },

    "Youthful": {
        "model_should": [
            "Use modern slang sparingly and naturally.",
            "Keep the tone energetic and current.",
            "Avoid forced or cringe phrasing."
        ]
    },

    "Pet Lovers": {
        "model_should": [
            "Use gentle, animal-themed metaphors.",
            "Maintain nurturing, wholesome energy.",
            "Avoid infantilizing the user.",
            "Keep the tone warm and comforting."
        ]
    },

    "Sports Enthusiasts": {
        "model_should": [
            "Use competitive or team-based metaphors.",
            "Keep the tone energetic and motivational.",
            "Maintain a hype, team-spirit feel."
        ]
    },

    "Promotional": {
        "model_should": [
            "Use persuasive, benefit-driven language.",
            "Maintain soft CTA energy.",
            "Avoid sounding spammy or aggressive."
        ]
    },

    "Toxic-Narcissist Defense": {
        "model_should": [
            "Identify manipulation patterns.",
            "Stay emotionally neutral and grounded.",
            "Set boundaries clearly and calmly.",
            "Avoid escalation or emotional entanglement.",
            "Avoid JADE: Justify, Argue, Defend, Explain."
        ]
    },

    "Techie": {
        "model_should": [
            "Use light technical jargon.",
            "Keep explanations logical and structured.",
            "Maintain a knowledgeable, analytical tone."
        ]
    },

    "Naturalist": {
        "model_should": [
            "Use nature-based metaphors.",
            "Maintain a grounded, calm tone.",
            "Avoid overly poetic or abstract language."
        ]
    },

    "Minimalist": {
        "model_should": [
            "Use extremely short replies.",
            "Remove all unnecessary words.",
            "Keep only essential meaning."
        ]
    },

    "Hippie": {
        "model_should": [
            "Use peaceful, spiritual language.",
            "Maintain a calm, flowing tone.",
            "Avoid parody or caricature."
        ]
    },

    "Fact-Driven": {
        "model_should": [
            "Use evidence-based statements.",
            "Avoid emotional or subjective language.",
            "Prioritize clarity and accuracy."
        ]
    },

    # ---------------------------------------
    # VIRAL TONES
    # ---------------------------------------

    "Savage": {
        "model_should": [
            "Use sharp, witty clapback energy.",
            "Stay clever without being abusive.",
            "Maintain confidence and attitude."
        ]
    },

    "Corporate Passive-Aggressive": {
        "model_should": [
            "Use overly polite but subtly pointed language.",
            "Maintain a professional veneer.",
            "Imply frustration without stating it directly."
        ]
    },

    "Therapist-Style": {
        "model_should": [
            "Use calm, reflective, emotionally intelligent language.",
            "Encourage clarity and boundaries.",
            "Avoid judgment or escalation."
        ]
    },

    "Influencer": {
        "model_should": [
            "Use trendy, aesthetic, upbeat language.",
            "Maintain soft-sell, lifestyle energy.",
            "Keep the tone polished and social-media friendly."
        ]
    },

    "Gen-Z Chaotic": {
        "model_should": [
            "Use chaotic, meme-coded humor.",
            "Maintain unhinged but harmless energy.",
            "Avoid offensive or explicit content."
        ]
    },

    "Dry British": {
        "model_should": [
            "Use understated, deadpan humor.",
            "Maintain subtle sarcasm.",
            "Keep the tone clever and reserved."
        ]
    },

    "Southern Sweet": {
        "model_should": [
            "Use warm, polite, Southern-style charm.",
            "Maintain gentle tone with subtle sass.",
            "Avoid stereotypes or caricatures."
        ]
    },

    "Lawyer-Style": {
        "model_should": [
            "Use precise, formal, legalistic language.",
            "Structure arguments clearly.",
            "Maintain a logical, evidence-based tone."
        ]
    },

    "Academic": {
        "model_should": [
            "Use intellectual, research-style language.",
            "Maintain structured, analytical tone.",
            "Avoid emotional or casual phrasing."
        ]
    },

    "Poetic": {
        "model_should": [
            "Use metaphorical, lyrical language.",
            "Maintain emotional depth and imagery.",
            "Avoid being overly abstract or confusing."
        ]
    },

    "Villain Arc": {
        "model_should": [
            "Use cold, calculated, powerful language.",
            "Maintain confidence and emotional detachment.",
            "Avoid cruelty or explicit hostility."
        ]
    },

    "Victorian Formal": {
        "model_should": [
            "Use ornate, old-fashioned formal language.",
            "Maintain politeness and dramatic flair.",
            "Avoid modern slang or casual phrasing."
        ]
    },

    "Weirdcore": {
        "model_should": [
            "Use surreal, dreamlike, uncanny language.",
            "Maintain aesthetic strangeness without confusion.",
            "Avoid disturbing or graphic content."
        ]
    },

    "Gamer": {
        "model_should": [
            "Use gaming metaphors and competitive energy.",
            "Maintain hype and tactical tone.",
            "Avoid toxic gamer stereotypes."
        ]
    },

    "Mom Friend": {
        "model_should": [
            "Use nurturing, practical, caring language.",
            "Offer reassurance and gentle guidance.",
            "Maintain wholesome, supportive tone."
        ]
    }
}

def build_tone_instructions(tone_name):
    tone = TONES.get(tone_name)
    if not tone:
        return ""
    rules = "\n".join(f"- {r}" for r in tone["model_should"])
    return f"\nTone Behavior:\n{rules}\n"

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
        n=3
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
    tone = request.form.get("tone", "Professional-Executive")
    rewrite_mode = request.form.get("rewrite", "false") == "true"
    length = request.form.get("length", "Medium")
    facts = request.form.get("facts", "")

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

    # ⭐ Build tone instructions
    tone_instructions = build_tone_instructions(tone)

    # ⭐ Build system prompt
    system_prompt = f"""
You are ReplyHero, an AI assistant that helps users write clear, polished, and context-aware replies.

Tone selected: {tone}
{tone_instructions}

Length style: {length_instruction}

Instruction:
{user_instruction}

Additional facts or corrections from the user (these override the conversation context and define the user's perspective):
{facts if facts else "None provided"}

Rules:
- Do not include explanations.
- Do not mention that you are an AI.
- If the user provides team allegiance, political stance, or personal identity in the facts, treat that as the user's true perspective and write the reply from that viewpoint.
- Return only the reply text.
- If the input comes from a screenshot or extracted text, assume the user is the person whose messages appear on the RIGHT side of the conversation. Always generate the reply from their perspective.
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
# Unified Reply Endpoint (Text + Image + Modes)
# ---------------------------------------
@app.route("/generate-reply", methods=["POST"])
def generate_reply_unified():
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

    mode = request.form.get("mode")
    include = request.form.get("include", "")

    # -----------------------------
    # MODE: Paste Existing Conversation
    # -----------------------------
    if mode == "paste":
        conversation = request.form.get("conversation", "")
        user_prompt = f"""
The user pasted an existing conversation.

Conversation:
{conversation}

Additional instructions:
{include}
"""

    # -----------------------------
    # MODE: Start New Conversation
    # -----------------------------
    elif mode == "start":
        draft = request.form.get("conversation", "")
        user_prompt = f"""
The user wants to start a new conversation. Rewrite or generate a polished message.

Draft message:
{draft}

Additional instructions:
{include}
"""

    # -----------------------------
    # MODE: Upload Screenshot
    # -----------------------------
    elif mode == "upload":
        if "image" not in request.files:
            return jsonify({"error": "No image uploaded"}), 400

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
                                "image_url": {"url": image_data_url, "detail": "auto"}
                            }
                        ]
                    }
                ]
            )
            extracted_text = vision_response.choices[0].message.content
        except Exception as e:
            return jsonify({"error": "OCR failed", "details": str(e)}), 500

        user_prompt = f"""
The user uploaded a screenshot. Extract meaning and generate a reply.

Extracted text:
{extracted_text}

Additional instructions:
{include}
"""

    # -----------------------------
    # MODE: Upload Email File
    # -----------------------------
    elif mode == "file":
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        uploaded = request.files["file"]

        mime = uploaded.mimetype
        original_ext = os.path.splitext(uploaded.filename)[1].lower()

        # Accept all common .msg MIME types
        msg_mime_types = {
            "application/vnd.ms-outlook",
            "application/octet-stream",
            "application/x-msdownload",
            "application/CDFV2-corrupt",
            "application/x-ole-storage"
        }

        # Case 1: No extension but MIME matches Outlook
        if original_ext == "" and mime in msg_mime_types:
            ext = ".msg"

        # Case 2: No extension but filename hints at Outlook
        elif original_ext == "" and "msg" in uploaded.filename.lower():
            ext = ".msg"

        # Case 3: Normal case
        else:
            ext = original_ext

        file_path = f"/tmp/uploaded_email{ext}"
        uploaded.save(file_path)

        logging.info(f"[upload] Uploaded filename: {uploaded.filename}")
        logging.info(f"[upload] Browser MIME type: {mime}")
        logging.info(f"[upload] Original extension: {original_ext}")
        logging.info(f"[upload] Final extension used: {ext}")
        logging.info(f"[upload] Saved to: {file_path}")

        extracted_text = extract_email_content(file_path)

        user_prompt = f"""
The user uploaded an email file. Extract meaning and generate a reply.

Extracted text:
{extracted_text}

Additional instructions:
{include}
"""

    else:
        return jsonify({"error": "Invalid mode"}), 400

    # -----------------------------
    # Tone, Length, Rewrite, Facts
    # -----------------------------
    tone = request.form.get("tone", "Professional-Executive")
    length = request.form.get("length", "Medium")
    rewrite_mode = request.form.get("rewrite", "false") == "true"
    facts = request.form.get("facts", "")

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

    # Build tone instructions
    tone_instructions = build_tone_instructions(tone)

    # Build system prompt
    system_prompt = f"""
You are ReplyHero, an AI assistant that helps users write clear, polished, and context-aware replies.

Tone selected: {tone}
{tone_instructions}

Length style: {length_instruction}

Instruction:
{user_instruction}

Additional facts or corrections from the user (these override the conversation context and define the user's perspective):
{facts if facts else "None provided"}

Rules:
- Do not include explanations.
- Do not mention that you are an AI.
- If the user provides team allegiance, political stance, or personal identity in the facts, treat that as the user's true perspective and write the reply from that viewpoint.
- Return only the reply text.
- If the input comes from a screenshot or extracted text, assume the user is the person whose messages appear on the RIGHT side of the conversation. Always generate the reply from their perspective.
"""

    # -----------------------------
    # Generate reply (ALL MODES)
    # -----------------------------
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

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
    tone = data.get("tone", "Professional-Executive")
    rewrite_mode = data.get("rewrite", False)
    length = data.get("length", "Medium")
    facts = data.get("facts", "")

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

    # Build tone instructions
    tone_instructions = build_tone_instructions(tone)

    # Build system prompt
    system_prompt = f"""
You are ReplyHero, an AI assistant that helps users write clear, polished, and context-aware replies.

Tone selected: {tone}
{tone_instructions}

Length style: {length_instruction}

Instruction:
{user_instruction}

Additional facts or corrections from the user (these override the conversation context and define the user's perspective):
{facts if facts else "None provided"}

Rules:
- Do not include explanations.
- Do not mention that you are an AI.
- If the user provides team allegiance, political stance, or personal identity in the facts, treat that as the user's true perspective and write the reply from that viewpoint.
- Return only the reply text.
- If the input comes from a screenshot or extracted text, assume the user is the person whose messages appear on the RIGHT side of the conversation. Always generate the reply from their perspective.
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
# Create Stripe Checkout Session
# ---------------------------------------
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not logged in"}), 401

    try:
        # ---------------------------------------
        # Create or retrieve Stripe customer
        # ---------------------------------------
        if not user.stripe_customer_id:
            customer = stripe.Customer.create(email=user.email)

            # Save Stripe customer ID to DB
            db = SessionLocal()
            u = db.get(User, user.id)
            u.stripe_customer_id = customer.id
            db.commit()
            db.close()
        else:
            customer = stripe.Customer.retrieve(user.stripe_customer_id)

        # ---------------------------------------
        # Create checkout session
        # ---------------------------------------
        session_obj = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer=customer.id,   # <-- IMPORTANT
            line_items=[{
                "price": "price_1TYy99PJsm5IOsusyPBPyZaB",  # LIVE PRICE ID
                "quantity": 1
            }],
            success_url="https://reply-hero.com/success.html",
            cancel_url="https://reply-hero.com/cancel.html"
        )

        return jsonify({"url": session_obj.url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

    db = SessionLocal()

    # -------------------------
    # UPGRADE USER TO PRO
    # -------------------------
    if event_type == "checkout.session.completed":
        customer_id = data["customer"]              # ⭐ FIXED
        subscription_id = data["subscription"]      # ⭐ FIXED

        user = db.query(User).filter_by(stripe_customer_id=customer_id).first()
        if user:
            user.plan = "pro"
            user.stripe_subscription_id = subscription_id
            db.commit()
            print("User upgraded to PRO:", user.email)

    elif event_type == "invoice.paid":
        customer_id = data["customer"]              # ⭐ FIXED

        user = db.query(User).filter_by(stripe_customer_id=customer_id).first()
        if user:
            user.plan = "pro"
            db.commit()
            print("Invoice paid — user confirmed PRO:", user.email)

    # -------------------------
    # DOWNGRADE USER TO FREE
    # -------------------------
    elif event_type == "customer.subscription.deleted":
        subscription_id = data["id"]                # ⭐ FIXED

        user = db.query(User).filter_by(stripe_subscription_id=subscription_id).first()
        if user:
            user.plan = "free"
            user.stripe_subscription_id = None
            db.commit()
            print("Subscription canceled — user downgraded:", user.email)

    elif event_type == "invoice.payment_failed":
        customer_id = data["customer"]              # ⭐ FIXED

        user = db.query(User).filter_by(stripe_customer_id=customer_id).first()
        if user:
            user.plan = "free"
            db.commit()
            print("Payment failed — user downgraded:", user.email)

    db.close()
    return "", 200


@app.route("/billing-portal", methods=["POST"])
def billing_portal():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not logged in"}), 401

    try:
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url="https://reply-hero.com"  # your frontend URL
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ---------------------------------------
# Run App
# ---------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)