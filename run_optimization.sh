#!/usr/bin/env bash
# ID 스위칭 최소화 하이퍼파라미터 최적화 실행기 (다중-인물 GT 기반)
#
#   ./run_optimization.sh                            # GT_DIR 폴더 안의 모든 .jsonl 로 최적화(자동 수집)
#   ./run_optimization.sh --gt-logs a.jsonl b.jsonl  # 특정 로그만 지정(CLI 가 폴더보다 우선)
#
# 원리: 단일-인물 debug 로그 N개(≥2)를 "같은 방에서 N명이 동시에" 처럼 겹쳐 정답 ID 를 아는 합성
#   GT 를 만들고, dets(융합 입력) 스트림을 여러 HP 로 replay 하며 채점 → 스위칭/교차병합이 최소인
#   조합을 찾는다. (replay 는 라이브와 100% 동일 재현이라 오프라인 최적화가 유효)
#
# 채점 규칙:
#   · OUT(전 센서 미검출)로 EXIT_GAP 이상 이탈 후 복귀 → ID 바뀌어도 허용
#   · 검출 중(최소 1센서) 또는 마진 안(±MARGIN_DEG°, MARGIN_MM mm)에서 다른 ID 로 교체·중복 → 금지(벌점)
#   · 두 사람을 한 ID 로 잘못 합침(교차병합) → 무거운 벌점(병합이 과도하게 느슨해지는 걸 막는 핵심)
#
# ※ 재식별(ReID: REID_DIST/REID_MAX_GAP)도 탐색 대상. 먹혔다 재등장 시 옛 ID 복원 → 스위칭↓ 이지만,
#   너무 넓/길면 남을 옛 ID 로 되살릴 위험(교차병합/스위칭 벌점으로 자동 견제). FIX_REID_DIST=0 이면 ReID 끔.
#
# ※ 로그 1개만 단독 분석하려면(하위호환): python optimize_fusion.py --log <파일> 을 직접 실행.
set -e
cd "$(dirname "$0")"

