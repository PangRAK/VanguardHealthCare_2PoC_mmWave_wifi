#!/usr/bin/env python3
"""
다중 센서 자동 포지셔닝 (외부 캘리브레이션) — 위치 + 설치자세(Yaw·Pitch·Roll)
================================================================================

여러 EPL(mmWave) 센서가 각자 로컬 좌표로 보고하는 "한 사람"의 궤적을 이용해,
각 센서가 방 안에서 어떻게 설치돼 있는지(위치 x,y + 설치자세 3각)를 추정하고
epl_config.json 에 기록한다.

추정하는 것 (센서가 "달려있는 방향"의 각도 — 탐지영역 FOV 와 무관):
    · (x, y)                위치 (mm)
    · Yaw  (heading_deg)    좌우방향 설치각도 — 방 +Y축에서 반시계로 돈 각(°)
    · Pitch(pitch_deg)      상하방향 설치각도 — 정면을 아래로 숙인 각(°, ≥0)
    · Roll (roll_deg)       기울어짐 각도 — 정면축(보어사이트) 기준 갸우뚱(°)
    · flip                  좌우반전(거울 장착)

※ FOV(수평 120°)·최대거리(6m)는 하드웨어 고정 스펙이라 여기서 건드리지 않는다.

물리 모델
---------
2D 레이더는 고도(elevation)를 못 재고, 사람은 (거의) 한 수평면 위를 걷는다.
센서 설치자세 R=(yaw,pitch,roll) 하에서 바닥점 room=(X,Y) 의 보고 로컬좌표는

    local = A·(room − pos),   A = U · Rot(−yaw),
    U = [[ s·cosR ,  s·sinR·sinP ],
         [   0    ,     cosP     ]]   (s=±1 반전, P=pitch, R=roll)

즉 **전방(Y)은 cosP 로 압축**(아래로 숙일수록), **좌우(X)는 cosR 로 압축 + shear**.
한 사람 궤적을 두 센서가 함께 보면 센서쌍 간 완전한 2D affine 이 나오고,
각 센서 affine 을 RQ 분해하면 (yaw,pitch,roll,flip) 이 분리된다.

관측 가능성 (정직하게)
----------------------
- x, y, Yaw : 기준센서 상대로 확실히 복원.
- Pitch     : 센서쌍의 전방압축비 → **가장 평평한 센서를 0°로 한 상대값**으로 복원(양호).
- Roll      : shear 항 sinR·sinP 에서 나옴 → **pitch 가 클수록 관측 가능**, 작은 roll 은
              cosR≈1−R²/2 (2차항)이라 노이즈에 묻힘 → 신뢰도 라벨(ok/low/n/a)을 붙인다.
- 절대각(방 기준)은 외부 기준이 필요 → 기본은 '기준센서 상대'로 보고한다.

파이프라인: 수집(실측시각) → 시간정렬 보간 → 센서쌍 affine(RANSAC) → 최대성분/기준선택
           → 전역 affine 번들조정(선형최소제곱) → RQ 분해 → 저장.

사용법
    python auto_positioning.py
    python auto_positioning.py --seconds 90
    python auto_positioning.py --ref 98bd80         # 기준센서 지정(수평 설치로 아는 센서)
    python auto_positioning.py --dry-run
    python auto_positioning.py --selftest
"""
from __future__ import annotations

import argparse
import collections
import heapq
import math
import random
import shutil
import sys
import time
from itertools import combinations

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print("❌ numpy 가 필요합니다.  ./run_auto_positioning.sh 사용")
    sys.exit(1)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# ============================================================================
# 기하 헬퍼 — 설치자세 ↔ affine
# ============================================================================
def _rot(th):
    c, s = math.cos(th), math.sin(th)
    return np.array([[c, -s], [s, c]])


def pose_to_Aloc(yaw, pitch, roll, flip):
    """설치자세 → room→local 2×2 A.  local = A·(room−pos).
    A = U·Rot(−yaw),  U=[[s·cosR, s·sinR·sinP],[0, cosP]]  (각도 rad)."""
    s = -1.0 if flip else 1.0
    cP = math.cos(pitch)
    cR, sR = math.cos(roll), math.sin(roll)
    U = np.array([[s * cR, s * sR * math.sin(pitch)], [0.0, cP]])
    return U @ _rot(-yaw)


def decompose_Aloc(A):
    """room→local 2×2 A → (yaw, pitch, roll, flip)  [rad, rad, rad, bool].
    A = U·Rot(−yaw) 의 RQ 분해. 전방행 크기=cosP, 좌우행에서 roll/flip.
    (pitch 정규화(가장 평평한 센서=0°)는 호출측에서 cosP 를 나눠 처리)."""
    c, d = float(A[1, 0]), float(A[1, 1])       # 전방(y_local) 행 = cosP·(−sinYaw, cosYaw)
    cP = math.hypot(c, d)
    if cP < 1e-9:
        return 0.0, math.pi / 2, 0.0, False     # 거의 수직(관측 밖)
    yaw = math.atan2(-c, d)
    U = A @ _rot(yaw)                            # = 상삼각 U
    u00, u01 = float(U[0, 0]), float(U[0, 1])
    flip = u00 < 0
    s = -1.0 if flip else 1.0
    cR = abs(u00)                                # cosR (≥0)
    pitch = math.acos(_clamp(cP, 0.0, 1.0))
    sP = math.sin(pitch)
    if sP > 5e-2:                                # pitch 충분 → shear 로 signed roll
        sinR = _clamp((s * u01) / sP, -1.0, 1.0)
        roll = math.atan2(sinR, min(cR, 1.0))
    else:                                        # pitch≈0 → roll 관측 불가
        roll = 0.0
    return yaw, pitch, roll, flip


