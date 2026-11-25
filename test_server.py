"""낙상 대시보드 서버 자동 테스트.  실행: python -m unittest -v test_server.py"""

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

import server

HERE = Path(__file__).resolve().parent
TEST_USER, TEST_PASSWORD = "admin", "test-password-123"


class ServerBase(unittest.TestCase):
    """실제 서버를 임시 기록 파일 + 빈 포트로 띄워서 HTTP로 확인하는 준비 (테스트는 아래 클래스들에).
    request()는 기본으로 로그인한 상태(입장권 쿠키)로 보내요."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.set_password(self.tmp.name, TEST_USER, TEST_PASSWORD)
        self.auth = server.Auth(self.tmp.name)
        self.session = self.auth.new_session()
        self.store = server.FallStore(Path(self.tmp.name) / "falls.db")
        self.httpd = server.make_server("127.0.0.1", 0, self.store, self.auth)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, raw=None, headers=None, login=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        sent = {"Content-Type": "application/json"}
        if login:
            sent["Cookie"] = f"{server.SESSION_COOKIE}={self.session}"
        sent.update(headers or {})
        conn.request(method, path, body=payload, headers=sent)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        self.last_headers = res.headers
        if res.getheader("Content-Type", "").startswith("application/json"):
            data = json.loads(data)
        return res.status, data

    def fall(self, source="test", confidence=None):
        body = {"source": source}
        if confidence is not None:
            body["confidence"] = confidence
        return self.request("POST", "/api/falls", body)


class ServerTest(ServerBase):
    """알람·확인·리포트·페이지."""

    # --- 기본 동작 ---

    def test_dashboard_page(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("PD-Dog 돌봄 대시보드", body.decode("utf-8"))

    def test_empty_list(self):
        self.assertEqual(self.request("GET", "/api/falls"), (200, {"events": [], "unacknowledged": 0}))

    def test_create_then_list_newest_first(self):
        status, first = self.fall("test")
        self.assertEqual(status, 201)
        self.assertFalse(first["duplicate"])
        ev = first["event"]
        self.assertEqual((ev["source"], ev["status"], ev["confidence"], ev["acknowledged_at"]),
                         ("test", "new", None, None))
        status, second = self.fall("camera", 0.9)
        self.assertEqual(status, 201)
        self.assertEqual(second["event"]["confidence"], 0.9)

        status, data = self.request("GET", "/api/falls")
        self.assertEqual(status, 200)
        self.assertEqual(data["unacknowledged"], 2)
        self.assertEqual([e["id"] for e in data["events"]], [second["event"]["id"], ev["id"]])

    def test_all_alarm_sources_accepted(self):
        sources = ("camera", "touch", "voice", "no_motion", "medication_missed", "test")
        for source in sources:
            with self.subTest(source):
                status, data = self.fall(source)
                self.assertEqual(status, 201)
                self.assertEqual(data["event"]["source"], source)
                self.assertFalse(data["event"]["simulated"])
        self.assertEqual(self.request("GET", "/api/falls")[1]["unacknowledged"], len(sources))

    def test_simulated_flag(self):
        status, data = self.request("POST", "/api/falls", {"source": "camera", "simulated": True})
        self.assertEqual(status, 201)
        self.assertTrue(data["event"]["simulated"])
        status, data = self.request("POST", "/api/falls", {"source": "touch", "simulated": "yes"})
        self.assertEqual(status, 400)

    # --- 확인 메모 ---

    def test_ack_with_note_keeps_first_note(self):
        _, created = self.fall("camera")
        event_id = created["event"]["id"]
        status, first = self.request("POST", f"/api/falls/{event_id}/ack", {"note": "  다친 곳 없음  "})
        self.assertEqual(status, 200)
        self.assertEqual(first["event"]["note"], "다친 곳 없음")
        _, again = self.request("POST", f"/api/falls/{event_id}/ack", {"note": "두 번째 메모"})
        self.assertEqual(again["event"]["note"], "다친 곳 없음")

    def test_ack_note_validation(self):
        _, created = self.fall("camera")
        event_id = created["event"]["id"]
        for name, body in {"너무 긺": {"note": "가" * 201}, "글자 아님": {"note": 5}}.items():
            with self.subTest(name):
                self.assertEqual(self.request("POST", f"/api/falls/{event_id}/ack", body)[0], 400)
        self.assertEqual(self.request("POST", f"/api/falls/{event_id}/ack", raw=b"not json")[0], 400)
        status, ok = self.request("POST", f"/api/falls/{event_id}/ack", {"note": "   "})  # 빈 메모 = 메모 없음
        self.assertEqual(status, 200)
        self.assertIsNone(ok["event"]["note"])

    # --- 주간 리포트 · 페이지 ---

    def test_report_has_last_week_only(self):
        _, old = self.fall("camera")
        _, recent = self.fall("touch")
        with self.store.db:  # 첫 기록을 10일 전 것으로 바꿔요
            self.store.db.execute("UPDATE falls SET created_at = ? WHERE id = ?",
                                  (server.now_iso(server.timedelta(days=10)), old["event"]["id"]))
        status, data = self.request("GET", "/api/report")
        self.assertEqual(status, 200)
        self.assertEqual([e["id"] for e in data["events"]], [recent["event"]["id"]])
        self.assertFalse(data["truncated"])
        self.assertEqual(data["days"], 7)

    def test_pages_served(self):
        for path, text in {"/report": "주간 재활 리포트", "/settings": "설정", "/demo-data.js": "PD_DEMO"}.items():
            with self.subTest(path):
                status, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(text, body.decode("utf-8"))

    # --- 확인(ack) ---

    def test_acknowledge_is_idempotent(self):
        _, created = self.fall("camera")
        event_id = created["event"]["id"]
        status, first = self.request("POST", f"/api/falls/{event_id}/ack")
        self.assertEqual(status, 200)
        self.assertEqual(first["event"]["status"], "acknowledged")
        self.assertIsNotNone(first["event"]["acknowledged_at"])

        status, again = self.request("POST", f"/api/falls/{event_id}/ack")
        self.assertEqual(status, 200)
        self.assertEqual(again["event"]["acknowledged_at"], first["event"]["acknowledged_at"])
        self.assertEqual(self.request("GET", "/api/falls")[1]["unacknowledged"], 0)

    def test_unknown_event_and_path_are_404(self):
        self.assertEqual(self.request("POST", "/api/falls/999/ack")[0], 404)
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("POST", "/api/falls/abc/ack")[0], 404)

    # --- 잘못된 입력 ---

    def test_invalid_input_is_400(self):
        cases = {
            "JSON 아님": b"not json",
            "빈 본문": b"",
            "객체 아님": b"[]",
            "source 없음": b"{}",
            "허용 안 된 source": b'{"source": "phone"}',
            "confidence 범위 밖": b'{"source": "camera", "confidence": 1.5}',
            "confidence 음수": b'{"source": "camera", "confidence": -0.1}',
            "confidence 문자열": b'{"source": "camera", "confidence": "0.5"}',
            "confidence true": b'{"source": "camera", "confidence": true}',
            "confidence NaN": b'{"source": "camera", "confidence": NaN}',
            "너무 큰 본문": b'{"source": "test", "pad": "' + b"x" * 5000 + b'"}',
        }
        for name, raw in cases.items():
            with self.subTest(name):
                status, data = self.request("POST", "/api/falls", raw=raw)
                self.assertEqual(status, 400)
                self.assertIn("error", data)
        self.assertEqual(self.request("GET", "/api/falls")[1]["events"], [])

    # --- 중복 억제 ---

    def test_duplicate_suppression(self):
        _, first = self.fall("camera", 0.8)
        status, dup = self.fall("camera", 0.95)
        self.assertEqual(status, 200)
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["event"]["id"], first["event"]["id"])

        status, other = self.fall("test")            # 다른 출처면 새로 만들어요
        self.assertEqual(status, 201)

        self.request("POST", f"/api/falls/{first['event']['id']}/ack")
        status, after_ack = self.fall("camera")      # 확인한 뒤면 새로 만들어요
        self.assertEqual(status, 201)
        self.assertNotEqual(after_ack["event"]["id"], first["event"]["id"])
        self.assertEqual(self.request("GET", "/api/falls")[1]["unacknowledged"], 2)

    def test_duplicate_window_expires_after_30s(self):
        _, first = self.fall("camera")
        with self.store.db:  # 첫 낙상을 31초 전에 일어난 것으로 바꿔요
            self.store.db.execute("UPDATE falls SET created_at = ? WHERE id = ?",
                                  (server.now_iso(server.timedelta(seconds=31)), first["event"]["id"]))
        status, second = self.fall("camera")
        self.assertEqual(status, 201)
        self.assertFalse(second["duplicate"])

    def test_old_unacknowledged_event_stays_listed(self):
        _, old = self.fall("camera")                # 확인 안 한 채로 둠
        for _ in range(server.RECENT_LIMIT + 5):    # 그 뒤로 기록이 50건 넘게 쌓여도
            _, ev = self.fall("test")
            self.request("POST", f"/api/falls/{ev['event']['id']}/ack")
        _, data = self.request("GET", "/api/falls")
        self.assertEqual(data["unacknowledged"], 1)
        self.assertIn(old["event"]["id"], [e["id"] for e in data["events"]])  # 알람 대상은 빠지지 않아요
        self.assertEqual(len(data["events"]), server.RECENT_LIMIT + 1)


class RobotTest(ServerBase):
    """로봇 상태·자세·설정."""

    STATUS = {"esp32_ok": True, "mode": "walking", "dog_battery_pct": 64, "pi_battery_pct": None,
              "pi_charging": None, "pi_temp_c": 52.5, "follow_distance_cm": 120, "laser_on": True,
              "bpm": 96, "robot_tipped": False}

    def test_no_robot_yet(self):
        status, data = self.request("GET", "/api/robot")
        self.assertEqual(status, 200)
        self.assertEqual((data["status"], data["online"]), (None, False))

    def test_status_roundtrip(self):
        self.assertEqual(self.request("POST", "/api/robot/status", self.STATUS)[0], 200)
        _, data = self.request("GET", "/api/robot")
        self.assertTrue(data["online"])
        self.assertLess(data["age_seconds"], 5)
        self.assertEqual(data["status"]["dog_battery_pct"], 64)
        self.assertIsNone(data["status"]["pi_battery_pct"])   # X1200이 없으면 null
        self.assertFalse(data["status"]["simulated"])
        self.assertEqual(self.request("GET", "/api/falls")[1]["events"], [])  # 정상 상태는 알람 없음

    def test_status_validation(self):
        bad = {"esp32_ok 없음": {k: v for k, v in self.STATUS.items() if k != "esp32_ok"},
               "배터리 범위 밖": dict(self.STATUS, dog_battery_pct=150),
               "모르는 모드": dict(self.STATUS, mode="dance"),
               "거리 글자": dict(self.STATUS, follow_distance_cm="1m"),
               "레이저 숫자": dict(self.STATUS, laser_on=1)}
        for name, body in bad.items():
            with self.subTest(name):
                self.assertEqual(self.request("POST", "/api/robot/status", body)[0], 400)

    def test_low_battery_alarm_once(self):
        low = dict(self.STATUS, dog_battery_pct=15)
        _, first = self.request("POST", "/api/robot/status", low)
        self.assertEqual([a["source"] for a in first["alarms"]], ["low_battery"])
        self.request("POST", f"/api/falls/{first['alarms'][0]['id']}/ack")
        _, again = self.request("POST", "/api/robot/status", low)   # 확인한 뒤에도 30분 동안은 다시 안 울려요
        self.assertEqual(again["alarms"], [])
        self.assertEqual(len(self.request("GET", "/api/falls")[1]["events"]), 1)

    def test_pi_low_battery_and_tipped(self):
        _, data = self.request("POST", "/api/robot/status",
                               dict(self.STATUS, pi_battery_pct=10, robot_tipped=True, simulated=True))
        self.assertEqual(sorted(a["source"] for a in data["alarms"]), ["low_battery", "robot_tipped"])
        self.assertTrue(all(a["simulated"] for a in data["alarms"]))

    def test_pose(self):
        points = [[0.5, 0.1 + i * 0.04, 0.9] for i in range(17)]
        self.assertEqual(self.request("POST", "/api/robot/pose", {"state": "standing", "keypoints": points})[0], 200)
        _, data = self.request("GET", "/api/robot/pose")
        self.assertEqual((data["pose"]["state"], len(data["pose"]["keypoints"])), ("standing", 17))
        self.assertEqual(self.request("POST", "/api/robot/pose", {"state": "none"})[0], 200)
        for name, body in {"점 개수": {"state": "lying", "keypoints": points[:5]},
                           "범위 밖": {"state": "lying", "keypoints": [[1.5, 0, 1]] * 17},
                           "모르는 상태": {"state": "dancing", "keypoints": points}}.items():
            with self.subTest(name):
                self.assertEqual(self.request("POST", "/api/robot/pose", body)[0], 400)

    def test_settings_default_save_and_persist(self):
        _, default = self.request("GET", "/api/settings")
        self.assertEqual(default["walk_bpm"], 96)
        self.assertIsNone(default["updated_at"])
        new = {"doses": [["21:00", " 자기 전 약 "], ["07:30", "아침 약"]],
               "walk_bpm": 100, "voice_target_db": 70, "follow_max_cm": 120}
        status, saved = self.request("POST", "/api/settings", new)
        self.assertEqual(status, 200)
        self.assertEqual(saved["doses"], [["07:30", "아침 약"], ["21:00", "자기 전 약"]])  # 시간순·공백 정리
        self.assertIsNotNone(saved["updated_at"])
        # 서버를 다시 열어도(같은 기록 파일) 남아 있어요
        reopened = server.FallStore(Path(self.tmp.name) / "falls.db")
        try:
            self.assertEqual(reopened.get_settings()["walk_bpm"], 100)
        finally:
            reopened.close()

    def test_settings_validation(self):
        good = {"doses": [["08:00", "아침 약"]], "walk_bpm": 96, "voice_target_db": 68, "follow_max_cm": 150}
        bad = {"박자 범위 밖": dict(good, walk_bpm=130),
               "박자 소수": dict(good, walk_bpm=96.5),
               "시각 형식": dict(good, doses=[["8시", "아침 약"]]),
               "25시": dict(good, doses=[["25:00", "아침 약"]]),
               "같은 시각": dict(good, doses=[["08:00", "a"], ["08:00", "b"]]),
               "약 이름 없음": dict(good, doses=[["08:00", "  "]]),
               "복약 0개": dict(good, doses=[])}
        for name, body in bad.items():
            with self.subTest(name):
                self.assertEqual(self.request("POST", "/api/settings", body)[0], 400)
        self.assertIsNone(self.request("GET", "/api/settings")[1]["updated_at"])  # 하나도 저장 안 됨


class AuthTest(ServerBase):
    """로그인해야만 들어갈 수 있고, 로봇은 로봇 키로 정해진 주소만 쓸 수 있어요."""

    def login(self, username=TEST_USER, password=TEST_PASSWORD):
        form = urlencode({"username": username, "password": password}).encode()
        status, _ = self.request("POST", "/login", raw=form, login=False,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        return status, self.last_headers["Location"], self.last_headers.get("Set-Cookie", "")

    def test_everything_needs_login(self):
        for path in ("/", "/report", "/settings", "/demo-data.js"):
            self.assertEqual(self.request("GET", path, login=False)[0], 303, path)
            self.assertEqual(self.last_headers["Location"], "/login")
        for path in ("/api/falls", "/api/report", "/api/robot", "/api/robot/pose", "/api/settings"):
            self.assertEqual(self.request("GET", path, login=False)[0], 401, path)
        for path, body in (("/api/falls", {"source": "test"}), ("/api/falls/1/ack", None),
                           ("/api/settings", dict(server.DEFAULT_SETTINGS)),
                           ("/api/robot/status", {"esp32_ok": True})):
            self.assertEqual(self.request("POST", path, body, login=False)[0], 401, path)
        self.assertEqual(self.request("GET", "/api/falls")[1]["events"], [])   # 아무것도 안 만들어졌어요

    def test_login_page_is_open(self):
        status, body = self.request("GET", "/login", login=False)
        self.assertEqual(status, 200)
        self.assertIn(b'name="password"', body)

    def test_login_then_use_dashboard(self):
        status, location, cookie = self.login()
        self.assertEqual((status, location), (303, "/"))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.session = cookie.split(";")[0].split("=", 1)[1]
        self.assertEqual(self.request("GET", "/api/falls")[0], 200)
        self.assertEqual(self.request("GET", "/login")[0], 303)   # 이미 로그인했으면 대시보드로

    def test_wrong_password_then_lockout(self):
        for _ in range(server.LOGIN_MAX_FAILS - 1):
            self.assertEqual(self.login(password="wrong-password")[1], "/login?e=wrong")
        self.assertEqual(self.login(password="wrong-password")[1], "/login?e=locked")
        status, location, cookie = self.login()                  # 잠긴 동안은 맞아도 막혀요
        self.assertEqual((location, cookie), ("/login?e=locked", ""))
        self.assertEqual(self.login(username="other")[1], "/login?e=locked")

    def test_wrong_username_rejected(self):
        self.assertEqual(self.login(username="root")[1], "/login?e=wrong")
        self.assertEqual(self.login(username="관리자")[1], "/login?e=wrong")

    def test_forged_or_expired_session_rejected(self):
        past = int(time.time()) - 10
        for token in ("", "abc", "9999999999.abc", "9999999999." + "0" * 64,
                      f"{past}.{self.auth._sign(past)}", "²." + "0" * 64, "é.é"):
            self.session = token
            self.assertEqual(self.request("GET", "/api/falls")[0], 401, token)

    def test_session_renewed_when_old(self):
        self.assertEqual(self.request("GET", "/api/falls")[0], 200)
        self.assertIsNone(self.last_headers.get("Set-Cookie"))   # 새 입장권은 그대로
        soon = int(time.time()) + 3600
        self.session = f"{soon}.{self.auth._sign(soon)}"
        self.assertEqual(self.request("GET", "/api/falls")[0], 200)
        self.assertIn(server.SESSION_COOKIE, self.last_headers.get("Set-Cookie", ""))

    def test_logout(self):
        self.assertEqual(self.request("POST", "/logout")[0], 303)
        self.assertEqual(self.last_headers["Location"], "/login")
        self.assertIn("Max-Age=0", self.last_headers["Set-Cookie"])

    def test_password_change_logs_everyone_out(self):
        server.set_password(self.tmp.name, TEST_USER, "another-password-1")
        self.httpd.auth = server.Auth(self.tmp.name)
        self.assertEqual(self.request("GET", "/api/falls")[0], 401)

    def test_robot_key(self):
        key = {"X-Robot-Key": self.auth.robot_key}
        self.assertEqual(self.request("POST", "/api/falls", {"source": "camera"}, headers=key, login=False)[0], 201)
        self.assertEqual(self.request("POST", "/api/robot/status", {"esp32_ok": True}, headers=key, login=False)[0], 200)
        self.assertEqual(self.request("POST", "/api/robot/pose", {"state": "none"}, headers=key, login=False)[0], 200)
        self.assertEqual(self.request("GET", "/api/settings", headers=key, login=False)[0], 200)
        # 로봇 키로는 기록 보기·알람 확인·설정 바꾸기를 못 해요
        self.assertEqual(self.request("GET", "/api/falls", headers=key, login=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/falls/1/ack", headers=key, login=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/settings", dict(server.DEFAULT_SETTINGS),
                                      headers=key, login=False)[0], 401)
        self.assertEqual(self.request("GET", "/", headers=key, login=False)[0], 303)
        for bad in ("wrong", "", self.auth.robot_key + "x", "é"):
            self.assertEqual(self.request("POST", "/api/falls", {"source": "camera"},
                                          headers={"X-Robot-Key": bad}, login=False)[0], 401, bad)

    def test_robot_key_file_kept(self):
        self.assertTrue(len(self.auth.robot_key) >= 20)
        self.assertEqual(server.Auth(self.tmp.name).robot_key, self.auth.robot_key)

    def test_password_not_stored_in_plain_text(self):
        text = (Path(self.tmp.name) / server.ADMIN_FILE).read_text(encoding="utf-8")
        self.assertNotIn(TEST_PASSWORD, text)


class OldDatabaseTest(unittest.TestCase):
    """예전 버전(메모·시연 칸 없음)에서 만든 기록 파일도 그대로 열려야 해요."""

    def test_old_database_is_upgraded(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "falls.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE falls (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
                       " confidence REAL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',"
                       " acknowledged_at TEXT)")
            db.execute("INSERT INTO falls (source, created_at) VALUES ('camera', ?)", (server.now_iso(),))
            db.commit()
            db.close()

            store = server.FallStore(path)
            try:
                events, unacknowledged = store.listing()
                self.assertEqual(unacknowledged, 1)
                self.assertEqual((events[0]["note"], events[0]["simulated"]), (None, False))
                self.assertEqual(store.acknowledge(events[0]["id"], "확인함")["note"], "확인함")
            finally:
                store.close()


class RealProcessTest(unittest.TestCase):
    """server.py를 실제 프로그램으로 실행하고, 껐다 켜도 기록이 남는지 확인해요."""

    def start(self, db):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "server.py"), "--port", "0", "--db", db,
                                 "--auth-dir", str(Path(db).parent)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, encoding="utf-8")
        line = proc.stdout.readline()
        match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
        self.assertIsNotNone(match, f"시작 메시지가 이상해요: {line!r}")
        return proc, int(match.group(1))

    def stop(self, proc):
        proc.terminate()
        proc.wait(timeout=10)
        proc.stdout.close()

    def test_refuses_to_start_without_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(HERE / "server.py"), "--port", "0",
                                     "--db", str(Path(tmp) / "falls.db"), "--auth-dir", tmp],
                                    capture_output=True, encoding="utf-8", timeout=30,
                                    env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("--set-password", result.stdout)

    def test_starts_and_keeps_records_after_restart(self):
        import simulate_fall
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "falls.db")
            server.set_password(tmp, TEST_USER, TEST_PASSWORD)
            proc, port = self.start(db)
            try:
                key = simulate_fall.read_robot_key(Path(tmp) / server.ROBOT_KEY_FILE)
                status, _ = simulate_fall.report_fall(f"http://127.0.0.1:{port}", "camera", 0.9, robot_key="wrong")
                self.assertEqual(status, 401)
                status, data = simulate_fall.report_fall(f"http://127.0.0.1:{port}", "camera", 0.9, robot_key=key)
                self.assertEqual(status, 201)
                event_id = data["event"]["id"]
            finally:
                self.stop(proc)

            proc, port = self.start(db)
            try:
                cookie = f"{server.SESSION_COOKIE}={server.Auth(tmp).new_session()}"
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/falls", headers={"Cookie": cookie})
                data = json.loads(conn.getresponse().read())
                conn.close()
                self.assertEqual([e["id"] for e in data["events"]], [event_id])
                self.assertEqual(data["unacknowledged"], 1)
            finally:
                self.stop(proc)


if __name__ == "__main__":
    unittest.main()
