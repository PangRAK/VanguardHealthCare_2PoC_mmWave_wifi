#!/usr/bin/env bash
# 기록 로그 리플레이 영상 저장기 — 최적화 없이 '지금 세팅' 그대로 재생해 MP4 로 남긴다
#
#   ./run_replay.sh                                  # LOG_DIR 의 .jsonl 전부를 겹쳐 영상 1개
#   ./run_replay.sh --per-log                        # 로그마다 따로 영상 1개씩
#   ./run_replay.sh --logs a.jsonl b.jsonl           # 특정 로그만(폴더보다 우선)
#   ./run_replay.sh --params ""                      # 파일 무시하고 아래 설정 블록 값으로 재생
#   ./run_replay.sh --set MERGE_MM=0 --set GATE_MM=900   # 개별 HP 만 바꿔 재생(가장 우선)
#   ./run_replay.sh --out debug_logs/analysis/x.mp4  # 저장 경로 지정
#   ./run_replay.sh --dwell-alert-sec 60             # 장기체류 경보 임계 1분(0=끔). 아래 설정 참조
#
# run_optimization.sh 와 같은 로그·같은 재생 경로(replay_video.py)를 쓰지만 탐색은 하지 않는다.
# → "지금 세팅으로 그때 그 장면이 어떻게 보이는가" 만 확인·공유하는 용도.
#
# ※ HP 결정 규칙은 run_gui.sh 와 동일: 아래 설정 블록 ← PARAMS_FILE 이 덮어씀 ← --set 이 덮어씀.
#   그래서 기본값(PARAMS_FILE=debug_logs/best_params.yaml)이면 라이브 GUI 가 지금 쓰는 값과
#   똑같은 세팅으로 재생된다. HP 각 항목의 뜻은 run_gui.sh 머리말 표를 참고.
# ※ 장면 합성 설정(INTERVAL_SECOND/COLLISION_MM/MARGIN_*)은 run_optimization.sh 와 같은 값으로
#   두세요 — 다르면 최적화가 채점한 장면과 다른 입력을 그리게 됩니다.
set -e
cd "$(dirname "$0")"

# ===== 입력 로그 — LOG_DIR 폴더 안의 모든 .jsonl 을 자동 수집(이름 정렬) =====
#   로그 ≥2개 = 같은 방에서 동시에 움직인 것처럼 겹친 다중-인물 장면(run_optimization.sh 와 동일).
#   로그 1개(또는 --per-log) = 그 로그 그대로 단일-인물 장면.
LOG_DIR="debug_logs/logs_for_optimization"
PER_LOG=0                                    # 1 = 겹치지 않고 로그마다 따로 영상 저장
# ===== 저장 =====
OUT_DIR="debug_logs/analysis"                # 지워도 되는 분석 산출물 폴더(run_optimization.sh 와 공유)
VIDEO_OUT="$OUT_DIR/replay.mp4"              # PER_LOG=1 이면 대신 $OUT_DIR/replay_<로그이름>.mp4
VIDEO_STRIDE=1                               # N프레임마다 1장 렌더(↑빠름·거침). 1=전프레임
VIDEO_WIDTH=960; VIDEO_HEIGHT=860            # libx264 라 홀수면 자동으로 1픽셀 줄임
LABEL=                                       # 화면 상단 제목. 비우면 자동("Current settings")
# ===== 장기체류 경보 (run_gui.sh 의 DWELL_ALERT_SEC 와 같은 규칙) =====
#   한 ID(사람)가 이 시간 이상 머물면 라이브 GUI 처럼 ① 화면 빨간 테두리 ② 그 사람 빨간 마커
#   ③ 알림음 — 을 재현한다. 영상은 실시간이 아니므로 ③은 그 시각에 '삐' 소리를 넣은 오디오
#   트랙으로 MP4 에 함께 굽는다(재생하면 그 순간 울린다).
#   ★ 시간 설정: 아래 값을 초 단위로. 0 = 끔.  예) 60 = 1분, 300 = 5분(제품/GUI 기본)
#      CLI 로 이번만:  ./run_replay.sh --dwell-alert-sec 60
#   ⚠ 기록 로그가 2분짜리면 300초(5분)로는 아무도 임계를 못 넘어 경보가 안 뜬다.
#     그런 경우 실행 로그에 "최대 체류 N초" 와 함께 낮추라는 안내가 나온다.
DWELL_ALERT_SEC=60
ALERT_BEEP=1                                 # 0 = 알림음 없이 화면 표시(테두리/마커)만
ALERT_BEEP_PERIOD=5                          # 경보가 계속될 때 알림음 재생 간격(초) — gui_qt 와 동일
# ===== 장면 합성 (run_optimization.sh 와 같은 값 유지) =====
INTERVAL_SECOND=0.1  # 융합 스텝 간격(초) = 제품 OD_TIME_INTERVAL_SECOND. 영상 fps 도 이 값(1/0.1=10fps)
COLLISION_MM=300     # 두 사람이 이 거리(mm) 내면 센서 병합 모사로 한 점 합침(겹치기 모드에서만 의미)
MERGE_COLLISIONS=1   # 1=병합 모사 켬, 0=끔(겹쳐도 점 2개)
MARGIN_DEG=55        # presence 판정 각도 마진(±도) — 겹치기 시 병합 대상 선별에 사용
MARGIN_MM=5000       # presence 판정 거리 마진(mm)
# ===== 융합 HP (run_gui.sh 와 같은 기본값. PARAMS_FILE 이 있으면 그 값이 덮어씀) =====
# 윈도·노이즈
WINDOW=10;          STRIDE=2;           FUSE_MIN_FRAMES=6
MOVE_MIN=250;       NOISE_RADIUS=1500;  RECENT_FRAMES=2;    JUMP_FACTOR=2.0
# 매칭·게이트
ASSIGN=hungarian;   GATE_MM=750;        DIR_PEN=120;        ANG_GATE=75
MERGE_MM=550;       COAST_GROW=350;     COAST_DECAY=0.85
# 확정·수명
QUEUE_K=3;          QUEUE_SIZE=5
MAX_MISS=1.2;       MAX_MISS_TENT=0.5;  DWELL=1;            DWELL_ENTER=0.4
# 재식별(ReID)
REID_DIST=700;      REID_MAX_GAP=3.0
# 필터 게인·기타
ALPHA=0.45;         BETA=0.20;          MAX_SPEED=3500;     PRED_DT_CAP=0.3
TRAIL_LEN=48
# 최적화 결과 불러오기: 지정하면 그 파일(YAML .yaml 또는 NAME=VALUE .sh)이 위 HP 를 덮어씀.
#   비우면 위 값 그대로. INTERVAL_SECOND 도 파일의 '_interval_second'(튜닝에 쓴 간격)로 동기화된다.
PARAMS_FILE="debug_logs/best_params.yaml"
# =========================================================

