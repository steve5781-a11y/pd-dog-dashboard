# PD-Dog 낙상 알람 대시보드

낙상 신호가 들어오면 보호자 화면이 빨갛게 바뀌고 경보음이 울려요. 보호자가 **"확인"**을 누르면 알람이 멈추고 기록이 남아요.

파이썬만 있으면 돼요 (따로 설치할 것 없음).

## 1. PC에서 실행하기

이 폴더에서 터미널을 열고, **처음 한 번만** 관리자 아이디·비밀번호를 정하세요:

```
python server.py --set-password
```

아이디는 그냥 Enter를 누르면 `admin`이에요. 비밀번호는 8자 이상이고, 입력해도 화면에 안 보이는 게 정상이에요.
(비밀번호를 바꿀 때도 같은 명령을 다시 실행하면 돼요. 바꾸면 이미 로그인해 둔 기기는 모두 로그아웃돼요.)

그다음 서버를 켜요:

```
python server.py
```

`낙상 대시보드 실행 중: http://127.0.0.1:8000` 이 보이면 브라우저로 그 주소를 여세요.
**로그인 화면**이 나오면 정한 아이디·비밀번호로 들어가요. 한 번 로그인하면 그 브라우저에서는 30일 동안 유지되고, 대시보드를 쓰는 동안은 자동으로 연장돼요. 오른쪽 위 **🚪 로그아웃**으로 나갈 수 있어요.

1. **"🔔 알람 소리 켜기"**를 한 번 누르세요. (브라우저 규칙 때문에 한 번 눌러야 소리가 나요)
2. **"🧪 테스트 낙상 발생"**을 누르면 → 빨간 화면 + 삐-삐 소리
3. **"확인"**을 누르면 → 초록 "정상"으로 돌아와요

서버를 끄려면 터미널에서 `Ctrl + C`.

## 2. 로봇 대신 신호 보내 보기

서버를 켜 둔 채로 **다른 터미널**에서:

```
python simulate_fall.py --source camera --confidence 0.9   # F6 낙상 감지
python simulate_fall.py --source touch                     # F2 터치 3초 신고
python simulate_fall.py --source voice                     # F2 "도와줘"
python simulate_fall.py --source no_motion                 # F7 무움직임
```

(복약 미확인은 `--source medication_missed`)

로봇은 로그인을 못 하니까 비밀번호 대신 **로봇 키**를 보내요. 서버가 처음 켜질 때 이 폴더에 `robot_key.txt`를 만들고, `simulate_fall.py`는 그 파일을 자동으로 읽어요.

## 화면 구성

| 주소 | 내용 |
|---|---|
| `/` 보호자 대시보드 | 지금 상태(🔴위급·🟠주의) · 확인 + 메모 · 오늘 요약 · 🎬 시연 패널 · 기능 카드 · 알람 기록 |
| `/report` 치료사용 주간 리포트 | 7일 안전 알람 집계 · 보호자 메모 · 보행/발성/복약 기록 · 인쇄/PDF |

| 영역 | 데이터 |
|---|---|
| 지금 상태 · 오늘 요약 · 낙상(F6·F7) · 위급 신고(F2) · 복약 미확인 알림 · 기록 · 리포트의 안전 알람 | **실시간** |
| 리듬 큐잉 레이저 박자(F3) | **미리보기** — 박자와 레이저 점을 화면에서 직접 체험 |
| 복약 일정(F1) · 발성(F4·F5) · 배터리 · 리포트의 보행/발성/복약 | **예시 데이터** (`static/demo-data.js`) — 로봇과 연결되면 실제 값으로 바꿀 자리 |

- 🎬 **시연 패널**에서 보낸 신호는 기록에 **"시연"**으로 표시되고, 리포트에서는 기본으로 빠져요.
- 알람 등급: 🔴 위급(낙상·위급 신고·무움직임·테스트) = 매초 경보음 / 🟠 주의(복약 미확인) = 3초마다 짧은 알림음.

## 3. 라즈베리파이(pd-dog)로 옮기기

1. 이 폴더(`fall_dashboard`)를 통째로 Pi에 복사
2. Pi에서 처음 한 번: `python3 server.py --set-password`
3. Pi에서: `python3 server.py --host 0.0.0.0`
4. 같은 와이파이의 휴대폰·PC 브라우저에서 `http://pd-dog.local:8000`
   (안 열리면 Pi에서 `hostname -I`로 IP 주소를 확인해 `http://<IP>:8000`)

## 4. F6 낙상 감지 코드와 연결하는 방법

카메라가 낙상을 감지했을 때 아래 한 줄만 부르면 돼요:

