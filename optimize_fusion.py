#!/usr/bin/env python3
"""
ID 스위칭 최소화 하이퍼파라미터 최적화 (기록 replay 기반)
================================================================================

run_debug_gui.sh 가 남긴 원시 기록(JSONL)을 읽어, 그 안의 dets(융합 트래커 입력) 스트림을
서로 다른 하이퍼파라미터로 FusionTracker 에 **재생(replay)** 하면서, "1명인데 ID 가 몇 번
바뀌는가"를 채점해 가장 안정적인(스위칭이 적은) 파라미터 조합을 찾는다.

핵심 전제
  · 기록은 '한 사람'이 돌아다닌 것 → 이상적으로는 하나의 ID 로만 추적돼야 한다.
  · dets/t 만 있으면 FusionTracker 결과가 결정적으로 재현되므로, 같은 기록을 다른 HP 로
    돌려 오프라인 비교가 가능하다(FUSE_HZ 는 기록 시 고정 — replay 로는 못 바꿈).

스위칭 채점(사용자 규칙 반영)
  · OUT   : 어떤 센서도 검출 못함(장소 완전 이탈). exit-gap 이상 지속 후 복귀 시 ID 바뀌어도 OK.
  · INSIDE: 마진 범위 안(|각도| ≤ margin-deg, 거리 ≤ margin-mm). '장소 안' → 스위칭/중복 금지(최고 벌점).
  · EDGE  : 검출은 되지만 마진 밖(가장자리). 최소 한 센서엔 잡힘 → 스위칭/중복 금지(중간 벌점).
  · coverage: INSIDE 인데 confirmed 트랙이 없으면 '추적 실패' — 스위칭 0을 노린 꼼수 방지용 벌점.

용법:  ./run_optimization.sh   (또는)   python optimize_fusion.py --log <파일> [옵션]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random

from fusion import FusionTracker, NAME2KW, replay_frames

# 친화 이름 ↔ tracker 인자 매핑은 fusion.FUSION_PARAMS(SSOT)에서 파생된 NAME2KW 를 그대로 쓴다
# (예전엔 여기 별도 dict 를 손으로 유지 → 드리프트 위험). SPACE(탐색 후보값)만 최적화 정책으로 남긴다.
KW2NAME = {v: k for k, v in NAME2KW.items()}

# 탐색 공간 (친화 이름 → 후보값 목록). --fix 로 고정된 것은 자동 제외.
SPACE = {
    "WINDOW": [3, 5, 8, 10, 12, 15, 20],
    "STRIDE": [1, 2, 3, 4],
    "FUSE_MIN_FRAMES": [1, 2, 3, 4, 6, 8],
    "MOVE_MIN": [100.0, 150.0, 200.0, 250.0, 300.0, 400.0],
    "NOISE_RADIUS": [0.0, 300.0, 500.0, 800.0, 1000.0, 1500.0, 2000.0],
    "RECENT_FRAMES": [1, 2, 3, 4, 6],
    "JUMP_FACTOR": [1.0, 1.5, 2.0, 3.0],
    "ASSIGN": ["greedy", "hungarian"],
    "GATE_MM": [500.0, 650.0, 750.0, 900.0, 1100.0, 1300.0, 1500.0],
    "DIR_PEN": [0.0, 60.0, 120.0, 200.0, 300.0],
    "ANG_GATE": [45.0, 60.0, 75.0, 90.0, 110.0],
    "MERGE_MM": [300.0, 450.0, 550.0, 700.0, 900.0, 1200.0],
    "COAST_GROW": [0.0, 150.0, 350.0, 600.0, 1000.0],
    "COAST_DECAY": [0.5, 0.7, 0.85, 0.95, 1.0],
    "QUEUE_K": [1, 2, 3, 4, 5],
    "QUEUE_SIZE": [3, 5, 7, 10],
    "REID_DIST": [0.0, 500.0, 800.0, 1200.0, 1800.0],   # 재식별 위치 임계(mm), 0=끔
    "REID_MAX_GAP": [1.5, 2.5, 4.0, 6.0],               # 재식별 시간 임계(초, max_miss 초과분만 유효)
    "MAX_MISS": [0.5, 1.0, 1.5, 2.0, 3.0, 5.0],
    "MAX_MISS_TENT": [0.3, 0.5, 0.8, 1.2],
    "ALPHA": [0.3, 0.45, 0.6, 0.8],
    "BETA": [0.1, 0.2, 0.35],
    "MAX_SPEED": [2500.0, 3500.0, 5000.0],
    "PRED_DT_CAP": [0.2, 0.3, 0.5],
}


# ---------------------------------------------------------------------------
def load_log(path):
    header, frames = None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if o.get("_type") == "header":
                header = o
            else:
                frames.append(o)
    return header, frames


def cast(v):
    """--fix 문자열 값을 bool/int/float/str 로 변환."""
    if isinstance(v, (bool, int, float)):
        return v
    s = v.strip()
    if s in ("greedy", "hungarian"):
        return s
    low = s.lower()
    if low == "true":                     # DWELL 등 불리언 (bool 없으면 dwell=false 가 켜져버림)
        return True
    if low == "false":
        return False
    try:
        if "." not in s and "e" not in low:
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


# ---------------------------------------------------------------------------
def replay(frames, params):
    """dets 스트림을 재생 → 프레임별 confirmed 트랙 목록(채점에 필요한 필드만 투영).
    공용 드라이버 fusion.replay_frames 를 사용(라이브와 동일한 step). members 는 트랙↔사람 귀속에,
    dwell_sec 는 체류시간 채점에 쓰인다. 단일-인물 score_run 은 id/x/y 만 보므로 추가 키는 무시(하위호환)."""
    return [[{"id": t["id"], "x": t["x"], "y": t["y"],
              "members": t.get("members", []), "dwell_sec": t.get("dwell_sec", 0.0)}
             for t in tracks]
            for tracks in replay_frames(frames, params, confirmed_only=True)]


def presence_class(frame, margin_deg, margin_mm):
    """프레임의 사람 상태: OUT(전 센서 미검출) / INSIDE(마진 안) / EDGE(검출되나 마진 밖)."""
    any_t = inside = False
    for s in frame["raw"]:
        for t in s["targets"]:
            any_t = True
            d = math.hypot(t["lx"], t["ly"])
            a = abs(math.degrees(math.atan2(t["lx"], t["ly"])))
            if d <= margin_mm and a <= margin_deg:
                inside = True
    if not any_t:
        return "OUT"
    return "INSIDE" if inside else "EDGE"


def score_run(frames, conf_per_frame, cls_per_frame, *, exit_gap, weights, min_cov):
    """스위칭/중복/커버리지 → (score, breakdown). 낮을수록 좋음.

    exit 처리: 전 센서 미검출(OUT)이 exit_gap 이상 지속되면 '진짜 이탈'로 래치(exit_latched)한다.
    이 래치는 '검출 재개'가 아니라 '다음 confirmed 트랙 재획득' 시점까지 유지된다 — 재확인은
    min_frames/queue_k 만큼 지연되므로, 첫 복귀 프레임에서 유예를 소진하면 정당한 재진입이
    스위칭으로 오판된다(리뷰 지적). 래치가 살아있는 동안의 ID 변경은 '정당한 재진입'으로 집계."""
    prev_dom = prev_pos = None
    out_run = 0.0
    exit_latched = False
    prev_t = None
    sw_in = sw_ed = 0
    dup_in_ep = dup_ed_ep = 0
    in_dup_run = ed_dup_run = False
    present = present_cov = inside = 0
    ids = set()
    legit_reentries = 0

    for f, conf, cls in zip(frames, conf_per_frame, cls_per_frame):
        t = f["t"]
        dt = (t - prev_t) if prev_t is not None else 0.0
        prev_t = t

        if cls == "OUT":
            out_run += dt
            if out_run >= exit_gap:
                exit_latched = True       # 진짜 이탈 — 재획득까지 래치 유지
            in_dup_run = ed_dup_run = False
            continue

        # 검출 프레임(INSIDE 또는 EDGE)
        present += 1
        if conf:
            present_cov += 1
        if cls == "INSIDE":
            inside += 1
            if len(conf) >= 2:
                if not in_dup_run:
                    dup_in_ep += 1; in_dup_run = True
            else:
                in_dup_run = False
            ed_dup_run = False
        else:  # EDGE
            if len(conf) >= 2:
                if not ed_dup_run:
                    dup_ed_ep += 1; ed_dup_run = True
            else:
                ed_dup_run = False
            in_dup_run = False

        # 사람의 '대표' 트랙 선택(sticky): 기존 id 가 살아있으면 유지, 아니면 최근 위치 최근접
        dom = None
        if conf:
            here = {c["id"]: c for c in conf}
            if prev_dom in here:
                dom = here[prev_dom]
            elif prev_pos is not None:
                dom = min(conf, key=lambda c: math.hypot(c["x"] - prev_pos[0],
                                                         c["y"] - prev_pos[1]))
            else:
                dom = conf[0]
        if dom is None:
            continue          # 검출됐지만 아직 confirmed 없음 → prev_dom/out_run/exit_latched 유지

        # 재획득 완료 시점: 여기서만 out_run/exit_latched 를 소거
        ids.add(dom["id"])
        if prev_dom is not None and dom["id"] != prev_dom:
            if exit_latched:
                legit_reentries += 1               # 완전 이탈 후 복귀 → 허용(벌점 없음)
            elif cls == "INSIDE":
                sw_in += 1
            else:
                sw_ed += 1
        prev_dom = dom["id"]
        prev_pos = (dom["x"], dom["y"])
        out_run = 0.0
        exit_latched = False

    # 커버리지: 검출(INSIDE+EDGE) 프레임 중 confirmed 트랙이 있던 비율.
    # (INSIDE 만 보던 이전 방식은 EDGE 전용 기록에서 무추적 꼼수를 못 막았음 — 리뷰 지적)
    cov = (present_cov / present) if present else 1.0
    invalid = present > 0 and cov < min_cov
    extra_ids = max(0, len(ids) - 1 - legit_reentries)   # 정당한 재진입 id 는 제외
    W = weights
    score = ((W["invalid"] if invalid else 0.0)
             + W["sw_in"] * sw_in + W["sw_ed"] * sw_ed
             + W["dup_in"] * dup_in_ep + W["dup_ed"] * dup_ed_ep
             + W["cov"] * (1.0 - cov) + W["ids"] * extra_ids)
    bd = dict(score=round(score, 1), sw_in=sw_in, sw_ed=sw_ed,
              dup_in=dup_in_ep, dup_ed=dup_ed_ep, n_ids=len(ids),
              reentry=legit_reentries, extra_ids=extra_ids,
              coverage=round(cov, 3), present=present, inside=inside, invalid=invalid)
    return score, bd


def score_run_multi(gt, conf_per_frame, *, exit_gap, weights, min_cov, collision_mm, dwell_gap=8.0):
    """다중-인물 스코어러. score_run 을 '사람별'로 확장 + 교차-인물 병합 벌점.

    · 사람 g 의 presence 는 그 사람 소스 로그의 raw 로만 판정(gt['cls'][i][g]).
    · 트랙→사람 귀속: 트랙 members 의 (sid,tid)를 gt['tid2gt'] 로 역매핑, 다수결로 소유자 결정.
    · 교차-인물 병합: 한 트랙 members 가 '동시에 present 이고 서로 겹치지 않은' 2명 이상에 걸치면
      = 두 사람을 하나로 잘못 합침(핵심 벌점).
    · R2(공간 충돌): 두 GT 사람이 collision_mm 이내로 겹친 프레임은 어떤 트래커도 분리 불가 →
      그 쌍의 교차벌점과, 그 순간 해당 사람의 스위칭 집계를 '불공정'으로 보고 제외.
    · 체류시간(dwell) 정확도: 사람별로 '기대 체류'(ep_dwell = 이번 체류 에피소드 동안 present 시간,
      완전 이탈 시 0으로 리셋)와 트래커가 보고한 대표 트랙의 dwell_sec 를 비교한다. ID 가 유지되면
      (ReID 포함) dwell_sec ≈ ep_dwell → 결손 0. 조각나면(새 ID) dwell_sec 가 리셋돼 결손 발생.
      dwell_deficit = 에피소드 내 최대 결손(초) — '정체성 조각으로 잃어버린 연속 체류시간'."""
    N = gt["n"]; frames = gt["frames"]; cls = gt["cls"]; pos = gt["positions"]
    tid2gt = gt["tid2gt"]
    st = [dict(prev_dom=None, prev_pos=None, out_run=0.0, exit_latched=False,
               sw_in=0, sw_ed=0, dup_in=0, dup_ed=0, in_dup_in=False, in_dup_ed=False,
               present=0, present_cov=0, inside=0, ids=set(), reentry=0,
               ep_start=None, dwell_deficit=0.0)
          for _ in range(N)]
    xmerge_frames = xmerge_ep = 0
    in_xmerge = False
    collision_frames = 0
    min_interperson = float("inf")
    prev_t = None

    for i, f in enumerate(frames):
        t = f["t"]; dt = (t - prev_t) if prev_t is not None else 0.0; prev_t = t
        conf = conf_per_frame[i]

        # 1) 트랙별 소유자(다수결) + members 가 걸친 gt 집합
        owners = []
        for tr in conf:
            votes = {}
            for m in tr.get("members", []):
                g = tid2gt.get((m[0], m[1]))
                if g is not None:
                    votes[g] = votes.get(g, 0) + 1
            owner = max(votes, key=votes.get) if votes else None
            owners.append((owner, set(votes.keys())))

        # 2) present 사람 + 쌍별 거리 → 겹침(collision) 집합
        present_gs = [g for g in range(N) if cls[i][g] != "OUT"]
        colliding = set()
        for a in range(len(present_gs)):
            for b in range(a + 1, len(present_gs)):
                g1, g2 = present_gs[a], present_gs[b]
                p1, p2 = pos[i][g1], pos[i][g2]
                if p1 and p2:
                    d = math.hypot(p1[0] - p2[0], p1[1] - p2[1])
                    if d < min_interperson:
                        min_interperson = d
                    if d < collision_mm:
                        colliding.add(frozenset((g1, g2)))
        if colliding:
            collision_frames += 1
        # 겹침 '그룹'(연결요소)의 대표(최소 index)만 커버리지를 집계한다.
        # 그룹은 센서에서 한 blob 으로 합쳐지고 그 blob 은 대표(최소 tid=최소 index)가 소유하므로,
        # 대표의 커버리지 = 'blob 을 추적했는가'. 이렇게 해야 겹침 중 트랙을 전부 놓치는 HP 가
        # 커버리지 벌점을 받는다(리뷰: 그 반대로 두면 겹침 구간 트래킹 품질을 구분 못함). 비대표
        # 그룹원은 '먹힌' 사람이라 커버리지에서 제외(불공정 방지).
        group_rep = {}
        if colliding:
            parent = {g: g for g in present_gs}

            def _find(a):
                while parent[a] != a:
                    parent[a] = parent[parent[a]]; a = parent[a]
                return a

            for fs in colliding:
                g1, g2 = tuple(fs)
                r1, r2 = _find(g1), _find(g2)
                if r1 != r2:
                    parent[max(r1, r2)] = min(r1, r2)     # 최소 index 가 루트(=대표)
            for g in present_gs:
                if any(g in fs for fs in colliding):
                    group_rep[g] = _find(g)

        # 3) 교차-인물 병합: 한 트랙이 present·비겹침 2명 이상에 걸치면 잘못된 병합
        frame_x = False
        for owner, S in owners:
            Sp = [g for g in S if g in present_gs]
            for a in range(len(Sp)):
                for b in range(a + 1, len(Sp)):
                    if frozenset((Sp[a], Sp[b])) not in colliding:
                        frame_x = True
        if frame_x:
            xmerge_frames += 1
            if not in_xmerge:
                xmerge_ep += 1; in_xmerge = True
        else:
            in_xmerge = False

        # 4) 사람별 confirmed 트랙 모음(소유자 기준)
        conf_by = [[] for _ in range(N)]
        for (owner, S), tr in zip(owners, conf):
            if owner is not None:
                conf_by[owner].append(tr)

        # 5) 사람별 상태머신(= score_run 로직을 사람별로)
        for g in range(N):
            s = st[g]; c = cls[i][g]; cf = conf_by[g]
            contended = g in group_rep
            if c == "OUT":
                s["out_run"] += dt
                if s["out_run"] >= exit_gap:
                    s["exit_latched"] = True             # (스위칭 재진입 로직용)
                if s["out_run"] >= dwell_gap:            # 긴 공백 → 체류 에피소드 종료(기대 dwell 리셋)
                    s["ep_start"] = None
                s["in_dup_in"] = s["in_dup_ed"] = False
                continue
            # 체류 에피소드 시작 = 이 사람의 첫 '확정 트랙' 관측 시각(t). dom.first_seen 과 정렬돼
            # 완벽 추적이면 결손 0(프레임/stride 양자화 노이즈 없음 — 리뷰 지적). exit_gap 이 아니라
            # dwell_gap(>reid_max_gap 탐색범위) 로만 리셋 → (exit_gap, reid_max_gap] 구간의 ReID 실패도
            # dwell 결손으로 잡힌다(리뷰 HIGH: 그 구간에 벌점 gradient 가 사라지던 문제 해결).
            if cf and s["ep_start"] is None:
                s["ep_start"] = t
            if contended:
                # 겹침(센서가 한 명으로 측정) 중엔 이 사람을 독립 추적/채점 불가:
                # 중복·스위칭·dwell결손 제외, 겹침 '전' 정체성(prev_dom) 보존(→ 분리 후 ReID 성패 채점).
                # 단 그룹 '대표'는 blob 커버리지를 집계(겹침 중 전부 놓치는 HP 방지). 위치만 GT 로 갱신.
                p = pos[i][g]
                if p is not None:
                    s["prev_pos"] = p
                s["out_run"] = 0.0                       # present 이므로 이탈 아님
                if group_rep[g] == g:
                    s["present"] += 1
                    if cf:
                        s["present_cov"] += 1
                # in_dup 플래그 보존(리셋 X): 겹침 사이 연속 중복 이중계수 방지.
                continue
            s["present"] += 1
            if cf:
                s["present_cov"] += 1
            # 한 사람이 동시에 2트랙↑ = 분열/중복. INSIDE/EDGE 를 나눠 단일 스코어러와 동일 가중.
            if c == "INSIDE":
                s["inside"] += 1
                if len(cf) >= 2:
                    if not s["in_dup_in"]:
                        s["dup_in"] += 1; s["in_dup_in"] = True
                else:
                    s["in_dup_in"] = False
                s["in_dup_ed"] = False
            else:  # EDGE
                if len(cf) >= 2:
                    if not s["in_dup_ed"]:
                        s["dup_ed"] += 1; s["in_dup_ed"] = True
                else:
                    s["in_dup_ed"] = False
                s["in_dup_in"] = False
            dom = None
            if cf:
                here = {x["id"]: x for x in cf}
                if s["prev_dom"] in here:
                    dom = here[s["prev_dom"]]
                elif s["prev_pos"] is not None:
                    dom = min(cf, key=lambda x: math.hypot(x["x"] - s["prev_pos"][0],
                                                           x["y"] - s["prev_pos"][1]))
                else:
                    dom = cf[0]
            if dom is None:
                continue
            s["ids"].add(dom["id"])
            if s["prev_dom"] is not None and dom["id"] != s["prev_dom"]:
                if s["exit_latched"]:
                    s["reentry"] += 1       # 완전 이탈 후 복귀 → 허용
                elif c == "INSIDE":
                    s["sw_in"] += 1         # (겹침 프레임은 위에서 이미 제외됨)
                else:
                    s["sw_ed"] += 1
            s["prev_dom"] = dom["id"]; s["prev_pos"] = (dom["x"], dom["y"])
            s["out_run"] = 0.0; s["exit_latched"] = False
            # dwell 결손: 대표(dom·화면에 보이는 정체성) 트랙의 dwell_sec 가 '기대 체류'(t-ep_start)에
            # 못 미친 최대치(초). ID 유지/ReID 성공 → dom.first_seen≈ep_start → 결손 0. 새 ID 로 조각나면
            # (에피소드 시작~조각 시점)의 연속 체류가 결손으로 잡힌다. 공백(OUT)도 ep_start 기준이라 포함.
            if s["ep_start"] is not None:
                meas = dom.get("dwell_sec", 0.0)
                s["dwell_deficit"] = max(s["dwell_deficit"], (t - s["ep_start"]) - meas)

    # 집계
    W = weights
    tot_sw_in = sum(s["sw_in"] for s in st)
    tot_sw_ed = sum(s["sw_ed"] for s in st)
    tot_dup_in = sum(s["dup_in"] for s in st)
    tot_dup_ed = sum(s["dup_ed"] for s in st)
    tot_present = sum(s["present"] for s in st)
    tot_cov = sum(s["present_cov"] for s in st)
    cov = (tot_cov / tot_present) if tot_present else 1.0
    invalid = tot_present > 0 and cov < min_cov
    tot_extra = sum(max(0, len(s["ids"]) - 1 - s["reentry"]) for s in st)
    tot_dwell_def = sum(s["dwell_deficit"] for s in st)      # 잃어버린 연속 체류시간(초) 합
    # 싱글(개인 추적) = 사람별 스위칭/중복/커버리지/무효, 멀티(교차) = 교차-인물 병합,
    # dwell(체류시간) = 정체성 조각으로 잃은 연속 체류시간(초)×가중. 종합=셋의 합.
    single_score = ((W["invalid"] if invalid else 0.0)
                    + W["sw_in"] * tot_sw_in + W["sw_ed"] * tot_sw_ed
                    + W["dup_in"] * tot_dup_in + W["dup_ed"] * tot_dup_ed
                    + W["cov"] * (1.0 - cov) + W["ids"] * tot_extra)
    multi_score = W["xmerge_ep"] * xmerge_ep + W["xmerge_fr"] * xmerge_frames
    dwell_score = W.get("dwell", 0.0) * tot_dwell_def
    score = single_score + multi_score + dwell_score
    per = [dict(sw_in=s["sw_in"], sw_ed=s["sw_ed"], dup=s["dup_in"] + s["dup_ed"],
                n_ids=len(s["ids"]), reentry=s["reentry"],
                cov=round((s["present_cov"] / s["present"]) if s["present"] else 1.0, 3),
                present=s["present"], dwell_deficit=round(s["dwell_deficit"], 2)) for s in st]
    bd = dict(score=round(score, 1),
              single_score=round(single_score, 1), multi_score=round(multi_score, 1),
              dwell_score=round(dwell_score, 1), dwell_deficit=round(tot_dwell_def, 2),
              sw_in=tot_sw_in, sw_ed=tot_sw_ed,
              dup=tot_dup_in + tot_dup_ed, dup_in=tot_dup_in, dup_ed=tot_dup_ed,
              xmerge_ep=xmerge_ep, xmerge_frames=xmerge_frames,
              coverage=round(cov, 3), invalid=invalid,
              min_interperson=(round(min_interperson) if min_interperson < float("inf") else None),
              collision_frames=collision_frames, per_person=per)
    return score, bd


# ---------------------------------------------------------------------------
def run_search(active, base, evaluate, iters, cd_rounds, seed):
    """랜덤 탐색 + 좌표하강. (best_score, best_choice, best_bd, samples) 반환. 단일/다중 공용.
    samples = 랜덤탐색 (choice, bd) 목록 — 상관계수 분석용(공간을 고르게 훑은 표본)."""
    rng = random.Random(seed)
    samples = []
    best_choice = {n: base.get(NAME2KW[n], vals[0]) for n, vals in active.items()}
    best_score, best_bd = evaluate(best_choice)
    samples.append((dict(best_choice), best_bd))
    print(f"\n랜덤 탐색 {iters}회…")
    for _ in range(iters):
        choice = {n: rng.choice(vals) for n, vals in active.items()}
        s, bd = evaluate(choice)
        samples.append((choice, bd))
        if s < best_score:
            best_score, best_choice, best_bd = s, choice, bd
    for r in range(cd_rounds):
        improved = False
        for n, vals in active.items():
            for v in vals:
                if best_choice.get(n) == v:
                    continue
                trial = dict(best_choice); trial[n] = v
                s, bd = evaluate(trial)
                if s < best_score:
                    best_score, best_choice, best_bd, improved = s, trial, bd, True
        print(f"  좌표하강 R{r+1}: score={best_score:.1f}  {best_bd}")
        if not improved:
            break
    return best_score, best_choice, best_bd, samples


def save_params(final, active, fixed, out_path, comment_lines):
    """최적 파라미터를 콘솔 출력 + (있으면) sh 스니펫 저장. 단일/다중 공용."""
    print("최적 파라미터 (run_gui.sh 값으로 사용):")
    lines = []
    for n in NAME2KW:
        if n not in active and n not in fixed:
            continue
        tag = "  # 고정" if n in fixed else ""
        lines.append(f"  {n:16s}= {final.get(NAME2KW[n])}{tag}")
    print("\n".join(lines))
    print("  (그 외 파라미터는 기록 당시 값 유지. --out 파일에는 전체가 저장됨)")
    if out_path:
        # 확장자로 형식 선택: .yaml/.yml → 'KEY: v'(YAML), 그 외 → 'KEY=v'(sh source 가능)
        yaml = out_path.lower().endswith((".yaml", ".yml"))
        sep = ": " if yaml else "="
        with open(out_path, "w", encoding="utf-8") as fh:
            for cl in comment_lines:
                fh.write(f"# {cl}\n")
            for n in NAME2KW:
                kw = NAME2KW[n]
                if kw in final:
                    v = final[kw]
                    if isinstance(v, bool):
                        v = 1 if v else 0
                    fh.write(f"{n}{sep}{v}\n")
        print(f"\n저장: {out_path}")


# ---------------------------------------------------------------------------
# 상관계수 분석 (다중-인물 모드) — 각 HP ↔ score(싱글/멀티/종합) Pearson 상관 + 성능개선 시각화
# ---------------------------------------------------------------------------
def _numify(v):
    """HP 값을 수치로. 범주형 ASSIGN 은 greedy=0/hungarian=1, bool 은 0/1."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return {"greedy": 0.0, "hungarian": 1.0}.get(v, float("nan"))


