import os
import secrets
import string
from flask import Flask, request, jsonify
import pymysql
from datetime import datetime, date

app = Flask(__name__)

# ══════════════════════════════════════════
#  TiDB 連線
# ══════════════════════════════════════════
def get_db_connection():
    return pymysql.connect(
        host=os.getenv("TIDB_HOST"),
        port=int(os.getenv("TIDB_PORT", 4000)),
        user=os.getenv("TIDB_USERNAME"),
        password=os.getenv("TIDB_PASSWORD"),
        database=os.getenv("TIDB_DB"),
        ssl_verify_cert=True,
        autocommit=True
    )

# ══════════════════════════════════════════
#  管理員金鑰驗證（後台專用）
#  從 Render 環境變數設定 ADMIN_KEY
# ══════════════════════════════════════════
def check_admin(req):
    return req.json.get("admin_key") == os.getenv("ADMIN_KEY", "")

# ══════════════════════════════════════════
#  金鑰產生器
#  格式：統編前4碼 - 統編後4碼 - 隨機4碼 - 隨機4碼
#  範例：1234-5678-A3F9-X7K2
# ══════════════════════════════════════════
def generate_license_key(tax_id: str) -> str:
    chars = string.ascii_uppercase + string.digits
    r1 = ''.join(secrets.choice(chars) for _ in range(4))
    r2 = ''.join(secrets.choice(chars) for _ in range(4))
    tid = tax_id.zfill(8)          # 補足8碼
    return f"{tid[:4]}-{tid[4:8]}-{r1}-{r2}"

@app.route('/')
def home():
    return "保固系統 API 運行中"

