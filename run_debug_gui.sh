#!/usr/bin/env bash
# Everything Presence Lite · mmWave 네이티브 GUI 실행기 — 디버그 모드 (Wi-Fi 판)
#
#   ./run_debug_gui.sh                       # epl_config.json 의 센서들에 무선 접속 (기본 60초 후 자동 종료)
#   ./run_debug_gui.sh --host 192.168.1.42   # 주소 직접 지정(1~n개 가능)
#   ./run_debug_gui.sh --demo                # 하드웨어 없이 합성 데이터
#   DURATION=120 ./run_debug_gui.sh          # 기록 시간 120초로 (0=수동 종료까지 계속)
#   ./run_debug_gui.sh --duration 120        # (동일, CLI 로 지정)
#
# ※ run_gui.sh 와 동일하되 '디버그'가 켜져 있음:
#   1) 융합(ID 매칭) '후' 결과(큰 점) + 융합 '전' 센서별 원시점(작은 점)을 동시에 표시
#   2) '묶음선' — 각 원시점이 어느 융합 ID 로 묶였는지 얇은 선으로 연결(묶이지 않은 점=필터/노이즈로 버려진 것)
#   3) 원시 검출 스트림을 JSONL 로 기록(REC 경로) → 분석·하이퍼파라미터 최적화(HPO)에 사용
#      (헤더에 '기록 후' 적용 HP 전체 + 센서 외부보정이 함께 남음)
#   화면에서 헤더의 '원시점'·'묶음선' 체크박스로 껐다 켤 수 있음.
#
# ┌────────────────────────────────────────────────────────────────────────────┐
# │  파이프라인과 하이퍼파라미터 적용 시점 (raw 기록 경계 기준)                        │
# ├────────────────────────────────────────────────────────────────────────────┤
# │  센서 원시 →(A)→ 방좌표 변환 →(B)→ dets 생성 →【★여기서 raw 기록★】→(C)→ FusionTracker → ID │
# │                                                                              │
# │ ● 기록 '전'에 이미 적용(=raw 에 녹아있음, run_gui.sh 튜닝값 아님):                   │
# │   - 하드웨어 고정: FOV 120°(±60), range 6m                                      │
# │   - 센서 외부보정(A): heading(Yaw)·pitch·roll·flip·(x,y) = room_transform (epl_config)│
# │   - 수집 상수: STALE_SEC(존재판정), 원시 TRAIL_LEN, dets 방향 최소이동 45mm(하드코드)  │
# │   - FUSE_HZ : '변환'은 아니고 raw 를 몇 Hz 로 '기록'할지 주기만 결정                  │
# │ ● 기록 '후'에 적용(=이 파일/‑run_gui.sh 의 튜닝값 전부, 재생 시 바꿔가며 실험 가능):    │
# │   WINDOW STRIDE FUSE_MIN_FRAMES MOVE_MIN NOISE_RADIUS RECENT_FRAMES JUMP_FACTOR │
# │   ASSIGN GATE_MM DIR_PEN ANG_GATE MERGE_MM COAST_GROW COAST_DECAY               │
# │   QUEUE_K QUEUE_SIZE MAX_MISS MAX_MISS_TENT DWELL/DWELL_ENTER                   │
# │   REID_DIST REID_MAX_GAP  ← ★ 재식별(먹혔다 재등장한 사람 옛 ID 로 되살림)      │
# │   ALPHA BETA MAX_SPEED PRED_DT_CAP  (+ 표시: LERP VEC_SCALE TRAIL_LEN)          │
# │  → 즉 run_gui.sh 의 튜닝값은 MOVE_MIN 포함 '전부' 기록 후 단계. raw 는 순수 입력.     │
# │  (각 값 의미·↑↓ 효과는 run_gui.sh 상단 표 참조)                                    │
# └────────────────────────────────────────────────────────────────────────────┘
set -e
cd "$(dirname "$0")"