def _cosP_of(A):
    return math.hypot(float(A[1, 0]), float(A[1, 1]))


def affine_fit(src, tgt):
    """tgt ≈ A·src + b. src,tgt (n,2) → A(2,2), b(2,) (최소제곱)."""
    src = np.asarray(src, float); tgt = np.asarray(tgt, float)
    D = np.hstack([src, np.ones((len(src), 1))])
    px, *_ = np.linalg.lstsq(D, tgt[:, 0], rcond=None)
    py, *_ = np.linalg.lstsq(D, tgt[:, 1], rcond=None)
    A = np.array([[px[0], px[1]], [py[0], py[1]]])
    b = np.array([px[2], py[2]])
    return A, b


def affine_apply(A, b, pts):
    pts = np.asarray(pts, float)
    return (A @ pts.T).T + b


def _tri_area(a, b, c):
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0


def _ang_spread_deg(headings):
    """방향(orientation, mod 180°) 집합의 최대 쌍간 각차(°). 음수/wrap 안전."""
    m = 0.0
    for i in range(len(headings)):
        for j in range(i + 1, len(headings)):
            dd = abs(headings[i] - headings[j]) % 180.0
            m = max(m, min(dd, 180.0 - dd))
    return m


def _minor_spread(pts):
    pts = np.asarray(pts, float)
    if len(pts) < 3:
        return 0.0
    w = np.linalg.eigvalsh(np.cov(pts.T))
    return float(math.sqrt(max(w[0], 0.0)))


def ransac_affine(src, tgt, inlier_mm=300.0, iters=600, min_area=8e4, seed=12345):
    """RANSAC 으로 이상치 배제한 affine 추정. (A, b, inlier_idx, rms, n_inl) 또는 None."""
    src = np.asarray(src, float); tgt = np.asarray(tgt, float)
    n = len(src)
    if n < 3:
        return None
    rng = random.Random(seed)
    best = None
    for _ in range(iters):
        i, j, k = rng.sample(range(n), 3)
        if _tri_area(src[i], src[j], src[k]) < min_area:
            continue
        A, b = affine_fit(src[[i, j, k]], tgt[[i, j, k]])
        inl = np.where(np.linalg.norm(affine_apply(A, b, src) - tgt, axis=1) < inlier_mm)[0]
        if best is None or len(inl) > len(best):
            best = inl
    if best is None or len(best) < 3:
        return None
    inl = best
    for _ in range(2):
        A, b = affine_fit(src[inl], tgt[inl])
        new = np.where(np.linalg.norm(affine_apply(A, b, src) - tgt, axis=1) < inlier_mm)[0]
        if len(new) < 3 or set(new.tolist()) == set(inl.tolist()):
            if len(new) >= 3:
                inl = new
            break
        inl = new
    A, b = affine_fit(src[inl], tgt[inl])
    rms = float(math.sqrt((np.linalg.norm(affine_apply(A, b, src[inl]) - tgt[inl], axis=1) ** 2).mean()))
    return A, b, inl, rms, int(len(inl))


# ============================================================================
# 센서쌍 affine + 진단
# ============================================================================
def build_edges(frames, ids, min_overlap=20, inlier_mm=300.0, min_area=8e4):
    """edges[(a,b)] = b-local→a-local affine(A,b). diag = 모든 쌍 진단."""
    edges, diag = {}, {}
    for a, b in combinations(ids, 2):
        src, tgt = [], []
        for f in frames:
            pa = f["positions"].get(a)
            pb = f["positions"].get(b)
            if pa is not None and pb is not None:
                tgt.append(pa); src.append(pb)     # A·src + b ≈ tgt  (b-local→a-local)
        raw = len(src)
        d = {"raw": raw, "accepted": False, "reason": "", "best_inliers": 0,
             "rms": None, "minor": None}
        if raw < min_overlap:
            d["reason"] = f"동시관측 부족(raw {raw} < {min_overlap})"; diag[(a, b)] = d; continue
        res = ransac_affine(np.array(src), np.array(tgt), inlier_mm=inlier_mm, min_area=min_area)
        if res is None:
            d["reason"] = "일관 변환 없음(움직임 부족/서로 다른 대상)"; diag[(a, b)] = d; continue
        A, bt, inl, rms, best = res
        d["best_inliers"] = best; d["rms"] = round(rms, 1)
        if best < min_overlap:
            d["reason"] = f"인라이어 부족({best} < {min_overlap}) — 겹침구역서 더 크게 이동"
            diag[(a, b)] = d; continue
        minor = _minor_spread(np.array(src)[inl])
        d.update(accepted=True, minor=round(minor, 1), reason="OK"); diag[(a, b)] = d
        edges[(a, b)] = {"A": A, "b": bt, "inliers": best, "rms": rms, "n": raw, "minor": minor}
    return edges, diag


def _connected_components(edges, ids):
    adj = collections.defaultdict(set)
    for (a, b) in edges:
        adj[a].add(b); adj[b].add(a)
    seen, comps = set(), []
    for node in ids:
        if node in adj and node not in seen:
            comp, stack = set(), [node]
            while stack:
                u = stack.pop()
                if u in comp:
                    continue
                comp.add(u); seen.add(u)
                stack.extend(adj[u] - comp)
            comps.append(comp)
    return comps


