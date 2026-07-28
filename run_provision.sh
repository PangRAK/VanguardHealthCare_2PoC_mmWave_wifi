#!/usr/bin/env bash
# 센서 Wi-Fi 연결(프로비저닝) 실행기  ―  USB로 1회만 실행
#
#   ./run_provision.sh            # 대화형 (포트 자동탐지 → AP 스캔 → SSID/PW 입력)
#   ./run_provision.sh --scan     # 주변 Wi-Fi 목록만
#   ./run_provision.sh --probe    # 장치 상태/정보만 확인(읽기전용)
#   ./run_provision.sh --ssid MyAP --password 's3cret'
#
set -e
cd "$(dirname "$0")"

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

exec ./.venv/bin/python provision_wifi.py "$@"