# ===== 입력: 다중-인물 GT — GT_DIR 폴더 안의 모든 .jsonl 을 자동 수집 =====
#   각 로그 = 정답 1명(단일-인물). 같은 방·센서배치·fuse_hz 로 기록한 로그만 겹칠 수 있음(≥2개 필요).
#   최적화에 쓸 로그를 이 폴더에 넣어두면 파일명을 일일이 지정할 필요 없이 전부 사용한다.
#   (CLI 로 --gt-logs a b c 를 주면 폴더 대신 그것을 우선 사용)
GT_DIR="debug_logs/logs_for_optimization"
GT_LOGS=""                                   # ↓ GT_DIR 안의 *.jsonl 을 이름 정렬 순으로 수집(폴더 없/빈=빈 값)
if [ -n "$GT_DIR" ] && [ -d "$GT_DIR" ]; then
  shopt -s nullglob                          # 매칭 0개면 패턴 그대로 남지 않도록
  for _f in "$GT_DIR"/*.jsonl; do GT_LOGS="$GT_LOGS $_f"; done
  shopt -u nullglob
  GT_LOGS="${GT_LOGS# }"                      # 앞쪽 공백 제거
fi
COLLISION_MM=300    # 두 사람이 이 거리(mm) 내면 '겹침': (R6)센서 병합 모사로 한 점 합침 + (R2)그 구간 개인채점 제외
MERGE_COLLISIONS=1  # 1=센서 병합 모사 켬(겹치면 한 점→멀어지면 갈라짐→ReID 채점 가능). 0=끔(겹쳐도 점 2개)
#   ※ ReID 를 의미있게 튜닝하려면 동선이 서로 겹치도록 기록한 로그를 쓰고 MERGE_COLLISIONS=1 로 둘 것.
W_XMERGE_EP=2000    # 교차-인물 병합(두 사람=한 ID) 에피소드 벌점 — 느슨한 HP 방지 핵심
W_XMERGE_FR=50      # 교차-인물 병합 프레임당 벌점
# ===== 탐색량 =====
ITERS=3000           # 랜덤 탐색 횟수 (↑ 더 넓게·느림)
CD_ROUNDS=4         # 좌표하강 정련 라운드
SEED=0
# ===== 채점 규칙 =====
MARGIN_DEG=55       # INSIDE 각도 마진(±도). 가로 110° → 55
MARGIN_MM=5000      # INSIDE 거리 마진(mm). 5m
EXIT_GAP=1.5        # 이 시간(초) 이상 전 센서 미검출 = 진짜 이탈(그 후 ID 바뀜 허용)
DWELL_GAP=8.0       # 이 시간(초) 이상 공백 = 체류 에피소드 종료. REID_MAX_GAP 탐색범위보다 커야(그 안 ReID 실패도 체류결손으로 채점)
MIN_COVERAGE=0.4    # INSIDE 최소 추적률(미만이면 '추적실패'로 무효 — 스위칭0 꼼수 방지)
# ===== 벌점 가중치 (클수록 그 항목을 더 강하게 억제) =====
W_SW_IN=1000        # 마진 안 스위칭 (최우선 억제)
W_SW_ED=400         # 가장자리 스위칭
W_DUP_IN=300        # 마진 안 중복(1명이 여러 ID)
W_DUP_ED=120        # 가장자리 중복
W_COV=500           # 추적 커버리지 손실
W_IDS=10            # 전체 등장 ID 수
W_DWELL=20          # 체류시간 결손 1초당 벌점(정체성 조각으로 잃은 연속 체류시간 — ReID 성패 반영). 0=끔
# ===== 파라미터 고정 (값 주면 '고정', 비우면 '최적화 대상') =====
#   예) MERGE_MM 을 100 으로 고정하고 나머지 최적화:  FIX_MERGE_MM=100
FIX_WINDOW=;          FIX_STRIDE=1;          FIX_FUSE_MIN_FRAMES=
FIX_MOVE_MIN=;        FIX_NOISE_RADIUS=;    FIX_RECENT_FRAMES=;   FIX_JUMP_FACTOR=
FIX_ASSIGN=;          FIX_GATE_MM=;         FIX_DIR_PEN=;         FIX_ANG_GATE=
FIX_MERGE_MM=0;       FIX_COAST_GROW=;      FIX_COAST_DECAY=
FIX_QUEUE_K=;         FIX_QUEUE_SIZE=;      FIX_MAX_MISS=;        FIX_MAX_MISS_TENT=
FIX_REID_DIST=;       FIX_REID_MAX_GAP=     # 재식별(ReID) — 위치/시간 임계. FIX_REID_DIST=0 이면 ReID 끔
FIX_ALPHA=;           FIX_BETA=;            FIX_MAX_SPEED=;       FIX_PRED_DT_CAP=
# ===== (선택) 탐색 대상 제한 — 비우면 '고정 안 한 전체' =====
OPTIMIZE=            # 예: OPTIMIZE="MERGE_MM,GATE_MM,MAX_MISS,QUEUE_K,RECENT_FRAMES"
# ===== 결과 저장 (지워도 되는 분석 산출물은 ANALYSIS_DIR 한 폴더에 모음) =====
ANALYSIS_DIR="debug_logs/analysis"           # 상관계수 csv/그림 + 비교영상 등 분석 결과 폴더
OUT="debug_logs/best_params.yaml"            # 최적 HP(실제로 쓰는 결과 — 폴더 밖에 둠, YAML)
CORR_DIR="$ANALYSIS_DIR"                      # HP↔score 상관계수(csv)+그림(svg). 비우면 안 함
VIDEO_OUT="$ANALYSIS_DIR/compare.mp4"         # 최적화 전(좌)/후(우) 좌우 비교 MP4. 비우면 생략
VIDEO_STRIDE=1                                # N프레임마다 1장 렌더(↑빠름·거침). 1=전프레임
# =========================================================

# CLI 로 --gt-logs 를 넘기면(문서화된 오버라이드) 영상 단계도 같은 로그를 쓰도록 GT_LOGS 동기화.
# (안 하면 최적화는 CLI 로그, 영상은 하드코딩 GT_LOGS 로 서로 다른 장면을 그림 — 리뷰 지적)
_a=("$@"); _n=${#_a[@]}; _i=0
while [ $_i -lt $_n ]; do
  if [ "${_a[$_i]}" = "--gt-logs" ]; then
    _cli=""; _j=$((_i + 1))
    while [ $_j -lt $_n ] && [ "${_a[$_j]:0:2}" != "--" ]; do
      _cli="$_cli ${_a[$_j]}"; _j=$((_j + 1))
    done
    GT_LOGS="${_cli# }"
  fi
  _i=$((_i + 1))
done

# 공통 인자 (단일/다중 공용)
COMMON=(--iters "$ITERS" --cd-rounds "$CD_ROUNDS" --seed "$SEED"
  --margin-deg "$MARGIN_DEG" --margin-mm "$MARGIN_MM" --exit-gap "$EXIT_GAP"
  --dwell-gap "$DWELL_GAP" --min-coverage "$MIN_COVERAGE"
  --w-switch-inside "$W_SW_IN" --w-switch-edge "$W_SW_ED"
  --w-dup-inside "$W_DUP_IN" --w-dup-edge "$W_DUP_ED"
  --w-coverage "$W_COV" --w-ids "$W_IDS" --w-dwell "$W_DWELL")

# 인자 조립: 공통 + 겹침/교차 관련. GT_LOGS 가 있으면 --gt-logs 추가.
# (GT_LOGS 가 비어 있으면 CLI 의 --gt-logs/--log 를 기대 — 둘 다 없으면 optimize_fusion.py 가 안내 후 종료)
ARGS=("${COMMON[@]}" --collision-mm "$COLLISION_MM"
      --w-xmerge-ep "$W_XMERGE_EP" --w-xmerge-fr "$W_XMERGE_FR")
if [ -n "$GT_LOGS" ]; then ARGS+=(--gt-logs $GT_LOGS); fi
if [ "$MERGE_COLLISIONS" != "1" ]; then ARGS+=(--no-collision-merge); fi

# FIX_* → --fix NAME=VAL (값이 있는 것만)
for kv in \
  WINDOW:$FIX_WINDOW STRIDE:$FIX_STRIDE FUSE_MIN_FRAMES:$FIX_FUSE_MIN_FRAMES \
  MOVE_MIN:$FIX_MOVE_MIN NOISE_RADIUS:$FIX_NOISE_RADIUS RECENT_FRAMES:$FIX_RECENT_FRAMES \
  JUMP_FACTOR:$FIX_JUMP_FACTOR ASSIGN:$FIX_ASSIGN GATE_MM:$FIX_GATE_MM DIR_PEN:$FIX_DIR_PEN \
  ANG_GATE:$FIX_ANG_GATE MERGE_MM:$FIX_MERGE_MM COAST_GROW:$FIX_COAST_GROW \
  COAST_DECAY:$FIX_COAST_DECAY QUEUE_K:$FIX_QUEUE_K QUEUE_SIZE:$FIX_QUEUE_SIZE \
  REID_DIST:$FIX_REID_DIST REID_MAX_GAP:$FIX_REID_MAX_GAP \
  MAX_MISS:$FIX_MAX_MISS MAX_MISS_TENT:$FIX_MAX_MISS_TENT ALPHA:$FIX_ALPHA \
  BETA:$FIX_BETA MAX_SPEED:$FIX_MAX_SPEED PRED_DT_CAP:$FIX_PRED_DT_CAP ; do
  name=${kv%%:*}; val=${kv#*:}
  if [ -n "$val" ]; then ARGS+=(--fix "$name=$val"); fi
done

if [ -n "$OPTIMIZE" ]; then ARGS+=(--optimize "$OPTIMIZE"); fi
if [ -n "$OUT" ]; then ARGS+=(--out "$OUT"); fi
if [ -n "$CORR_DIR" ]; then ARGS+=(--corr-out "$CORR_DIR"); fi

if [ ! -d .venv ]; then
  echo "[setup] 가상환경(.venv) 생성…"; python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi

# 사용할 GT 로그 수 안내(폴더 자동수집·CLI 오버라이드 공통). 부모 $@ 를 안 건드리게 서브셸에서 카운트.
_gt_cnt=$(set -- $GT_LOGS; echo $#)
if [ "$_gt_cnt" -ge 2 ]; then
  echo "[GT] 다중-인물 GT: ${GT_DIR:-CLI} 에서 로그 ${_gt_cnt}개 사용"
elif [ "$_gt_cnt" -eq 1 ]; then
  echo "⚠  GT 로그가 1개뿐 — 다중-인물 GT 는 ≥2개 필요. '$GT_DIR' 에 로그를 더 넣거나 --gt-logs 로 지정하세요."
else
  echo "⚠  GT 로그가 없습니다. '$GT_DIR' 폴더에 .jsonl 을 넣거나 --gt-logs 로 지정하세요."
fi

# 위 설정을 앞에, CLI 인자("$@")를 뒤에 → 같은 플래그면 뒤(CLI)가 우선
./.venv/bin/python optimize_fusion.py "${ARGS[@]}" "$@"

# 최적화 후: 합친 로그로 '최적화 전(base) / 후(best)' 좌우 비교 MP4 저장
# (GT_LOGS 로 구성한 다중-인물 모드 + best_params(OUT) 가 있을 때만)
if [ -n "$GT_LOGS" ] && [ -n "$VIDEO_OUT" ] && [ -f "$OUT" ]; then
  echo ""; echo "[video] 최적화 전/후 비교 영상 생성 중… → $VIDEO_OUT"
  VID=(--gt-logs $GT_LOGS --best-params "$OUT" --out "$VIDEO_OUT"
       --margin-deg "$MARGIN_DEG" --margin-mm "$MARGIN_MM" --collision-mm "$COLLISION_MM")
  if [ "$MERGE_COLLISIONS" != "1" ]; then VID+=(--no-collision-merge); fi
  if [ -n "$VIDEO_STRIDE" ]; then VID+=(--frame-stride "$VIDEO_STRIDE"); fi
  ./.venv/bin/python replay_video.py "${VID[@]}"
fi
