from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import firebase_admin
from firebase_admin import credentials, firestore
import os, easyocr, cv2, json, requests
from pyzbar.pyzbar import decode
from werkzeug.security import generate_password_hash, check_password_hash

# LINE Bot SDK
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, TextSendMessage, 
    ButtonsTemplate, TemplateSendMessage, MessageAction
)

app = Flask(__name__)
app.secret_key = "m42_money_2026_final"

# --- 1. ตั้งค่า LINE (Messaging API) ---
LINE_ACCESS_TOKEN = 'วาง_CHANNEL_ACCESS_TOKEN_ตัวยาว_ที่นี่'
LINE_CHANNEL_SECRET = 'วาง_CHANNEL_SECRET_ที่นี่'

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. เชื่อมต่อ Firebase (ใช้ข้อมูลจาก Project Settings) ---
if not firebase_admin._apps:
    if os.environ.get('FIREBASE_CONFIG'):
        cred = credentials.Certificate(json.loads(os.environ.get('FIREBASE_CONFIG')))
    elif os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
    else:
        cred = None
    if cred: firebase_admin.initialize_app(cred)

db = firestore.client()
reader = easyocr.Reader(['th', 'en'], gpu=False)

# --- 3. LINE Webhook Logic (รับข้อมูลจากแชท) ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    # คำสั่งแสดงเมนูปุ่ม (Inline Commands)
    if text in ["เมนู", "เริ่ม", "สวัสดี"]:
        buttons_template = ButtonsTemplate(
            title='ระบบเงินห้อง ม.4/2',
            text='ยินดีต้อนรับ! เลือกรายการที่ต้องการครับ',
            actions=[
                MessageAction(label='📝 ลงทะเบียนเลขที่', text='ลงทะเบียน'),
                MessageAction(label='💰 เช็คยอดหนี้', text='เช็คยอดหนี้'),
                MessageAction(label='💳 วิธีจ่ายเงิน', text='วิธีจ่ายเงิน')
            ]
        )
        line_bot_api.reply_message(event.reply_token, TemplateSendMessage(alt_text='เมนูระบบ', template=buttons_template))

    elif text == "ลงทะเบียน":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="กรุณาพิมพ์ 'เลขที่' ตามด้วยหมายเลขของคุณ\nตัวอย่าง: เลขที่ 5"))

    elif text.startswith("เลขที่"):
        try:
            no = text.replace("เลขที่", "").strip()
            student_id = f"user{no}"
            doc_ref = db.collection('students').document(student_id)
            if doc_ref.get().exists:
                doc_ref.update({'line_uid': user_id})
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ สำเร็จ! เลขที่ {no} ผูกกับ LINE นี้แล้วครับ"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ ไม่พบเลขที่นี้ในระบบ"))
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="รูปแบบผิด! ลองพิมพ์ 'เลขที่ 5'"))

    elif text == "เช็คยอดหนี้":
        docs = db.collection('students').where('line_uid', '==', user_id).limit(1).stream()
        found = False
        for d in docs:
            u = d.to_dict()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📊 เลขที่ {u['username'].replace('user','')}\n💰 ยอดหนี้คงเหลือ: {u['debt']} บาท"))
            found = True
        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ กรุณาลงทะเบียนก่อนครับ"))

    elif text == "วิธีจ่ายเงิน":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📱 สแกนจ่ายพร้อมเพย์หัวหน้าห้อง แล้วส่งสลิปเข้ามาในแชทนี้ได้เลย!"))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    # ค้นหาเลขที่จาก LINE UID
    docs = db.collection('students').where('line_uid', '==', user_id).limit(1).stream()
    student_id = None
    for d in docs: student_id = d.id

    if not student_id:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ ลงทะเบียนเลขที่ก่อนส่งสลิปนะครับ"))
        return

    # บันทึกรูปเพื่อตรวจสอบ
    msg_content = line_bot_api.get_message_content(event.message.id)
    path = f"slip_{user_id}.jpg"
    with open(path, 'wb') as f:
        for chunk in msg_content.iter_content(): f.write(chunk)

    try:
        img = cv2.imread(path)
        qr = decode(img)
        if not qr:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ ไม่พบ QR ในสลิป กรุณาส่งใหม่ครับ"))
            return

        qr_data = qr[0].data.decode('utf-8')
        # เช็คสลิปซ้ำใน Firebase
        if db.collection('used_slips').document(qr_data).get().exists:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ สลิปนี้ถูกใช้ไปแล้วครับ"))
            return

        # อ่านยอดเงิน (OCR)
        results = reader.readtext(path, detail=0)
        amt = 0
        for i, t in enumerate(results):
            if any(k in t for k in ["บาท", "Baht", "จำนวนเงิน"]):
                for off in [-1, 0, 1]:
                    try:
                        val = float(results[i+off].replace(",", ""))
                        if val > 0: amt = val; break
                    except: continue
            if amt > 0: break

        if amt > 0:
            # หักหนี้อัตโนมัติ
            batch = db.batch()
            batch.set(db.collection('used_slips').document(qr_data), {'by': student_id, 'at': firestore.SERVER_TIMESTAMP})
            batch.update(db.collection('students').document(student_id), {'debt': firestore.Increment(-amt)})
            batch.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ ตัดยอดสำเร็จ {amt} บาท! หนี้ลดลงแล้วครับ"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❓ อ่านยอดไม่สำเร็จ กรุณาติดต่อหัวหน้าห้องครับ"))
    finally:
        if os.path.exists(path): os.remove(path)

# --- 4. Web Admin Routes (สำหรับหัวหน้าห้องดูสรุป) ---
@app.route('/')
def index():
    if 'user_id' not in session or session.get('role') != 'admin': return redirect(url_for('login'))
    docs = db.collection('students').where('role', '==', 'user').stream()
    students = [[d.id, d.to_dict()['username'], d.to_dict()['name'], d.to_dict().get('line_uid','-'), d.to_dict()['debt']] for d in docs]
    return render_template('admin.html', students=sorted(students, key=lambda x: x[1]))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form['username']; p = request.form['password']
        doc = db.collection('students').document(u).get()
        if doc.exists and check_password_hash(doc.to_dict()['password'], p):
            session.update({'user_id': u, 'role': doc.to_dict()['role']})
            return redirect(url_for('index'))
    return render_template('login.html')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)