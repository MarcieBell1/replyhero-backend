from flask import Flask, request, jsonify, session
from flask_cors import CORS
from passlib.hash import bcrypt
from openai import OpenAI
from dotenv import load_dotenv
import os
import secrets
from datetime import datetime
import stripe

# ---------------------------------------
# Load environment variables
# ---------------------------------------
load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# ---------------------------------------
# Helper: Generate API Key
# ---------------------------------------
def generate_api_key():
    return secrets.token_hex(32)

# ---------------------------------------
# Database Setup (PostgreSQL + SQLAlchemy)
# ---------------------------------------
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from passlib.hash import bcrypt

DATABASE_URL = os.getenv("DATABASE_URL")

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

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---------------------------------------
# SESSION COOKIE SETTINGS (REQUIRED FOR NETLIFY + RENDER)
# ---------------------------------------
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_DOMAIN=".onrender.com"
)

# ---------------------------------------
# CORS SETTINGS (MUST NOT USE "*")
# ---------------------------------------
CORS(app,
     supports_credentials=True,
     resources={r"/*": {"origins": [
         "https://cute-melomakarona-3312b6.netlify.app",
         "http://localhost:5500"
     ]}},
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "OPTIONS"])
# ---------------------------------------
# OpenAI client
# ---------------------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------------------
# Helper: Get Current User (Session)
# ---------------------------------------
def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    return db.query(User).get(user_id)

# ---------------------------------------
# Signup Route
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
        return jsonify({"error": "Email already registered"}), 400

    user = User(
        email=email,
        password_hash=bcrypt.hash(password),
        free_uses=0,
        plan="free"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    session["user_id"] = user.id
    return jsonify({"message": "Signup successful"})

# ---------------------------------------
# Login Route
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
        return jsonify({"error": "Invalid credentials"}), 401

    session["user_id"] = user.id
    return jsonify({"message": "Login successful"})

# ---------------------------------------
# Logout Route
# ---------------------------------------
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})

# ---------------------------------------
# API Key Authentication Helper
# ---------------------------------------
def get_user_by_api_key(provided_key):
    if not provided_key:
        return None

    db = SessionLocal()
    users = db.query(User).all()

    for user in users:
        if user.api_key_hash and bcrypt.verify(provided_key, user.api_key_hash):
            return user

    return None

# ---------------------------------------
# Generate / Regenerate API Key
# ---------------------------------------
@app.route("/generate_api_key", methods=["POST"])
def generate_api_key_route():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    db = SessionLocal()

    new_key = generate_api_key()
    hashed_key = bcrypt.hash(new_key)

    user.api_key_hash = hashed_key
    db.commit()

    return jsonify({
        "api_key": new_key,
        "message": "Store this key securely. You will not see it again."
    })

# ---------------------------------------
# Protected Reply Route
# ---------------------------------------
@app.route("/reply", methods=["POST"])
def reply():
    # 1. Try session authentication
    user = get_current_user()

    # 2. If no session, try API key
    if not user:
        api_key = request.headers.get("X-API-Key")
        user = get_user_by_api_key(api_key)

    # 3. If still no user → reject
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    # Free plan limit
    FREE_LIMIT = 7

    db = SessionLocal()
    user = db.query(User).get(user.id)

    if user.plan == "free" and user.free_uses >= FREE_LIMIT:
        return jsonify({
            "error": "limit_reached",
            "message": "You’ve used your 7 free replies. Upgrade to continue."
        }), 402

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    conversation = data.get("conversation", [])
    if not isinstance(conversation, list):
        return jsonify({"error": "Conversation must be a list"}), 400

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

    # Build message history
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history (if any)
    for turn in conversation:
        messages.append(turn)

    # Add the new user message
    messages.append({"role": "user", "content": data.get("message", "")})

    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.7,
    )

    reply_text = completion.choices[0].message.content.strip()

    if user.plan == "free":
        user.free_uses += 1
        db.commit()

    return jsonify({"reply": reply_text})


# ---------------------------------------
# Run App
# ---------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)