def select_component_and_ref(edges, ids, ref_id=None):
    """가장 큰 성분과 기준센서를 고른다. 반환 (anchored:set, ref, comps)."""
    comps = _connected_components(edges, ids)
    if not comps:
        return set(), None, []

    def cw(c):
        return sum(e["inliers"] for (a, b), e in edges.items() if a in c and b in c)

    main = max(comps, key=lambda c: (len(c), cw(c)))
    inc = collections.defaultdict(int)
    for (a, b), e in edges.items():
        if a in main and b in main:
            inc[a] += e["inliers"]; inc[b] += e["inliers"]
    if ref_id in main:
        ref = ref_id
    else:
        ref = max(main, key=lambda k: (inc[k], -ids.index(k)))
    return set(main), ref, comps


def _corrs(edges, frames, anchored, inlier_mm=None):
    """anchored 쌍의 동시관측 (i, pi_local, j, pj_local) 목록. inlier_mm 주면 쌍 affine 로 필터."""
    out = []
    for (a, b), e in edges.items():
        if a not in anchored or b not in anchored:
            continue
        A, bt = e["A"], e["b"]                      # b-local→a-local
        for f in frames:
            pa = f["positions"].get(a); pb = f["positions"].get(b)
            if pa is None or pb is None:
                continue
            pa = np.array(pa, float); pb = np.array(pb, float)
            if inlier_mm is not None and np.linalg.norm(A @ pb + bt - pa) >= inlier_mm:
                continue
            out.append((a, pa, b, pb))
    return out


# ============================================================================
# 전역 affine 번들조정 (선형최소제곱) — 각 센서 local→room 변환 B, 위치 d
# ============================================================================
def refine_affine_ba(edges, frames, anchored, ref, inlier_mm=300.0,
                     huber_mm=200.0, rounds=3):
    """B_s·local_s + d_s = B_t·local_t + d_t 를 만족하는 {B_s(local→room), d_s(=위치)} 를
    선형최소제곱으로 푼다. 기준센서: B=I, d=0 (게이지). 볼록 → 단일 해, 지역최소 없음.
    반환 {sid: {'B':2×2, 'd':(2,)}}  (room = B·local + d)."""
    nonref = [s for s in anchored if s != ref]
    idx = {s: k for k, s in enumerate(nonref)}
    NP = 6 * len(nonref)
    cons = _corrs(edges, frames, anchored, inlier_mm=inlier_mm)
    out = {ref: {"B": np.eye(2), "d": np.zeros(2)}}
    if NP == 0 or not cons:
        return out

    def build(sqrtw):
        rows, rhs = [], []
        for n, (i, pi, j, pj) in enumerate(cons):
            w = 1.0 if sqrtw is None else sqrtw[n]
            for axis in (0, 1):
                row = np.zeros(NP); r = 0.0
                for name, p, sign in ((i, pi, 1.0), (j, pj, -1.0)):
                    if name == ref:
                        r -= sign * p[axis]          # I·p (상수) → 우변
                    else:
                        base = 6 * idx[name]
                        if axis == 0:
                            row[base + 0] += sign * p[0]; row[base + 1] += sign * p[1]; row[base + 4] += sign
                        else:
                            row[base + 2] += sign * p[0]; row[base + 3] += sign * p[1]; row[base + 5] += sign
                rows.append(row * w); rhs.append(r * w)
        return np.array(rows), np.array(rhs)

    M, rhs = build(None)
    theta, *_ = np.linalg.lstsq(M, rhs, rcond=None)
    for _ in range(max(0, rounds - 1)):             # IRLS (Huber) 로 이상치 완화
        w = np.ones(len(cons))
        for n, (i, pi, j, pj) in enumerate(cons):
            def val(name, p):
                if name == ref:
                    return p
                base = 6 * idx[name]
                return theta[base:base + 4].reshape(2, 2) @ p + theta[base + 4:base + 6]
            r = float(np.linalg.norm(val(i, pi) - val(j, pj)))
            w[n] = 1.0 if r <= huber_mm else huber_mm / max(r, 1e-6)
        M, rhs = build(np.sqrt(w))
        theta, *_ = np.linalg.lstsq(M, rhs, rcond=None)

    for s in nonref:
        base = 6 * idx[s]
        out[s] = {"B": theta[base:base + 4].reshape(2, 2), "d": theta[base + 4:base + 6].copy()}
    return out


def _ba_rms(sol, edges, frames, anchored, ref, inlier_mm=300.0):
    cons = _corrs(edges, frames, anchored, inlier_mm=inlier_mm)
    if not cons:
        return 0.0

    def val(name, p):
        if name == ref:
            return p
        return sol[name]["B"] @ p + sol[name]["d"]
    tot = sum(float(((val(i, pi) - val(j, pj)) ** 2).sum()) for i, pi, j, pj in cons)
    return math.sqrt(tot / len(cons))