```python
from simulate_fall import report_fall
report_fall("http://127.0.0.1:8000", "camera", 0.92)   # 0.92 = 감지 신뢰도(0~1)
```

같은 낙상을 계속 보내도 **30초 안의 중복은 알람 1건**으로 합쳐져요.

## 5. 로봇(라즈베리파이)이 대시보드에 보내는 것

라즈베리파이 코드에서 아래 주소로 JSON을 보내면 대시보드에 바로 나와요. 모르는 값은 `null`로 보내면 돼요.

**모든 요청에 로봇 키 헤더를 붙여야 해요.** 없거나 틀리면 `401`로 거절돼요.
```python
ROBOT_KEY = open("/home/pi/fall_dashboard/robot_key.txt").read().strip()
headers = {"Content-Type": "application/json", "X-Robot-Key": ROBOT_KEY}
```
로봇 키로 쓸 수 있는 건 아래 ①~④뿐이에요. 기록 보기·알람 확인·설정 바꾸기는 로그인한 사람만 할 수 있어요.

**① 로봇 상태 — 2초마다** `POST /api/robot/status`
```json
{"esp32_ok": true, "mode": "walking",
 "dog_battery_pct": 64, "pi_battery_pct": null, "pi_charging": null, "pi_temp_c": 52.0,
 "follow_distance_cm": 120, "laser_on": true, "bpm": 96, "robot_tipped": false}
```
| 값 | 어디서 | 비고 |
|---|---|---|
| `esp32_ok` | Pi ↔ ESP32 USB(UART) 응답 여부 | 필수 |
| `dog_battery_pct` | ESP32 `Hiwonder.Battery_power()` → UART | 20% 미만이면 🟠 배터리 부족 알람 |
| `pi_battery_pct`, `pi_charging` | X1200 배터리 칩 (I²C 0x36) | X1200이 없으면 `null` → "측정 안 됨" |
| `pi_temp_c` | `vcgencmd measure_temp` | 75°C 이상일 때만 화면에 경고 |
| `follow_distance_cm` | VL53L1X ToF (뒤쪽) | 설정의 한도보다 멀면 "기다리는 중" |
| `robot_tipped` | MechDog 기울기 센서 | true면 🟠 로봇 넘어짐 알람 |
| `mode` | `idle` `walking` `voice` `medication` `approaching` `charging` | |

10초 넘게 상태가 안 오면 화면에 **"로봇 연결 끊김"**이 떠요.

**② 자세 — 카메라가 볼 때마다** `POST /api/robot/pose`
```json
{"state": "standing", "keypoints": [[0.50, 0.12, 0.9], ... 17개]}
```
YOLO 자세 추정의 관절 점 17개(COCO 순서)를 화면 크기 0~1로 나눠서 보내요. `state`: `none` `standing` `sitting` `falling` `lying`. **영상은 보내지 않아요.**

**③ 설정 가져오기** `GET /api/settings` → 복약 시간, 목표 박자, 목표 목소리 크기, 따라오기 한도
(설정 화면 `/settings`에서 치료사·보호자가 바꿔요)

**④ 위급 알람** `POST /api/falls` (위 2번 참고)

## 6. 자동 테스트

```
python -m unittest -v test_server.py
```

## 알아둘 점 (한계)

- **관리자 계정은 1개**예요. 팀원 모두 같은 아이디·비밀번호를 써서, 기록에 "누가 확인했는지"는 남지 않아요.
- **주고받는 내용이 암호화되지는 않아요** (https가 아닌 http). 같은 와이파이에서 작정하고 엿보면 비밀번호가 보일 수 있으니, 다른 곳에서 쓰는 비밀번호는 쓰지 마세요. 시연·교내 프로젝트용이에요.
- 비밀번호를 잊으면 서버 컴퓨터에서 `python server.py --set-password`로 새로 정하면 돼요. 같은 기기에서 5번 틀리면 1분 동안 로그인이 막혀요.
- `admin.json`(비밀번호 해시)과 `robot_key.txt`(로봇 키)는 다른 사람과 공유하지 마세요. `robot_key.txt`를 지우면 서버가 새 키를 만들어요 (그러면 로봇 코드도 새 키를 읽어야 해요).
- 휴대폰 화면이 꺼져 있거나 브라우저가 뒤에 숨어 있으면 소리가 늦거나 안 날 수 있어요. 원격 알림은 F2(텔레그램)로 보완할 예정이에요.
- 서버가 꺼지면 화면에 **"서버 연결 끊김"**과 **"상태 알 수 없음"**이 떠요. 이때는 알람을 받을 수 없어요.
- 기록은 `falls.db` 파일에 저장돼요. 지우면 기록이 초기화돼요.
