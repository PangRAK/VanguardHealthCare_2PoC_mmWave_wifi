#!/usr/bin/env python3
"""
멀티센서 융합 트래킹 (multi-sensor person fusion)
================================================================================

여러 EPL(mmWave) 센서가 각자 보고하는 타겟들을 **공통 방(room) 좌표계**에서 묶어,
같은 사람이면 하나의 전역 ID·하나의 점으로 표현한다.

입력은 이미 방 좌표(rx, ry)로 변환된 검출점들이다(SensorHub 가 room_transform 으로 변환).
각 센서는 서로 다른 위치·방향으로 설치돼 있어도, 공통 공간에서 위치·이동방향(모멘텀)이
일치하면 동일인으로 판단한다.

노이즈 억제 (window / stride)
----------------------------
검출을 슬라이딩 윈도로 모아 처리한다:
  · window : 함께 보는 최근 프레임 수. 창 안에서 (센서,타겟)별로 median 위치(스파이크 강건)
             + 전·후반 median 차로 이동방향을 구한다.
  · stride : 몇 프레임마다 한 번 추론할지. stride=1 → 원본 fps, stride=window → 비겹침 창.
             출력(GUI 갱신) 주기 = fps/stride.
  · min_frames : 창에서 이 횟수 미만 나타난 검출은 일시적/유령으로 보고 버린다.
  · 점프 클램프 : 한 스텝에 물리적으로 불가능한 이동(>vmax·dt·factor)은 제한(위치 튐 차단).
  · move_min : 이동이 이 값을 넘을 때만 '방향'을 인정(노이즈로 가짜 방향 생기는 것 방지).

확정 큐 (queue confirmation)
------------------------
  · 최근 queue_size 프레임 중 queue_k 번 관측되면 트랙을 '확정'한다(깜빡임/유령 억제).
    (사내 명칭 'queue' — queue_size = 큐 길이, queue_k = 확정 임계).

재식별 (ReID — 사라졌다 다시 나타난 사람 되살리기)
------------------------
  두 사람이 너무 가까우면 '센서 자체'가 한 사람으로만 측정해 한 점이 다른 점을 먹는다(하드웨어
  한계 — 못 막음). 이때 먹힌 사람의 확정 트랙은 coast 후 소멸한다. 나중에 두 사람이 멀어져
  점이 다시 둘로 갈라지면, 되살아난 점이 '새 ID' 로 잡히는 게 문제다(원래 그 사람 ID 여야 함).

  ※ 판별의 한계와 원리: mmWave 는 외형/특징이 없어 한 점이 '진짜 그 사람'인지 '노이즈'인지
    단일 프레임으로는 구분 불가하다. ReID 는 '식별'이 아니라 위치·시간 기반 '추정'이며, 세 가지로
    오판을 억제한다 — (1) 후보는 '확정'까지 갔던 트랙만(반짝 유령 ID 는 애초 후보 아님),
    (2) 위치(reid_dist)·시간(reid_max_gap) 게이트, (3) '지속성' — 부활 점을 즉시 옛 ID 로 확정하지
    않고 queue 재확정(몇 프레임 지속)을 거친 뒤에야 옛 ID 를 복원한다. 노이즈는 지속되지 않으므로
    확정 전에 소멸해 화면에 옛 ID 를 남기지 않는다(= queue 확정과 같은 '지속=실체' 논리).
    남는 한계: '다른 실제 사람'이 같은 자리에 그 시간창 안에 들어오면 지속성으로도 못 거른다
    (실체이므로) — reid_dist/reid_max_gap 을 좁혀 확률만 낮출 수 있는 본질적 트레이드오프.
  · reid_dist_mm  : 위치 임계. 소멸한 '확정' 트랙의 마지막 관측 위치에서 이 반경(mm) 안에 새 관측이
                    생기면 그 사람의 재등장 '후보'로 보고 옛 ID 로 (재확정 뒤) 부활. 0 = ReID 끔.
  · 체류시간(dwell): 각 트랙은 first_seen(처음 관측 시각)을 갖고 dwell_sec = now - first_seen 로 보고된다.
                    ReID 부활 트랙은 옛 트랙의 first_seen 을 물려받아 '사라졌던 공백까지 포함해' 체류시간이
                    끊기지 않고 이어진다(옛 체류시간 + 공백 시간 계속 체류 가정). tracks() 의 dwell_sec.
                    주의: ReID 가 '다른 사람'을 잘못 되살리면(위치·시간만으로는 구분 불가) 그 사람이 옛
                    first_seen 을 물려받아 실제보다 큰 dwell 을 보고할 수 있다(= ID 오매칭 tradeoff 가 체류시간
                    에도 그대로 전이). reid_dist/reid_max_gap 을 좁혀 확률만 낮출 수 있는 본질적 한계.
  · reid_max_gap_sec : 시간 임계. '마지막으로 실제 검출된 시점'부터 이 시간(초) 안에 다시 나타나야
                    같은 ID 로 되살린다(넘으면 남남). 트랙은 max_miss 만큼 coast 한 뒤에야 소멸/병합돼
                    ReID 후보로 들어오므로 '실효 재식별 창 ≈ reid_max_gap − max_miss'. 따라서 max_miss
                    보다 '넉넉히'(예: +1초 이상) 커야 한다(기본 3.0 vs max_miss 1.2 → 약 1.8초 창).
  · NOISE_RADIUS 와의 관계(중요): ReID 판정은 noise_radius 흡수보다 '먼저' 한다. 즉 재등장 점이
                    다른 확정트랙의 noise_radius 안에 들어와도, 소멸한 옛 트랙과 매칭되면 흡수하지
                    않고 되살린다. → noise_radius 를 넓혀 노이즈를 잡아도 '알던 사람'의 재등장 ReID
                    는 죽지 않는다(둘을 분리). 반대로 '처음 보는' 갑툭튀는 여전히 흡수된다.

플러그인(켜고 끄는) 옵션
------------------------
  · assign="greedy"|"hungarian" : 트랙↔관측 매칭. hungarian = 전역 최적(교차 시 ID 스왑↓).
  · dwell(bool) : 재실 카운트 디바운스. 트랙이 dwell_enter_sec 이상 유지돼야 '카운트'에 포함
                 (진입 튐 방지). 이탈 유예는 coast(max_miss)로 처리. track_count 는 이 'counted' 수.

2단계 구조: Stage A(센서 간 centroid-linkage) → Stage B(등속예측·매칭·alpha-beta·생성/소멸/병합).
관측 한계: 두 사람 ~0.7m 이내면 합쳐질 수 있음 / 교차 시 ID 스왑 가능 / window↑=노이즈↓·지연↑.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import deque

# 트랙 색상 팔레트 (센서 색과 구분되는 밝은 톤 — 사람별 색)
TRACK_PALETTE = ["#4ea3ff", "#ff6b9d", "#5be37a", "#ffd166",
                 "#b18cff", "#ff8f5d", "#43e0d0", "#f45b69"]


def track_color(tid: int) -> str:
    return TRACK_PALETTE[tid % len(TRACK_PALETTE)]


# ---------------------------------------------------------------------------
# CLI/설정 헬퍼 (gui/web/cli 공통)
# ---------------------------------------------------------------------------
def resolve_fusion_opts(cfg=None, **overrides):
    """config 의 'fusion' 섹션 기본값 + override(None 은 무시) → FusionTracker 파라미터 dict."""
    opts = dict((cfg or {}).get("fusion") or {})
    for k, v in overrides.items():
        if v is not None:
            opts[k] = v
    return opts


# ---------------------------------------------------------------------------
# 융합 파라미터 단일 정의(SSOT). CLI 플래그·설정 이름·기록 헤더가 모두 여기서 파생된다.
# (예전엔 _FUSION_ARG_MAP / optimize_fusion.NAME2KW / mmwave_reader 기록 dict 3벌을 손으로
#  맞췄고, 새 HP 추가 시 드리프트 위험이 있었다 → 한 곳으로 통합)
#   cli   : CLI 플래그(--cli).  attr = cli 의 '-'→'_' (argparse dest)
#   kw    : FusionTracker 생성자 인자명
#   attr_tk: FusionTracker 가 저장하는 속성명(기록 헤더에서 현재값 읽기용)
#   name  : 사용자 친화 대문자 이름(optimize_fusion / run_*.sh 표기)
#   kind  : "int" | "float" | "str" | "bool"
#   help  : CLI 도움말(끝의 (기본값) 포함)
#   choices: 범주형(assign) 만
# ---------------------------------------------------------------------------
class _P:
    __slots__ = ("cli", "kw", "attr_tk", "name", "kind", "help", "choices")

    def __init__(self, cli, kw, attr_tk, name, kind, help, choices=None):
        self.cli = cli; self.kw = kw; self.attr_tk = attr_tk; self.name = name
        self.kind = kind; self.help = help; self.choices = choices

    @property
    def attr(self):                     # argparse dest (CLI 플래그의 '-'→'_')
        return self.cli.replace("-", "_")


FUSION_PARAMS = [
    _P("window", "window", "window", "WINDOW", "int", "윈도 크기(프레임): median 스무딩. ↑노이즈↓·지연↑ (5)"),
    _P("stride", "stride", "stride", "STRIDE", "int", "추론 간격(프레임): 출력주기=fuse_hz/stride (1)"),
    _P("fuse-min-frames", "min_frames", "min_frames", "FUSE_MIN_FRAMES", "int", "창 내 최소 등장 프레임(미만=유령 제거) (2)"),
    _P("move-min", "move_min_mm", "move_min", "MOVE_MIN", "float", "방향 인정 최소 이동(mm): 노이즈 가짜방향 방지 (250)"),
    _P("noise-radius", "noise_radius_mm", "noise_radius", "NOISE_RADIUS", "float", "확정 포인트 반경(mm) 내 갑툭튀=흡수. GUI 원. 0=끔 (400)"),
    _P("recent-frames", "recent_frames", "recent_frames", "RECENT_FRAMES", "int", "최근 이 프레임에 없는 tid=스테일 제외(반전 ID 스위칭↓) (2)"),
    _P("jump-factor", "jump_factor", "jump_factor", "JUMP_FACTOR", "float", "한 스텝 최대이동=max_speed·dt·이 값, 초과분 클램프 (2.0)"),
    _P("assign", "assign", "assign", "ASSIGN", "str", "매칭: greedy | hungarian(교차 ID 스왑↓) (greedy)", ["greedy", "hungarian"]),
    _P("gate-mm", "gate_mm", "gate", "GATE_MM", "float", "트랙↔관측 매칭 최대거리(mm) (750)"),
    _P("dir-pen", "dir_pen_mm", "dir_pen_mm", "DIR_PEN", "float", "이동방향 불일치 매칭 비용(mm), 0=방향 무시 (120)"),
    _P("ang-gate", "ang_gate_deg", "ang_gate", "ANG_GATE", "float", "Stage A/방향 게이트 각도(°) (75)"),
    _P("merge-mm", "merge_mm", "merge_mm", "MERGE_MM", "float", "두 확정 트랙 이 거리 내면 중복 병합(mm) (550)"),
    _P("coast-grow", "coast_grow", "coast_grow", "COAST_GROW", "float", "가려짐 1초당 게이트 확장(mm) (350)"),
    _P("coast-decay", "coast_decay", "coast_decay", "COAST_DECAY", "float", "coast 중 속도 감쇠 0~1: 반전/가림 오버슈트↓ (0.85)"),
    _P("queue-k", "queue_k", "queue_k", "QUEUE_K", "int", "확정 큐: 최근 queue-size 중 이 횟수 관측되면 확정 (3)"),
    _P("queue-size", "queue_size", "queue_size", "QUEUE_SIZE", "int", "확정 큐 길이(최근 프레임 수) (5)"),
    _P("reid-dist", "reid_dist_mm", "reid_dist", "REID_DIST", "float", "재식별(ReID) 위치 임계(mm): 소멸한 확정 ID 를 이 반경 내 재등장 관측에 되살림. 0=끔 (0)"),
    _P("reid-max-gap", "reid_max_gap_sec", "reid_max_gap", "REID_MAX_GAP", "float", "재식별 시간 임계(초): 마지막 검출 후 이 시간 내 재등장해야 같은 ID. 실효창≈gap−max_miss 라 max_miss 보다 넉넉히 커야 함 (3.0)"),
    _P("max-miss", "max_miss_sec", "max_miss", "MAX_MISS", "float", "확정 트랙 미검출 유지시간(초) (1.2)"),
    _P("max-miss-tentative", "max_miss_tentative_sec", "max_miss_tent", "MAX_MISS_TENT", "float", "미확정 트랙 유지시간(초) (0.5)"),
    _P("dwell", "dwell", "dwell", "DWELL", "bool", "재실 카운트 디바운스 켜기"),
    _P("dwell-enter", "dwell_enter_sec", "dwell_enter", "DWELL_ENTER", "float", "카운트 진입 지연(초) (0.4)"),
    _P("alpha", "alpha", "alpha", "ALPHA", "float", "위치 보정 게인 0~1 (0.45)"),
    _P("beta", "beta", "beta", "BETA", "float", "속도 보정 게인 0~1 (0.20)"),
    _P("max-speed", "max_speed_mm_s", "vmax", "MAX_SPEED", "float", "사람 최대 속도 상한(mm/s) (3500)"),
    _P("pred-dt-cap", "pred_dt_cap", "pred_dt_cap", "PRED_DT_CAP", "float", "예측 이동 dt 상한(초) (0.3)"),
    _P("trail-len", "trail_len", "trail_len", "TRAIL_LEN", "int", "궤적 길이(점 개수) (48)"),
]

# 파생 테이블 (모두 위 SSOT 에서 생성 — 손으로 유지하지 않는다)
_FUSION_ARG_MAP = tuple((p.attr, p.kw) for p in FUSION_PARAMS)   # (arg_attr, tracker_param)
NAME2KW = {p.name: p.kw for p in FUSION_PARAMS}                  # 대문자 이름 → tracker_param
_KIND_CAST = {"int": int, "float": float}


def add_fusion_args(ap):
    """융합/추적 하이퍼파라미터 CLI 플래그 전체(gui/web/cli 공통) — FUSION_PARAMS 에서 파생. 미지정 시 기본값."""
    g = ap.add_argument_group("fusion", "멀티센서 융합/추적 하이퍼파라미터 (미지정 시 기본값)")
    for p in FUSION_PARAMS:
        if p.kind == "bool":
            g.add_argument(f"--{p.cli}", dest=p.attr, action="store_const", const=True,
                           default=None, help=p.help)
        elif p.kind == "str":
            g.add_argument(f"--{p.cli}", choices=p.choices, default=None, help=p.help)
        else:
            g.add_argument(f"--{p.cli}", type=_KIND_CAST[p.kind], default=None, help=p.help)
    # fuse-hz / interval-sec 는 FusionTracker 인자가 아니라 hub 스텝 주기(apply_fusion_opts 에서 처리)
    g.add_argument("--fuse-hz", type=float, default=None, help="융합 처리 주파수(Hz) — window/stride '프레임' 기준 (15)")
    g.add_argument("--interval-sec", type=float, default=None,
                   help="융합 스텝 간격(초). 제품(vanguard_mmwave) OD_TIME_INTERVAL_SECOND 와 같은 "
                        "뜻이며 --fuse-hz 보다 우선(= 1/fuse-hz). 미지정 시 --fuse-hz 사용")


def tracker_params(tracker):
    """실행 중인 FusionTracker 의 현재 파라미터를 {tracker_param: value} 로 반환(기록 헤더용).
    FUSION_PARAMS 에서 파생 → 기록 헤더가 CLI/optimize 와 항상 같은 파라미터 집합을 쓴다."""
    return {p.kw: getattr(tracker, p.attr_tk) for p in FUSION_PARAMS}


def apply_fusion_opts(hub, args):
    """CLI 인자(+ epl_config 의 fusion 섹션)를 반영해 hub 의 융합 트래커를 설정."""
    try:
        from epl_config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    over = {param: getattr(args, attr, None) for attr, param in _FUSION_ARG_MAP}
    opts = resolve_fusion_opts(cfg, **over)
    if opts:
        hub.enable_fusion(**opts)
    # 융합 스텝 주기 — hub 의 스텝 상한. 초(interval-sec) 가 Hz(fuse-hz) 보다 우선한다:
    # 제품(vanguard_mmwave)이 OD_TIME_INTERVAL_SECOND(초) 로 이 주기를 정하므로 같은 단위로 맞춘다.
    iv = getattr(args, "interval_sec", None)
    hz = getattr(args, "fuse_hz", None)
    try:
        if iv and float(iv) > 0:
            hub._fuse_interval = float(iv)
        elif hz:
            hub._fuse_interval = 1.0 / float(hz)
    except (ValueError, ZeroDivisionError, AttributeError):
        pass


def replay_frames(frames, params, confirmed_only=True):
    """기록된 프레임(dets 스트림)을 주어진 파라미터로 FusionTracker 에 재생 → 프레임별 트랙 목록.
    라이브(mmwave_reader)와 '동일한' FusionTracker.step 을 쓰므로 같은 dets → 같은 결과(결정적).
    offline 재생기(optimize_fusion.replay, replay_video.replay_full)의 공용 드라이버 — 각자
    필요한 필드만 이 결과에서 투영(project)한다(예전엔 이 루프가 두 파일에 중복 구현됐음).
    frames[i] 는 최소 'dets'(list of {sid,tid,x,y,dir?}) 와 't'(초) 를 갖는다."""
    tk = FusionTracker(**params)
    out = []
    for f in frames:
        dets = [{"sid": d["sid"], "tid": d["tid"], "x": d["x"], "y": d["y"],
                 "dir": (tuple(d["dir"]) if d.get("dir") else None)} for d in f["dets"]]
        tracks = tk.step(dets, f["t"])
        out.append([t for t in tracks if t["confirmed"]] if confirmed_only else list(tracks))
    return out


# ---------------------------------------------------------------------------
# 기하 헬퍼
# ---------------------------------------------------------------------------
def _dist(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def _unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else None


def _ang_deg(u, v):
    d = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(d))


def _hungarian(cost):
    """정사각 비용행렬(n×n)에 대한 최소비용 완전매칭. 반환 row→col(리스트). O(n^3).
    (e-maxx 표준 구현, 1-indexed 내부처리)."""
    n = len(cost)
    if n == 0:
        return []
    INF = float("inf")
    u = [0.0] * (n + 1); v = [0.0] * (n + 1); p = [0] * (n + 1); way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i; j0 = 0
        minv = [INF] * (n + 1); used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]; delta = INF; j1 = -1
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur; way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]; j1 = j
            for j in range(0, n + 1):
                if used[j]:
                    u[p[j]] += delta; v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
            if j0 == 0:
                break
    ans = [0] * n
    for j in range(1, n + 1):
        if p[j] > 0:
            ans[p[j] - 1] = j - 1
    return ans


# ============================================================================
# Stage A — 센서 간 동일인 클러스터링 (centroid-linkage)
# ============================================================================
def cluster_detections(dets, gate_mm=750.0, ang_gate_deg=75.0):
    """dets → 관측 목록. 두 클러스터 '중심거리'≤gate & 방향 비상충 & 센서 비겹침일 때만 병합."""
    clusters = []
    for i, d in enumerate(dets):
        du = d.get("dir")
        clusters.append({"idx": [i], "sx": d["x"], "sy": d["y"], "n": 1,
                         "sensors": {d["sid"]},
                         "dx": (du[0] if du else 0.0), "dy": (du[1] if du else 0.0),
                         "ndir": (1 if du else 0)})

    def cen(c):
        return c["sx"] / c["n"], c["sy"] / c["n"]

    def cdir(c):
        return _unit(c["dx"], c["dy"]) if c["ndir"] else None

    while len(clusters) > 1:
        best = None
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                ca, cb = clusters[a], clusters[b]
                if ca["sensors"] & cb["sensors"]:
                    continue
                (ax, ay), (bx, by) = cen(ca), cen(cb)
                d = _dist(ax, ay, bx, by)
                if d > gate_mm:
                    continue
                da, db = cdir(ca), cdir(cb)
                if da and db and _ang_deg(da, db) > ang_gate_deg:
                    continue
                if best is None or d < best[0]:
                    best = (d, a, b)
        if best is None:
            break
        _, a, b = best
        ca, cb = clusters[a], clusters[b]
        ca["idx"] += cb["idx"]; ca["sx"] += cb["sx"]; ca["sy"] += cb["sy"]
        ca["n"] += cb["n"]; ca["sensors"] |= cb["sensors"]
        ca["dx"] += cb["dx"]; ca["dy"] += cb["dy"]; ca["ndir"] += cb["ndir"]
        del clusters[b]

    obs = []
    for c in clusters:
        obs.append({"x": c["sx"] / c["n"], "y": c["sy"] / c["n"], "dir": cdir(c),
                    "members": [(dets[i]["sid"], dets[i]["tid"]) for i in c["idx"]],
                    "sensors": sorted(c["sensors"])})
    return obs


# ============================================================================
# Stage B — 시간축 다중객체추적 (+ window/stride, 플러그인 옵션)
# ============================================================================
class FusionTracker:
    def __init__(self, gate_mm=750.0, max_miss_sec=1.2,
                 max_miss_tentative_sec=0.5, alpha=0.45, beta=0.20,
                 max_speed_mm_s=3500.0, merge_mm=550.0, ang_gate_deg=75.0,
                 coast_grow=350.0, pred_dt_cap=0.3, dir_pen_mm=120.0, trail_len=48,
                 window=5, stride=1, min_frames=2, move_min_mm=250.0, jump_factor=2.0,
                 recent_frames=2, coast_decay=0.85, noise_radius_mm=400.0,
                 assign="greedy", queue_k=3, queue_size=5,
                 reid_dist_mm=0.0, reid_max_gap_sec=3.0,
                 dwell=False, dwell_enter_sec=0.4):
        self.gate = gate_mm
        self.max_miss = max_miss_sec
        self.max_miss_tent = max_miss_tentative_sec
        self.alpha = alpha
        self.beta = beta
        self.vmax = max_speed_mm_s
        self.merge_mm = merge_mm
        self.ang_gate = ang_gate_deg
        self.coast_grow = coast_grow
        self.pred_dt_cap = pred_dt_cap
        self.dir_pen_mm = dir_pen_mm
        self.trail_len = trail_len
        # window/stride
        self.window = max(1, int(window))
        self.stride = max(1, int(stride))
        self.min_frames = max(1, int(min_frames))
        self.move_min = move_min_mm
        self.jump_factor = jump_factor
        self.recent_frames = max(1, int(recent_frames))   # 최근성 게이트(스테일 tid 제외)
        self.coast_decay = float(coast_decay)             # coast 중 속도 감쇠(반전 시 오버슈트↓)
        self.noise_radius = max(0.0, float(noise_radius_mm))  # 기존 확정트랙 반경 내 갑툭튀=노이즈로 흡수
        # 확정 큐 (최근 queue_size 중 queue_k 관측 → 확정)
        self.queue_size = max(1, int(queue_size))
        self.queue_k = max(1, min(int(queue_k), self.queue_size))
        # 재식별(ReID): 소멸한 확정 트랙을 잠시 기억했다가 근처 재등장 시 옛 ID 로 부활
        self.reid_dist = max(0.0, float(reid_dist_mm))       # 위치 임계(mm). 0=끔
        self.reid_max_gap = max(0.0, float(reid_max_gap_sec))  # 시간 임계(초, 마지막 검출 기준)
        # 플러그인 옵션
        self.assign = assign if assign in ("greedy", "hungarian") else "greedy"
        self.dwell = bool(dwell)
        self.dwell_enter = max(0.0, float(dwell_enter_sec))
        # 상태
        self._buf = deque(maxlen=self.window)
        self._frame_i = 0
        self._last_out = []
        self._infer_count = 0
        self._tracks = []
        self._next_id = 1
        self._last_infer_t = None
        self._lost = []            # ReID 후보: 소멸한 확정 트랙 [{id,x,y,lost_t,members,sensors}]
        self.reid_total = 0        # '재확정'된 부활 누적 수(성공한 ReID · 디버그/telemetry)

    # -- 창 내 집계 --
    def _aggregate_window(self):
        frames = list(self._buf)
        if not frames:
            return []
        minf = min(self.min_frames, len(frames))
        # 최근성: 최근 recent_frames 프레임에 등장한 (센서,tid)만 유효.
        # 센서가 tid 를 재배정/소멸시키면(예: 급반전) 옛 tid 는 최근 프레임에서 사라지므로
        # 창에 남은 옛 표본으로 '스테일 유령'을 만들지 않는다(→ 같은센서 분리로 인한 ID 스위칭 방지).
        recent = set()
        for dets in frames[-self.recent_frames:]:
            for d in dets:
                recent.add((d["sid"], d["tid"]))
        by_key = {}
        for dets in frames:
            for d in dets:
                by_key.setdefault((d["sid"], d["tid"]), []).append(
                    (d["x"], d["y"], d.get("dir")))
        out = []
        for (sid, tid), sm in by_key.items():
            if (sid, tid) not in recent:
                continue
            if len(sm) < minf:
                continue
            xs = [s[0] for s in sm]; ys = [s[1] for s in sm]
            mx = statistics.median(xs); my = statistics.median(ys)
            if len(sm) >= 4:
                h = len(sm) // 2
                ox, oy = statistics.median(xs[:h]), statistics.median(ys[:h])
                nx, ny = statistics.median(xs[h:]), statistics.median(ys[h:])
                dvx, dvy = nx - ox, ny - oy
            else:
                dvx, dvy = xs[-1] - xs[0], ys[-1] - ys[0]
            dv = _unit(dvx, dvy) if math.hypot(dvx, dvy) >= self.move_min else None
            if dv is None and len(sm) < 2 and sm[-1][2]:
                dv = sm[-1][2]
            out.append({"sid": sid, "tid": tid, "x": mx, "y": my, "dir": dv})
        return out

    def step(self, dets, now):
        """매 프레임 호출. 버퍼링 후 stride 마다 추론. 항상 최신 트랙 반환."""
        self._buf.append(list(dets))
        self._frame_i += 1
        if self._frame_i % self.stride == 0:
            self._last_out = self._infer(self._aggregate_window(), now)
        return self._last_out

    def _new_track(self, o, now, reid_from=None, first_seen=None):
        """새 트랙 생성. reid_from 이 주어지면 그 옛 ID 로 '부활' 후보로 만든다.
        단, 부활 트랙도 '즉시' 확정하지 않고 일반 신규와 동일하게 queue 재확정을 거친다
        (지속되면=실제 재등장 → 옛 ID 복원 / 단발 노이즈는 확정 전에 소멸 → 옛 ID 표시 안 됨).
        reid_from 은 '새 ID 발급' 대신 '옛 ID 재사용'만 결정한다.
        first_seen: 이 사람이 '처음 관측된 시각'. ReID 부활 시 옛 트랙의 값을 물려받아 체류시간
        (dwell = now - first_seen)이 사라졌던 공백까지 포함해 이어지게 한다(옛 체류+공백 계속 체류)."""
        if reid_from is not None:
            tid = reid_from
        else:
            tid = self._next_id; self._next_id += 1
        init_conf = self.queue_k <= 1
        tr = {"id": tid, "x": o["x"], "y": o["y"], "vx": 0.0, "vy": 0.0,
              "sx": o["x"], "sy": o["y"],                           # 마지막 '관측' 위치(ReID 기준)
              "first_seen": (now if first_seen is None else first_seen),  # 체류시간 기준점(ReID 시 승계)
              "hits": 1, "age": 0.0, "confirmed": init_conf, "coasting": False,
              "last_seen": now, "members": o["members"], "sensors": o["sensors"],
              "hist": deque([True], maxlen=self.queue_size),
              "reid_pending": (reid_from is not None), "reid_hold": 0,
              "trail": deque(maxlen=self.trail_len)}
        tr["trail"].append((o["x"], o["y"]))
        return tr

    def _mark_reid_confirmed(self, tr):
        """부활 후보가 '재확정'된 순간 — 단발 노이즈가 아니라 지속되는 실제 재등장으로 판정.
        이때 비로소 옛 ID 가 화면에 복원되고 reid_total(성공한 부활)이 증가한다."""
        tr["reid_pending"] = False
        tr["reid_hold"] = 6                 # 표식 + 즉시 재병합 방지(몇 프레임)
        self.reid_total += 1

    def _remember_lost(self, tr):
        """소멸/병합으로 사라지는 '확정' 트랙을 ReID 후보로 기억(부활 대비).
        마지막 '관측' 위치(sx,sy)·시각(last_seen) 기준. 근접 병합으로 '먹힌' 사람은 이 경로로도 들어온다."""
        self._lost.append({"id": tr["id"], "x": tr.get("sx", tr["x"]),
                           "y": tr.get("sy", tr["y"]), "lost_t": tr["last_seen"],
                           "first_seen": tr.get("first_seen", tr["last_seen"]),  # 체류시간 승계용
                           "members": tr["members"], "sensors": tr["sensors"]})
        if len(self._lost) > 64:                      # 버퍼 상한(폭주 방지)
            self._lost = self._lost[-64:]

    def _clamp_speed(self, tr):
        sp = math.hypot(tr["vx"], tr["vy"])
        if sp > self.vmax:
            k = self.vmax / sp
            tr["vx"] *= k; tr["vy"] *= k

    def _match(self, cand, nt, no):
        """cand: [(ti, oi, cost, within_gate)] → {ti: oi}. assign 방식에 따라."""
        if self.assign == "hungarian" and nt and no:
            n = max(nt, no)
            DUMMY, FORBID = 1e6, 1e12
            C = [[DUMMY] * n for _ in range(n)]
            for ti, oi, cost, within in cand:
                C[ti][oi] = cost if within else FORBID
            ans = _hungarian(C)
            m = {}
            for ti in range(nt):
                oi = ans[ti]
                if oi < no and C[ti][oi] < DUMMY:
                    m[ti] = oi
            return m
        # greedy (기본)
        m, tu, ou = {}, set(), set()
        for ti, oi, cost, within in sorted((c for c in cand if c[3]), key=lambda c: c[2]):
            if ti in tu or oi in ou:
                continue
            tu.add(ti); ou.add(oi); m[ti] = oi
        return m

    def _infer(self, dets, now):
        self._infer_count += 1
        if self._last_infer_t is None:
            dt = float(self.stride) / 15.0
        else:
            dt = min(max(now - self._last_infer_t, 1e-3), 0.5)
        self._last_infer_t = now
        dtv = max(dt, 1e-2)
        pdt = min(dt, self.pred_dt_cap)
        maxstep = self.vmax * dtv * self.jump_factor

        obs = cluster_detections(dets, gate_mm=self.gate, ang_gate_deg=self.ang_gate)

        # 1) 예측
        for tr in self._tracks:
            tr["x"] += tr["vx"] * pdt; tr["y"] += tr["vy"] * pdt
            tr["age"] += dt; tr["coasting"] = True
            if tr.get("reid_hold", 0) > 0:          # 부활 표식은 몇 스텝 유지 후 소거(디버그 표시용)
                tr["reid_hold"] -= 1

        # 2) 게이팅 + 매칭(greedy/hungarian)
        cand = []
        for ti, tr in enumerate(self._tracks):
            gate = self.gate + min(now - tr["last_seen"], self.max_miss) * self.coast_grow
            for oi, o in enumerate(obs):
                d = _dist(tr["x"], tr["y"], o["x"], o["y"])
                pen = 0.0
                tvu = _unit(tr["vx"], tr["vy"])
                if tvu and o.get("dir"):
                    pen = (_ang_deg(tvu, o["dir"]) / 180.0) * self.dir_pen_mm
                cand.append((ti, oi, d + pen, d <= gate))
        matches = self._match(cand, len(self._tracks), len(obs))

        # 3) M-of-N 관측 이력 기록(예측 대상 트랙 전체, 생성 전)
        for ti, tr in enumerate(self._tracks):
            tr["hist"].append(ti in matches)

        # 4) 매칭 갱신 (점프 클램프 → alpha-beta)
        for ti, oi in matches.items():
            tr = self._tracks[ti]; o = obs[oi]
            rx = o["x"] - tr["x"]; ry = o["y"] - tr["y"]
            mag = math.hypot(rx, ry)
            if mag > maxstep and maxstep > 0:
                f = maxstep / mag; rx *= f; ry *= f
            tr["x"] += self.alpha * rx; tr["y"] += self.alpha * ry
            tr["vx"] += self.beta * rx / dtv; tr["vy"] += self.beta * ry / dtv
            self._clamp_speed(tr)
            tr["hits"] += 1; tr["last_seen"] = now; tr["coasting"] = False
            tr["members"] = o["members"]; tr["sensors"] = o["sensors"]
            tr["sx"] = tr["x"]; tr["sy"] = tr["y"]         # 마지막 실제 관측 위치 갱신(ReID 기준점)
            if not tr["confirmed"]:
                tr["confirmed"] = sum(tr["hist"]) >= self.queue_k
                if tr["confirmed"] and tr.get("reid_pending"):
                    self._mark_reid_confirmed(tr)          # 부활 후보가 지속됨 → 옛 ID 복원 확정
            tr["trail"].append((tr["x"], tr["y"]))

        # 4b) coast(미매칭) 트랙은 속도 감쇠 → 옛 방향으로 튀어나가지 않게(반전/가림 시 재획득 안정)
        for ti, tr in enumerate(self._tracks):
            if ti not in matches:
                tr["vx"] *= self.coast_decay; tr["vy"] *= self.coast_decay

        # 5) 재식별(ReID) → 노이즈 흡수 → 신규. 우선순위가 핵심:
        #    (a) 소멸한 '확정' 트랙(=먹혀서 사라진 사람)의 마지막 위치 reid_dist 안에 재등장하면
        #        옛 ID '후보'로 부활 — noise_radius 흡수보다 먼저 판정하므로, 다른 트랙 반경 안이어도 살림.
        #        단 즉시 확정하진 않고 재확정(지속성)을 거친다 → 단발 노이즈는 확정 전에 사라짐.
        #    (b) 그 외 확정포인트 반경 내 갑툭튀는 노이즈로 흡수(신규 억제).
        #    (c) 나머지는 진짜 신규.
        matched_obs = set(matches.values())
        conf_pts = [(t["x"], t["y"]) for t in self._tracks if t["confirmed"]]
        if self.reid_dist > 0:                        # gap 초과한 옛 후보는 잊는다
            self._lost = [L for L in self._lost if (now - L["lost_t"]) <= self.reid_max_gap]
        alive_ids = {t["id"] for t in self._tracks}
        for oi, o in enumerate(obs):
            if oi in matched_obs:
                continue
            if self.reid_dist > 0 and self._lost:     # (a) ReID: 최근 소멸 확정트랙 중 최근접 부활
                best, bestd = None, self.reid_dist
                for L in self._lost:
                    if L["id"] in alive_ids:          # 이미 살아있는 ID 는 부활 대상 아님
                        continue
                    dd = _dist(o["x"], o["y"], L["x"], L["y"])
                    if dd <= bestd:
                        best, bestd = L, dd
                if best is not None:
                    self._lost.remove(best)
                    nt = self._new_track(o, now, reid_from=best["id"],
                                         first_seen=best.get("first_seen"))
                    if nt["confirmed"] and nt.get("reid_pending"):   # queue_k<=1 → 즉시 확정 경로
                        self._mark_reid_confirmed(nt)
                    self._tracks.append(nt); alive_ids.add(nt["id"])
                    continue
            if self.noise_radius > 0 and any(         # (b) 기존 확정 포인트 근처 → 노이즈로 흡수
                    _dist(o["x"], o["y"], cx, cy) <= self.noise_radius for cx, cy in conf_pts):
                continue
            self._tracks.append(self._new_track(o, now))   # (c) 진짜 신규

        # 6) 소멸(타임아웃) — 확정 트랙이 죽으면 ReID 후보로 기억(부활 대비).
        survivors = []
        for tr in self._tracks:
            ttl = self.max_miss if tr["confirmed"] else self.max_miss_tent
            if (now - tr["last_seen"]) <= ttl:
                survivors.append(tr)
            elif self.reid_dist > 0 and tr["confirmed"]:
                self._remember_lost(tr)
        self._tracks = survivors

        # 7) 중복 병합
        self._merge_duplicates(now)
        return self.tracks()

    def _merge_duplicates(self, now):
        merged = True
        while merged:
            merged = False
            conf = [t for t in self._tracks if t["confirmed"]]
            for i in range(len(conf)):
                for j in range(i + 1, len(conf)):
                    a, b = conf[i], conf[j]
                    if _dist(a["x"], a["y"], b["x"], b["y"]) > self.merge_mm:
                        continue
                    if (now - a["last_seen"]) < 1e-9 and (now - b["last_seen"]) < 1e-9:
                        continue
                    # 갓 부활한(reid_hold>0) 트랙은 잠시 병합 보호 — 옆 트랙과 즉시 합쳐져
                    # ReID 가 무효화되거나 서로 ID 가 바뀌는 것 방지(막 갈라선 두 사람일 수 있음).
                    if a.get("reid_hold", 0) > 0 or b.get("reid_hold", 0) > 0:
                        continue
                    ua, ub = _unit(a["vx"], a["vy"]), _unit(b["vx"], b["vy"])
                    if ua and ub and _ang_deg(ua, ub) > 120.0:
                        continue
                    keep, drop = (a, b) if a["id"] <= b["id"] else (b, a)
                    keep["x"] = (a["x"] + b["x"]) / 2; keep["y"] = (a["y"] + b["y"]) / 2
                    keep["hits"] = max(a["hits"], b["hits"])
                    keep["age"] = max(a["age"], b["age"])
                    keep["last_seen"] = max(a["last_seen"], b["last_seen"])
                    keep["sensors"] = sorted(set(keep["sensors"]) | set(drop["sensors"]))
                    # 근접 병합으로 '먹혀' 사라지는 확정 트랙(=사람)은 ReID 후보로 기억 → 갈라서면 부활.
                    if self.reid_dist > 0 and drop["confirmed"]:
                        self._remember_lost(drop)
                    self._tracks.remove(drop)
                    merged = True
                    break
                if merged:
                    break

    def _is_counted(self, tr):
        return tr["confirmed"] and (not self.dwell or tr["age"] >= self.dwell_enter)

    def tracks(self):
        out = []
        for tr in sorted(self._tracks, key=lambda t: t["id"]):
            sp = math.hypot(tr["vx"], tr["vy"])
            out.append({
                "id": tr["id"], "x": round(tr["x"], 1), "y": round(tr["y"], 1),
                "vx": round(tr["vx"], 1), "vy": round(tr["vy"], 1),
                "speed": round(sp / 1000.0, 3),
                "heading_deg": round(math.degrees(math.atan2(tr["vy"], tr["vx"])), 1),
                "n_sensors": len(tr["sensors"]), "sensors": list(tr["sensors"]),
                "members": [list(m) for m in tr["members"]],
                "confirmed": bool(tr["confirmed"]), "counted": bool(self._is_counted(tr)),
                "coasting": bool(tr["coasting"]), "reid": bool(tr.get("reid_hold", 0) > 0),
                "dwell_sec": round(max(0.0, (self._last_infer_t if self._last_infer_t is not None
                                             else tr["first_seen"]) - tr["first_seen"]), 2),
                "age": round(tr["age"], 2), "color": track_color(tr["id"]),
                "trail": [[round(x, 1), round(y, 1)] for x, y in tr["trail"]],
            })
        return out

    def confirmed_count(self):
        return sum(1 for t in self._tracks if t["confirmed"])

    def counted_count(self):
        return sum(1 for t in self._tracks if self._is_counted(t))


# ============================================================================
# 합성 자체검증
# ============================================================================
def selftest():
    print("=== 멀티센서 융합 트래킹 자체검증 ===")
    ok_all = True
    rng = random.Random(11)
    holder = {}

    SENSORS = {"A": (-2500.0, 0.0), "B": (2500.0, 0.0), "C": (0.0, 4500.0)}
    SRANGE = 6500.0

    def run(people_fn, n_steps=70, dt=0.1, noise=80.0, vis=None, sbias=None,
            spurious=None, perturb=None, warmup=12, topts=None):
        tr = FusionTracker(**(topts or {}))
        holder["tr"] = tr; holder["counts"] = []
        if sbias is None:
            sbias = {s: (0.0, 0.0) for s in SENSORS}

        def default_vis(sid, pi, px, py):
            return _dist(SENSORS[sid][0], SENSORS[sid][1], px, py) <= SRANGE
        vis = vis or default_vis
        history = []
        for k in range(n_steps):
            t = k * dt
            people = people_fn(t)
            dets = []
            for pi, (px, py, vx, vy) in enumerate(people):
                du = _unit(vx, vy)
                for sid in SENSORS:
                    if not vis(sid, pi, px, py):
                        continue
                    bx, by = sbias[sid]
                    dets.append({"sid": sid, "tid": pi + 1,
                                 "x": px + bx + rng.gauss(0, noise),
                                 "y": py + by + rng.gauss(0, noise), "dir": du})
            if perturb:
                perturb(k, dets, rng)
            if spurious:
                dets += spurious(k, rng)
            allt = tr.step(dets, t)
            holder["counts"].append((t, tr.counted_count(), tr.confirmed_count()))
            if k >= warmup:
                history.append((t, people, [x for x in allt if x["confirmed"]]))
        return history

    def check(name, history, n_people, *, expect_sensors=None, allow_id_switch=False,
              count_tol=0.9):
        nonlocal ok_all
        fails = []
        pid_to_tid = {}; switches = 0; cnt_ok = 0
        for (t, people, tracks) in history:
            if len(tracks) == n_people:
                cnt_ok += 1
            frame_ids = {}
            for pi, (px, py, vx, vy) in enumerate(people):
                near = min(tracks, key=lambda tk: _dist(tk["x"], tk["y"], px, py),
                           default=None)
                if near is None or _dist(near["x"], near["y"], px, py) > 900:
                    fails.append(f"t={t:.1f} 사람{pi} 트랙없음"); continue
                if not allow_id_switch and near["id"] in frame_ids.values():
                    fails.append(f"t={t:.1f} 동일ID 붕괴")
                frame_ids[pi] = near["id"]
                prev = pid_to_tid.get(pi)
                if prev is not None and prev != near["id"]:
                    switches += 1
                pid_to_tid[pi] = near["id"]
                if expect_sensors is not None and near["n_sensors"] < expect_sensors:
                    fails.append(f"t={t:.1f} 사람{pi} 센서수 {near['n_sensors']}<{expect_sensors}")
        if cnt_ok < count_tol * len(history):
            fails.append(f"트랙수 정확도 {cnt_ok}/{len(history)} (<{count_tol:.0%})")
        if not allow_id_switch and switches > 0:
            fails.append(f"ID 전환 {switches}회")
        uniq = list(dict.fromkeys(fails))
        ok = not fails; ok_all &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else "  ← " + "; ".join(uniq[:4])))

    def report(name, cond, detail=""):
        nonlocal ok_all
        ok_all &= cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else "  ← " + detail))

    def P1(t):
        return [(900 * math.sin(t * 0.7), 2200 + 500 * math.sin(t * 0.5), 0, 0)]
    def P3(t):
        return [(-1500, 2000 + 200 * math.sin(t), 0, 0),
                (0, 2600 + 200 * math.sin(t + 1), 0, 0),
                (1500, 2000 + 200 * math.sin(t + 2), 0, 0)]

    check("A 1명·3센서겹침", run(P1), 1, expect_sensors=3, count_tol=0.95)
    check("B 2명 분리", run(lambda t: [
        (-2000 + 200 * math.sin(t), 1800, 0, 0),
        (2000 + 200 * math.cos(t), 1900, 0, 0)]), 2, count_tol=0.95)
    check("C 2명 근접겹침", run(lambda t: [
        (-800, 2200 + 300 * math.sin(t * 0.6), 0, 0),
        (800, 2200 + 300 * math.sin(t * 0.6 + 1), 0, 0)]), 2, count_tol=0.95)

    def vis_d(sid, pi, px, py):
        if sid == "C":
            return False
        if sid == "A":
            return px <= 400 and _dist(*SENSORS["A"], px, py) <= SRANGE
        return px >= -400 and _dist(*SENSORS["B"], px, py) <= SRANGE
    check("D 핸드오프(단일센서 구간)", run(lambda t: [
        (-2200 + 900 * t, 2200, 900, 0)], n_steps=52, vis=vis_d), 1, count_tol=0.8)
    check("E 교차(2명)", run(lambda t: [
        (-1500 + 300 * t, 2200, 300, 0),
        (1500 - 300 * t, 2300, -300, 0)], n_steps=80),
        2, allow_id_switch=True, count_tol=0.8)

    def spurious_f(k, rng):
        return [{"sid": "C", "tid": 3, "x": 1800.0, "y": 4000.0, "dir": None}] if k % 12 == 4 else []
    check("F 단발유령 억제", run(P1, spurious=spurious_f), 1, count_tol=0.95)
    check("G 3명·3센서", run(P3), 3, count_tol=0.9)
    check("H 캘리브바이어스(중)", run(lambda t: [
        (700 * math.sin(t * 0.7), 2300, 0, 0)],
        sbias={"A": (150, -80), "B": (-120, 100), "C": (60, 130)}),
        1, expect_sensors=3, count_tol=0.9)
    check("I 넓은퍼짐 1명(회귀:지름)", run(lambda t: [(0, 2300, 0, 0)], noise=40.0,
        sbias={"A": (400, 0), "B": (-400, 0), "C": (0, 0)}), 1, count_tol=0.95)
    check("J 근접2명 독립관측(회귀:병합)", run(lambda t: [
        (-275, 2200, 0, 0), (275, 2200, 0, 0)]), 2, count_tol=0.9)

    def vis_k(sid, pi, px, py):
        if sid == "C":
            return False
        return (sid == "A") if pi == 0 else (sid == "B")
    check("K 서로소센서 2명(회귀:gate)", run(lambda t: [
        (-450, 2200, 0, 0), (450, 2200, 0, 0)], vis=vis_k, noise=40.0), 2, count_tol=0.9)

    def vis_l(sid, pi, px, py):
        return sid == "A" and _dist(*SENSORS["A"], px, py) <= SRANGE
    check("L 단일센서 1명", run(lambda t: [
        (-1500 + 300 * math.sin(t * 0.5), 2200, 0, 0)], vis=vis_l), 1, count_tol=0.9)

    def perturb_m(k, dets, rng):
        if k % 5 == 0:
            for d in dets:
                if d["sid"] == "A":
                    d["x"] += 1800.0; d["y"] -= 1200.0
    hist_m = run(lambda t: [(300 * math.sin(t * 0.4), 2300, 0, 0)], perturb=perturb_m)
    maxdev = max((min((_dist(tk["x"], tk["y"], p[0][0], p[0][1]) for tk in tks), default=9e9)
                  for (t, p, tks) in hist_m if tks), default=9e9)
    report(f"M 스파이크 흡수(최대편차 {maxdev:.0f}mm<450)", maxdev < 450, f"maxdev={maxdev:.0f}")

    hist_n = run(lambda t: [(500 * math.sin(t * 0.5), 2300, 0, 0)],
                 n_steps=80, warmup=16, topts={"stride": 4, "window": 6})
    check("N stride=4 추적", hist_n, 1, count_tol=0.9)
    report(f"N stride=4 추론빈도(≈20, 실제 {holder['tr']._infer_count})",
           18 <= holder["tr"]._infer_count <= 22, f"infer={holder['tr']._infer_count}")
    check("O window=1 동작유지", run(lambda t: [
        (600 * math.sin(t * 0.6), 2300, 0, 0)], topts={"window": 1}), 1, count_tol=0.9)

    # ---- 플러그인 옵션 ----
    # P: 헝가리안 매칭으로도 3명 안정 추적
    check("P assign=hungarian 3명", run(P3, topts={"assign": "hungarian"}), 3, count_tol=0.9)
    # Q: 확정 큐 — 검출이 간헐적으로(3프레임마다) 통째 결측돼도 1명 안정 확정·유지
    def drop_q(k, dets, rng):
        if k % 3 == 2:
            dets.clear()
    check("Q 확정큐 flicker 안정", run(P1, perturb=drop_q,
        topts={"queue_k": 3, "queue_size": 5}), 1, count_tol=0.9)
    # R: dwell 디바운스 — 진입 후 dwell_enter 전엔 카운트 0, 후엔 1
    run(P1, n_steps=30, warmup=0, topts={"dwell": True, "dwell_enter_sec": 0.6})
    counts = holder["counts"]
    delayed = any(cf >= 1 and c == 0 for (t, c, cf) in counts if t < 0.6)   # 확정됐지만 아직 카운트 X
    late_ok = all(c == 1 for (t, c, cf) in counts if t >= 0.9)
    report("R dwell 디바운스(진입지연 후 카운트)", delayed and late_ok,
           f"delayed={delayed} late_ok={late_ok}")
    # S: 옵션 전부 켬 동시 동작 (3명)
    check("S 전옵션 동시(hungarian+queue+dwell)", run(P3,
        topts={"assign": "hungarian", "queue_k": 3, "queue_size": 5,
               "dwell": True, "dwell_enter_sec": 0.3}),
        3, count_tol=0.85)
    # T(회귀): 단일센서에서 사람이 급반전 → 센서가 같은 사람을 다른 tid 로 재배정(1→2).
    #   최근성 게이트가 없으면 옛 tid 가 창에 '스테일 유령'으로 남아 같은센서 분리→ID 스위칭 발생.
    #   사용자 실사용 설정(window=10,stride=2,min_frames=4,hungarian,queue,dwell)에서 전역 ID 안정 확인.
    def people_t(t):   # 한 번 반전 후 계속 멀어짐(삼각파) — 반환점 유령과 실제 경로가 갈라짐
        x = (-1800 + 500 * t) if t < 2.0 else (-1800 + 500 * 2.0 - 500 * (t - 2.0))
        return [(x, 2200, 0, 0)]
    def vis_t(sid, pi, px, py):
        return sid == "A" and _dist(*SENSORS["A"], px, py) <= SRANGE
    def perturb_t(k, dets, rng):
        if k >= 20:                      # 반전 시점에 센서가 같은 사람을 다른 tid 로 재배정
            for d in dets:
                d["tid"] = 2
    check("T tid재배정+반전 ID안정", run(people_t, n_steps=60, vis=vis_t, perturb=perturb_t,
        topts={"window": 10, "stride": 2, "min_frames": 4, "assign": "hungarian",
               "queue_k": 3, "queue_size": 5, "dwell": True}), 1, count_tol=0.8)
    # U(회귀): 확정 트랙 옆(반경 내)에 같은 센서 유령이 갑자기·지속 생김 → 노이즈로 흡수(신규 트랙 억제)
    def spurious_u(k, rng):
        return [{"sid": "A", "tid": 9, "x": 300.0, "y": 2300.0, "dir": None}] if k >= 15 else []
    check("U 근처유령 흡수(noise_radius=500)", run(lambda t: [(0, 2300, 0, 0)],
        spurious=spurious_u, topts={"noise_radius_mm": 500}), 1, count_tol=0.9)
    # U2(제어): 반경을 100 으로 줄이면 흡수 안 되어 유령이 별도 트랙 → 반경 파라미터가 실제로 동작함
    check("U2 작은반경→유령 별도트랙", run(lambda t: [(0, 2300, 0, 0)],
        spurious=spurious_u, topts={"noise_radius_mm": 100}), 2, count_tol=0.7)

    # ---- 재식별(ReID) : 실제 시나리오 = 두 사람이 근접해 '센서가 한 점으로' 측정(먹힘) →
    #      먹힌 트랙이 소멸/병합으로 사라짐 → 다시 멀어지면 새 ID 가 아니라 옛 ID 로 부활해야 함.
    #      (단일-인물 가림 테스트는 병합 경로를 안 타 실제 실패를 못 잡음 — 리뷰 지적으로 교체)
    def people_reid(t):     # 두 사람 접근(0~3s) → 근접 유지(3~5s) → 다시 멀어짐(5~8s)
        if t < 3.0:   fr = t / 3.0
        elif t < 5.0: fr = 1.0
        else:         fr = max(0.0, 1.0 - (t - 5.0) / 3.0)
        x = 1300.0 - (1300.0 - 200.0) * fr
        return [(-x, 2300.0, 0, 0), (x, 2300.0, 0, 0)]

    def sensor_merge(k, dets, rng):     # 근접 구간엔 각 센서가 두 사람을 한 타겟(중점)으로 보고
        if 3.0 <= k * 0.1 <= 5.0:
            by = {}
            for d in dets:
                by.setdefault(d["sid"], []).append(d)
            dets.clear()
            for sid, ds in by.items():
                if len(ds) >= 2:
                    mx = sum(d["x"] for d in ds) / len(ds); my = sum(d["y"] for d in ds) / len(ds)
                    dets.append({"sid": sid, "tid": 1, "x": mx, "y": my, "dir": None})
                else:
                    dets.extend(ds)

    def reid_run(reid_dist):
        return run(people_reid, n_steps=80, warmup=0, perturb=sensor_merge,
                   topts={"window": 5, "queue_k": 3, "queue_size": 5, "max_miss_sec": 1.2,
                          "assign": "hungarian", "gate_mm": 750, "merge_mm": 550,
                          "noise_radius_mm": 1500, "reid_dist_mm": reid_dist,
                          "reid_max_gap_sec": 3.0})

    def ids_stats(hist, tmin):
        mx = 0; after = set()
        for (t, p, tks) in hist:
            for z in tks:
                mx = max(mx, z["id"])
                if t >= tmin:
                    after.add(z["id"])
        return mx, sorted(after)

    h_on = reid_run(1200.0); rt_on = holder["tr"].reid_total
    mx_on, aft_on = ids_stats(h_on, 6.5)
    report("V 2인 근접병합→분리 ReID(옛 ID 부활·새 ID 억제)",
           mx_on <= 2 and rt_on >= 1, f"max_id={mx_on} 분리후={aft_on} reid_total={rt_on}")
    reid_fired = any(z.get("reid") for (t, p, tks) in h_on for z in tks)
    report("V2 ReID 표식(reid=True 발생)", reid_fired, "부활 표식 없음")

    h_off = reid_run(0.0)
    mx_off, aft_off = ids_stats(h_off, 6.5)
    report("W ReID 끔→분리 시 새 ID(대조)", mx_off > 2, f"max_id={mx_off} 분리후={aft_off}")

    # V3: 체류시간(dwell)이 ReID 부활 후 '공백까지 포함'해 이어지는가.
    #   부활 트랙의 마지막 dwell_sec ≈ 경과한 총 시간(≈그 사람이 처음 등장한 뒤 흐른 시간)이어야 함
    #   (새로 0부터 시작하면 실패). h_on 마지막 프레임에서 dwell 최대값을 본다.
    last_dwell = 0.0; last_t = 0.0
    for (t, p, tks) in h_on:
        if tks:
            last_t = t
            last_dwell = max(last_dwell, max(z.get("dwell_sec", 0.0) for z in tks))
    report(f"V3 dwell 공백포함 지속(끝 dwell {last_dwell:.1f}≈경과 {last_t:.1f}s)",
           last_dwell >= last_t - 0.6, f"dwell={last_dwell:.2f} t={last_t:.2f}")

    # ---- 파라미터 레지스트리(SSOT) 일관성 ----
    # (1) 파생 테이블이 기대 스냅샷과 정확히 일치(전사 오류 방지)
    exp_argmap = {
        "window": "window", "stride": "stride", "fuse_min_frames": "min_frames",
        "move_min": "move_min_mm", "noise_radius": "noise_radius_mm",
        "recent_frames": "recent_frames", "jump_factor": "jump_factor",
        "assign": "assign", "gate_mm": "gate_mm", "dir_pen": "dir_pen_mm",
        "ang_gate": "ang_gate_deg", "merge_mm": "merge_mm", "coast_grow": "coast_grow",
        "coast_decay": "coast_decay", "queue_k": "queue_k", "queue_size": "queue_size",
        "reid_dist": "reid_dist_mm", "reid_max_gap": "reid_max_gap_sec",
        "max_miss": "max_miss_sec", "max_miss_tentative": "max_miss_tentative_sec",
        "dwell": "dwell", "dwell_enter": "dwell_enter_sec", "alpha": "alpha", "beta": "beta",
        "max_speed": "max_speed_mm_s", "pred_dt_cap": "pred_dt_cap", "trail_len": "trail_len",
    }
    report("레지스트리 _FUSION_ARG_MAP 파생 일치", dict(_FUSION_ARG_MAP) == exp_argmap,
           f"diff={set(dict(_FUSION_ARG_MAP).items()) ^ set(exp_argmap.items())}")
    report("레지스트리 NAME2KW kw 커버리지", set(NAME2KW.values()) == set(exp_argmap.values()),
           "kw 집합 불일치")
    # (2) tracker_params round-trip: 각 kw 로 넣은 값이 같은 kw 로 되읽힌다(kw→attr_tk 매핑 정확성)
    probe = {"window": 7, "stride": 3, "min_frames": 4, "move_min_mm": 123.0,
             "noise_radius_mm": 234.0, "recent_frames": 5, "jump_factor": 1.7,
             "assign": "hungarian", "gate_mm": 812.0, "dir_pen_mm": 91.0, "ang_gate_deg": 66.0,
             "merge_mm": 501.0, "coast_grow": 222.0, "coast_decay": 0.7, "queue_k": 2,
             "queue_size": 6, "reid_dist_mm": 640.0, "reid_max_gap_sec": 4.4, "max_miss_sec": 1.1,
             "max_miss_tentative_sec": 0.6, "dwell": True, "dwell_enter_sec": 0.5, "alpha": 0.4,
             "beta": 0.15, "max_speed_mm_s": 2800.0, "pred_dt_cap": 0.25, "trail_len": 40}
    tp = tracker_params(FusionTracker(**probe))
    mism = {k: (probe[k], tp.get(k)) for k in probe if tp.get(k) != probe[k]}
    report("tracker_params round-trip(kw→attr 매핑 정확)", not mism, f"불일치={mism}")

    print("=== 결과:", "전부 PASS ✅" if ok_all else "일부 FAIL ❌", "===")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