# ============================================================================
# 파이프라인
# ============================================================================
def estimate_positions(frames, ids, *, min_overlap=20, inlier_mm=300.0, min_area=8e4,
                       ref_id=None, min_spread_mm=400.0, refine=True):
    edges, diag = build_edges(frames, ids, min_overlap=min_overlap,
                              inlier_mm=inlier_mm, min_area=min_area)
    anchored, ref, comps = select_component_and_ref(edges, ids, ref_id=ref_id)
    warnings = []
    if ref_id is not None and ref_id not in anchored and any(ref_id == s for s in ids):
        warnings.append(f"--ref '{ref_id}' 는 기준그룹에 없어 무시됨(자동 선택). 다른 센서와 겹치게 재측정.")

    if not anchored or ref is None:
        placements = {sid: {"anchored": False, "reason": "겹치는 센서 관측 없음"} for sid in ids}
        pairs = _pairs_report(diag, min_overlap)
        return {"placements": placements, "ref": None, "pairs": pairs,
                "components": [sorted(c) for c in comps], "warnings": warnings,
                "global_rms": 0.0}

    sol = refine_affine_ba(edges, frames, anchored, ref, inlier_mm=inlier_mm) if refine \
        else {s: {"B": np.eye(2), "d": np.zeros(2)} for s in anchored}
    g_rms = _ba_rms(sol, edges, frames, anchored, ref, inlier_mm=inlier_mm)

    # 각 센서: A(room→local) = B⁻¹ → RQ 분해. pitch 는 '가장 평평한 센서=0°' 로 정규화.
    Aloc, cosP = {}, {}
    for sid in anchored:
        B = sol[sid]["B"]
        det = float(np.linalg.det(B))
        if abs(det) < 1e-9:
            B = B + np.eye(2) * 1e-6
        A = np.linalg.inv(B)
        Aloc[sid] = A
        cosP[sid] = _cosP_of(A)
    # pitch 기준 = 기준센서(프레임 원점, cosP≈1). '기준센서가 수평이면 절대 pitch'.
    # (max(cosP) 로 정규화하면 노이즈가 최댓값을 위로 편향시켜 수평센서에도 가짜 pitch 가 붙음)
    cPref = cosP.get(ref, 1.0)
    if cPref < 1e-6:
        cPref = 1.0

    placements = {}
    for sid in ids:
        if sid not in anchored:
            rs = _reason(sid, diag, comps, min_overlap)
            placements[sid] = {"anchored": False, "reason": rs}
            warnings.append(f"센서 {sid}: {rs}")
            continue
        A = Aloc[sid]
        yaw, pitch_raw, roll, flip = decompose_Aloc(A)
        pitch = math.acos(_clamp(cosP[sid] / cPref, 0.0, 1.0))   # 기준센서 기준 상대 pitch
        # roll 재계산 (정규화된 pitch 사용)
        U = A @ _rot(yaw)
        u00, u01 = float(U[0, 0]), float(U[0, 1])
        s = -1.0 if u00 < 0 else 1.0
        cR = min(abs(u00), 1.0)
        sP = math.sin(pitch)
        if sP > 5e-2:
            roll = math.atan2(_clamp((s * u01) / sP, -1.0, 1.0), cR)
            roll_conf = "ok" if pitch >= math.radians(15) else "low"
        else:
            roll = 0.0
            roll_conf = "n/a"                                    # pitch≈0 → roll 관측불가
        pos = sol[sid]["d"]
        r = [e["rms"] for (a, b), e in edges.items() if sid in (a, b)]
        placements[sid] = {
            "anchored": True, "is_ref": (sid == ref),
            "x": float(pos[0]), "y": float(pos[1]),
            "heading_deg": math.degrees(yaw),      # Yaw (좌우방향 설치각도)
            "pitch_deg": math.degrees(pitch),      # Pitch (상하방향 설치각도)
            "roll_deg": math.degrees(roll),        # Roll (기울어짐 각도)
            "roll_conf": roll_conf, "flip": bool(flip),
            "rms": (min(r) if r else None),
        }

    # 경고: 궤적이 일직선이면 자세 불안정
    for (a, b), e in edges.items():
        if e["minor"] < min_spread_mm:
            warnings.append(f"센서쌍 {a}·{b}: 궤적이 거의 일직선(minor={e['minor']:.0f}mm) → 자세 추정 불안정")
    if any(p.get("anchored") and p.get("roll_conf") in ("low", "n/a") for p in placements.values()):
        warnings.append("일부 센서의 Roll 은 신뢰도 낮음(pitch 가 작을수록 roll 관측이 어려움). "
                        "실제 설치가 갸우뚱하지 않다면 0°로 봐도 무방.")

    return {"placements": placements, "ref": ref,
            "pairs": _pairs_report(diag, min_overlap),
            "components": [sorted(c) for c in comps], "warnings": warnings,
            "global_rms": g_rms}


def _pairs_report(diag, min_overlap):
    return [{"pair": [a, b], "raw": d["raw"], "best_inliers": d["best_inliers"],
             "rms": d["rms"], "minor": d["minor"], "accepted": d["accepted"], "reason": d["reason"]}
            for (a, b), d in sorted(diag.items(), key=lambda kv: (-kv[1]["accepted"], -kv[1]["raw"]))]


def _reason(sid, diag, comps, min_overlap):
    if any(sid in c for c in comps):
        return "다른 센서와 겹치지만 기준 그룹과 미연결 별도 그룹 → 기준 그룹과도 겹치게 재측정"
    raws = [d for (a, b), d in diag.items() if sid in (a, b)]
    mx = max((d["raw"] for d in raws), default=0)
    if any(d["raw"] >= min_overlap for d in raws):
        return "겹침 표본 있으나 정합/인라이어 부족 → 겹침구역서 더 크게(곡선) 이동해 재측정"
    if mx > 0:
        return f"겹침 표본 적음(최대 {mx} < {min_overlap}) → 겹침구역에 더 머물며 재측정"
    return "어떤 센서와도 겹침 없음(고립) → 기존 설정 유지"