def _pearson(xs, ys):
    """Pearson 상관계수. 표본<2 이거나 한쪽 분산 0이면 None."""
    pairs = [(x, y) for x, y in zip(xs, ys) if not math.isnan(x)]
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    return sxy / math.sqrt(sxx * syy)


def compute_correlations(samples, active_names):
    """samples=[(choice,bd)] → [(name, {single,multi,total: r|None})]. score 는 벌점(낮을수록 좋음)."""
    targets = (("single", "single_score"), ("multi", "multi_score"),
               ("dwell", "dwell_score"), ("total", "score"))
    rows = []
    for name in active_names:
        xs = [_numify(choice[name]) for choice, _ in samples]
        rec = {}
        for key, bkey in targets:
            ys = [bd[bkey] for _, bd in samples]
            rec[key] = _pearson(xs, ys)
        rows.append((name, rec))
    return rows


def report_correlations(rows, n_samples):
    def f(r):
        return "   n/a" if r is None else f"{r:+5.2f}"
    print(f"\n[상관계수] HP ↔ score (Pearson · 랜덤표본 {n_samples}개). "
          f"+면 값↑=score↑(나빠짐), −면 값↑=score↓(좋아짐). |값| 클수록 영향 큼")
    print(f"  {'param':16s}  {'싱글':>6} {'멀티':>6} {'체류':>6} {'종합':>6}")
    for name, rec in sorted(rows, key=lambda r: -abs(r[1]["total"] or 0.0)):
        print(f"  {name:16s}  {f(rec['single'])} {f(rec['multi'])} {f(rec['dwell'])} {f(rec['total'])}")


