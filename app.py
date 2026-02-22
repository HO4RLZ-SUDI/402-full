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
from pyzbar.pyzbar import decode

import qrcode
import os

# =========================
# 🔐 CONFIG
# =========================

CHANNEL_ACCESS_TOKEN = "2c01ccicuhDmb2DTG8ZHYcIqNfqNjQbA/MNkBvk3gquOdHiwLDIMrerR9YBgNJScZaT7aFPCbu9LqAKu7LmJLQh0cC0O9kB+2YLxOHzeIN5T4/G+hXON/QZpOPTfFlFobApRyOgU6YzwrvLk2J0W+QdB04t89/1O/w1cDnyilFU="
CHANNEL_SECRET = "9154168417b2763e858a587a531db8c5"
FIREBASE_KEY_PATH = "serviceAccount.json"

ADMIN_UID = "U6b0f65a4cea28060111979d38aa8b3ab"
MY_PROMPTPAY = "0627798207"

NGROK_URL = "https://1d94-184-22-189-34.ngrok-free.app"

# =========================
# 🔧 INIT
# =========================

app = Flask(__name__)

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

cred = credentials.Certificate(FIREBASE_KEY_PATH)
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
    codes = decode(img)

    if codes:
        return codes[0].data.decode("utf-8")

    return None


def get_student(uid):
    docs = db.collection("students").where("line_uid", "==", uid).get()
    for d in docs:
        return d.id, d.to_dict()
    return None, None


def generate_qr(phone, amount):
    payload = pp_qrcode.generate_payload(phone, amount)

    img = qrcode.make(payload)

    path = f"qr_{amount}.png"
    img.save(path)

    return path
# =========================
# 🌐 SERVE QR FILE
# =========================

@app.route("/qr/<filename>")
def serve_qr(filename):
    return send_file(filename, mimetype="image/png")


# =========================
# 🌐 ADMIN DASHBOARD
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

    # 📝 สมัคร
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

    # 💰 เช็คยอด
    elif text == "เช็คยอด":
        sid, data = get_student(uid)

        if not data:
            reply = "❌ ยังไม่ได้ลงทะเบียน"
        else:
            reply = f"💰 ยอดค้าง: {data['debt']} บาท"

    # 💸 จ่ายเงิน → ส่ง QR
    elif text in ["จ่ายเงิน", "วิธีจ่ายเงิน"]:
        sid, data = get_student(uid)

        if not data:
            reply = "❌ ลงทะเบียนก่อน"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        debt = data["debt"]

        if debt <= 0:
            reply = "✅ ไม่มีหนี้"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        qr_path = generate_qr(MY_PROMPTPAY, debt)

        line_bot_api.reply_message(
            event.reply_token,
            [
                TextSendMessage(text=f"💸 ยอดค้าง {debt} บาท\nโอนแล้วส่งสลิป"),
                ImageSendMessage(
                    original_content_url=f"{NGROK_URL}/qr/{qr_path}",
                    preview_image_url=f"{NGROK_URL}/qr/{qr_path}"
                )
            ]
        )
        return

    # ======================
    # 👑 ADMIN COMMANDS
    # ======================

    elif text.startswith("เพิ่มหนี้"):
        if uid != ADMIN_UID:
            reply = "❌ ไม่มีสิทธิ์"
        else:
            amount = int(text.split()[1])

            docs = db.collection("students").stream()
            for d in docs:
                db.collection("students").document(d.id).update({
                    "debt": firestore.Increment(amount)
                })

            reply = f"✅ เพิ่มหนี้ทุกคน +{amount}"

    elif text.startswith("เพิ่ม "):
        if uid != ADMIN_UID:
            reply = "❌ ไม่มีสิทธิ์"
        else:
            _, sid, amount = text.split()

            db.collection("students").document(sid).update({
                "debt": firestore.Increment(int(amount))
            })

            reply = f"✅ เพิ่มหนี้ {sid} +{amount}"

    elif text.startswith("เงินสด"):
        if uid != ADMIN_UID:
            reply = "❌ ไม่มีสิทธิ์"
        else:
            _, sid, amount = text.split()

            db.collection("students").document(sid).update({
                "debt": firestore.Increment(-int(amount))
            })

            reply = f"💵 รับเงินสด {sid} {amount}"

    elif text == "สรุป":
        if uid != ADMIN_UID:
            reply = "❌ ไม่มีสิทธิ์"
        else:
            docs = db.collection("students").stream()

            msg = "📊 สรุปหนี้\n"
            for d in docs:
                data = d.to_dict()
                msg += f"{d.id}: {data['debt']} บาท\n"

            reply = msg

    else:
        reply = "❓ ไม่เข้าใจคำสั่ง"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# =========================
# 📸 IMAGE MESSAGE (SLIP)
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

    # 🛡️ กันสลิปซ้ำ
    slip_hash = hash_slip(img_bytes)

    if slip_hash in data.get("slips", []):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("❌ สลิปซ้ำ")
        )
        return

    # 📷 อ่าน QR (ไม่ตรวจเบอร์แล้ว)
    qr = extract_qr(img_bytes)

    if not qr:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("❌ ไม่พบ QR ในสลิป")
        )
        return

    # 💰 อ่านยอดเงิน
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

    reply = f"✅ ชำระ {amount} บาท\nคงเหลือ {new_debt} บาท"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

# =========================
# 🚀 RUN
# =========================

if __name__ == "__main__":
    app.run(port=5000, debug=True)