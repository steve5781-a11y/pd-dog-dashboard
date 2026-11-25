"""PD-Dog 돌봄 대시보드 서버.

파이썬 기본 기능만 사용해서 pip 설치 없이 PC와 라즈베리파이에서 똑같이 돌아가요.

처음 한 번:  python server.py --set-password   (관리자 아이디·비밀번호 정하기)
실행:  python server.py                    (이 PC에서만 접속)
       python server.py --host 0.0.0.0    (같은 와이파이의 휴대폰·PC에서도 접속)
"""

import argparse
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
HTML = "text/html; charset=utf-8"

# --- 로그인 ---
ADMIN_FILE = "admin.json"          # 관리자 아이디 + 비밀번호 해시 (비밀번호 원래 글자는 저장 안 함)
ROBOT_KEY_FILE = "robot_key.txt"   # 로봇 전용 열쇠. 로봇은 로그인 대신 이 값을 X-Robot-Key 헤더로 보내요
SESSION_COOKIE = "pddog_session"
SESSION_SECONDS = 30 * 24 * 3600   # 한 번 로그인하면 30일 유지 (대시보드를 쓰는 동안 자동 연장)
PBKDF2_ROUNDS = 200_000
LOGIN_MAX_FAILS = 5                # 같은 기기에서 이만큼 틀리면
LOGIN_LOCK_SECONDS = 60            # 이 시간 동안 로그인 막기
ROBOT_ROUTES = {                   # 로봇 키로도 쓸 수 있는 주소 (나머지는 로그인해야만)
    ("POST", "/api/falls"), ("POST", "/api/robot/status"), ("POST", "/api/robot/pose"),
    ("GET", "/api/settings"),
}
PAGES = {  # 주소 → (파일, 종류)
    "/": ("index.html", "text/html; charset=utf-8"),
    "/report": ("report.html", "text/html; charset=utf-8"),
    "/settings": ("settings.html", "text/html; charset=utf-8"),
    "/demo-data.js": ("demo-data.js", "text/javascript; charset=utf-8"),
}

# 알람 신호를 보낼 수 있는 곳 (PRD 기준):
#   camera=F6 카메라 낙상, no_motion=F7 무움직임, touch=F2 터치 3초 신고,
#   voice=F2 음성 "도와줘", medication_missed=F1 복약 30분 미확인, test=테스트,
#   low_battery=배터리 부족, robot_tipped=로봇이 넘어짐 (이 둘은 로봇 상태를 보고 서버가 만들어요)
SOURCES = {"camera", "no_motion", "touch", "voice", "medication_missed", "test", "low_battery", "robot_tipped"}
DUPLICATE_WINDOW = timedelta(seconds=30)  # 같은 신호가 반복 전송돼도 알람이 쌓이지 않게
LOW_BATTERY_PCT = 20                      # 이보다 낮으면 '배터리 부족' 알람
AUTO_ALARM_QUIET = {                      # 자동 알람은 확인한 뒤에도 이 시간 동안 다시 울리지 않게
    "low_battery": timedelta(minutes=30),
    "robot_tipped": timedelta(minutes=5),
}
ROBOT_TIMEOUT = 10                        # 로봇 신호가 이 초 동안 없으면 '연결 끊김'
ROBOT_MODES = {"idle", "walking", "voice", "medication", "approaching", "charging"}
POSE_STATES = {"none", "standing", "sitting", "falling", "lying"}
KEYPOINTS = 17                            # YOLO 자세 추정의 관절 점 개수 (COCO 형식)
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
DEFAULT_SETTINGS = {
    "doses": [["08:00", "아침 약"], ["12:30", "점심 약"], ["18:00", "저녁 약"], ["22:00", "자기 전 약"]],
    "walk_bpm": 96,          # F3 목표 박자 (보/분, 80~110)
    "voice_target_db": 68,   # F4 목표 목소리 크기 (dB)
    "follow_max_cm": 150,    # F3 이보다 멀어지면 로봇이 기다려요
}
SETTING_RANGES = {"walk_bpm": (80, 110), "voice_target_db": (50, 90), "follow_max_cm": (50, 300)}
MAX_BODY = 4096                           # 요청 본문 최대 크기(바이트)
MAX_NOTE = 200                            # 확인 메모 최대 글자 수
RECENT_LIMIT = 50                         # 기록 표에 보여줄 최근 건수
REPORT_DAYS = 7                           # 주간 리포트 기간
REPORT_LIMIT = 2000                       # 리포트에 담는 최대 건수
ACK_PATH = re.compile(r"/api/falls/(\d{1,18})/ack")


