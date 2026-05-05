import os
import secrets
import string
from flask import Flask, request, jsonify
import pymysql
from datetime import datetime, date

app = Flask(__name__)

# ══════════════════════════════════════════
#  TiDB 連線
#  實際欄位：license_key, client_id, company_name,
#            is_active, created_date, expired_date
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

def check_admin(req):
    """管理員金鑰驗證（Render 環境變數 ADMIN_KEY）"""
    return req.json.get("admin_key") == os.getenv("ADMIN_KEY", "")

def generate_license_key(tax_id: str) -> str:
    """
    金鑰格式：統編前4碼-統編後4碼-隨機4碼-隨機4碼
    範例：1234-5678-A3F9-X7K2
    """
    chars = string.ascii_uppercase + string.digits
    r1 = ''.join(secrets.choice(chars) for _ in range(4))
    r2 = ''.join(secrets.choice(chars) for _ in range(4))
    tid = tax_id.zfill(8)
    return f"{tid[:4]}-{tid[4:8]}-{r1}-{r2}"

def to_date(val):
    """統一把 date / datetime / str 轉成 date 物件"""
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()

@app.route('/')
def home():
    return "保固系統 API 運行中"

# ══════════════════════════════════════════
#  ✅ 啟動驗證
#  本地端每次啟動時呼叫，確認金鑰有效且未到期
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
                return jsonify({"valid": False,
                                "reason": "金鑰不存在"}), 403

            client_id, company_name, expired_date, is_active = row

            if not is_active:
                return jsonify({"valid": False,
                                "reason": "授權已停用，請聯絡服務商"}), 403

            today   = date.today()
            expired = to_date(expired_date)

            # ✅ 永久授權：到期日為 9999-12-31
            is_permanent = (expired.year == 9999)

            if is_permanent:
                return jsonify({
                    "valid":        True,
                    "client_id":    client_id,
                    "company_name": company_name,
                    "expired_date": "永久授權",
                    "days_left":    99999,
                    "is_permanent": True
                }), 200

            days_left = (expired - today).days

            if days_left < 0:
                return jsonify({
                    "valid":  False,
                    "reason": f"授權已於 {expired} 到期，請聯絡服務商續約"
                }), 403

            return jsonify({
                "valid":        True,
                "client_id":    client_id,
                "company_name": company_name,
                "expired_date": str(expired),
                "days_left":    days_left,
                "is_permanent": False
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
                "DELETE FROM equipment_master WHERE client_id = %s",
                (client_id,))

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

        return jsonify({"status": "success", "client": client_id,
                        "data": data}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  ✅ 後台：新增客戶 + 自動產生金鑰
# ══════════════════════════════════════════
@app.route('/admin/create_license', methods=['POST'])
def create_license():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    content      = request.json or {}
    tax_id       = content.get("tax_id", "").strip()
    company_name = content.get("company_name", "").strip()
    months       = int(content.get("months", 12))
    is_permanent = content.get("permanent", False)
    created_str  = content.get("created_date", "").strip()

    if not tax_id or not company_name:
        return jsonify({"error": "統編與公司名稱為必填"}), 400

    license_key = generate_license_key(tax_id)

    # 建立日期（前端傳入 or 預設今天）
    today = date.today()
    try:
        created = datetime.strptime(created_str, "%Y-%m-%d").date() \
                  if created_str else today
    except ValueError:
        created = today

    # 到期日
    if is_permanent:
        expired = date(9999, 12, 31)   # 永久授權
    else:
        exp_year  = today.year + (today.month + months - 1) // 12
        exp_month = (today.month + months - 1) % 12 + 1
        expired   = date(exp_year, exp_month, today.day)

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT license_key FROM license_manager WHERE client_id = %s",
                (tax_id,)
            )
            existing = cursor.fetchone()
            if existing:
                return jsonify({
                    "error": f"統編 {tax_id} 已存在，現有金鑰：{existing[0]}"
                }), 409

            cursor.execute("""
                INSERT INTO license_manager
                    (license_key, client_id, company_name,
                     is_active, created_date, expired_date)
                VALUES (%s, %s, %s, TRUE, %s, %s)
            """, (license_key, tax_id, company_name,
                  str(created), str(expired)))

        return jsonify({
            "status":       "success",
            "company_name": company_name,
            "tax_id":       tax_id,
            "license_key":  license_key,
            "expired_date": str(expired),
            "is_permanent": is_permanent,
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
            try:
                exp = to_date(r[4])
                # ✅ 永久授權（9999-12-31）特殊處理，不做天數計算
                is_permanent = (exp.year == 9999)
                days_left    = 99999 if is_permanent else (exp - today).days
                exp_str      = str(exp)
            except Exception:
                exp_str      = str(r[4]) if r[4] else ""
                is_permanent = False
                days_left    = 0

            created = str(r[5])[:10] if r[5] else ""
            clients.append({
                "client_id":    r[0],
                "company_name": r[1],
                "license_key":  r[2],
                "is_active":    bool(r[3]),
                "expired_date": exp_str,
                "days_left":    days_left,
                "created_date": created
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
    is_active = content.get("is_active", True)

    if not tax_id:
        return jsonify({"error": "缺少統編"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE license_manager SET is_active = %s "
                "WHERE client_id = %s",
                (is_active, tax_id)
            )
        status = "啟用" if is_active else "停用"
        return jsonify({"status": "success",
                        "message": f"{tax_id} 已{status}"}), 200

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
                "SELECT expired_date FROM license_manager "
                "WHERE client_id = %s", (tax_id,)
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "找不到此統編"}), 404

            old_exp = to_date(row[0])

            # ✅ 永久授權不允許延長
            if old_exp.year == 9999:
                return jsonify({
                    "error": "此客戶為永久授權，無需延長"
                }), 400

            base     = max(old_exp, date.today())
            new_year = base.year + (base.month + months - 1) // 12
            new_mon  = (base.month + months - 1) % 12 + 1
            new_exp  = date(new_year, new_mon, base.day)

            cursor.execute(
                "UPDATE license_manager SET expired_date = %s "
                "WHERE client_id = %s",
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


# ══════════════════════════════════════════
#  ✅ 後台：設為永久授權
# ══════════════════════════════════════════
@app.route('/admin/set_permanent', methods=['POST'])
def set_permanent():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    tax_id = (request.json or {}).get("tax_id", "").strip()
    if not tax_id:
        return jsonify({"error": "缺少統編"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE license_manager SET expired_date = '9999-12-31' "
                "WHERE client_id = %s",
                (tax_id,)
            )
        return jsonify({"status": "success",
                        "message": f"{tax_id} 已設為永久授權"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  ✅ 後台：修改建立日期
# ══════════════════════════════════════════
@app.route('/admin/edit_created_date', methods=['POST'])
def edit_created_date():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    content      = request.json or {}
    tax_id       = content.get("tax_id", "").strip()
    created_date = content.get("created_date", "").strip()

    if not tax_id or not created_date:
        return jsonify({"error": "缺少統編或日期"}), 400

    try:
        datetime.strptime(created_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "日期格式錯誤，請使用 YYYY-MM-DD"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE license_manager SET created_date = %s "
                "WHERE client_id = %s",
                (created_date, tax_id)
            )
        return jsonify({"status": "success",
                        "message": f"{tax_id} 建立日期已更新為 {created_date}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()



# ══════════════════════════════════════════
#  ✅ 後台：取消永久授權（改回指定到期日）
# ══════════════════════════════════════════
@app.route('/admin/cancel_permanent', methods=['POST'])
def cancel_permanent():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    content     = request.json or {}
    tax_id      = content.get("tax_id", "").strip()
    expired_str = content.get("expired_date", "").strip()

    if not tax_id or not expired_str:
        return jsonify({"error": "缺少統編或到期日"}), 400

    try:
        datetime.strptime(expired_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "日期格式錯誤，請使用 YYYY-MM-DD"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE license_manager SET expired_date = %s "
                "WHERE client_id = %s",
                (expired_str, tax_id)
            )
        return jsonify({"status": "success",
                        "message": f"{tax_id} 永久授權已取消，到期日改為 {expired_str}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ══════════════════════════════════════════
#  ✅ 後台：刪除客戶（同時刪除設備資料）
# ══════════════════════════════════════════
@app.route('/admin/delete_license', methods=['POST'])
def delete_license():
    if not check_admin(request):
        return jsonify({"error": "無管理員權限"}), 403

    tax_id = (request.json or {}).get("tax_id", "").strip()
    if not tax_id:
        return jsonify({"error": "缺少統編"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 先刪除該客戶的設備資料
            cursor.execute(
                "DELETE FROM equipment_master WHERE client_id = %s",
                (tax_id,)
            )
            eq_count = cursor.rowcount
            # 再刪除授權記錄
            cursor.execute(
                "DELETE FROM license_manager WHERE client_id = %s",
                (tax_id,)
            )
        return jsonify({
            "status":  "success",
            "message": f"{tax_id} 已刪除，同時清除 {eq_count} 筆設備資料"
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