# ---- SVG (외부 의존성 없이 stdlib 로 벡터 그림 생성) ----
_C_BG = "#0f1620"; _C_TX = "#dce6f0"; _C_MU = "#7e93a8"; _C_GRID = "#3a4a5a"
_C_BAD = "#ff5d6c"; _C_GOOD = "#39d98a"; _C_BASE = "#6b7d90"


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _svg_corr(title, subtitle, pairs):
    """발산형 가로 막대(−1..+1). pairs=[(name, r|None)] 정렬됨."""
    W, rh, top, left, axr = 780, 26, 66, 175, 68
    axw = W - left - axr
    H = top + rh * max(1, len(pairs)) + 34

    def X(r):
        return left + axw * (0.5 + 0.5 * max(-1.0, min(1.0, r)))
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'font-family="-apple-system,sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="{_C_BG}"/>',
         f'<text x="{left}" y="24" fill="{_C_TX}" font-size="15" font-weight="bold">{_esc(title)}</text>',
         f'<text x="{left}" y="42" fill="{_C_MU}" font-size="11">{_esc(subtitle)}</text>']
    for gv in (-1.0, -0.5, 0.0, 0.5, 1.0):
        gx = X(gv)
        p.append(f'<line x1="{gx:.1f}" y1="{top-6}" x2="{gx:.1f}" y2="{H-18}" '
                 f'stroke="{_C_MU if gv==0 else _C_GRID}" stroke-width="1"/>')
        p.append(f'<text x="{gx:.1f}" y="{H-5}" fill="{_C_MU}" text-anchor="middle">{gv:+.1f}</text>')
    for i, (name, r) in enumerate(pairs):
        y = top + i * rh
        p.append(f'<text x="{left-8}" y="{y+rh*0.68:.1f}" fill="{_C_TX}" text-anchor="end">{_esc(name)}</text>')
        if r is None:
            p.append(f'<text x="{X(0)+6:.1f}" y="{y+rh*0.68:.1f}" fill="{_C_MU}">n/a</text>')
            continue
        x0, x1 = X(0), X(r)
        col = _C_BAD if r > 0 else _C_GOOD
        p.append(f'<rect x="{min(x0,x1):.1f}" y="{y+4:.1f}" width="{abs(x1-x0):.1f}" '
                 f'height="{rh-10}" fill="{col}"/>')
        anc = "start" if r >= 0 else "end"
        p.append(f'<text x="{x1+(5 if r>=0 else -5):.1f}" y="{y+rh*0.68:.1f}" '
                 f'fill="{_C_TX}" text-anchor="{anc}">{r:+.2f}</text>')
    p.append("</svg>")
    return "\n".join(p)


