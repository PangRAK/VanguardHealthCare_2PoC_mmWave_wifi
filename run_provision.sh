#!/usr/bin/env bash
# 센서 Wi-Fi 연결(프로비저닝) 실행기  ―  USB로 1회만 실행
#
#   ./run_provision.sh            # 대화형 (포트 자동탐지 → AP 스캔 → SSID/PW 입력)
#   ./run_provision.sh --scan     # 주변 Wi-Fi 목록만
#   ./run_provision.sh --probe    # 장치 상태/정보만 확인(읽기전용)
#   ./run_provision.sh --ssid MyAP --password 's3cret'
#   ./run_provision.sh --camera-id 7   # 아래 CAMERA_ID 대신 이번만 다른 카메라로
#
set -e
cd "$(dirname "$0")"

# ============================================================================
#  설정 — 값만 바꾸면 적용. CLI 로 넘긴 인자가 있으면 그게 우선.
# ============================================================================
# 이번에 연결하는 센서를 귀속시킬 카메라(stream) 식별자.
#  · 귀속은 **센서 id 에 담긴다**: id = "{organization}-{cameraId}-{sensorId}".
#    즉 이 값을 바꾸면 센서 id 자체가 바뀐다(정체성 변경 — 스크립트가 경고를 출력한다).
#  · 카메라를 나눠 쓰는 현장은 값을 바꿔가며 그 카메라 센서들을 프로비저닝한다.
#    (예: 카메라 7 센서 2대 등록 → CAMERA_ID=8 로 바꿔 3대 등록)
#  · 비우면(CAMERA_ID=) 센서 id 를 건드리지 않는다.
#  · x/y/heading 은 **방 좌표계** 값이고 카메라마다 별도의 방 좌표계이므로, 등록 후
#    카메라마다 ./run_auto_positioning_v2.sh --camera-id <id> 를 따로 돌려야 한다.
#
# ★★ 현장 설치 시 반드시 **병원이 부여한 숫자 cameraId** 로 교체하세요.
#    제품의 AddStreamModel.cameraId 는 int 라서 기본값 'camera1' 은 어떤 스트림과도
#    매칭되지 않습니다(= 그 스트림은 센서 0개 → 체류 알람이 아예 안 나감).
CAMERA_ID="camera1"
ORGANIZATION="pia"

if [ ! -d .venv ]; then
  echo "[setup] 가상환경(.venv) 생성…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
# 프로비저닝은 pyserial 만 있으면 됨
if ! ./.venv/bin/python -c "import serial" 2>/dev/null; then
  echo "[setup] pyserial 설치…"
  ./.venv/bin/pip install -q pyserial
fi

# 위 설정을 앞에, CLI 인자("$@")를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
exec ./.venv/bin/python provision_wifi.py \
  --camera-id "$CAMERA_ID" --organization "$ORGANIZATION" "$@"