# ---- CLI 가로채기: 스크립트가 알아야 하는 것만 잡고 나머지는 replay_video.py 로 넘김 ----
LOGS=(); PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --params) PARAMS_FILE="$2"; shift 2;;
    --params=*) PARAMS_FILE="${1#*=}"; shift;;
    --per-log) PER_LOG=1; shift;;
    --out) VIDEO_OUT="$2"; shift 2;;
    --out=*) VIDEO_OUT="${1#*=}"; shift;;
    --logs|--gt-logs)                       # 이후 '--' 로 시작하지 않는 인자를 모두 로그로
      shift
      while [ $# -gt 0 ] && [ "${1:0:2}" != "--" ]; do LOGS+=("$1"); shift; done;;
    *) PASS+=("$1"); shift;;
  esac
done

# 로그를 CLI 로 안 줬으면 LOG_DIR 에서 자동 수집
if [ ${#LOGS[@]} -eq 0 ] && [ -n "$LOG_DIR" ] && [ -d "$LOG_DIR" ]; then
  shopt -s nullglob                          # 매칭 0개면 패턴 그대로 남지 않도록
  for _f in "$LOG_DIR"/*.jsonl; do LOGS+=("$_f"); done
  shopt -u nullglob
fi
if [ ${#LOGS[@]} -eq 0 ]; then
  echo "⚠  재생할 로그가 없습니다. '$LOG_DIR' 폴더에 .jsonl 을 넣거나 --logs 로 지정하세요."
  exit 1
fi

# ---- PARAMS_FILE 로 위 HP 덮어쓰기 (run_gui.sh 와 같은 규칙) ----
#   .yaml/.yml → 플랫 YAML(KEY: v) 파싱, 그 외 → NAME=VALUE 를 source(하위호환).
#   '_interval_second'(튜닝에 쓴 간격)는 INTERVAL_SECOND 로 되살려 재생 조건을 자동으로 맞춘다.
if [ -n "$PARAMS_FILE" ]; then
  if [ -f "$PARAMS_FILE" ]; then
    echo "[params] 설정 불러오기: $PARAMS_FILE  (위 기본값을 덮어씀)"
    case "$PARAMS_FILE" in
      *.yaml|*.yml)
        while IFS= read -r _line; do
          _line="${_line%%#*}"                       # 주석 제거
          case "$_line" in *:*) ;; *) continue;; esac
          _key="${_line%%:*}"; _val="${_line#*:}"
          _key="$(printf '%s' "$_key" | tr -d '[:space:]')"          # 키 공백 제거
          _val="${_val#"${_val%%[![:space:]]*}"}"; _val="${_val%"${_val##*[![:space:]]}"}"  # 값 trim
          if [ "$_key" = "_interval_second" ]; then _key=INTERVAL_SECOND; fi
          case "$_key" in [A-Z]*) printf -v "$_key" '%s' "$_val";; esac
        done < "$PARAMS_FILE"
        ;;
      *) source "$PARAMS_FILE"                          # 하위호환(NAME=VALUE)
         if [ -n "${_interval_second:-}" ]; then INTERVAL_SECOND="$_interval_second"; fi;;
    esac
    if [ -z "$LABEL" ]; then LABEL="Current settings ($(basename "$PARAMS_FILE"))"; fi
  else
    echo "[params] 경고: 파일이 없어 기본 설정으로 재생합니다 → $PARAMS_FILE"
  fi
fi
if [ -z "$LABEL" ]; then LABEL="Current settings (run_replay.sh)"; fi

# ---- HP → --set NAME=VALUE (replay_video.py 가 fusion.NAME2KW 로 해석) ----
SETS=()
for _n in WINDOW STRIDE FUSE_MIN_FRAMES MOVE_MIN NOISE_RADIUS RECENT_FRAMES JUMP_FACTOR \
          ASSIGN GATE_MM DIR_PEN ANG_GATE MERGE_MM COAST_GROW COAST_DECAY \
          QUEUE_K QUEUE_SIZE MAX_MISS MAX_MISS_TENT DWELL DWELL_ENTER \
          REID_DIST REID_MAX_GAP ALPHA BETA MAX_SPEED PRED_DT_CAP TRAIL_LEN ; do
  _v="${!_n}"
  if [ -n "$_v" ]; then SETS+=(--set "$_n=$_v"); fi
done

COMMON=(--interval-sec "$INTERVAL_SECOND" --collision-mm "$COLLISION_MM"
        --margin-deg "$MARGIN_DEG" --margin-mm "$MARGIN_MM"
        --frame-stride "$VIDEO_STRIDE" --width "$VIDEO_WIDTH" --height "$VIDEO_HEIGHT"
        --dwell-alert-sec "$DWELL_ALERT_SEC" --alert-beep-period "$ALERT_BEEP_PERIOD"
        "${SETS[@]}")
if [ "$MERGE_COLLISIONS" != "1" ]; then COMMON+=(--no-collision-merge); fi
if [ "$ALERT_BEEP" != "1" ]; then COMMON+=(--no-alert-beep); fi

if [ ! -d .venv ]; then
  echo "[setup] 가상환경(.venv) 생성…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
# 렌더에 gui_qt(RadarPlot) 를 그대로 쓰므로 GUI 의존성이 필요
if ! ./.venv/bin/python -c "import PySide6, pyqtgraph, numpy, aioesphomeapi" 2>/dev/null; then
  echo "[setup] 의존성 설치 (PySide6, pyqtgraph …)… 잠시 걸립니다"
  ./.venv/bin/pip install -q -r requirements.txt -r requirements-gui.txt
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "⚠  ffmpeg 가 없어 MP4 로 저장할 수 없습니다. (brew install ffmpeg)"
  exit 1
fi

# 위 설정을 앞에, 사용자가 CLI 로 넘긴 나머지 인자를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
if [ "$PER_LOG" = "1" ]; then
  echo "[replay] 로그별 재생: ${#LOGS[@]}개 → $OUT_DIR/replay_<로그이름>.mp4"
  for _log in "${LOGS[@]}"; do
    _base="$(basename "$_log")"; _base="${_base%.jsonl}"; _base="${_base// /_}"
    echo ""; echo "── $_base ──"
    ./.venv/bin/python replay_video.py --logs "$_log" \
      --out "$OUT_DIR/replay_$_base.mp4" --label "$LABEL · $_base" \
      "${COMMON[@]}" "${PASS[@]}"
  done
else
  echo "[replay] ${#LOGS[@]}개 로그를 겹쳐 재생 → $VIDEO_OUT"
  ./.venv/bin/python replay_video.py --logs "${LOGS[@]}" \
    --out "$VIDEO_OUT" --label "$LABEL" \
    "${COMMON[@]}" "${PASS[@]}"
fi
