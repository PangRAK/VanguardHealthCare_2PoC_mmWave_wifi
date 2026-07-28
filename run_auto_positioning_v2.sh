#!/usr/bin/env bash
# 다중 로그 기반 자동 포지셔닝(v2) — 여러 debug 로그를 한꺼번에 활용해 센서 배치 추정
#
#   ./run_auto_positioning_v2.sh                 # GT_DIR 폴더의 모든 .jsonl 로 배치 추정 → epl_config.json 저장
#   ./run_auto_positioning_v2.sh --dry-run       # 계산만(저장 안 함)
#   ./run_auto_positioning_v2.sh --gt-logs a.jsonl b.jsonl   # 특정 로그만(폴더보다 우선)
#   ./run_auto_positioning_v2.sh --ref 98bd80    # 기준센서 지정(수평 설치로 아는 센서)
#   ./run_auto_positioning_v2.sh --selftest      # 합성 다중로그로 알고리즘 검증(하드웨어/파일 불필요)
#
# run_auto_positioning.sh(지금 걸어다니며 1회 측정) 와 결과물은 동일(epl_config.json 의
# x,y,heading_deg(Yaw),pitch_deg,roll_deg,flip 저장). 대신 '이미 기록해 둔 로그 여러 개'를
# 풀링해 추정한다 → 단일 측정보다 대응점이 많아 배치가 더 안정적으로 나온다.
#
# 전제: 모든 로그가 '동일한 센서 배치'에서 '한 사람'을 기록한 것.
#   (run_optimization.sh 가 쓰는 단일-인물 로그와 동일 → 같은 폴더를 그대로 재사용)
# ★ 각 로그에서 사람이 '두 센서가 함께 보는 겹침 구역'을 곡선으로 지나가야 배치가 풀린다.
set -e
cd "$(dirname "$0")"

# ============================================================================
#  설정 — 값만 바꾸면 적용. CLI 로 넘긴 인자가 있으면 그게 우선.
# ============================================================================
GT_DIR="debug_logs/logs_for_optimization"   # 이 폴더의 모든 *.jsonl 을 풀링(run_optimization.sh 와 동일 폴더)
HZ=15                # 프레임화 주파수(Hz) — 보통 그대로
MAX_GAP_MS=700       # 보간 허용 최대 공백(ms)
MIN_OVERLAP=20       # 센서쌍 최소 동시관측 표본(풀링하면 로그별로 적어도 합쳐서 채워짐)
INLIER_MM=300        # RANSAC 인라이어 임계(mm)
REF=                 # 기준센서 id(수평 설치로 아는 센서). 비우면 자동선택. 예: REF=98bd80

APOS_ARGS=(--logs-dir "$GT_DIR" --hz "$HZ" --max-gap-ms "$MAX_GAP_MS"
           --min-overlap "$MIN_OVERLAP" --inlier-mm "$INLIER_MM")
if [ -n "$REF" ]; then APOS_ARGS+=(--ref "$REF"); fi

if [ ! -d .venv ]; then
  echo "[setup] 가상환경(.venv) 생성…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
# 다중 로그 포지셔닝 의존성: numpy(계산)만 필요(라이브 수신 없음 → aioesphomeapi 불필요)
if ! ./.venv/bin/python -c "import numpy" 2>/dev/null; then
  echo "[setup] 의존성 설치 (numpy)…"
  ./.venv/bin/pip install -q numpy
fi

# 위 설정을 앞에, CLI 인자("$@")를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
exec ./.venv/bin/python auto_positioning_multi.py "${APOS_ARGS[@]}" "$@"
