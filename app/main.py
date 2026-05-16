from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


@app.route("/reply", methods=["POST"])
def reply():
    data = request.get_json()

    conversation = data.get("conversation", "").strip()
    tone = data.get("tone", "Professional")
    rewrite_mode = data.get("rewrite", False)
    length = data.get("length", "Medium")

    if not conversation:
        return jsonify({"error": "Conversation is required."}), 400

    # ⭐ FINAL length rules (no duplicates)
    length_instruction = {
        "Short": "Keep the reply to 1 short sentence.",
        "Medium": "Write a reply that is 2–3 sentences long.",
        "Long": "Write a detailed reply that is 4–6 sentences long."
    }.get(length, "Write a concise reply.")

    # ⭐ Rewrite vs Generate
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

    # ⭐ System prompt
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

    # ⭐ Call OpenAI
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": conversation}
        ],
        temperature=0.7,
    )

    reply_text = completion.choices[0].message.content.strip()

    return jsonify({"reply": reply_text})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)