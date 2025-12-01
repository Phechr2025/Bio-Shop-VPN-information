import io
import json
import re
import sqlite3
import base64

import qrcode
import requests
from flask import Flask, render_template, request, redirect

DB_PATH = "config.db"

app = Flask(__name__)


# ------------- DB helpers -------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_base_url TEXT NOT NULL,
            panel_username TEXT NOT NULL,
            panel_password TEXT NOT NULL,
            sub_template TEXT NOT NULL
        )
        """
    )
    cur.execute("SELECT id FROM config LIMIT 1")
    if cur.fetchone() is None:
        # ค่าเริ่มต้นให้เพชรแก้ในหน้า /admin เอง
        cur.execute(
            "INSERT INTO config (panel_base_url, panel_username, panel_password, sub_template) "
            "VALUES (?, ?, ?, ?)",
            (
                "http://st.fr.bio-th.shop:7899",  # ตัวอย่าง base url panel
                "admin",
                "password",
                "http://ph.patron.org.cnc.internet.inc.cloudflare.net.fr.bio-th.shop:2096/sub/{email}",
            ),
        )
    conn.commit()
    conn.close()


def get_config():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT panel_base_url, panel_username, panel_password, sub_template FROM config LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "panel_base_url": row[0],
        "panel_username": row[1],
        "panel_password": row[2],
        "sub_template": row[3],
    }


def set_config(panel_base_url, panel_username, panel_password, sub_template):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM config")
    cur.execute(
        "INSERT INTO config (panel_base_url, panel_username, panel_password, sub_template) "
        "VALUES (?, ?, ?, ?)",
        (panel_base_url.strip(), panel_username.strip(), panel_password.strip(), sub_template.strip()),
    )
    conn.commit()
    conn.close()


# ------------- Helper functions -------------
def generate_qr_data_uri(text: str) -> str:
    """สร้าง QR code แล้วคืนค่าเป็น data URI สำหรับ img src"""
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def login_and_get_session(cfg):
    """ล็อกอินเข้า 3x-ui และคืนค่า requests.Session ที่ล็อกอินแล้ว"""
    s = requests.Session()
    base = cfg["panel_base_url"].rstrip("/")
    login_url = f"{base}/login"
    data = {"username": cfg["panel_username"], "password": cfg["panel_password"]}
    resp = s.post(login_url, data=data, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    # ถ้าล็อกอินสำเร็จจะได้ cookie ติดอยู่ใน session
    return s


def fetch_inbounds(session, cfg):
    base = cfg["panel_base_url"].rstrip("/")
    # เส้นทางมาตรฐานของ 3x-ui หลาย ๆ ตัว
    candidates = [
        f"{base}/panel/api/inbounds",
        f"{base}/xui/inbound/list",  # เผื่อบางฟอร์คใช้ path นี้
    ]
    last_err = None
    for url in candidates:
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                # บางตัวห่ออยู่ใน key "obj"
                if isinstance(data, dict) and "obj" in data:
                    return data["obj"]
                return data
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError("ไม่พบ API inbounds ที่ใช้งานได้")


def find_client_by_id(inbounds, client_id: str):
    """ค้นหา client ใน inbounds โดยใช้ id ตรง ๆ"""
    client_info = None
    inbound_info = None
    traffic_info = None

    for inbound in inbounds:
        # client list อาจอยู่ใน key 'clients' หรือใน settings JSON
        clients = inbound.get("clients")
        if clients is None:
            settings = inbound.get("settings")
            if isinstance(settings, str):
                try:
                    settings_json = json.loads(settings)
                    clients = settings_json.get("clients")
                except Exception:
                    clients = None
        if not clients:
            continue

        # clientStats สำหรับ usage
        stats = inbound.get("clientStats", []) or inbound.get("clientstats", [])

        for c in clients:
            if str(c.get("id")) == client_id:
                client_info = c
                inbound_info = inbound
                # หา traffic ตรง id เดียวกัน
                for st in stats:
                    if str(st.get("id")) == client_id:
                        traffic_info = st
                        break
                return client_info, inbound_info, traffic_info

    return None, None, None


def fetch_sub_configs(sub_url: str):
    try:
        resp = requests.get(sub_url, timeout=15)
        resp.raise_for_status()
    except Exception:
        return sub_url, []
    text = resp.text
    # split ตามบรรทัด แล้วกรองเฉพาะโปรโตคอลที่สนใจ
    configs = []
    pattern = re.compile(r"^(vmess|vless|trojan|ss)://", re.IGNORECASE)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if pattern.match(line):
            configs.append(line)
    return text, configs


# ------------- Routes -------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/lookup", methods=["POST"])
def lookup():
    client_id = request.form.get("client_id", "").strip()
    if not client_id:
        return render_template("index.html", error="กรุณากรอก Client ID")

    cfg = get_config()
    if not cfg:
        return render_template("index.html", error="ยังไม่ได้ตั้งค่าในหน้า Admin")

    try:
        session = login_and_get_session(cfg)
        inbounds = fetch_inbounds(session, cfg)
        client, inbound, traffic = find_client_by_id(inbounds, client_id)

        if not client:
            return render_template(
                "index.html",
                error=f"ไม่พบ Client ID นี้ใน 3x-ui: {client_id}",
            )

        # เตรียมข้อมูลแสดงผล
        email = client.get("email", "")
        enable = client.get("enable", True)
        flow = client.get("flow", "")
        total_gb = client.get("totalGB") or client.get("total") or ""
        expiry_time = client.get("expiryTime") or client.get("expiry") or 0
        limit_ip = client.get("limitIp") or client.get("ipLimit") or ""

        usage_up = usage_down = usage_total = ""
        if traffic:
            usage_up = traffic.get("up", "")
            usage_down = traffic.get("down", "")
            usage_total = traffic.get("total", "")

        inbound_proto = inbound.get("protocol", "")
        inbound_remark = inbound.get("remark", "")
        listen = inbound.get("listen", "")
        port = inbound.get("port", "")

        # เตรียม Subscription URL จาก template
        sub_url = ""
        if cfg.get("sub_template"):
            try:
                sub_url = cfg["sub_template"].format(id=client_id, email=email)
            except Exception:
                sub_url = cfg["sub_template"]

        # ดึง config จาก sub (ถ้าตั้งค่า)
        sub_raw = ""
        configs = []
        if sub_url:
            sub_raw, configs = fetch_sub_configs(sub_url)

        qr_data_uri = generate_qr_data_uri(sub_url) if sub_url else ""

        data = {
            "client_id": client_id,
            "email": email,
            "enable": enable,
            "flow": flow,
            "total_gb": total_gb,
            "expiry_time": expiry_time,
            "limit_ip": limit_ip,
            "usage_up": usage_up,
            "usage_down": usage_down,
            "usage_total": usage_total,
            "inbound_proto": inbound_proto,
            "inbound_remark": inbound_remark,
            "listen": listen,
            "port": port,
            "sub_url": sub_url,
            "sub_raw": sub_raw,
            "configs": configs,
            "qr_data_uri": qr_data_uri,
        }

        return render_template("result.html", data=data)

    except Exception as e:
        return render_template("index.html", error=f"เกิดข้อผิดพลาด: {e}")


@app.route("/admin", methods=["GET", \"POST\"])
def admin():
    ADMIN_PASSWORD = "1234"

    cfg = get_config()
    message = None

    if request.method == "POST":
        pwd = request.form.get("admin_password", "")
        if pwd != ADMIN_PASSWORD:
            message = "รหัสผ่านแอดมินไม่ถูกต้อง"
        else:
            panel_base_url = request.form.get("panel_base_url", "")
            panel_username = request.form.get("panel_username", "")
            panel_password = request.form.get("panel_password", "")
            sub_template = request.form.get("sub_template", "")

            if not panel_base_url or not panel_username or not panel_password:
                message = "กรุณากรอกข้อมูลให้ครบ"
            else:
                set_config(panel_base_url, panel_username, panel_password, sub_template)
                cfg = get_config()
                message = "บันทึกการตั้งค่าเรียบร้อยแล้ว"

    return render_template("admin.html", cfg=cfg, message=message)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=10000, debug=True)
