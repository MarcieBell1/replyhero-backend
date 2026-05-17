from flask import Flask, request, jsonify, session
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
import os
import secrets
from datetime import datetime
import stripe  # ⭐ ADD THIS

# ---------------------------------------
# Load environment variables FIRST
# ---------------------------------------
load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # ⭐ ADD THIS

# ---------------------------------------
# Helper: Generate API Key
# ---------------------------------------
def generate_api_key():
    return secrets.token_hex(32)  # 64‑character key

# ---------------------------------------
# Database Setup (PostgreSQL + SQLAlchemy)
# ---------------------------------------
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from passlib.hash import bcrypt   # ⭐ Needed for password hashing

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ---------------------------------------
# Flask App Setup
# ---------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this")

# Enable CORS (allow cookies)
CORS(app, supports_credentials=True)

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
    free_uses = Column(Integer, default=0)  # ⭐ NEW: track how many free replies used
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

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
from werkzeug.security import generate_password_hash, check_password_hash
from flask import request, jsonify, session

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    # Check if user already exists
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    existing = cur.fetchone()

    if existing:
        return jsonify({"error": "Email already registered"}), 400

    # Hash password
    hashed = generate_password_hash(password)

    # Insert new user
    cur.execute("""
        INSERT INTO users (email, password_hash, free_uses, plan)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """, (email, hashed, 0, "free"))

    user_id = cur.fetchone()[0]
    conn.commit()
    cur.close()

    # Auto‑login after signup
    session["user_id"] = user_id

    return jsonify({"message": "Signup successful", "user_id": user_id})

# ---------------------------------------
# Signup Route
# ---------------------------------------
@app.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()
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

    # ---------------------------------------
    # ⭐ Free usage limit: 7 total for free plan
    # ---------------------------------------
    FREE_LIMIT = 7

    # We need a DB session bound to this user to safely update free_uses
    db = SessionLocal()
    user = db.query(User).get(user.id)

    if user.plan == "free" and user.free_uses >= FREE_LIMIT:
        return jsonify({
            "error": "limit_reached",
            "message": "You’ve used your 7 free replies. Upgrade to continue."
        }), 402

    data = request.get_json()

    conversation = data.get("conversation", "").strip()
    tone = data.get("tone", "Professional")
    rewrite_mode = data.get("rewrite", False)
    length = data.get("length", "Medium")

    if not conversation:
        return jsonify({"error": "Conversation is required."}), 400

    # Length rules
    length_instruction = {
        "Short": "Keep the reply to 1 short sentence.",
        "Medium": "Write a reply that is 2–3 sentences long.",
        "Long": "Write a detailed reply that is 4–6 sentences long."
    }.get(length, "Write a concise reply.")

    # Rewrite vs Generate
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

    # System prompt
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

    # Call OpenAI
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": conversation}
        ],
        temperature=0.7,
    )

    reply_text = completion.choices[0].message.content.strip()

    # ⭐ Increment free usage for free-plan users
    if user.plan == "free":
        user.free_uses += 1
        db.commit()

    return jsonify({"reply": reply_text})


# ---------------------------------------
# Run App (Render-compatible)
# ---------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)