# ══════════════════════════════════════════
#  ✅ 啟動驗證（本地端每次啟動時呼叫）
#  回傳：公司名稱、授權到期日、剩餘天數
# ══════════════════════════════════════════
@app.route('/verify', methods=['POST'])
def verify_license():
    content      = request.json or {}
    received_key = content.get("key", "").strip()

    if not received_key:
        return jsonify({"error": "缺少授權金鑰"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT client_id, company_name, expired_date, is_active
                FROM license_manager
                WHERE license_key = %s
            """, (received_key,))
            row = cursor.fetchone()

            if not row:
                return jsonify({
                    "valid": False,
                    "reason": "金鑰不存在"
                }), 403

            client_id, company_name, expired_date, is_active = row

            if not is_active:
                return jsonify({
                    "valid": False,
                    "reason": "授權已停用，請聯絡服務商"
                }), 403

            today         = date.today()
            expired       = expired_date if isinstance(expired_date, date) \
                            else datetime.strptime(str(expired_date), "%Y-%m-%d").date()
            days_left     = (expired - today).days

            if days_left < 0:
                return jsonify({
                    "valid": False,
                    "reason": f"授權已於 {expired} 到期，請聯絡服務商續約"
                }), 403

            return jsonify({
                "valid":        True,
                "client_id":    client_id,
                "company_name": company_name,
                "expired_date": str(expired),
                "days_left":    days_left
            }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  上傳資料 Push
# ══════════════════════════════════════════
@app.route('/sync', methods=['POST'])
def sync_data():
    content      = request.json or {}
    received_key = content.get("key", "")
    data_list    = content.get("data")

    if not received_key or data_list is None:
        return jsonify({"error": "缺少金鑰或資料"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT client_id FROM license_manager "
                "WHERE license_key = %s AND is_active = TRUE",
                (received_key,)
            )
            result = cursor.fetchone()
            if not result:
                return jsonify({"error": "無效或已停用的授權金鑰"}), 403

            client_id = result[0]
            cursor.execute(
                "DELETE FROM equipment_master WHERE client_id = %s", (client_id,))

            sql = """
                INSERT INTO equipment_master
                (client_id, school, classroom, brand, device_name,
                 model, serial, mac_address, finish_date, warranty_years)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            for row in data_list:
                cursor.execute(sql, (client_id, *row))

        return jsonify({"status": "success", "client": client_id}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  從雲端下載 Pull
# ══════════════════════════════════════════
@app.route('/pull', methods=['POST'])
def pull_data():
    content      = request.json or {}
    received_key = content.get("key", "")

    if not received_key:
        return jsonify({"error": "缺少授權金鑰"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT client_id FROM license_manager "
                "WHERE license_key = %s AND is_active = TRUE",
                (received_key,)
            )
            result = cursor.fetchone()
            if not result:
                return jsonify({"error": "無效或已停用的授權金鑰"}), 403

            client_id = result[0]
            cursor.execute("""
                SELECT school, classroom, brand, device_name, model,
                       serial, mac_address,
                       DATE_FORMAT(finish_date, '%%Y-%%m-%%d'),
                       warranty_years
                FROM equipment_master
                WHERE client_id = %s
                ORDER BY id ASC
            """, (client_id,))

            data = [list(row) for row in cursor.fetchall()]

        return jsonify({"status": "success", "client": client_id, "data": data}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  ✅ 後台：新增客戶 + 自動產生金鑰
#  需帶 admin_key 才能呼叫
# ══════════════════════════════════════════
@app.route('/admin/create_license', methods=['POST'])
def create_license():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    content      = request.json or {}
    tax_id       = content.get("tax_id", "").strip()       # 統編（必填）
    company_name = content.get("company_name", "").strip() # 公司名稱
    months       = int(content.get("months", 12))          # 授權月數，預設1年

    if not tax_id or not company_name:
        return jsonify({"error": "統編與公司名稱為必填"}), 400

    # 用統編當 client_id（唯一）
    client_id   = tax_id
    license_key = generate_license_key(tax_id)

    # 計算到期日
    today        = date.today()
    exp_year     = today.year  + (today.month + months - 1) // 12
    exp_month    = (today.month + months - 1) % 12 + 1
    expired_date = date(exp_year, exp_month, today.day)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 檢查統編是否已存在
            cursor.execute(
                "SELECT license_key FROM license_manager WHERE client_id = %s",
                (client_id,)
            )
            existing = cursor.fetchone()
            if existing:
                return jsonify({
                    "error": f"統編 {tax_id} 已存在，現有金鑰：{existing[0]}"
                }), 409

            cursor.execute("""
                INSERT INTO license_manager
                    (client_id, company_name, license_key,
                     is_active, expired_date, created_date)
                VALUES (%s, %s, %s, TRUE, %s, %s)
            """, (client_id, company_name, license_key,
                  str(expired_date), str(today)))

        return jsonify({
            "status":       "success",
            "company_name": company_name,
            "tax_id":       tax_id,
            "license_key":  license_key,
            "expired_date": str(expired_date),
            "months":       months
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  ✅ 後台：查詢所有客戶清單
# ══════════════════════════════════════════
@app.route('/admin/list_licenses', methods=['POST'])
def list_licenses():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT client_id, company_name, license_key,
                       is_active, expired_date, created_date
                FROM license_manager
                ORDER BY created_date DESC
            """)
            rows = cursor.fetchall()

        today   = date.today()
        clients = []
        for r in rows:
            exp   = r[4] if isinstance(r[4], date) \
                    else datetime.strptime(str(r[4]), "%Y-%m-%d").date()
            clients.append({
                "client_id":    r[0],
                "company_name": r[1],
                "license_key":  r[2],
                "is_active":    bool(r[3]),
                "expired_date": str(exp),
                "days_left":    (exp - today).days,
                "created_date": str(r[5])
            })

        return jsonify({"status": "success", "clients": clients}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  ✅ 後台：停用 / 啟用客戶
# ══════════════════════════════════════════
@app.route('/admin/toggle_license', methods=['POST'])
def toggle_license():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    content   = request.json or {}
    tax_id    = content.get("tax_id", "").strip()
    is_active = content.get("is_active", True)   # True=啟用 False=停用

    if not tax_id:
        return jsonify({"error": "缺少統編"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE license_manager SET is_active = %s WHERE client_id = %s",
                (is_active, tax_id)
            )
        status = "啟用" if is_active else "停用"
        return jsonify({"status": "success", "message": f"{tax_id} 已{status}"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  ✅ 後台：延長授權到期日
# ══════════════════════════════════════════
@app.route('/admin/extend_license', methods=['POST'])
def extend_license():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    content = request.json or {}
    tax_id  = content.get("tax_id", "").strip()
    months  = int(content.get("months", 12))

    if not tax_id:
        return jsonify({"error": "缺少統編"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT expired_date FROM license_manager WHERE client_id = %s",
                (tax_id,)
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "找不到此統編"}), 404

            old_exp  = row[0] if isinstance(row[0], date) \
                       else datetime.strptime(str(row[0]), "%Y-%m-%d").date()
            # 從今天或舊到期日（取較大值）往後加
            base     = max(old_exp, date.today())
            new_year = base.year  + (base.month + months - 1) // 12
            new_mon  = (base.month + months - 1) % 12 + 1
            new_exp  = date(new_year, new_mon, base.day)

            cursor.execute(
                "UPDATE license_manager SET expired_date = %s WHERE client_id = %s",
                (str(new_exp), tax_id)
            )

        return jsonify({
            "status":       "success",
            "tax_id":       tax_id,
            "new_expired":  str(new_exp),
            "added_months": months
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
