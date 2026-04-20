import os
from flask import Flask, request, jsonify
import pymysql
from datetime import datetime

app = Flask(__name__)

# --- TiDB 連線函式 ---
def get_db_connection():
    return pymysql.connect(
        host=os.getenv("TIDB_HOST"),
        port=int(os.getenv("TIDB_PORT", 4000)),
        uuser=os.getenv("TIDB_USERNAME"),,
        password=os.getenv("TIDB_PASSWORD"),
        database=os.getenv("TIDB_DB"),
        ssl_verify_cert=True,
        autocommit=True
    )

@app.route('/')
def home():
    return "保固系統 API 運行中"

@app.route('/sync', methods=['POST'])
def sync_data():
    content = request.json
    received_key = content.get("key")
    data_list = content.get("data")

    if not received_key or data_list is None:
        return jsonify({"error": "缺少金鑰或資料"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # 1. 驗證金鑰並取得 client_id
            cursor.execute(
                "SELECT client_id FROM license_manager WHERE license_key = %s AND is_active = TRUE",
                (received_key,)
            )
            result = cursor.fetchone()
            if not result:
                return jsonify({"error": "無效或已停用的授權金鑰"}), 403
            
            client_id = result[0]

            # 2. 清除舊資料並寫入新資料
            cursor.execute("DELETE FROM equipment_master WHERE client_id = %s", (client_id,))
            
            sql = """
                INSERT INTO equipment_master 
                (client_id, school, classroom, brand, device_name, model, serial, mac_address, finish_date, warranty_years)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            for row in data_list:
                # 確保 PC 端傳來的 row 長度符合
                cursor.execute(sql, (client_id, *row))
            
        return jsonify({"status": "success", "client": client_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
