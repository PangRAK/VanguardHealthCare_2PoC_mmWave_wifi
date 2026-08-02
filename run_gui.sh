#!/usr/bin/env bash
# Everything Presence Lite · mmWave 네이티브 GUI 실행기 (PySide6 + pyqtgraph, Wi-Fi 판)
#
#   ./run_gui.sh                       # epl_config.json 의 센서에 무선 접속
#   ./run_gui.sh --host 192.168.1.42   # 주소 직접 지정
#   ./run_gui.sh --demo                # 하드웨어 없이 합성 데이터
#
# ※ 먼저 ./run_provision.sh 로 센서를 Wi-Fi 에 연결해 두어야 합니다.
#
# ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
# │  하이퍼파라미터 설명 (값은 아래 '설정' 블록에서 바꾸세요. CLI 인자가 있으면 그게 우선.)                                             │
# ├─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
# │ [단위] '프레임' = 융합 스텝 1회(INTERVAL_SECOND, 기본 0.1초=10Hz). GUI 60fps·센서 원본 ~3Hz 와 다름.                                │
# │ [증상별] 위치튐→WINDOW↑ / ID스위칭→STRIDE↑·ASSIGN=hungarian·RECENT_FRAMES↑                                                          │
# │          1명이여러명→FUSE_MIN_FRAMES↑·NOISE_RADIUS↑ / 인원수깜빡→DWELL=1                                                            │
# │ [ReID] 가까웠다멀어진뒤 새 ID로 바뀜(먹혔다 재등장)→REID_DIST↑·REID_MAX_GAP↑ (NOISE_RADIUS 와 독립)                                 │
# │ [표기] 무표시=원래 제품 트래커 · ★=이번 노이즈억제 추가 · ⊕=이번 추가 옵션(토글)                                                    │
# │        각 줄 뒤 ↑=값을 올릴 때 / ↓=값을 내릴 때 의미                                                                                │
# │                                                                                                                                     │
# │ ● 윈도·노이즈  ← 그룹 전체 ★ (이번에 추가된 노이즈 억제 파이프라인)                                                                 │
# │ ★ WINDOW            최근 N프레임 median 스무딩(실사용 8~12).   ↑ 노이즈↓·지연↑  ↓ 반응↑·노이즈↑                                     │
# │ ★ STRIDE            N프레임마다 1회 추론(출력주기=스텝주기×STRIDE).   ↑ 안정↑·반응↓  ↓ 반응↑·요동↑                                  │
# │ ★ FUSE_MIN_FRAMES   창에서 이 횟수 미만 등장=유령 버림.   ↑ 유령↓·엄격(진짜도 놓칠↑)  ↓ 관대(유령 통과↑)                            │
# │ ★ MOVE_MIN          이동이 이 mm 넘을 때만 '방향' 인정.   ↑ 가짜방향↓(느린이동 방향무시)  ↓ 방향 민감(가짜방향↑)                    │
# │ ★ NOISE_RADIUS      확정포인트 이 반경(mm)내 '처음보는' 갑툭튀=흡수(GUI 원,0=끔).   ↑ 흡수↑(유령↓)  ↓ 약함 (ReID 재등장은 흡수 예외)│
# │ ★ RECENT_FRAMES     최근 이 프레임에 없는 (센서,tid)=스테일 제외.   ↑ 관대(깜빡임 견딤·반전스위칭억제 약화)  ↓ 엄격(반전 ID스위칭↓) │
# │ ★ JUMP_FACTOR       한 스텝 최대이동=MAX_SPEED×dt×값, 초과분 클램프.   ↑ 느슨(스파이크 통과↑)  ↓ 빡셈(빠른이동 잘릴수)              │
# │ ● 매칭·게이트                                                                                                                       │
# │ ⊕ ASSIGN            greedy | hungarian.   모드(높낮음 아님): greedy=근접우선 / hungarian=전역최적(교차 ID스왑↓)                     │
# │   GATE_MM           트랙↔관측 매칭 최대 거리(mm).   ↑ 관대(끊김↓·재획득↑·오매칭↑)  ↓ 엄격(오매칭↓·놓침↑)                            │
# │   DIR_PEN           이동방향 불일치 매칭 비용(mm).   ↑ 방향 불일치 회피↑(교차 도움)  ↓ 방향 무시(0=무시)                            │
# │   ANG_GATE          Stage A·방향 게이트 각도(°).   ↑ 관대(방향차 커도 묶음)  ↓ 엄격(조금만 달라도 분리)                             │
# │   MERGE_MM          두 확정 트랙이 이 거리(mm)내면 중복 병합.   ↑ 병합 적극(중복↓·근접인원 합쳐질위험↑)  ↓ 보존(중복↑)              │
# │   COAST_GROW        가려짐(coast) 1초당 게이트 확장(mm).   ↑ 빨리 확장(재획득↑·오매칭↑)  ↓ 보수적                                   │
# │ ★ COAST_DECAY       coast 중 속도 감쇠(0~1).   ↑(1쪽) 관성 유지(반전 오버슈트↑)  ↓(0쪽) 즉시 감속(반전 안정)                        │
# │ ● 확정·수명                                                                                                                         │
# │ ⊕ QUEUE_K           확정 큐 임계 — 최근 QUEUE_SIZE 중 이 횟수 관측=확정.   ↑ 엄격(유령↓·확정 지연↑)  ↓ 빨리 확정(유령↑)             │
# │ ⊕ QUEUE_SIZE        확정 큐 길이(최근 프레임 수)·사내 명칭 'queue'.   ↑ 긴 이력(깜빡임 견딤↑·유령 혼입↑)  ↓ 짧은 이력(엄격·취약)    │
# │   MAX_MISS          확정 트랙이 안 보여도 유지되는 시간(초).   ↑ 오래 유지(가림 강건·잔상/인원과다↑)  ↓ 빨리 소멸                   │
# │   MAX_MISS_TENT     미확정(후보) 트랙 유지 시간(초).   ↑ 후보 오래 유지(유령 확정 기회↑)  ↓ 빨리 버림(신규 놓칠수↑)                 │
# │ ⊕ DWELL/DWELL_ENTER 재실 '인원수' 디바운스(DWELL 0/1=끔/켬).   ENTER↑ 더 머물러야 카운트(진입튐↓·지연↑)  ↓ 확정 즉시 카운트         │
# │ ● 재식별(ReID)  ← 그룹 전체 ★ (근접 병합으로 먹혀 사라진 사람이 재등장하면 옛 ID 로 되살림)                                         │
# │ ★ REID_DIST         소멸한 확정 ID 를 이 반경(mm)내 재등장에 되살림(0=끔).   ↑ 멀어도 되살림(오ReID↑)  ↓ 가까이만(엄격)             │
# │ ★ REID_MAX_GAP      마지막 검출 후 이 초 내 재등장해야 같은 ID(MAX_MISS 초과분만 유효).   ↑ 오래 기억(되살림↑·오ReID↑)  ↓ 짧게      │
# │ ● 필터 게인·기타  ← 전부 원래 제품 트래커. [게인]=잔차(측정-예측) 반영 비율(0~1)                                                    │
# │   ALPHA             위치 보정 게인(0~1).   ↑ 측정 추종(반응↑·노이즈↑)  ↓ 예측 신뢰(부드럽·지연↑)                                    │
# │   BETA              속도 보정 게인(0~1).   ↑ 속도 빨리 갱신(전환 추종↑·요동↑)  ↓ 관성↑·전환 늦음                                    │
# │   MAX_SPEED         사람 최대 속도 상한(mm/s).   ↑ 빠른이동 허용(스파이크 통과↑)  ↓ 빡셈(빠른이동 잘림)                             │
# │   PRED_DT_CAP       예측 이동 dt 상한(초).   ↑ 공백시 예측 크게 전진(튐↑)  ↓ 보수적                                                 │
# │   TRAIL_LEN         궤적 길이(점 개수).   ↑ 꼬리 길게  ↓ 짧게 (표시만)                                                              │
# │   INTERVAL_SECOND    융합 스텝 간격(초)·window/stride '프레임' 기준(제품과 같은 값 필수).   ↓ 자주(반응↑·CPU↑)  ↑ 드물게            │
# │ ● GUI 표시(이 스크립트 전용 · 원래 표시값)                                                                                          │
# │   LERP              화면 위치 보간(0~1).   ↑(1쪽) 즉시 스냅(딱딱)  ↓(작게) 부드럽·느림                                              │
# │   VEC_SCALE         속도벡터 화살표 길이 배율.   ↑ 화살표 길게  ↓ 짧게 (표시만)                                                     │
# │ ⊕ DWELL_ALERT_SEC   한 ID 가 이 초 이상 체류하면 경보(빨간 테두리+빨간 마커+알림음). 0=끔. 기본 300(=5분)                            │
# └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
set -e
cd "$(dirname "$0")"