def now_iso(delta=timedelta(0)):
    # 모든 시각을 같은 형식(UTC)으로 저장해야 문자열 비교로 시간 순서를 비교할 수 있어요.
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="milliseconds")


def to_event(row):
    event = dict(row)
    event["simulated"] = bool(event["simulated"])
    return event


class FallStore:
    """알람 기록을 SQLite 파일에 저장하고 꺼내요."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()  # 여러 요청이 동시에 와도 한 번에 하나씩 처리
        with self.lock, self.db:
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS falls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    confidence REAL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    acknowledged_at TEXT
                )"""
            )
            # 예전 버전에서 만든 기록 파일에도 새 칸을 붙여요 (기존 기록은 그대로).
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(falls)")}
            if "note" not in columns:
                self.db.execute("ALTER TABLE falls ADD COLUMN note TEXT")
            if "simulated" not in columns:
                self.db.execute("ALTER TABLE falls ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                " id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def _get(self, event_id):
        row = self.db.execute("SELECT * FROM falls WHERE id = ?", (event_id,)).fetchone()
        return to_event(row) if row else None

    def add(self, source, confidence, simulated=False):
        """새 알람을 저장해요. 30초 안에 같은 곳에서 온 미확인 알람이 있으면 그걸 돌려줘요."""
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM falls WHERE source = ? AND status = 'new' AND created_at >= ?"
                " ORDER BY id DESC LIMIT 1",
                (source, now_iso(DUPLICATE_WINDOW)),
            ).fetchone()
            if row:
                return to_event(row), True
            cur = self.db.execute(
                "INSERT INTO falls (source, confidence, created_at, simulated) VALUES (?, ?, ?, ?)",
                (source, confidence, now_iso(), int(simulated)),
            )
            return self._get(cur.lastrowid), False

    def add_if_quiet(self, source, simulated=False):
        """로봇 상태를 보고 서버가 만드는 자동 알람. 최근에 같은 알람이 있었으면(확인했어도) 만들지 않아요."""
        with self.lock, self.db:
            recent = self.db.execute(
                "SELECT 1 FROM falls WHERE source = ? AND created_at >= ? LIMIT 1",
                (source, now_iso(AUTO_ALARM_QUIET[source])),
            ).fetchone()
            if recent:
                return None
            cur = self.db.execute(
                "INSERT INTO falls (source, confidence, created_at, simulated) VALUES (?, NULL, ?, ?)",
                (source, now_iso(), int(simulated)),
            )
            return self._get(cur.lastrowid)

    def get_settings(self):
        with self.lock:
            row = self.db.execute("SELECT value, updated_at FROM settings WHERE id = 1").fetchone()
        if not row:
            return dict(DEFAULT_SETTINGS, updated_at=None)
        return dict(json.loads(row["value"]), updated_at=row["updated_at"])

    def save_settings(self, settings):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO settings (id, value, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(settings, ensure_ascii=False), now_iso()),
            )
        return self.get_settings()

    def acknowledge(self, event_id, note=None):
        """보호자가 '확인'을 누른 알람을 처리해요. 이미 확인된 건이면 첫 확인 시각·메모를 그대로 둬요."""
        with self.lock, self.db:
            self.db.execute(
                "UPDATE falls SET status = 'acknowledged', acknowledged_at = ?, note = ?"
                " WHERE id = ? AND status = 'new'",
                (now_iso(), note, event_id),
            )
            return self._get(event_id)

    def listing(self):
        """최근 50건 + 그보다 오래됐어도 아직 확인 안 된 알람 전부 (알람을 놓치지 않게)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM falls WHERE status = 'new'"
                " OR id IN (SELECT id FROM falls ORDER BY id DESC LIMIT ?)"
                " ORDER BY id DESC",
                (RECENT_LIMIT,),
            ).fetchall()
            unacknowledged = self.db.execute(
                "SELECT COUNT(*) FROM falls WHERE status = 'new'"
            ).fetchone()[0]
        return [to_event(r) for r in rows], unacknowledged

    def report(self):
        """주간 리포트용: 최근 7일(+시간대 여유 1일) 알람. 날짜별 묶기는 브라우저가 현지 시간으로 해요."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM falls WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                (now_iso(timedelta(days=REPORT_DAYS + 1)), REPORT_LIMIT + 1),
            ).fetchall()
        return [to_event(r) for r in rows[:REPORT_LIMIT]], len(rows) > REPORT_LIMIT

    def close(self):
        self.db.close()