# ============================================================================
# 데이터 수집 + 시간정렬
# ============================================================================
def collect_series(hub, ids, duration, hz=15.0, max_age_ms=500, progress=True):
    series = {sid: [] for sid in ids}
    last = {sid: None for sid in ids}
    dt = 1.0 / hz
    t_end = time.monotonic() + duration
    last_print = 0.0
    while True:
        now = time.monotonic()
        if now >= t_end:
            break
        snap = hub.snapshot(); ts = snap["ts"]; live = 0
        for s in snap["sensors"]:
            sid = s["id"]
            if sid not in ids:
                continue
            cands = [t for t in s["targets"]
                     if t["present"] and (t["age_ms"] is None or t["age_ms"] < max_age_ms)]
            if not cands:
                last[sid] = None; continue
            lp = last[sid]
            cand = (min(cands, key=lambda t: (t["x"] - lp[0]) ** 2 + (t["y"] - lp[1]) ** 2)
                    if lp is not None else cands[0])
            true_t = ts - (cand["age_ms"] or 0) / 1000.0
            series[sid].append((true_t, cand["x"], cand["y"]))
            last[sid] = (cand["x"], cand["y"]); live += 1
        if progress and now - last_print >= 1.0:
            last_print = now
            cnt = "  ".join(f"{sid}:{len(series[sid])}" for sid in ids)
            sys.stdout.write(f"\r  ⏱  남은시간 {int(t_end-now):2d}s | 표본 {cnt} | 지금보임 {live}개   ")
            sys.stdout.flush()
        time.sleep(dt)
    if progress:
        sys.stdout.write("\n")
    return series


def _interp_at(ts_arr, xs, ys, tg, max_gap):
    n = len(ts_arr)
    if n == 0:
        return None
    if tg <= ts_arr[0]:
        return (xs[0], ys[0]) if ts_arr[0] - tg <= max_gap else None
    if tg >= ts_arr[-1]:
        return (xs[-1], ys[-1]) if tg - ts_arr[-1] <= max_gap else None
    j = int(np.searchsorted(ts_arr, tg))
    t0, t1 = ts_arr[j - 1], ts_arr[j]
    if t1 - t0 > max_gap:
        return None
    w = (tg - t0) / (t1 - t0) if t1 > t0 else 0.0
    return (xs[j - 1] + w * (xs[j] - xs[j - 1]), ys[j - 1] + w * (ys[j] - ys[j - 1]))


def resample_to_frames(series, ids, hz=15.0, max_gap_ms=700.0):
    have = [(sid, sorted(series[sid])) for sid in ids if len(series[sid]) >= 2]
    if not have:
        return []
    t_min = min(s[0][0] for _s, s in have)
    t_max = max(s[-1][0] for _s, s in have)
    if t_max <= t_min:
        return []
    max_gap = max_gap_ms / 1000.0
    prep = {sid: (np.array([p[0] for p in s]), np.array([p[1] for p in s]),
                  np.array([p[2] for p in s])) for sid, s in have}
    frames, tg, step = [], t_min, 1.0 / hz
    while tg <= t_max + 1e-9:
        pos = {}
        for sid, (ta, xa, ya) in prep.items():
            p = _interp_at(ta, xa, ya, tg, max_gap)
            if p is not None:
                pos[sid] = p
        frames.append({"ts": tg, "positions": pos}); tg += step
    return frames


# ============================================================================
# 저장 / 리포트
# ============================================================================
def apply_to_config(result, sensors_meta, *, path=None, dry_run=False):
    from epl_config import load_config, save_config, upsert_sensor, CONFIG_PATH
    path = path or CONFIG_PATH
    cfg = load_config()
    by_id = {s.get("id"): s for s in (cfg.get("sensors") or [])}
    updated, added, skipped = [], [], []
    for sid, p in result["placements"].items():
        if not p.get("anchored"):
            skipped.append(sid); continue
        s = by_id.get(sid)
        if s is None:
            meta = sensors_meta.get(sid, {})
            s = upsert_sensor(cfg, {"id": sid, "node_name": meta.get("node_name", ""),
                                    "host": meta.get("host", ""), "name": meta.get("name", ""),
                                    "color": meta.get("color", "")})
            by_id[sid] = s; added.append(sid)
        else:
            updated.append(sid)
        s["x"] = round(p["x"], 1); s["y"] = round(p["y"], 1)
        s["heading_deg"] = round(p["heading_deg"], 2)     # Yaw
        s["pitch_deg"] = round(p["pitch_deg"], 2)         # Pitch
        s["roll_deg"] = round(p["roll_deg"], 2)           # Roll
        s["flip"] = bool(p["flip"])
        s.pop("fwd_scale", None)                          # 구 필드 제거
    info = {"updated": updated, "added": added, "skipped": skipped, "path": path, "written": False}
    if dry_run:
        return info
    try:
        shutil.copyfile(path, path + ".bak")
    except OSError:
        pass
    save_config(cfg, path); info["written"] = True
    return info


_CONF_LABEL = {"ok": "", "low": " (신뢰낮음)", "n/a": " (관측불가)"}