# ===== 설정 (여기서 값만 바꾸세요 — 설명은 위 표 참조) =====
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
TRAIL_LEN=48
# 융합 스텝 간격(초) — 제품(Product-AI-mono `vanguard_mmwave`)의 OD_TIME_INTERVAL_SECOND 와 같은 뜻.
#   0.1초마다 스냅샷 1개 = 융합 1스텝. WINDOW/STRIDE/QUEUE_SIZE/RECENT_FRAMES/FUSE_MIN_FRAMES 는
#   '프레임 수' 라 이 값이 곧 그 파라미터들의 시간 단위다(WINDOW=10 → 0.1초면 1.0초 창).
#   ⚠ run_optimization.sh 의 INTERVAL_SECOND 와 같은 값이어야 합니다 — 다르면 튜닝된 최적값의
#     시간 폭이 실행 때 달라집니다. (예전 값은 FUSE_HZ=15 → INTERVAL_SECOND=0.0667 에 해당)
#     PARAMS_FILE 이 있으면 그 파일에 적힌 '튜닝에 쓴 간격'이 이 값을 덮어써 자동으로 맞춰줍니다.
#   ⚠ CLI 로 덮어쓸 때는 --interval-sec 를 쓰세요. --fuse-hz 는 이 값(초)에 밀려 무시됩니다
#     (초가 Hz 보다 우선 — fusion.apply_fusion_opts). 예) ./run_gui.sh --interval-sec 0.0667
INTERVAL_SECOND=0.1
# GUI 표시
LERP=0.30;          VEC_SCALE=700
# 장기체류 경보: 한 사람(ID)이 이 시간(초) 이상 머물면 화면 빨간 테두리 + 그 사람 빨간 표시 + 알림음.
#   기본 300초(=5분). 0 이면 끔.  덮어쓰기: ./run_gui.sh --dwell-alert-sec 600
DWELL_ALERT_SEC=300
# 최적화 결과 불러오기: 지정하면 그 파일(YAML .yaml 또는 NAME=VALUE .sh)의 값이 위 설정을 덮어씀. 비우면 위 값.
#   예) PARAMS_FILE="debug_logs/best_params.yaml"  또는  ./run_gui.sh --params debug_logs/best_params.yaml
PARAMS_FILE="debug_logs/best_params.yaml"
# =========================================================

