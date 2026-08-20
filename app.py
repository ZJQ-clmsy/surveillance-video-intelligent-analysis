import csv
import io
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import cv2
from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory
from ultralytics import YOLO

try:
    from openpyxl import Workbook
except Exception:
    Workbook = None

app = Flask(__name__)

# ================= 配置 =================
# 按你的实际模型路径修改
MODEL_PATH = r"C:\Users\Administrator\Desktop\final1\models\aqm.pt"

# 建议将图片根目录放在 static 文件夹下，否则网页无法直接通过 URL 访问
STATIC_ROOT = os.path.join(app.root_path, "static")
IMG_ROOT = os.path.join(STATIC_ROOT, "picture")
ILLEGAL_ROOT = os.path.join(STATIC_ROOT, "illegal")
LOG_FILE = os.path.join(STATIC_ROOT, "records.csv")

CAPTURE_INTERVAL_SECONDS = 3

os.makedirs(IMG_ROOT, exist_ok=True)
os.makedirs(ILLEGAL_ROOT, exist_ok=True)
os.makedirs(STATIC_ROOT, exist_ok=True)

model = YOLO(MODEL_PATH)
camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
#camera = cv2.VideoCapture(0, cv2.CAP_MSMF)

# ================= 工具函数 =================
def normalize_date(date_text: Optional[str]) -> Optional[str]:
    """支持 YYYY-MM-DD 和 YYYY.MM.DD，统一转为 YYYY.MM.DD。"""
    if not date_text:
        return None
    text = date_text.strip().replace("-", ".")
    try:
        return datetime.strptime(text, "%Y.%m.%d").strftime("%Y.%m.%d")
    except ValueError:
        return None


def parse_date(date_text: str) -> Optional[datetime]:
    date_text = date_text.replace("-", ".")
    try:
        return datetime.strptime(date_text, "%Y.%m.%d")
    except ValueError:
        return None


def in_date_range(date_str: str, start_date: Optional[str], end_date: Optional[str]) -> bool:
    current = parse_date(date_str)
    start = parse_date(start_date) if start_date else None
    end = parse_date(end_date) if end_date else None
    if current is None:
        return False
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def static_rel_path(abs_path: str) -> str:
    return os.path.relpath(abs_path, STATIC_ROOT).replace("\\", "/")


def ensure_log_header() -> None:
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["date", "time", "is_illegal", "image_path", "violation_type"],
            )
            writer.writeheader()


def append_log(date_str: str, time_str: str, is_illegal: bool, image_path: str) -> None:
    ensure_log_header()
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "time", "is_illegal", "image_path", "violation_type"],
        )
        writer.writerow(
            {
                "date": date_str,
                "time": time_str.replace(".", ":"),
                "is_illegal": "1" if is_illegal else "0",
                "image_path": image_path,
                "violation_type": "未佩戴安全帽" if is_illegal else "无",
            }
        )


def read_logs() -> List[Dict[str, str]]:
    """优先读取 CSV 日志；如果没有日志，则从旧图片目录生成兼容记录。"""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        rows.sort(key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True)
        return rows

    records: List[Dict[str, str]] = []

    for root, _, files in os.walk(IMG_ROOT):
        date_str = os.path.basename(root)
        if not parse_date(date_str):
            continue
        for filename in files:
            if filename.lower().endswith((".jpg", ".jpeg", ".png")):
                time_part = os.path.splitext(filename)[0].replace(".", ":")
                rel = static_rel_path(os.path.join(root, filename))
                records.append(
                    {
                        "date": date_str,
                        "time": time_part,
                        "is_illegal": "0",
                        "image_path": rel,
                        "violation_type": "无",
                    }
                )

    illegal_set = set()
    for root, _, files in os.walk(ILLEGAL_ROOT):
        date_str = os.path.basename(root)
        if not parse_date(date_str):
            continue
        for filename in files:
            if filename.lower().endswith((".jpg", ".jpeg", ".png")):
                rel = static_rel_path(os.path.join(root, filename))
                illegal_set.add(rel)
                time_part = os.path.splitext(filename)[0].replace("warn_", "").replace(".", ":")
                records.append(
                    {
                        "date": date_str,
                        "time": time_part,
                        "is_illegal": "1",
                        "image_path": rel,
                        "violation_type": "未佩戴安全帽",
                    }
                )

    records.sort(key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True)
    return records


def filter_logs(records: List[Dict[str, str]], start_date: Optional[str], end_date: Optional[str], only_illegal: bool = False) -> List[Dict[str, str]]:
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    result = []
    for row in records:
        if only_illegal and row.get("is_illegal") != "1":
            continue
        if in_date_range(row.get("date", ""), start, end):
            result.append(row)
    return result


def count_from_logs(records: List[Dict[str, str]]) -> Dict[str, int]:
    total = len(records)
    illegal = sum(1 for row in records if row.get("is_illegal") == "1")
    safe = max(0, total - illegal)
    return {"total": total, "illegal": illegal, "safe": safe}


def last_seven_days_labels() -> List[str]:
    today = datetime.now()
    days = []
    for offset in range(6, -1, -1):
        days.append((today - timedelta(days=offset)).strftime("%Y.%m.%d"))
    return days