class RobotState:
    """로봇(라즈베리파이)이 보낸 가장 최근 상태와 자세. 기록할 필요가 없어 메모리에만 둬요."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = {}   # 이름 → (값, 받은 시각 ISO, 받은 순간 monotonic)

    def put(self, name, value):
        with self.lock:
            self.latest[name] = (value, now_iso(), time.monotonic())

    def get(self, name):
        with self.lock:
            value, received_at, mono = self.latest.get(name, (None, None, None))
        age = None if mono is None else round(time.monotonic() - mono, 1)
        return {name: value, "received_at": received_at, "age_seconds": age,
                "online": age is not None and age <= ROBOT_TIMEOUT}


def hash_password(password, salt, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds).hex()


def set_password(auth_dir, username, password):
    """관리자 아이디·비밀번호를 저장해요. 비밀번호는 되돌릴 수 없는 해시로만 남겨요.
    입장권 서명 열쇠(secret)도 새로 만들어서, 이미 로그인해 둔 기기는 모두 로그아웃돼요."""
    salt = secrets.token_bytes(16)
    data = {"username": username, "salt": salt.hex(), "rounds": PBKDF2_ROUNDS,
            "hash": hash_password(password, salt), "secret": secrets.token_hex(32)}
    (Path(auth_dir) / ADMIN_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")


def same(a, b):
    """두 글자를 비교하는 데 걸리는 시간으로 정답을 알아낼 수 없게 비교해요."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class Auth:
    """관리자 로그인 + 로봇 키 확인."""

    def __init__(self, auth_dir):
        auth_dir = Path(auth_dir)
        data = json.loads((auth_dir / ADMIN_FILE).read_text(encoding="utf-8"))  # 없으면 FileNotFoundError
        self.username = data["username"]
        self.salt = bytes.fromhex(data["salt"])
        self.rounds = data["rounds"]
        self.hash = data["hash"]
        self.secret = bytes.fromhex(data["secret"])
        key_file = auth_dir / ROBOT_KEY_FILE
        if not key_file.exists():
            key_file.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
        self.robot_key = key_file.read_text(encoding="utf-8").strip()
        self.lock = threading.Lock()
        self.fails = {}   # 기기 IP → (틀린 횟수, 잠금이 풀리는 시각)

    def login(self, ip, username, password):
        """'ok'(성공), 'wrong'(틀림), 'locked'(너무 많이 틀려서 잠시 막힘) 중 하나를 돌려줘요."""
        with self.lock:
            count, until = self.fails.get(ip, (0, 0))
            if until > time.monotonic():
                return "locked"
        ok = same(username, self.username) & same(hash_password(password, self.salt, self.rounds), self.hash)
        with self.lock:
            if ok:
                self.fails.pop(ip, None)
                return "ok"
            count += 1
            if count >= LOGIN_MAX_FAILS:
                self.fails[ip] = (0, time.monotonic() + LOGIN_LOCK_SECONDS)
                return "locked"
            self.fails[ip] = (count, 0)
            return "wrong"

    def _sign(self, expires):
        return hmac.new(self.secret, str(expires).encode(), hashlib.sha256).hexdigest()

    def new_session(self):
        """입장권(쿠키 값) = 만료시각.서명. 서명은 서버만 아는 열쇠로 만들어서 위조할 수 없어요."""
        expires = int(time.time()) + SESSION_SECONDS
        return f"{expires}.{self._sign(expires)}"

    def session_left(self, token):
        """올바른 입장권이면 남은 초, 아니면 None."""
        expires, _, sig = token.partition(".")
        if not re.fullmatch(r"\d{1,12}", expires) or not same(sig, self._sign(int(expires))):
            return None
        left = int(expires) - time.time()
        return left if left > 0 else None

    def robot_ok(self, key):
        return bool(key) and same(key, self.robot_key)


