import re
import sqlite3
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect

app = Flask(__name__)


# ------------- Database helpers -------------
DB_PATH = "config.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS baseurl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL
        )
        """
    )
    # ถ้ายังไม่มี base url ให้ใส่ค่าตัวอย่างไว้ก่อน (แก้ในหน้า /admin ได้)
    cur.execute("SELECT url FROM baseurl LIMIT 1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO baseurl (url) VALUES (?)",
            ("http://ph.patron.org.cnc.internet.inc.cloudflare.net.fr.bio-th.shop:2096/sub",),
        )
    conn.commit()
    conn.close()


def get_base_url():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url FROM baseurl LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ""


def set_base_url(url: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM baseurl")
    cur.execute("INSERT INTO baseurl (url) VALUES (?)", (url.strip(),))
    conn.commit()
    conn.close()


# ------------- Scraper -------------
def parse_subscription_page(html: str):
    """
    พยายามดึงข้อมูลจากหน้า subscription แบบ generic ที่หน้าตาเหมือนในรูป
    - หา QR code รูปแรกในหน้า
    - หา text: Subscription ID, Status, Downloaded, Uploaded, Usage,
      Total quota, Last Online, Expiry
    - ดึงลิงก์ vmess://, vless://, trojan://, ss:// ทั้งหมด
    """
    soup = BeautifulSoup(html, "html.parser")

    # QR code: เอา <img> แรกในหน้า
    qr_img = None
    img = soup.find("img")
    if img and img.get("src"):
        qr_img = img["src"]

    def find_value(label_text):
        # หา element ที่มีข้อความ label_text แล้วเอา text ถัดไป
        el = soup.find(string=lambda t: t and label_text in t)
        if not el:
            return ""
        # กรณีอยู่ใน <td> หรือ <th>
        parent = el.parent
        # ถ้าเป็นเซลล์ในตารางให้เอา td/th ถัดไป
        if parent.name in ["td", "th"]:
            nxt = parent.find_next_sibling(["td", "th"])
            if nxt:
                return nxt.get_text(strip=True)
        # ถ้าไม่ใช่ ให้หาถัดไปใน DOM
        nxt = parent.find_next(string=True)
        if nxt:
            return nxt.strip()
        return ""

    data = {
        "qrcode": qr_img or "",
        "subscription_id": find_value("Subscription ID"),
        "status": find_value("Status"),
        "downloaded": find_value("Downloaded"),
        "uploaded": find_value("Uploaded"),
        "usage": find_value("Usage"),
        "total_quota": find_value("Total quota"),
        "last_online": find_value("Last Online"),
        "expiry": find_value("Expiry"),
    }

    # ดึง config : vmess / vless / trojan / ss
    pattern = re.compile(r"(vmess|vless|trojan|ss)://[^\s\"'<]+")
    configs = pattern.findall(html)
    # pattern.findall คืนเฉพาะชื่อโปรโตคอล ถ้าใช้ group; ใช้ finditer แทน
    configs = [m.group(0) for m in pattern.finditer(html)]
    data["configs"] = configs

    return data


def fetch_subscription(sub_id: str):
    base = get_base_url()
    if not base:
        raise RuntimeError("ยังไม่ได้ตั้งค่า Base URL ในหน้าแอดมิน")

    base = base.rstrip("/")  # กัน / ซ้ำ
    # ถ้า base ลงท้ายด้วย /sub ให้ต่อ id ต่อท้าย
    url = f"{base}/{sub_id}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return parse_subscription_page(resp.text)


# ------------- Routes -------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/lookup", methods=["POST"])
def lookup():
    sub_id = request.form.get("sub_id", "").strip()
    if not sub_id:
        return render_template("index.html", error="กรุณากรอก Subscription ID")

    try:
        data = fetch_subscription(sub_id)
        return render_template("result.html", data=data, sub_id=sub_id)
    except Exception as e:
        return render_template("index.html", error=f"เกิดข้อผิดพลาด: {e}")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    ADMIN_PASSWORD = "1234"  # สามารถแก้ทีหลังได้

    msg = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        new_url = request.form.get("base_url", "").strip()
        if pwd != ADMIN_PASSWORD:
            msg = "รหัสผ่านไม่ถูกต้อง"
        else:
            if not new_url:
                msg = "กรุณากรอก Base URL"
            else:
                set_base_url(new_url)
                msg = "บันทึก Base URL เรียบร้อยแล้ว"

    return render_template("admin.html", base_url=get_base_url(), message=msg)


# ------------- Main -------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=10000, debug=True)