def print_report(result, names=None):
    names = names or {}
    nm = lambda k: names.get(k, k)
    print("\n" + "=" * 66)
    print("  자동 포지셔닝 결과  (위치 + 설치자세 Yaw·Pitch·Roll)")
    print("=" * 66)
    print("  [센서쌍 진단]")
    for e in result["pairs"] or []:
        a, b = e["pair"]; mark = "✅" if e["accepted"] else "❌"
        extra = (f"정합 {e['best_inliers']}/{e['raw']}  오차 {e['rms']}mm  분포 {e['minor']}mm"
                 if e["accepted"] else f"동시관측 {e['raw']}  ({e['reason']})")
        print(f"    {mark} {nm(a)} ↔ {nm(b)} : {extra}")
    if not result["pairs"]:
        print("    (센서쌍 없음)")
    ref = result["ref"]
    if ref is None:
        print("\n  ❌ 겹치는 센서 관측으로 배치를 계산하지 못했습니다.")
        print("     → 두 센서가 함께 보는 구역에서 한 사람이 '천천히 곡선으로' 충분히 움직이도록 재측정.")
        return
    print(f"\n  기준(원점) 센서: {nm(ref)}   전역 정합오차(RMS): {result['global_rms']:.0f} mm")
    print("  · 위치·Yaw 는 기준센서 상대,  Pitch 는 가장 평평한 센서를 0°로 한 상대값,  Roll 은 기준센서 상대.")
    print("\n  [추정된 설치자세]")
    for sid, p in result["placements"].items():
        if p.get("anchored"):
            tag = " (기준)" if p.get("is_ref") else ""
            rms = f"  rms {p['rms']:.0f}mm" if p.get("rms") is not None else ""
            rc = _CONF_LABEL.get(p.get("roll_conf", "ok"), "")
            fwd_sc = 1.0 / max(math.cos(math.radians(p["pitch_deg"])), 1e-3)   # 앞뒤 스케일
            lat_sc = 1.0 / max(math.cos(math.radians(p["roll_deg"])), 1e-3)    # 좌우 스케일
            print(f"    ✅ {nm(sid)}{tag}: 위치=({p['x']:.0f},{p['y']:.0f})mm  "
                  f"Yaw(좌우)={p['heading_deg']:.1f}°  Pitch(상하)={p['pitch_deg']:.1f}°  "
                  f"Roll(기울)={p['roll_deg']:.1f}°{rc}  반전={'예' if p['flip'] else '아니오'}{rms}")
            print(f"         └ 유도 스케일: 앞뒤 ×{fwd_sc:.3f} (=1/cosPitch)  "
                  f"좌우 ×{lat_sc:.3f} (=1/cosRoll)")
        else:
            print(f"    ⚠️  {nm(sid)}: 미고정 — {p.get('reason','')}")


# ============================================================================
# 합성 자체검증 (ground-truth Yaw·Pitch·Roll 복원)
# ============================================================================
def _synth_local(gt, room, rng_mm=6000.0, fov_half=60.0):
    """설치자세 gt 인 센서가 바닥점 room 을 보고하는 로컬좌표. FOV/거리 밖이면 None.
    (FOV 수평 120°=±60°, range 6m 는 하드웨어 고정 스펙 — 추정 대상 아님)."""
    yaw = math.radians(gt["yaw"]); pitch = math.radians(gt.get("pitch", 0.0))
    roll = math.radians(gt.get("roll", 0.0))
    A = pose_to_Aloc(yaw, pitch, roll, gt.get("flip", False))
    d = np.array(room, float) - np.array([gt["x"], gt["y"]], float)
    dist = math.hypot(d[0], d[1])
    if dist < 300.0 or dist > rng_mm:
        return None
    fwd = np.array([-math.sin(yaw), math.cos(yaw)])       # 보어사이트 수평방향
    bearing = math.degrees(math.atan2(d[0] * fwd[1] - d[1] * fwd[0], float(d @ fwd)))
    if abs(bearing) > fov_half:
        return None
    return tuple(A @ d)


