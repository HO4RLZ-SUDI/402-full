from flask import Flask, request, abort, send_file
from linebot import LineBotApi, WebhookHandler
from linebot.models import *

import firebase_admin
from firebase_admin import credentials, firestore

from promptpay import qrcode as pp_qrcode
import easyocr
import cv2
import numpy as np
import hashlib
import qrcode

import os
import json

# =========================
# 🔐 CONFIG (ใช้ ENV)
# =========================

CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_TOKEN")
CHANNEL_SECRET = os.environ.get("LINE_SECRET")
ADMIN_UID = os.environ.get("ADMIN_UID")
MY_PROMPTPAY = os.environ.get("PROMPTPAY")
BASE_URL = os.environ.get("BASE_URL")  # URL ของ Render

# =========================
# 🔧 INIT
# =========================

app = Flask(__name__)

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 🔥 Firebase from ENV
firebase_key = os.environ.get("FIREBASE_KEY")
cred = credentials.Certificate(json.loads(firebase_key))
firebase_admin.initialize_app(cred)
db = firestore.client()

ocr = easyocr.Reader(['th', 'en'])

# =========================
# 🧠 UTIL FUNCTIONS
# =========================

def hash_slip(img):
    return hashlib.sha256(img).hexdigest()


def extract_amount(img_bytes):
    img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
    texts = ocr.readtext(img, detail=0)

    best = None
    for t in texts:
        t = t.replace(",", "").strip()
        try:
            val = float(t)
            if 0 < val < 100000:
                if best is None or val > best:
                    best = val
        except:
            pass

    return int(best) if best else None


def extract_qr(img_bytes):
    img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)

    detector = cv2.QRCodeDetector()
    data, _, _ = detector.detectAndDecode(img)

    return data if data else None


def get_student(uid):
    docs = db.collection("students").where("line_uid", "==", uid).get()
    for d in docs:
        return d.id, d.to_dict()
    return None, None


def generate_qr(phone, amount):
    payload = pp_qrcode.generate_payload(phone, amount)
    img = qrcode.make(payload)

    path = f"/tmp/qr_{amount}.png"
    img.save(path)

    return path

# =========================
# 🌐 SERVE QR
# =========================

@app.route("/qr/<filename>")
def serve_qr(filename):
    return send_file(f"/tmp/{filename}", mimetype="image/png")

# =========================
# 🌐 DASHBOARD
# =========================

@app.route("/")
def home():
    docs = db.collection("students").stream()

    html = "<h1>💰 Debt Dashboard</h1><table border=1>"
    html += "<tr><th>No</th><th>Name</th><th>Debt</th></tr>"

    for d in docs:
        data = d.to_dict()
        html += f"<tr><td>{d.id}</td><td>{data.get('name')}</td><td>{data.get('debt')}</td></tr>"

    html += "</table>"
    return html


@app.route("/remind")
def remind():
    docs = db.collection("students").stream()

    for d in docs:
        data = d.to_dict()

        if data.get("debt", 0) > 0 and data.get("line_uid"):
            line_bot_api.push_message(
                data["line_uid"],
                TextSendMessage(text=f"⏰ คุณยังค้าง {data['debt']} บาท")
            )

    return "Reminded"


# =========================
# 🤖 WEBHOOK
# =========================

@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        print("ERROR:", e)
        abort(400)

    return "OK"


# =========================
# 💬 TEXT MESSAGE
# =========================

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text.strip()
    uid = event.source.user_id

    if text == "ลงทะเบียน":
        reply = "พิมพ์: เลขที่ <เลข> เช่น เลขที่ 7"

    elif text.startswith("เลขที่"):
        num = text.replace("เลขที่", "").strip()

        db.collection("students").document(num).set({
            "line_uid": uid,
            "name": f"เลขที่ {num}",
            "debt": 0,
            "slips": []
        }, merge=True)

        reply = f"✅ ลงทะเบียนเลขที่ {num}"

    elif text == "เช็คยอด":
        sid, data = get_student(uid)

        if not data:
            reply = "❌ ยังไม่ได้ลงทะเบียน"
        else:
            reply = f"💰 ยอดค้าง: {data['debt']} บาท"

    elif text in ["จ่ายเงิน", "วิธีจ่ายเงิน"]:
        sid, data = get_student(uid)

        if not data:
            reply = "❌ ลงทะเบียนก่อน"
        else:
            debt = data["debt"]

            if debt <= 0:
                reply = "✅ ไม่มีหนี้"
            else:
                qr_path = generate_qr(MY_PROMPTPAY, debt)

                line_bot_api.reply_message(
                    event.reply_token,
                    [
                        TextSendMessage(text=f"💸 ยอดค้าง {debt} บาท\nโอนแล้วส่งสลิป"),
                        ImageSendMessage(
                            original_content_url=f"{BASE_URL}/qr/qr_{debt}.png",
                            preview_image_url=f"{BASE_URL}/qr/qr_{debt}.png"
                        )
                    ]
                )
                return

    else:
        reply = "❓ ไม่เข้าใจคำสั่ง"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# =========================
# 📸 IMAGE MESSAGE
# =========================

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    uid = event.source.user_id
    sid, data = get_student(uid)

    if not data:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("❌ ลงทะเบียนก่อน")
        )
        return

    content = line_bot_api.get_message_content(event.message.id)
    img_bytes = content.content

    slip_hash = hash_slip(img_bytes)

    if slip_hash in data.get("slips", []):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("❌ สลิปซ้ำ")
        )
        return

    qr = extract_qr(img_bytes)
    if not qr:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("❌ ไม่พบ QR")
        )
        return

    amount = extract_amount(img_bytes)
    if not amount:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("❌ อ่านยอดไม่ได้")
        )
        return

    new_debt = max(0, data["debt"] - amount)

    db.collection("students").document(sid).update({
        "debt": new_debt,
        "slips": firestore.ArrayUnion([slip_hash])
    })

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(f"✅ ชำระ {amount} บาท\nคงเหลือ {new_debt} บาท")
    )


# =========================
# 🚀 RUN (Render)
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)