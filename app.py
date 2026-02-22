from flask import Flask, render_template, request, redirect, url_for, jsonify, session
import firebase_admin
from firebase_admin import credentials, firestore
import os
import easyocr
import cv2
from pyzbar.pyzbar import decode
from promptpay import qrcode
from werkzeug.security import generate_password_hash, check_password_hash
import json

app = Flask(__name__)
app.secret_key = "m42_v2_secure_key"
MY_PROMPTPAY_ID = "0627798207" # เบอร์พร้อมเพย์ของคุณ

# 1. เชื่อมต่อ Firebase (แบบป้องกันการเรียกซ้ำ)
if not firebase_admin._apps:
    # ตรวจสอบ Environment Variable ก่อน (สำหรับ Render)
    if os.environ.get('FIREBASE_CONFIG'):
        try:
            cred_dict = json.loads(os.environ.get('FIREBASE_CONFIG'))
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            print("✅ Connected to Firebase via Environment Variable")
        except Exception as e:
            print(f"❌ Error loading FIREBASE_CONFIG: {e}")
    # ถ้าไม่มี Env ให้เช็คจากไฟล์ (สำหรับรันในคอมตัวเอง)
    elif os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
        print("✅ Connected to Firebase via serviceAccountKey.json")
    else:
        print("❌ ไม่พบการตั้งค่า Firebase! กรุณาเช็ค Environment Variable หรือไฟล์ serviceAccountKey.json")

db = firestore.client()

# โหลด AI Reader
print("⌛ กำลังโหลด AI Reader...")
reader = easyocr.Reader(['th', 'en'], gpu=False)

# ฟังก์ชันเตรียมข้อมูลเริ่มต้นใน Firebase
def init_firebase():
    students_ref = db.collection('students')
    if not students_ref.limit(1).get():
        print("🚀 กำลังสร้างรายชื่อนักเรียน 29 คนใน Firebase...")
        students_ref.document('admin').set({
            'username': 'admin',
            'name': 'หัวหน้าห้อง',
            'password': generate_password_hash('admin123'),
            'debt': 0,
            'role': 'admin'
        })
        for i in range(1, 30):
            uid = f'user{i}'
            students_ref.document(uid).set({
                'username': uid,
                'name': f'เลขที่ {i}',
                'password': generate_password_hash('1234'),
                'debt': 0,
                'role': 'user'
            })
        print("✅ สร้างข้อมูลสำเร็จ")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        uname = request.form['username']
        pw = request.form['password']
        user_doc = db.collection('students').document(uname).get()
        
        if user_doc.exists:
            user = user_doc.to_dict()
            if check_password_hash(user['password'], pw):
                session.update({'user_id': uname, 'role': user['role'], 'name': user['name']})
                return redirect(url_for('index'))
        return "ชื่อผู้ใช้หรือรหัสผ่านผิด"
    return render_template('login.html')

@app.route('/')
def index():
    if 'user_id' not in session: return redirect(url_for('login'))
    
    if session['role'] == 'admin':
        docs = db.collection('students').where('role', '==', 'user').stream()
        students = []
        for doc in docs:
            d = doc.to_dict()
            students.append([doc.id, d['username'], d['name'], '', d['debt']])
        students.sort(key=lambda x: x[1])
        return render_template('admin.html', students=students)
    else:
        doc = db.collection('students').document(session['user_id']).get()
        user = doc.to_dict()
        s_data = [doc.id, user['username'], user['name'], '', user['debt']]
        return render_template('user.html', s=s_data)

@app.route('/verify_slip/<string:student_id>', methods=['POST'])
def verify_slip(student_id):
    if 'slip' not in request.files: return jsonify({"status": "error", "message": "ไม่พบไฟล์"})
    file = request.files['slip']
    temp_path = f"temp_{student_id}.jpg"
    file.save(temp_path)

    try:
        img = cv2.imread(temp_path)
        qr_codes = decode(img)
        if not qr_codes: return jsonify({"status": "error", "message": "ไม่พบ QR Code ในสลิป"})
        
        qr_raw = qr_codes[0].data.decode('utf-8')
        used_ref = db.collection('used_slips').document(qr_raw)
        
        if used_ref.get().exists:
            return jsonify({"status": "error", "message": "❌ สลิปนี้ถูกใช้ไปแล้ว!"})

        results = reader.readtext(temp_path, detail=0)
        amount_found = 0
        for i, text in enumerate(results):
            if any(k in text for k in ["บาท", "Baht", "จำนวนเงิน"]):
                for offset in [-1, 0, 1]:
                    try:
                        val = results[i+offset].replace(",", "")
                        if float(val) > 0:
                            amount_found = float(val)
                            break
                    except: continue
            if amount_found > 0: break

        if amount_found > 0:
            batch = db.batch()
            batch.set(used_ref, {'used_at': firestore.SERVER_TIMESTAMP, 'by': student_id})
            student_ref = db.collection('students').document(student_id)
            batch.update(student_ref, {'debt': firestore.Increment(-amount_found)})
            batch.commit()
            return jsonify({"status": "success", "message": f"✅ ตัดยอดสำเร็จ {amount_found} บาท"})
        
        return jsonify({"status": "error", "message": "อ่านยอดเงินไม่เจอ"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

@app.route('/add_daily_debt')
def add_daily_debt():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    docs = db.collection('students').where('role', '==', 'user').stream()
    batch = db.batch()
    for doc in docs:
        batch.update(db.collection('students').document(doc.id), {'debt': firestore.Increment(10)})
    batch.commit()
    return redirect(url_for('index'))

@app.route('/pay_cash/<string:student_id>')
def pay_cash(student_id):
    if session.get('role') != 'admin': return redirect(url_for('login'))
    db.collection('students').document(student_id).update({'debt': firestore.Increment(-10)})
    return redirect(url_for('index'))

@app.route('/get_qr/<int:amount>')
def get_qr(amount):
    payload = qrcode.generate_payload(MY_PROMPTPAY_ID, amount)
    return jsonify({"payload": payload})

@app.route('/change_password', methods=['POST'])
def change_password():
    if 'user_id' not in session: return redirect(url_for('login'))
    new_pw = generate_password_hash(request.form['new_password'])
    db.collection('students').document(session['user_id']).update({'password': new_pw})
    return "เปลี่ยนรหัสผ่านสำเร็จ! <a href='/'>กลับหน้าหลัก</a>"

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    init_firebase()
    app.run(debug=True, host='0.0.0.0', port=5000)