def selftest():
    print("=== 자동 포지셔닝 자체검증 (위치 + Yaw·Pitch·Roll) ===")
    rng = random.Random(7)
    ok_all = True

    # 0) affine ↔ 자세 왕복 정합
    print("  [0] pose→A→pose 왕복")
    rt_ok = True
    for (y, p, r, f) in [(0, 0, 0, False), (30, 20, 10, False), (-40, 35, -15, True),
                         (170, 25, 8, False), (60, 5, 0, True)]:
        A = pose_to_Aloc(math.radians(y), math.radians(p), math.radians(r), f)
        ry, rp, rr, rf = decompose_Aloc(A)
        dy = abs(((math.degrees(ry) - y) + 180) % 360 - 180)
        dp = abs(math.degrees(rp) - p)
        dr = abs(math.degrees(rr) - r) if p >= 5 else 0.0
        if dy > 1e-4 or dp > 1e-4 or dr > 1e-4 or rf != f:
            rt_ok = False; print(f"      FAIL ({y},{p},{r},{f}) → dy={dy:.4f} dp={dp:.4f} dr={dr:.4f} flip={rf}")
    print("      " + ("PASS" if rt_ok else "FAIL")); ok_all &= rt_ok

    def path_room(frac):
        return (2500 + 1800 * math.sin(2 * math.pi * frac * 1.5),
                2800 + 1500 * math.sin(2 * math.pi * frac * 0.9 + 0.7))

    def frames_of(gts, n=520, noise=25.0, occl=None, stagger=False, dt=0.06):
        series = {sid: [] for sid in gts}
        for kk in range(n):
            room = path_room(kk / n); t = kk * dt
            for sid, gt in gts.items():
                if occl and occl(sid, room):
                    continue
                loc = _synth_local(gt, room)
                if loc is None:
                    continue
                off = (list(gts).index(sid) * 0.031) if stagger else 0.0
                series[sid].append((t + off, loc[0] + rng.gauss(0, noise), loc[1] + rng.gauss(0, noise)))
        return resample_to_frames(series, list(gts), hz=15.0, max_gap_ms=300.0)

    def evaluate(name, gts, res, *, unanchored=(), check_roll=True):
        nonlocal ok_all
        fails = []
        if res["ref"] is None:
            fails.append("ref=None")
        for sid, gt in gts.items():
            p = res["placements"].get(sid, {})
            if sid in unanchored:
                if p.get("anchored"):
                    fails.append(f"{sid} 앵커됨(고립이어야)")
                continue
            if not p.get("anchored"):
                fails.append(f"{sid} 미앵커"); continue
            if bool(p["flip"]) != bool(gt.get("flip", False)):
                fails.append(f"{sid} flip")
            dpos = math.hypot(p["x"] - gt["x"], p["y"] - gt["y"])
            if dpos >= 400:
                fails.append(f"{sid} pos {dpos:.0f}mm")
            dyaw = abs(((p["heading_deg"] - gt["yaw"]) + 180) % 360 - 180)
            if dyaw >= 6:
                fails.append(f"{sid} yaw {dyaw:.1f}°")
            dpit = abs(p["pitch_deg"] - gt.get("pitch", 0.0))
            if dpit >= 6:
                fails.append(f"{sid} pitch {dpit:.1f}°")
            if check_roll and gt.get("pitch", 0.0) >= 15.0:
                drol = abs(p["roll_deg"] - gt.get("roll", 0.0))
                if drol >= 9:
                    fails.append(f"{sid} roll {drol:.1f}°")
        ok = not fails
        ok_all &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else "  ← " + "; ".join(fails)))

    # 기준 s1 은 항상 수평·정면·원점(레벨) → 복원값이 GT 절대값과 직접 비교 가능
    S1 = {"x": 0, "y": 0, "yaw": 0, "pitch": 0, "roll": 0}
    # A: yaw 다양, pitch/roll 0
    gA = {"s1": dict(S1), "s2": {"x": 5000, "y": 0, "yaw": 40},
          "s3": {"x": 2500, "y": 5600, "yaw": 180}}
    evaluate("A 삼각형(yaw만)", gA, estimate_positions(frames_of(gA), list(gA), min_overlap=15, ref_id="s1"))

    # B: 반전 포함
    gB = {"s1": dict(S1), "s2": {"x": 5000, "y": 0, "yaw": 40, "flip": True},
          "s3": {"x": 2500, "y": 5600, "yaw": 175}}
    evaluate("B 반전", gB, estimate_positions(frames_of(gB), list(gB), min_overlap=15, ref_id="s1"))

    # C: 체인 (s1↔s3 비겹침, s2 가 다리)
    def occl_c(sid, room):
        return (sid == "s1" and room[0] > 2700) or (sid == "s3" and room[0] < 2300)
    gC = {"s1": dict(S1), "s2": {"x": 5000, "y": 0, "yaw": 40},
          "s3": {"x": 2500, "y": 5600, "yaw": 180}}
    evaluate("C 체인", gC, estimate_positions(frames_of(gC, occl=occl_c), list(gC), min_overlap=12, ref_id="s1"))

    # D: 고립 센서 s4
    def occl_d(sid, room):
        return sid == "s4" and not (room[0] < 900)
    gD = {"s1": dict(S1), "s2": {"x": 5000, "y": 0, "yaw": 40},
          "s4": {"x": -6000, "y": 2800, "yaw": 90}}
    evaluate("D 고립", gD, estimate_positions(frames_of(gD, occl=occl_d), list(gD), min_overlap=15, ref_id="s1"),
             unanchored=("s4",))

    # E: 시간정렬(스태거)
    evaluate("E 시간정렬", gA, estimate_positions(frames_of(gA, stagger=True), list(gA), min_overlap=15, ref_id="s1"))

    # F: pitch 있음 (roll 0)
    gF = {"s1": dict(S1), "s2": {"x": 5000, "y": 0, "yaw": 40, "pitch": 30},
          "s3": {"x": 2500, "y": 5600, "yaw": 180, "pitch": 20}}
    evaluate("F pitch", gF, estimate_positions(frames_of(gF), list(gF), min_overlap=15, ref_id="s1"))

    # G: pitch + roll (pitch 충분 → roll 관측 가능)
    gG = {"s1": dict(S1), "s2": {"x": 5000, "y": 0, "yaw": 40, "pitch": 35, "roll": 15},
          "s3": {"x": 2500, "y": 5600, "yaw": 180, "pitch": 28, "roll": -12}}
    evaluate("G pitch+roll", gG, estimate_positions(frames_of(gG), list(gG), min_overlap=15, ref_id="s1"))

    # H: pitch 작음 + roll → roll 관측불가 라벨, 위치/yaw/pitch 는 여전히 OK (roll 값 강제검사 안함)
    gH = {"s1": dict(S1), "s2": {"x": 5000, "y": 0, "yaw": 40, "pitch": 3, "roll": 10},
          "s3": {"x": 2500, "y": 5600, "yaw": 180, "pitch": 2, "roll": -8}}
    resH = estimate_positions(frames_of(gH), list(gH), min_overlap=15, ref_id="s1")
    evaluate("H 저pitch(roll검사X)", gH, resH, check_roll=False)
    lowflag = all(resH["placements"][s].get("roll_conf") in ("low", "n/a")
                  for s in ("s2", "s3") if resH["placements"][s].get("anchored"))
    print(f"  [{'PASS' if lowflag else 'FAIL'}] H roll 신뢰도 라벨(low/n-a)"); ok_all &= lowflag

    print("=== 결과:", "전부 PASS ✅" if ok_all else "일부 FAIL ❌", "===")
    return 0 if ok_all else 1