def _svg_improve(title, rows):
    """base→best 성능개선. rows=[(name, base, best)] (전부 낮을수록 좋은 지표). 행별 정규화 막대."""
    W, rh, top, left, valw = 780, 34, 60, 150, 130
    barw = W - left - valw
    H = top + rh * max(1, len(rows)) + 16
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'font-family="-apple-system,sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="{_C_BG}"/>',
         f'<text x="{left}" y="24" fill="{_C_TX}" font-size="15" font-weight="bold">{_esc(title)}</text>',
         f'<text x="{left}" y="42" fill="{_C_MU}" font-size="11">'
         f'회색=base(기록당시 HP) · 초록=best(최적) · 낮을수록 좋음</text>']
    for i, (name, base, best) in enumerate(rows):
        y = top + i * rh
        m = max(base, best, 1e-9)
        wb = barw * (base / m)
        wg = barw * (best / m)
        p.append(f'<text x="{left-8}" y="{y+rh*0.5:.1f}" fill="{_C_TX}" text-anchor="end">{_esc(name)}</text>')
        p.append(f'<rect x="{left}" y="{y+3:.1f}" width="{wb:.1f}" height="{rh/2-4:.1f}" fill="{_C_BASE}"/>')
        p.append(f'<rect x="{left}" y="{y+rh/2:.1f}" width="{wg:.1f}" height="{rh/2-4:.1f}" fill="{_C_GOOD}"/>')
        drop = "" if base == 0 else f"  (−{(base-best)/base*100:.0f}%)" if best <= base else f"  (+{(best-base)/base*100:.0f}%)"
        def fmt(v):
            return f"{v:.0f}" if abs(v - round(v)) < 1e-6 else f"{v:.2f}"
        p.append(f'<text x="{left+barw+6:.1f}" y="{y+rh*0.5:.1f}" fill="{_C_TX}">'
                 f'{fmt(base)} → {fmt(best)}{drop}</text>')
    p.append("</svg>")
    return "\n".join(p)