# ================= 视频流与抓拍 =================
def generate_frames(detect: bool = False):
    last_screenshot_time = time.time()
    while True:
        success, frame = camera.read()
        if not success:
            break

        curr_time = datetime.now()
        cv2.putText(
            frame,
            curr_time.strftime("%Y-%m-%d %H:%M:%S"),
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 255, 255),
            2,
        )

        if detect:
            results = model(frame, verbose=False)
            has_nowear = False
            for result in results:
                for box in result.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    cls_name = model.names[int(box.cls[0])]
                    conf = float(box.conf[0])
                    color = (0, 255, 0) if cls_name == "wear" else (0, 0, 255)
                    if cls_name == "nowear":
                        has_nowear = True
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
                    cv2.putText(
                        frame,
                        f"{cls_name} {conf:.2f}",
                        (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        color,
                        2,
                    )

            if time.time() - last_screenshot_time >= CAPTURE_INTERVAL_SECONDS:
                save_img(frame, has_nowear)
                last_screenshot_time = time.time()

        ret, buffer = cv2.imencode(".jpg", frame)
        if not ret:
            continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"


def save_img(frame, is_illegal: bool) -> None:
    now = datetime.now()
    date_str = now.strftime("%Y.%m.%d")
    time_str = now.strftime("%H.%M.%S")

    p_path = os.path.join(IMG_ROOT, date_str)
    os.makedirs(p_path, exist_ok=True)
    normal_abs = os.path.join(p_path, f"{time_str}.jpg")
    cv2.imwrite(normal_abs, frame)
    normal_rel = static_rel_path(normal_abs)

    log_path = normal_rel
    if is_illegal:
        i_path = os.path.join(ILLEGAL_ROOT, date_str)
        os.makedirs(i_path, exist_ok=True)
        illegal_abs = os.path.join(i_path, f"warn_{time_str}.jpg")
        cv2.imwrite(illegal_abs, frame)
        log_path = static_rel_path(illegal_abs)

    append_log(date_str, time_str, is_illegal, log_path)


# ================= 页面路由 =================
@app.route("/")
def index():
    template_path = os.path.join(app.root_path, "templates", "index.html")
    if os.path.exists(template_path):
        return render_template("index.html")
    return send_from_directory(app.root_path, "index.html")


@app.route("/video_feed")
def video_feed():
    return Response(generate_frames(detect=False), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/detect_feed")
def detect_feed():
    return Response(generate_frames(detect=True), mimetype="multipart/x-mixed-replace; boundary=frame")


# ================= API：统计分析 =================
@app.route("/api/stats")
def get_stats():
    records = read_logs()
    counts = count_from_logs(records)
    total = counts["total"]
    illegal = counts["illegal"]
    safe = counts["safe"]
    violation_rate = round((illegal / total * 100), 2) if total else 0
    compliance_rate = round((safe / total * 100), 2) if total else 0
    today = datetime.now().strftime("%Y.%m.%d")
    today_illegal = sum(1 for row in records if row.get("date") == today and row.get("is_illegal") == "1")
    seven_days = last_seven_days_labels()
    daily_illegal_map = {day: 0 for day in seven_days}
    all_daily_illegal: Dict[str, int] = {}
    for row in records:
        if row.get("is_illegal") == "1":
            day = row.get("date", "")
            all_daily_illegal[day] = all_daily_illegal.get(day, 0) + 1
            if day in daily_illegal_map:
                daily_illegal_map[day] += 1

    daily_ranking = [
        {"date": day, "count": count}
        for day, count in sorted(all_daily_illegal.items(), key=lambda item: item[1], reverse=True)
    ]

    return jsonify(
        {
            "total": total,
            "safe": safe,
            "illegal": illegal,
            "violation_rate": violation_rate,
            "compliance_rate": compliance_rate,
            "today_illegal": today_illegal,
            "seven_days_trend": {
                "labels": seven_days,
                "values": [daily_illegal_map[day] for day in seven_days],
            },
            "daily_ranking": daily_ranking[:10],
        }
    )


# ================= API：模块3 违规照片库 =================
@app.route("/api/illegal_images")
def get_illegal_images():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    records = filter_logs(read_logs(), start_date, end_date, only_illegal=True)
    images = [
        {
            "date": row.get("date", ""),
            "time": row.get("time", ""),
            "path": row.get("image_path", ""),
            "violation_type": row.get("violation_type", "未佩戴安全帽"),
        }
        for row in records
    ]
    return jsonify(images)


# ================= API：模块5 系统日志 =================
@app.route("/api/logs")
def get_logs():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    only_illegal = request.args.get("only_illegal", "0") == "1"
    records = filter_logs(read_logs(), start_date, end_date, only_illegal=only_illegal)
    return jsonify(records[:500])


# ================= API：模块6 今日告警 =================
@app.route("/api/today_alerts")
def get_today_alerts():
    today = datetime.now().strftime("%Y.%m.%d")
    records = filter_logs(read_logs(), today, today, only_illegal=True)
    return jsonify({"date": today, "count": len(records), "records": records})


# ================= API：模块7 数据导出 =================
@app.route("/api/export")
def export_records():
    export_type = request.args.get("type", "csv").lower()
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    only_illegal = request.args.get("only_illegal", "1") == "1"

    records = filter_logs(read_logs(), start_date, end_date, only_illegal=only_illegal)
    fields = ["date", "time", "image_path", "violation_type"]

    if export_type == "xlsx":
        if Workbook is None:
            return jsonify({"error": "当前环境未安装 openpyxl，无法导出 Excel，请先 pip install openpyxl。"}), 500
        wb = Workbook()
        ws = wb.active
        ws.title = "违规记录"
        ws.append(["日期", "时间", "图片路径", "违规类型"])
        for row in records:
            ws.append([row.get(field, "") for field in fields])
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        filename = f"helmet_violation_records_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日期", "时间", "图片路径", "违规类型"])
    for row in records:
        writer.writerow([row.get(field, "") for field in fields])
    data = output.getvalue().encode("utf-8-sig")
    filename = f"helmet_violation_records_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=filename,
        mimetype="text/csv; charset=utf-8",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
