#!/usr/bin/env bash
# Everything Presence Lite · mmWave 웹 시각화 실행기 (Wi-Fi 판)
#
#   ./run.sh                       # epl_config.json 의 센서에 무선 접속 → 브라우저 열기
#   ./run.sh --host 192.168.1.42   # 주소 직접 지정
#   ./run.sh --demo                # 하드웨어 없이 합성 데이터
#   ./run.sh --transport web       # web_server SSE 폴백
#
# ※ 먼저 ./run_provision.sh 로 센서를 Wi-Fi 에 연결해 두어야 합니다.
#
# ┌────────────────────────────────────────────────────────────────────────────┐
# │  하이퍼파라미터 설명 (값은 아래 '설정' 블록에서 바꾸세요. CLI 인자가 있으면 그게 우선.)   │
# ├────────────────────────────────────────────────────────────────────────────┤
# │ [단위] '프레임' = 융합 처리 프레임(FUSE_HZ, 기본 15Hz).                            │
# │ [증상별] 위치튐→WINDOW↑ / ID스위칭→STRIDE↑·ASSIGN=hungarian·RECENT_FRAMES↑            │
# │          1명이여러명→FUSE_MIN_FRAMES↑·NOISE_RADIUS↑ / 인원수깜빡→DWELL=1               │
# │                                                                              │
# │ [구분] ★=이번 노이즈억제 추가: WINDOW STRIDE FUSE_MIN_FRAMES MOVE_MIN NOISE_RADIUS      │
# │        RECENT_FRAMES JUMP_FACTOR COAST_DECAY  ·  ⊕=이번 추가 옵션: ASSIGN QUEUE_* DWELL │
# │        그 외는 원래 제품 트래커 파라미터. (자세한 표기는 run_gui.sh 표)                  │
# │ ● 윈도·노이즈(★)  WINDOW(median 스무딩) STRIDE(추론간격/출력주기) FUSE_MIN_FRAMES(유령컷)  │
# │   MOVE_MIN(방향인정 최소이동mm) NOISE_RADIUS(확정포인트 반경내 갑툭튀=흡수mm,0=끔)        │
# │   RECENT_FRAMES(스테일 tid 제외) JUMP_FACTOR(스텝 최대이동 클램프)                     │
# │ ● 매칭·게이트  ASSIGN(greedy|hungarian) GATE_MM(매칭최대거리) DIR_PEN(방향불일치비용)    │
# │   ANG_GATE(방향게이트°) MERGE_MM(중복병합거리) COAST_GROW(가림시 게이트확장) COAST_DECAY  │
# │ ● 확정·수명  QUEUE_K/QUEUE_SIZE(확정 큐: 최근 SIZE중 K관측=확정) MAX_MISS/              │
# │   MAX_MISS_TENT(트랙 유지시간s) DWELL/DWELL_ENTER(인원수 디바운스)                     │
# │ ● 필터게인·기타  ALPHA(위치게인) BETA(속도게인) MAX_SPEED(속도상한mm/s)                 │
# │   PRED_DT_CAP(예측dt상한) TRAIL_LEN(궤적길이) FUSE_HZ(융합 주파수)                     │
# │   (자세한 설명은 run_gui.sh 상단 표 참조)                                           │
# └────────────────────────────────────────────────────────────────────────────┘
set -e
cd "$(dirname "$0")"

# ===== 설정 (여기서 값만 바꾸세요 — 설명은 위/‑ run_gui.sh 표 참조) =====
# 윈도·노이즈
WINDOW=10;          STRIDE=2;           FUSE_MIN_FRAMES=6
MOVE_MIN=250;       NOISE_RADIUS=1500;  RECENT_FRAMES=2;    JUMP_FACTOR=2.0
# 매칭·게이트
ASSIGN=hungarian;   GATE_MM=750;        DIR_PEN=120;        ANG_GATE=75
MERGE_MM=550;       COAST_GROW=350;     COAST_DECAY=0.85
# 확정·수명
QUEUE_K=3;          QUEUE_SIZE=5
MAX_MISS=1.2;       MAX_MISS_TENT=0.5;  DWELL=1;            DWELL_ENTER=0.4
# 필터 게인·기타
ALPHA=0.45;         BETA=0.20;          MAX_SPEED=3500;     PRED_DT_CAP=0.3
TRAIL_LEN=48;       FUSE_HZ=15
# =====================================================================

FUSION_ARGS=(
  --fuse-hz "$FUSE_HZ"
  --window "$WINDOW" --stride "$STRIDE" --fuse-min-frames "$FUSE_MIN_FRAMES"
  --move-min "$MOVE_MIN" --noise-radius "$NOISE_RADIUS" --recent-frames "$RECENT_FRAMES"
  --jump-factor "$JUMP_FACTOR"
  --assign "$ASSIGN" --gate-mm "$GATE_MM" --dir-pen "$DIR_PEN" --ang-gate "$ANG_GATE"
  --merge-mm "$MERGE_MM" --coast-grow "$COAST_GROW" --coast-decay "$COAST_DECAY"
  --queue-k "$QUEUE_K" --queue-size "$QUEUE_SIZE"
  --max-miss "$MAX_MISS" --max-miss-tentative "$MAX_MISS_TENT" --dwell-enter "$DWELL_ENTER"
  --alpha "$ALPHA" --beta "$BETA" --max-speed "$MAX_SPEED"
  --pred-dt-cap "$PRED_DT_CAP" --trail-len "$TRAIL_LEN"
)
if [ "$DWELL" = "1" ]; then FUSION_ARGS+=(--dwell); fi

if [ ! -d .venv ]; then
  echo "[setup] 가상환경(.venv) 생성…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
# 무선 데이터 수신 의존성 확인 후 없으면 설치
if ! ./.venv/bin/python -c "import aioesphomeapi, serial" 2>/dev/null; then
  echo "[setup] 의존성 설치 (aioesphomeapi, pyserial, zeroconf)…"
  ./.venv/bin/pip install -q -r requirements.txt
fi

# 위 설정을 앞에, CLI 인자("$@")를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
exec ./.venv/bin/python server.py "${FUSION_ARGS[@]}" "$@"
