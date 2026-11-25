"""가짜 낙상 신호 보내기 (나중에 F6 카메라 낙상 감지 코드가 할 일을 대신해요).

사용:  python simulate_fall.py
       python simulate_fall.py --source camera --confidence 0.9 --url http://127.0.0.1:8000

F6 코드에서는 아래 report_fall() 함수를 그대로 가져다 쓰면 돼요:
    from simulate_fall import report_fall
    report_fall("http://127.0.0.1:8000", "camera", 0.92)

대시보드는 로그인한 사람만 쓸 수 있어서, 로봇은 비밀번호 대신 '로봇 키'를 보내요.
키는 서버가 처음 켜질 때 이 폴더에 만드는 robot_key.txt 에 있고, 여기서 자동으로 읽어요.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROBOT_KEY_FILE = Path(__file__).resolve().parent / "robot_key.txt"


def read_robot_key(path=ROBOT_KEY_FILE):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def report_fall(url, source="camera", confidence=None, timeout=5, robot_key=None):
    """대시보드 서버에 낙상 발생을 알려요. (HTTP 상태 코드, 응답 내용)을 돌려줘요."""
    body = {"source": source}
    if confidence is not None:
        body["confidence"] = confidence
    req = urllib.request.Request(
        url.rstrip("/") + "/api/falls",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Robot-Key": robot_key if robot_key is not None else read_robot_key()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 400·404 같은 응답도 내용을 보여줘요
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="대시보드에 가짜 낙상 신호 보내기")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="대시보드 서버 주소")
    parser.add_argument("--source", default="camera",
                        choices=["camera", "touch", "voice", "no_motion", "medication_missed", "test"],
                        help="camera=낙상(F6), touch=터치 신고(F2), voice='도와줘'(F2), "
                             "no_motion=무움직임(F7), medication_missed=복약 미확인(F1)")
    parser.add_argument("--confidence", type=float, help="감지 신뢰도 0~1 (선택)")
    args = parser.parse_args(argv)

    try:
        status, data = report_fall(args.url, args.source, args.confidence)
    except urllib.error.URLError as e:
        print(f"서버에 연결할 수 없어요: {e.reason}  (server.py가 켜져 있나요?)")
        return 1
    print(status, json.dumps(data, ensure_ascii=False))
    if status == 401:
        print(f"로봇 키가 맞지 않아요. {ROBOT_KEY_FILE} 가 서버와 같은 폴더에 있는지 확인하세요.")
    return 0 if status in (200, 201) else 1


if __name__ == "__main__":
    sys.exit(main())