# ===== 융합 설정 (run_gui.sh 와 동일하게 유지 — '기록 후' 단계에 적용됨) =====
# 윈도·노이즈
WINDOW=10;          STRIDE=2;           FUSE_MIN_FRAMES=6
MOVE_MIN=250;       NOISE_RADIUS=1500;  RECENT_FRAMES=2;    JUMP_FACTOR=2.0
# 매칭·게이트
ASSIGN=hungarian;   GATE_MM=750;        DIR_PEN=120;        ANG_GATE=75
MERGE_MM=550;       COAST_GROW=350;     COAST_DECAY=0.85
# 확정·수명
QUEUE_K=3;          QUEUE_SIZE=5
MAX_MISS=1.2;       MAX_MISS_TENT=0.5;  DWELL=1;            DWELL_ENTER=0.4
# 재식별(ReID) — 먹혀서 사라졌다 재등장한 사람을 옛 ID 로 되살림. REID_DIST=0 이면 끔.
#   REID_DIST 는 센서 병합 한계(~0.7m)에 맞춘 보수값 권장. 너무 크면(예 1000+) 문 근처에서 '다른 사람'이
#   나간 사람 ID 를 가로챌 위험↑. REID_MAX_GAP 은 MAX_MISS 보다 넉넉히 커야 유효(실효창 ≈ GAP − MAX_MISS).
REID_DIST=700;      REID_MAX_GAP=3.0
# 필터 게인·기타
ALPHA=0.45;         BETA=0.20;          MAX_SPEED=3500;     PRED_DT_CAP=0.3
TRAIL_LEN=48;       FUSE_HZ=15
# GUI 표시
LERP=0.30;          VEC_SCALE=700
# ===== 디버그 =====
SHOW_RAW=1;         SHOW_LINKS=1        # 시작 시 원시점·묶음선 표시(0=끔, 화면에서 토글 가능)
REC_DIR="debug_logs"                    # 원시 기록 저장 폴더
REC="$REC_DIR/raw_$(date +%Y%m%d_%H%M%S).jsonl"   # 기록 파일(빈 값으로 두면 기록 안 함)
# 실행(기록) 시간(초). 이 시간이 지나면 자동 종료하며 기록 파일을 정상 마감한다.
#   기본 60초. 0 이면 수동 종료(창 닫기)까지 계속.
#   덮어쓰기: DURATION=120 ./run_debug_gui.sh   또는   ./run_debug_gui.sh --duration 120
DURATION="${DURATION:-120}"
# =========================================================

FUSION_ARGS=(
  --fuse-hz "$FUSE_HZ"
  --window "$WINDOW" --stride "$STRIDE" --fuse-min-frames "$FUSE_MIN_FRAMES"
  --move-min "$MOVE_MIN" --noise-radius "$NOISE_RADIUS" --recent-frames "$RECENT_FRAMES"
  --jump-factor "$JUMP_FACTOR"
  --assign "$ASSIGN" --gate-mm "$GATE_MM" --dir-pen "$DIR_PEN" --ang-gate "$ANG_GATE"
  --merge-mm "$MERGE_MM" --coast-grow "$COAST_GROW" --coast-decay "$COAST_DECAY"
  --queue-k "$QUEUE_K" --queue-size "$QUEUE_SIZE"
  --reid-dist "$REID_DIST" --reid-max-gap "$REID_MAX_GAP"
  --max-miss "$MAX_MISS" --max-miss-tentative "$MAX_MISS_TENT" --dwell-enter "$DWELL_ENTER"
  --alpha "$ALPHA" --beta "$BETA" --max-speed "$MAX_SPEED"
  --pred-dt-cap "$PRED_DT_CAP" --trail-len "$TRAIL_LEN"
)
if [ "$DWELL" = "1" ]; then FUSION_ARGS+=(--dwell); fi

GUI_ARGS=(--lerp "$LERP" --vec-scale "$VEC_SCALE")
if [ "$SHOW_RAW" = "1" ]; then GUI_ARGS+=(--show-raw); fi
if [ "$SHOW_LINKS" = "1" ]; then GUI_ARGS+=(--show-links); fi
if [ -n "$REC" ]; then mkdir -p "$REC_DIR"; GUI_ARGS+=(--record "$REC"); fi
if [ "$DURATION" != "0" ]; then GUI_ARGS+=(--duration "$DURATION"); fi

if [ ! -d .venv ]; then
  echo "[setup] 가상환경(.venv) 생성…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi

# GUI + 무선 의존성 확인 후 없으면 설치
if ! ./.venv/bin/python -c "import PySide6, pyqtgraph, numpy, aioesphomeapi" 2>/dev/null; then
  echo "[setup] 의존성 설치 (PySide6, pyqtgraph, aioesphomeapi …)… 잠시 걸립니다"
  ./.venv/bin/pip install -q -r requirements.txt -r requirements-gui.txt
fi

# 위 설정을 앞에, 사용자가 CLI 로 넘긴 인자("$@")를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
exec ./.venv/bin/python gui_qt.py "${FUSION_ARGS[@]}" "${GUI_ARGS[@]}" "$@"