# CLI 에서 --params <파일> 을 가로채고(위 PARAMS_FILE 보다 우선), 나머지 인자는 gui_qt.py 로 전달
GUI_PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --params) PARAMS_FILE="$2"; shift 2;;
    --params=*) PARAMS_FILE="${1#*=}"; shift;;
    *) GUI_PASS+=("$1"); shift;;
  esac
done

# 최적화 결과 파일이 지정되면 그 값으로 위 설정을 덮어씀 (변수명은 run_gui.sh 와 동일).
#   .yaml/.yml → 플랫 YAML(KEY: v) 파싱, 그 외 → NAME=VALUE 를 source(하위호환).
#   ★ INTERVAL_SECOND 는 optimize_fusion.py 가 '튜닝에 쓴 간격'을 `_interval_second` 키로 함께
#     적으므로, 파일이 있으면 그 값으로 자동 동기화된다(= 튜닝 조건 그대로 재생).
#     ('_' 접두는 같은 파일을 먹는 제품이 그 키를 조용히 무시하게 하려는 것 — optimize_fusion.py 참조)
#     LERP/VEC_SCALE 는 파일에 없어 위 기본값 유지.
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
          # 튜닝 조건 키는 '_' 접두(제품이 조용히 무시하도록) → 여기서 설정 변수명으로 되살린다
          if [ "$_key" = "_interval_second" ]; then _key=INTERVAL_SECOND; fi
          case "$_key" in [A-Z]*) printf -v "$_key" '%s' "$_val";; esac
        done < "$PARAMS_FILE"
        ;;
      *) source "$PARAMS_FILE"                          # 하위호환(NAME=VALUE)
         if [ -n "${_interval_second:-}" ]; then INTERVAL_SECOND="$_interval_second"; fi;;
    esac
  else
    echo "[params] 경고: 파일이 없어 기본 설정으로 실행합니다 → $PARAMS_FILE"
  fi
fi

FUSION_ARGS=(
  --interval-sec "$INTERVAL_SECOND"
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
GUI_ARGS=(--lerp "$LERP" --vec-scale "$VEC_SCALE" --dwell-alert-sec "$DWELL_ALERT_SEC")

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

# 위 설정을 앞에, 사용자가 CLI 로 넘긴 나머지 인자를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
exec ./.venv/bin/python gui_qt.py "${FUSION_ARGS[@]}" "${GUI_ARGS[@]}" "${GUI_PASS[@]}"