class BadRequest(Exception):
    pass


def parse_json_object(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BadRequest("본문이 올바른 JSON이 아니에요")
    if not isinstance(data, dict):
        raise BadRequest("본문은 JSON 객체여야 해요")
    return data


def parse_fall(body):
    """알람 요청을 검사해서 (source, confidence, simulated)를 돌려줘요. 잘못되면 BadRequest."""
    data = parse_json_object(body)
    source = data.get("source")
    if source not in SOURCES:
        raise BadRequest("source는 " + ", ".join(sorted(SOURCES)) + " 중 하나여야 해요")
    confidence = data.get("confidence")
    if confidence is not None:
        # bool은 파이썬에서 숫자로 취급되므로 따로 막아요. NaN은 범위 비교에서 걸러져요.
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or not 0 <= confidence <= 1:
            raise BadRequest("confidence는 0~1 사이 숫자여야 해요")
        confidence = float(confidence)
    simulated = data.get("simulated", False)
    if not isinstance(simulated, bool):
        raise BadRequest("simulated는 true 또는 false여야 해요")
    return source, confidence, simulated


def parse_ack(body):
    """확인 요청의 메모를 꺼내요. 본문이 없어도 돼요 (메모 없이 확인)."""
    if not body.strip():
        return None
    note = parse_json_object(body).get("note")
    if note is None:
        return None
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise BadRequest(f"메모는 {MAX_NOTE}자 이하 글자여야 해요")
    return note.strip() or None


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def opt_number(data, key, lo, hi):
    value = data.get(key)
    if value is None:
        return None
    if not is_number(value) or not lo <= value <= hi:
        raise BadRequest(f"{key}는 {lo}~{hi} 사이 숫자여야 해요")
    return value


def opt_bool(data, key):
    value = data.get(key)
    if value is not None and not isinstance(value, bool):
        raise BadRequest(f"{key}는 true 또는 false여야 해요")
    return value


def parse_status(body):
    """로봇 상태 (라즈베리파이가 몇 초마다 보내요). 모르는 값은 null로 보내면 돼요."""
    data = parse_json_object(body)
    if not isinstance(data.get("esp32_ok"), bool):
        raise BadRequest("esp32_ok(로봇 몸 응답 여부)는 true 또는 false여야 해요")
    mode = data.get("mode")
    if mode is not None and mode not in ROBOT_MODES:
        raise BadRequest("mode는 " + ", ".join(sorted(ROBOT_MODES)) + " 중 하나여야 해요")
    return {
        "esp32_ok": data["esp32_ok"],
        "mode": mode,
        "dog_battery_pct": opt_number(data, "dog_battery_pct", 0, 100),     # MechDog 배터리 (ESP32가 잼)
        "pi_battery_pct": opt_number(data, "pi_battery_pct", 0, 100),       # X1200을 쓸 때만
        "pi_charging": opt_bool(data, "pi_charging"),
        "pi_temp_c": opt_number(data, "pi_temp_c", -20, 120),
        "follow_distance_cm": opt_number(data, "follow_distance_cm", 0, 500),  # ToF: 로봇 뒤 사용자 거리
        "laser_on": opt_bool(data, "laser_on"),
        "bpm": opt_number(data, "bpm", 40, 160),
        "robot_tipped": opt_bool(data, "robot_tipped"),                     # MechDog 기울기 센서
        "simulated": bool(opt_bool(data, "simulated")),
    }


def parse_pose(body):
    """카메라 자세 추정 결과. 영상 대신 관절 점 17개 [x, y, 신뢰도](0~1)만 받아요."""
    data = parse_json_object(body)
    state = data.get("state")
    if state not in POSE_STATES:
        raise BadRequest("state는 " + ", ".join(sorted(POSE_STATES)) + " 중 하나여야 해요")
    points = data.get("keypoints")
    if points is None and state == "none":
        points = []
    if not isinstance(points, list) or len(points) not in (0, KEYPOINTS) or not all(
            isinstance(p, list) and len(p) == 3 and all(is_number(v) and 0 <= v <= 1 for v in p)
            for p in points):
        raise BadRequest(f"keypoints는 [x, y, 신뢰도](0~1) {KEYPOINTS}개여야 해요")
    return {"state": state, "keypoints": points, "simulated": bool(opt_bool(data, "simulated"))}


def parse_settings(body):
    """치료사·보호자 설정. 잘못된 값은 저장하지 않아요."""
    data = parse_json_object(body)
    doses = data.get("doses")
    if not isinstance(doses, list) or not 1 <= len(doses) <= 8:
        raise BadRequest("복약 시간은 1~8개여야 해요")
    clean = []
    for dose in doses:
        if not (isinstance(dose, list) and len(dose) == 2 and all(isinstance(v, str) for v in dose)):
            raise BadRequest("복약 시간은 [\"08:00\", \"아침 약\"] 형식이어야 해요")
        hm, label = dose[0], dose[1].strip()
        if not TIME_RE.fullmatch(hm):
            raise BadRequest(f"시각 '{hm[:10]}'이 올바르지 않아요 (예: 08:00)")
        if not 1 <= len(label) <= 20:
            raise BadRequest("약 이름은 1~20자여야 해요")
        clean.append([hm, label])
    clean.sort()
    if len({hm for hm, _ in clean}) != len(clean):
        raise BadRequest("같은 시각이 두 번 들어 있어요")
    settings = {"doses": clean}
    for key, (lo, hi) in SETTING_RANGES.items():
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise BadRequest(f"{key}는 {lo}~{hi} 사이 정수여야 해요")
        settings[key] = value
    return settings


class Handler(BaseHTTPRequestHandler):
    server_version = "PDDogDashboard/2.0"

    def do_GET(self):
        self._safely(self._route_get)

    def do_POST(self):
        self._safely(self._route_post)

    # --- 로그인 확인 (문지기) ---

    def _cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _logged_in(self):
        left = self.server.auth.session_left(self._cookie(SESSION_COOKIE))
        if left is None:
            return False
        if left < SESSION_SECONDS / 2:   # 계속 쓰는 동안은 입장권을 새로 바꿔 줘서 갑자기 로그아웃되지 않게
            self._set_cookie = self._session_cookie(self.server.auth.new_session(), SESSION_SECONDS)
        return True

    def _allowed(self, method, path):
        """로그인했거나, 로봇 키를 가진 로봇이 로봇용 주소를 부를 때만 통과. 아니면 막고 False."""
        if self._logged_in():
            return True
        if (method, path) in ROBOT_ROUTES and self.server.auth.robot_ok(self.headers.get("X-Robot-Key")):
            return True
        if method == "GET" and path in PAGES:
            self._redirect("/login")
        else:
            self._json(401, {"error": "로그인이 필요해요"})
        return False

    @staticmethod
    def _session_cookie(value, max_age):
        return f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"

    def _login(self):
        form = parse_qs(self._read_body().decode("utf-8", "replace"))
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        result = self.server.auth.login(self.client_address[0], username, password)
        if result == "ok":
            self._set_cookie = self._session_cookie(self.server.auth.new_session(), SESSION_SECONDS)
            self._redirect("/")
        else:
            self.log_message("로그인 실패 (%s)", result)
            self._redirect("/login?e=" + result)

    # --- 주소별 처리 ---

    def _route_get(self):
        path = urlsplit(self.path).path
        if path == "/login":
            if self._logged_in():
                self._redirect("/")
            else:
                self._send(200, (STATIC / "login.html").read_bytes(), HTML)
            return
        if not self._allowed("GET", path):
            return
        if path in PAGES:
            name, content_type = PAGES[path]
            self._send(200, (STATIC / name).read_bytes(), content_type)
        elif path == "/api/falls":
            events, unacknowledged = self.server.store.listing()
            self._json(200, {"events": events, "unacknowledged": unacknowledged})
        elif path == "/api/report":
            events, truncated = self.server.store.report()
            self._json(200, {"events": events, "truncated": truncated, "days": REPORT_DAYS})
        elif path == "/api/robot":
            self._json(200, self.server.robot.get("status"))
        elif path == "/api/robot/pose":
            self._json(200, self.server.robot.get("pose"))
        elif path == "/api/settings":
            self._json(200, self.server.store.get_settings())
        else:
            self._json(404, {"error": "없는 주소예요"})

    def _route_post(self):
        path = urlsplit(self.path).path
        if path == "/login":
            self._login()
            return
        if path == "/logout":
            self._set_cookie = self._session_cookie("", 0)
            self._redirect("/login")
            return
        if not self._allowed("POST", path):
            return
        ack = ACK_PATH.fullmatch(path)
        if path == "/api/falls":
            source, confidence, simulated = parse_fall(self._read_body())
            event, duplicate = self.server.store.add(source, confidence, simulated)
            self._json(200 if duplicate else 201, {"event": event, "duplicate": duplicate})
        elif ack:
            note = parse_ack(self._read_body(required=False))
            event = self.server.store.acknowledge(int(ack.group(1)), note)
            if event is None:
                self._json(404, {"error": "그런 알람 기록이 없어요"})
            else:
                self._json(200, {"event": event})
        elif path == "/api/robot/status":
            status = parse_status(self._read_body())
            self.server.robot.put("status", status)
            alarms = []   # 상태를 보고 자동 알람 (배터리 부족, 로봇 넘어짐)
            low = [v for v in (status["dog_battery_pct"], status["pi_battery_pct"]) if v is not None]
            if low and min(low) < LOW_BATTERY_PCT:
                alarms.append(self.server.store.add_if_quiet("low_battery", status["simulated"]))
            if status["robot_tipped"]:
                alarms.append(self.server.store.add_if_quiet("robot_tipped", status["simulated"]))
            self._json(200, {"ok": True, "alarms": [a for a in alarms if a]})
        elif path == "/api/robot/pose":
            self.server.robot.put("pose", parse_pose(self._read_body()))
            self._json(200, {"ok": True})
        elif path == "/api/settings":
            self._json(200, self.server.store.save_settings(parse_settings(self._read_body())))
        else:
            self._json(404, {"error": "없는 주소예요"})

    def _read_body(self, required=True):
        raw = self.headers.get("Content-Length")
        if raw is None and not required:
            return b""
        try:
            length = int(raw or "")
        except ValueError:
            raise BadRequest("Content-Length가 필요해요")
        if length < 0 or length > MAX_BODY:
            raise BadRequest(f"본문은 {MAX_BODY}바이트 이하여야 해요")
        return self.rfile.read(length)

    def _safely(self, route):
        self._set_cookie = None   # 이번 응답에 실어 보낼 쿠키 (로그인·로그아웃·입장권 연장 때만)
        try:
            route()
        except BadRequest as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # 내부 오류 내용은 화면(응답)에 내보내지 않고 서버 창에만 남겨요
            self.log_error("내부 오류: %r", e)
            self._json(500, {"error": "서버 내부 오류"})

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send(self, status, body, content_type, location=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if location:
            self.send_header("Location", location)
        if self._set_cookie:
            self.send_header("Set-Cookie", self._set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self._send(303, b"", HTML, location)

    def log_request(self, code="-", size="-"):
        # 대시보드는 1초마다 조회하고 로봇은 몇 초마다 상태를 보내므로, 그런 성공 요청은 찍지 않아요.
        code_text = str(getattr(code, "value", code))
        routine = self.command == "GET" or urlsplit(self.path).path.startswith("/api/robot/")
        if not (routine and code_text.startswith("2")):
            super().log_request(code, size)


class DashboardServer(ThreadingHTTPServer):
    # 윈도우에서는 이 옵션이 켜져 있으면 같은 포트에 서버가 두 개 떠도 오류가 안 나서,
    # 예전에 켜 둔 서버가 몰래 대신 응답할 수 있어요. 그래서 윈도우에서는 꺼요.
    allow_reuse_address = sys.platform != "win32"


def make_server(host, port, store, auth):
    server = DashboardServer((host, port), Handler)
    server.daemon_threads = True
    server.store = store
    server.auth = auth
    server.robot = RobotState()
    return server


def ask_password(auth_dir):
    """터미널에서 관리자 아이디·비밀번호를 물어보고 저장해요."""
    import getpass
    username = input("관리자 아이디 (그냥 Enter = admin): ").strip() or "admin"
    if len(username) > 32:
        print("아이디는 32자 이하로 해 주세요.")
        return 1
    while True:
        password = getpass.getpass("비밀번호 (8자 이상 · 입력해도 화면에 안 보여요): ")
        if len(password) < 8:
            print("8자 이상으로 해 주세요.")
        elif getpass.getpass("비밀번호 한 번 더: ") != password:
            print("두 번 입력한 비밀번호가 달라요. 다시 해 주세요.")
        else:
            break
    set_password(auth_dir, username, password)
    print(f"저장했어요 (아이디: {username}). 이제 python server.py 로 켜고 로그인하세요.")
    print("이미 로그인해 둔 기기는 모두 로그아웃돼요.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="PD-Dog 돌봄 대시보드 서버")
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0 으로 하면 같은 와이파이의 다른 기기에서도 접속 (기본: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="포트 번호 (기본: 8000)")
    parser.add_argument("--db", default=str(HERE / "falls.db"), help="기록 파일 경로")
    parser.add_argument("--auth-dir", default=str(HERE), help="admin.json·robot_key.txt가 있는 폴더")
    parser.add_argument("--set-password", action="store_true", help="관리자 아이디·비밀번호 정하기(바꾸기)")
    args = parser.parse_args(argv)

    if args.set_password:
        return ask_password(args.auth_dir)
    try:
        auth = Auth(args.auth_dir)
    except FileNotFoundError:
        print("관리자 비밀번호가 아직 없어요. 먼저 이걸 실행하세요:  python server.py --set-password")
        return 1

    store = FallStore(args.db)
    try:
        server = make_server(args.host, args.port, store, auth)
    except OSError:
        store.close()
        print(f"{args.port}번 포트를 이미 다른 프로그램이 쓰고 있어요. 다른 창에서 server.py가 켜져 있는지 확인하세요.")
        return 1
    port = server.server_address[1]
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print(f"PD-Dog 대시보드 실행 중: http://{shown_host}:{port}", flush=True)
    if args.host in ("0.0.0.0", ""):
        print(f"다른 기기에서는 http://<이 컴퓨터의 IP 주소>:{port} 로 접속하세요", flush=True)
    print(f"관리자 아이디: {auth.username} · 로봇 키 파일: {Path(args.auth_dir) / ROBOT_KEY_FILE}", flush=True)
    print("끄려면 Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
