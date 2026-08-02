#!/usr/bin/env bash
# 다중 센서 자동 포지셔닝(외부 캘리브레이션) 실행기
#
#   ./run_auto_positioning.sh                 # 센서 연결 확인 → 측정 → 계산 → epl_config.json 저장
#   ./run_auto_positioning.sh --seconds 90    # 측정 시간(기본 60초)
#   ./run_auto_positioning.sh --dry-run       # 계산만(저장 안 함)
#   ./run_auto_positioning.sh --host a.local b.local c.local
#   ./run_auto_positioning.sh --camera-id 7   # 아래 CAMERA_ID 대신 이번만 다른 카메라로
#   ./run_auto_positioning.sh --selftest      # 합성 데이터로 알고리즘 검증(하드웨어 불필요)
#
# ※ 먼저 ./run_provision.sh 로 센서들을 Wi-Fi 에 연결해 두어야 합니다.
# ★ 측정 중에는 방에 '한 사람'만 있어야 하며, 겹치는 구역을 포함해 곡선으로 걸어 다니세요.
# ★ 카메라를 나눠 쓰면 CAMERA_ID 를 바꿔 카메라마다 따로 측정하세요 — x/y/heading 은
#   **방 좌표계** 값이고 카메라마다 별도의 방 좌표계입니다.
set -e
cd "$(dirname "$0")"

# ============================================================================
#  측정 설정 — 값만 바꾸면 적용됩니다. CLI 로 넘긴 인자가 있으면 그게 우선.
# ============================================================================
MEASURE_SECONDS=120   # 측정(걸어다니는) 시간(초). 센서 많거나 방 넓으면 90~120 권장
START_DELAY=3        # 시작 전 카운트다운(초) — 자리 잡을 준비 시간
HZ=15                # 샘플링/보간 주파수(Hz) — 보통 그대로 두면 됨
REF=                 # 기준센서 id(수평 설치로 아는 센서). 비우면 자동선택. 예: REF=98bd80
# 캘리브레이션할 카메라(stream) 식별자. id 접두가 이 카메라를 가리키는 센서에만
# 접속·저장한다. 비우면(CAMERA_ID=) 카메라 구분 없이 등록된 전 센서를 쓴다.
CAMERA_ID="1"
ORGANIZATION="pia"

APOS_ARGS=(--seconds "$MEASURE_SECONDS" --start-delay "$START_DELAY" --hz "$HZ"
           --camera-id "$CAMERA_ID" --organization "$ORGANIZATION")
if [ -n "$REF" ]; then APOS_ARGS+=(--ref "$REF"); fi

if [ ! -d .venv ]; then
  echo "[setup] 가상환경(.venv) 생성…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
# 자동 포지셔닝 의존성: numpy(계산) + aioesphomeapi(수신)
if ! ./.venv/bin/python -c "import numpy, aioesphomeapi" 2>/dev/null; then
  echo "[setup] 의존성 설치 (numpy, aioesphomeapi, zeroconf)…"
  ./.venv/bin/pip install -q -r requirements.txt numpy
fi

# 위 설정을 앞에, CLI 인자("$@")를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
exec ./.venv/bin/python auto_positioning.py "${APOS_ARGS[@]}" "$@"