def save_analysis(rows, bd_base, best_bd, out_dir, n_samples):
    """corr.csv + corr_{single,multi,total}.svg + improvement.svg 저장."""
    os.makedirs(out_dir, exist_ok=True)

    def fc(r):
        return "" if r is None else f"{r:.4f}"
    with open(os.path.join(out_dir, "corr.csv"), "w", encoding="utf-8") as fh:
        fh.write("param,single,multi,dwell,total\n")
        for name, rec in rows:
            fh.write(f"{name},{fc(rec['single'])},{fc(rec['multi'])},"
                     f"{fc(rec['dwell'])},{fc(rec['total'])}\n")

    sub = "양(+,빨강)=값↑ 이면 score↑(나빠짐) · 음(−,초록)=값↑ 이면 score↓(좋아짐)"
    for key, title in (("single", "싱글(개인 추적) 기준"),
                       ("multi", "멀티(교차-인물 병합) 기준"),
                       ("dwell", "체류시간(연속 dwell 결손) 기준"),
                       ("total", "종합(싱글+멀티+체류) 기준")):
        pairs = sorted(((n, rec[key]) for n, rec in rows),
                       key=lambda pr: (pr[1] is None, -abs(pr[1] or 0.0)))
        with open(os.path.join(out_dir, f"corr_{key}.svg"), "w", encoding="utf-8") as fh:
            fh.write(_svg_corr(f"HP ↔ score 상관계수 · {title}", sub, pairs))

    imp = [("종합 score", bd_base["score"], best_bd["score"]),
           ("싱글 score", bd_base["single_score"], best_bd["single_score"]),
           ("멀티 score", bd_base["multi_score"], best_bd["multi_score"]),
           ("체류 score", bd_base.get("dwell_score", 0), best_bd.get("dwell_score", 0)),
           ("스위칭(마진)", bd_base["sw_in"], best_bd["sw_in"]),
           ("스위칭(엣지)", bd_base["sw_ed"], best_bd["sw_ed"]),
           ("중복 dup", bd_base["dup"], best_bd["dup"]),
           ("교차병합 ep", bd_base["xmerge_ep"], best_bd["xmerge_ep"]),
           ("교차병합 frame", bd_base["xmerge_frames"], best_bd["xmerge_frames"]),
           ("체류결손(초)", bd_base.get("dwell_deficit", 0), best_bd.get("dwell_deficit", 0))]
    with open(os.path.join(out_dir, "improvement.svg"), "w", encoding="utf-8") as fh:
        fh.write(_svg_improve("성능 개선 (base → best)", imp))
    print(f"\n분석 저장: {out_dir}/  (corr.csv, corr_single/multi/dwell/total.svg, improvement.svg)")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="ID 스위칭 최소화 HP 최적화 (기록 replay)")
    ap.add_argument("--log", default=None, help="단일-인물 JSONL 기록(단일 모드)")
    ap.add_argument("--gt-logs", nargs="+", default=None,
                    help="단일-인물 로그 N개(≥2)를 겹쳐 다중-인물 GT 로 최적화(다중 모드)")
    ap.add_argument("--collision-mm", type=float, default=700.0,
                    help="두 GT 사람이 이 거리(mm) 내면 '겹침': (R6) 센서 병합 모사로 한 점 합침 + (R2) 그 구간 개인채점 제외")
    ap.add_argument("--no-collision-merge", dest="merge_collisions", action="store_false",
                    default=True, help="[다중] 센서 병합 모사(R6) 끄기 — 겹쳐도 점 2개 유지(옛 동작)")
    ap.add_argument("--w-xmerge-ep", type=float, default=2000.0, help="교차-인물 병합 에피소드 벌점")
    ap.add_argument("--w-xmerge-fr", type=float, default=50.0, help="교차-인물 병합 프레임당 벌점")
    ap.add_argument("--gt-out", default=None, help="병합된 다중-인물 GT 를 jsonl 로 저장(선택)")
    ap.add_argument("--corr-out", default=None,
                    help="[다중] HP↔score 상관계수(csv) + 상관/성능개선 그림(svg)을 이 폴더에 저장")
    ap.add_argument("--iters", type=int, default=400, help="랜덤 탐색 횟수 (기본 400)")
    ap.add_argument("--cd-rounds", type=int, default=3, help="좌표하강 정련 라운드 (기본 3)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fix", action="append", default=[],
                    help="파라미터 고정 NAME=VAL (예: --fix MERGE_MM=100). 반복 가능")
    ap.add_argument("--optimize", default=None,
                    help="탐색할 파라미터만 지정(쉼표구분). 미지정=고정 제외 전체")
    ap.add_argument("--margin-deg", type=float, default=55.0, help="INSIDE 각도 마진(±도), 기본 55(=110°)")
    ap.add_argument("--margin-mm", type=float, default=5000.0, help="INSIDE 거리 마진(mm), 기본 5000")
    ap.add_argument("--exit-gap", type=float, default=1.5, help="이 시간(초) 이상 전 센서 미검출=진짜 이탈(스위칭 재진입 허용)")
    ap.add_argument("--dwell-gap", type=float, default=8.0,
                    help="[다중] 체류 에피소드 종료로 보는 공백(초). reid-max-gap 탐색범위보다 커야 함(그 안의 ReID 실패도 dwell 결손으로 채점)")
    ap.add_argument("--min-coverage", type=float, default=0.4, help="INSIDE 최소 추적 커버리지(미만=무효)")
    ap.add_argument("--w-switch-inside", type=float, default=1000.0)
    ap.add_argument("--w-switch-edge", type=float, default=400.0)
    ap.add_argument("--w-dup-inside", type=float, default=300.0)
    ap.add_argument("--w-dup-edge", type=float, default=120.0)
    ap.add_argument("--w-coverage", type=float, default=500.0)
    ap.add_argument("--w-ids", type=float, default=10.0)
    ap.add_argument("--w-dwell", type=float, default=20.0,
                    help="[다중] 체류시간 결손 1초당 벌점(정체성 조각으로 잃은 연속 체류시간). 0=끔")
    ap.add_argument("--w-invalid", type=float, default=1e6)
    ap.add_argument("--out", default=None, help="최적 설정을 이 경로에 sh 스니펫으로 저장")
    ap.add_argument("--list-space", action="store_true", help="탐색 공간 출력 후 종료")
    args = ap.parse_args()

    if args.list_space:
        for n, vals in SPACE.items():
            print(f"  {n:16s} {vals}")
        return 0

    if not args.log and not args.gt_logs:
        ap.error("--log(단일) 또는 --gt-logs(다중) 중 하나가 필요합니다")

    weights = dict(sw_in=args.w_switch_inside, sw_ed=args.w_switch_edge,
                   dup_in=args.w_dup_inside, dup_ed=args.w_dup_edge,
                   cov=args.w_coverage, ids=args.w_ids, invalid=args.w_invalid,
                   xmerge_ep=args.w_xmerge_ep, xmerge_fr=args.w_xmerge_fr,
                   dwell=args.w_dwell)

    # 고정 파라미터 (단일/다중 공통)
    fixed = {}
    for item in args.fix:
        if "=" not in item:
            print(f"[무시] --fix 형식은 NAME=VAL: {item}"); continue
        n, v = item.split("=", 1)
        n = n.strip().upper()
        if n not in NAME2KW:
            print(f"[무시] 알 수 없는 파라미터: {n}"); continue
        fixed[n] = cast(v.strip())
    fixed_kw = {NAME2KW[n]: v for n, v in fixed.items()}

    if args.optimize:
        want = [x.strip().upper() for x in args.optimize.split(",") if x.strip()]
        for n in want:
            if n not in SPACE:
                print(f"[무시] 탐색 불가(SPACE에 없는) 파라미터: {n}")
    else:
        want = list(SPACE.keys())
    active = {n: SPACE[n] for n in want if n in SPACE and n not in fixed}

    def make_resolve(base):
        def resolve(choice):
            p = dict(base)
            for n, v in choice.items():
                p[NAME2KW[n]] = v
            p.update(fixed_kw)
            # queue_k > queue_size 는 트래커가 min 으로 클램프 → 출력/캐시도 일치
            if isinstance(p.get("queue_k"), int) and isinstance(p.get("queue_size"), int):
                p["queue_k"] = min(p["queue_k"], p["queue_size"])
            return p
        return resolve

    # =====================================================================
    # 다중-인물 모드 (--gt-logs): 단일 로그 N개를 겹쳐 GT 구성 후 교차벌점 포함 채점
    # =====================================================================
    if args.gt_logs:
        from merge_gt import build_gt
        gt = build_gt(args.gt_logs, margin_deg=args.margin_deg, margin_mm=args.margin_mm,
                      collision_mm=args.collision_mm, merge_collisions=args.merge_collisions,
                      gt_out=args.gt_out)
        base = dict(gt["base_hp"]); base.pop("fuse_hz", None)
        resolve = make_resolve(base)
        cache = {}

        def evaluate(choice):
            p = resolve(choice)
            key = tuple(sorted((k, str(v)) for k, v in p.items()))
            if key in cache:
                return cache[key]
            conf = replay(gt["frames"], p)
            res = score_run_multi(gt, conf, exit_gap=args.exit_gap, weights=weights,
                                  min_cov=args.min_coverage, collision_mm=args.collision_mm,
                                  dwell_gap=args.dwell_gap)
            cache[key] = res
            return res

        base_score, bd_base = evaluate({})
        print(f"\n[base replay]  score={bd_base['score']}  sw_in={bd_base['sw_in']} "
              f"sw_ed={bd_base['sw_ed']} dup={bd_base['dup']} 교차병합(ep/frame)="
              f"{bd_base['xmerge_ep']}/{bd_base['xmerge_frames']} cov={bd_base['coverage']} "
              f"체류결손={bd_base['dwell_deficit']}s(score {bd_base['dwell_score']}) "
              f"invalid={bd_base['invalid']}")
        print(f"  최소 인물간 거리={bd_base['min_interperson']}mm · 겹침프레임={bd_base['collision_frames']} "
              f"(collision-mm={args.collision_mm:.0f})")
        for g, pp in enumerate(bd_base["per_person"]):
            print(f"  사람{g}({gt['labels'][g]}): sw_in={pp['sw_in']} dup={pp['dup']} "
                  f"n_ids={pp['n_ids']} reentry={pp['reentry']} cov={pp['cov']} 체류결손={pp['dwell_deficit']}s")

        if not active:
            print("\n탐색할 파라미터가 없습니다(모두 고정?)."); return 0
        if fixed:
            print("\n고정: " + "  ".join(f"{n}={v}" for n, v in fixed.items()))
        print(f"탐색 파라미터({len(active)}): {', '.join(active.keys())}")

        best_score, best_choice, best_bd, samples = run_search(
            active, base, evaluate, args.iters, args.cd_rounds, args.seed)
        final = resolve(best_choice)
        # 상관계수 분석 (HP ↔ 싱글/멀티/종합 score) + 성능개선 시각화
        corr_rows = compute_correlations(samples, list(active.keys()))
        report_correlations(corr_rows, len(samples))
        if args.corr_out:
            save_analysis(corr_rows, bd_base, best_bd, args.corr_out, len(samples))
        print("\n" + "=" * 70)
        print(f"최적 결과: score={best_score:.1f}")
        print(f"  {best_bd}")
        print(f"  개선(base→best): 교차병합 ep {bd_base['xmerge_ep']}→{best_bd['xmerge_ep']} "
              f"(frame {bd_base['xmerge_frames']}→{best_bd['xmerge_frames']}), "
              f"sw_in {bd_base['sw_in']}→{best_bd['sw_in']}, dup {bd_base['dup']}→{best_bd['dup']}, "
              f"체류결손 {bd_base['dwell_deficit']}s→{best_bd['dwell_deficit']}s")
        for g, pp in enumerate(best_bd["per_person"]):
            print(f"  사람{g}: sw_in={pp['sw_in']} dup={pp['dup']} n_ids={pp['n_ids']} "
                  f"cov={pp['cov']} 체류결손={pp['dwell_deficit']}s")
        print("=" * 70)
        save_params(final, active, fixed, args.out,
                    [f"optimize_fusion.py 다중-인물 결과 (score={best_score:.1f})",
                     f"GT: {' + '.join(gt['labels'])} ({gt['n']}인, {gt['length']}프레임)",
                     f"교차병합 ep {bd_base['xmerge_ep']}→{best_bd['xmerge_ep']}, "
                     f"sw_in {bd_base['sw_in']}→{best_bd['sw_in']}"])
        return 0

    # =====================================================================
    # 단일-인물 모드 (--log): 기존 동작 (회귀 없음)
    # =====================================================================
    header, frames = load_log(args.log)
    if not frames:
        print("기록에 프레임이 없습니다."); return 1
    base = dict((header or {}).get("hyperparams_after_record") or {})
    base.pop("fuse_hz", None)
    resolve = make_resolve(base)
    cls_per_frame = [presence_class(f, args.margin_deg, args.margin_mm) for f in frames]
    cache = {}

    def evaluate(choice):
        p = resolve(choice)
        key = tuple(sorted((k, str(v)) for k, v in p.items()))
        if key in cache:
            return cache[key]
        conf = replay(frames, p)
        res = score_run(frames, conf, cls_per_frame,
                        exit_gap=args.exit_gap, weights=weights, min_cov=args.min_coverage)
        cache[key] = res
        return res

    print(f"기록: {args.log}  |  프레임 {len(frames)}  |  센서 {len((header or {}).get('sensors',[]))}")
    inside_n = cls_per_frame.count("INSIDE"); edge_n = cls_per_frame.count("EDGE")
    out_n = cls_per_frame.count("OUT")
    print(f"프레임 상태: INSIDE {inside_n} · EDGE {edge_n} · OUT {out_n}  "
          f"(마진 ±{args.margin_deg:.0f}°/{args.margin_mm:.0f}mm, exit-gap {args.exit_gap}s)")

    conf_live = [[{"id": t["id"], "x": t["x"], "y": t["y"]}
                  for t in f["tracks"] if t.get("confirmed")] for f in frames]
    _, bd_live = score_run(frames, conf_live, cls_per_frame,
                           exit_gap=args.exit_gap, weights=weights, min_cov=args.min_coverage)
    print(f"\n[라이브 기록]  {bd_live}")
    base_score, bd_base = evaluate({})
    print(f"[base replay]  {bd_base}   (라이브와 유사하면 replay 신뢰 가능)")

    if not active:
        print("\n탐색할 파라미터가 없습니다(모두 고정?). --optimize 로 지정하세요.")
        return 0
    if fixed:
        print(f"\n고정: " + "  ".join(f"{n}={v}" for n, v in fixed.items()))
    print(f"탐색 파라미터({len(active)}): {', '.join(active.keys())}")

    best_score, best_choice, best_bd, _ = run_search(
        active, base, evaluate, args.iters, args.cd_rounds, args.seed)
    final = resolve(best_choice)
    print("\n" + "=" * 70)
    print(f"최적 결과: score={best_score:.1f}")
    print(f"  {best_bd}")
    print(f"  개선: 라이브 sw_in {bd_live['sw_in']}→{best_bd['sw_in']}, "
          f"n_ids {bd_live['n_ids']}→{best_bd['n_ids']}, dup_in {bd_live['dup_in']}→{best_bd['dup_in']}")
    print("=" * 70)
    save_params(final, active, fixed, args.out,
                [f"optimize_fusion.py 결과 (score={best_score:.1f})",
                 f"라이브 sw_in={bd_live['sw_in']} n_ids={bd_live['n_ids']} → "
                 f"최적 sw_in={best_bd['sw_in']} n_ids={best_bd['n_ids']}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