# ============================================================================
# CLI
# ============================================================================
def _wait_connected(hub, ids, need=2, timeout=40.0):
    t_end = time.monotonic() + timeout; grace = None
    while time.monotonic() < t_end:
        snap = hub.snapshot()
        conn = [s["id"] for s in snap["sensors"] if s["connected"] and s["id"] in ids]
        sys.stdout.write(f"\r  센서 연결 대기… {len(conn)}/{len(ids)} 연결됨   "); sys.stdout.flush()
        if len(conn) == len(ids):
            sys.stdout.write("\n"); return conn
        if len(conn) >= need:
            grace = grace or (time.monotonic() + 6.0)
            if time.monotonic() >= grace:
                sys.stdout.write("\n"); return conn
        time.sleep(0.5)
    sys.stdout.write("\n")
    snap = hub.snapshot()
    return [s["id"] for s in snap["sensors"] if s["connected"] and s["id"] in ids]


def main() -> int:
    ap = argparse.ArgumentParser(description="다중 센서 자동 포지셔닝(위치 + Yaw·Pitch·Roll)")
    ap.add_argument("--host", nargs="+", default=None)
    ap.add_argument("--transport", choices=["api", "web"], default="api")
    ap.add_argument("--noise-psk", default=None)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--hz", type=float, default=15.0)
    ap.add_argument("--max-gap-ms", type=float, default=700.0)
    ap.add_argument("--min-overlap", type=int, default=20)
    ap.add_argument("--inlier-mm", type=float, default=300.0)
    ap.add_argument("--ref", default=None, help="기준센서 id(수평 설치로 아는 센서). 미지정 시 자동선택.")
    ap.add_argument("--no-refine", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--start-delay", type=float, default=3.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    from mmwave_reader import SensorHub
    from mmwave_wifi_reader import build_sources

    hub = SensorHub()
    workers, desc = build_sources(hub, hosts=args.host, transport=args.transport, noise_psk=args.noise_psk)
    print(f"데이터 소스: {desc}")
    meta_list = hub.sensor_states()
    ids = [m["id"] for m, _ in meta_list]
    names = {m["id"]: m["name"] for m, _ in meta_list}
    sensors_meta = {m["id"]: {"name": m.get("name", ""), "host": m.get("host", ""),
                              "node_name": m.get("node_name", ""), "color": m.get("color", "")}
                    for m, _ in meta_list}
    if len(ids) < 2:
        print(f"❌ 최소 2개 센서 필요. (등록/발견: {len(ids)}개)"); return 2

    for w in workers:
        w.start()
    try:
        print("\n센서 연결을 확인합니다…")
        conn = _wait_connected(hub, ids, need=2)
        if len(conn) < 2:
            print(f"❌ 연결된 센서 부족({len(conn)}개)."); return 3
        print(f"✅ {len(conn)}/{len(ids)}개 연결: {', '.join(names.get(c, c) for c in conn)}")
        print("\n" + "-" * 64)
        print("  [측정 안내]  ★ 방에는 '한 사람'만. 겹침 구역 포함해 방 전체를")
        print("  '천천히, 곡선으로' 걸으세요. (센서 방향이 서로 다를수록 자세 추정 정확)")
        print("-" * 64)
        if not args.yes:
            input("  준비되면 Enter … ")
        for c in range(int(args.start_delay), 0, -1):
            sys.stdout.write(f"\r  시작까지 {c}초…   "); sys.stdout.flush(); time.sleep(1.0)
        print("\r  ▶ 측정 시작! 걸어 다니세요.                 ")
        series = collect_series(hub, ids, args.seconds, hz=args.hz)
    except (KeyboardInterrupt, EOFError):
        print("\n취소됨."); return 130
    finally:
        for w in workers:
            w.stop()

    print("  수집 표본:", "  ".join(f"{names.get(s,s)}:{len(series[s])}" for s in ids))
    frames = resample_to_frames(series, ids, hz=args.hz, max_gap_ms=args.max_gap_ms)
    result = estimate_positions(frames, ids, min_overlap=args.min_overlap,
                                inlier_mm=args.inlier_mm, ref_id=args.ref, refine=not args.no_refine)
    print_report(result, names)
    if result["warnings"]:
        print("\n  [경고]")
        for w in result["warnings"]:
            print(f"    - {w}")
    if result["ref"] is None:
        print("\n저장할 배치가 없습니다."); return 1

    info = apply_to_config(result, sensors_meta, dry_run=args.dry_run)
    chg = info["updated"] + info["added"]
    if info["written"]:
        print(f"\n💾 저장 완료: {info['path']}  (백업: {info['path']}.bak)")
        print(f"   갱신/추가: {', '.join(names.get(s,s) for s in chg) or '없음'}")
        if info["skipped"]:
            print(f"   유지(미고정): {', '.join(names.get(s,s) for s in info['skipped'])}")
        print("\n다음: ./run_gui.sh 로 오버레이 확인.")
    else:
        print(f"\n(dry-run) 저장 안 함. 갱신 대상: {', '.join(names.get(s,s) for s in chg) or '없음'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
