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

Roll 고정 모드 (--fix-roll)
---------------------------
실제 설치가 갸우뚱하지 않다고 아는 경우(대부분의 벽/천장 거치) roll 을 추정하지 말고
**0° 로 고정한 채 x·y·Yaw·Pitch 만** 최적화한다. 이때 사후에 roll 값만 0 으로 덮어쓰면
저장된 자세가 실제 적합된 affine 과 어긋나므로(전단이 남은 채 사라짐), 아래처럼
**모형 자체를 제약**해 다시 푼다:

    roll=0  ⇒  U=[[s,0],[0,cosP]]  ⇒  B(=local→room)=Rot(yaw)·diag(s, 1/cosP)

즉 센서당 미지수가 6개(자유 affine)에서 4개(yaw, pitch, x, y)로 줄고 전단항이 사라진다.
관측이 약한 roll 로 노이즈를 흡수하지 않으므로 Yaw/Pitch/위치가 더 안정적이다.
대신 좌우 스케일 오차(=cosR 로 흡수되던 성분)를 흡수할 자유도가 없어 정합오차(RMS)는
자유 해보다 커진다.
★ 그 RMS 증가는 '정확도가 나빠졌다' 는 뜻이 아니다 — 자유도가 많은 쪽이 항상 RMS 가 낮다.
  합성검증에서 고정 해는 RMS 가 96→98mm 로 커지는 동안 **GT 위치오차는 131→36mm 로 줄었다**.
  그래서 리포트는 RMS 변화와 함께 '자유 해가 쓰던 roll 각도' 를 보여주고, 그 각도가 작으면
  (=RMS 격차가 roll 탓이 아니면) --free-roll 로 되돌리지 말라고 명시한다.

파이프라인: 수집(실측시각) → 시간정렬 보간 → 센서쌍 affine(RANSAC) → 최대성분/기준선택
           → 전역 affine 번들조정(선형최소제곱) → [--fix-roll 이면 roll≡0 제약 재최적화]
           → RQ 분해 → 저장.

사용법
    python auto_positioning.py
    python auto_positioning.py --seconds 90
    python auto_positioning.py --ref 98bd80         # 기준센서 지정(수평 설치로 아는 센서)
    python auto_positioning.py --fix-roll           # Roll 을 0° 로 고정하고 나머지만 최적화
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


# ============================================================================
# roll≡0 제약 번들조정 (가우스-뉴턴) — 전단(shear) 없는 affine 만 허용
# ============================================================================
#  g = 1/cosPitch (기준센서 대비 전방 확대율). pitch≥0 ⇒ g≥1 이므로 g 를 [1, G_MAX] 로 묶는다.
#  ★ 왜 g≥1 을 강제하나: 저장은 pitch=acos(1/g) 로 하고 런타임(room_transform)은 그 pitch 로
#    전방 스케일을 되돌린다. g<1(=기준센서보다 평평)을 허용하면 pitch 가 0° 로 clamp 되어
#    '적합한 변환 ≠ 저장/적용되는 변환' 이 된다(x·y·Yaw 가 쓰이지 않을 스케일에 맞춰 틀어짐).
#    경계에 걸리면 = 기준센서가 다른 센서보다 더 숙여져 있다는 뜻 → 경고로 --ref 재선택 안내.
G_MIN, G_MAX = 1.0, 12.0        # g 범위(pitch 0°…85°). 비물리 해·GN 발산 방지 가드
_GN_ITERS, _GN_LS = 80, 6       # 가우스-뉴턴 반복 / 라인서치 반감 횟수
#  ↑ 활성집합이 걸리면(경계에 붙은 센서가 있으면) 수십 회가 필요하다 — 실측 로그에서 30회대.
#    잔차·야코비가 numpy 벡터화라 한 회가 수 ms 수준이어서 넉넉히 잡아도 체감 비용이 없다.


def B_roll0(yaw, g, s):
    """roll≡0 제약 하의 local→room 2×2.  B = Rot(yaw)·diag(s, g),  g=1/cosPitch, s=±1(반전).
    (A=room→local 은 이 행렬의 역: diag(s, 1/g)·Rot(−yaw) → 상삼각 U 의 전단항 u01=0)."""
    c, sn = math.cos(yaw), math.sin(yaw)
    return np.array([[c * s, -sn * g], [sn * s, c * g]])


def refine_affine_ba_roll0(edges, frames, anchored, ref, init, *, inlier_mm=300.0,
                           huber_mm=200.0):
    """roll≡0 제약 하에서 {yaw, pitch(=acos 1/g), 위치 d} 를 가우스-뉴턴으로 최적화한다.

    자유 affine 번들조정(refine_affine_ba, 센서당 6개)의 부분모형(센서당 4개: yaw,g,dx,dy).
    잔차는 자유 해와 동일하게 'B_i·local_i + d_i = B_j·local_j + d_j'(기준센서 B=I, d=0 게이지).
    · flip(s) 은 이산값이라 최적화하지 않고 init(자유 해)의 det 부호로 고정한다.
    · init 을 초기값으로 IRLS(Huber) + 백트래킹 라인서치 → 비용이 줄 때만 갱신하므로 발산 없음.
    · yaw 때문에 비선형이지만 초기값이 자유 해라 실질적으로 국소=전역 해에 붙는다.
    · ★ g 의 상자제약([G_MIN,G_MAX])은 **활성집합(active set)** 으로 다룬다. 경계에 붙은 g 를
      단순히 clamp 만 하면 그 성분이 섞인 스텝 전체가 상승방향이 되어 라인서치가 전부 실패하고
      GN 이 최적점 훨씬 앞에서 멈춘다(실측: 위치 393mm·Yaw 5.4° 오차, RMS 603 vs 419mm).
      그래서 경계에서 밖으로 밀리는 g 는 **야코비 열을 빼고 최소제곱을 다시 풀어** 나머지
      자유도(yaw·위치)가 제 방향을 찾게 한다.
    반환 형식은 refine_affine_ba 와 동일: {sid: {'B': 2×2(local→room), 'd': (2,)위치}}."""
    nonref = [s for s in anchored if s != ref]
    idx = {s: k for k, s in enumerate(nonref)}
    NP = 4 * len(nonref)
    cons = _corrs(edges, frames, anchored, inlier_mm=inlier_mm)
    out = {ref: {"B": np.eye(2), "d": np.zeros(2)}}
    if NP == 0 or not cons:
        return out

    # ---- 초기값: 자유 해의 affine 을 (yaw, g, flip) 으로 사영 --------------------
    par, sgn = {}, {}
    for s in nonref:
        B0 = init.get(s, {}).get("B")
        d0 = init.get(s, {}).get("d")
        if B0 is None or d0 is None:
            par[s] = [0.0, 1.0, 0.0, 0.0]; sgn[s] = 1.0
            continue
        det = float(np.linalg.det(B0))
        sgn[s] = -1.0 if det < 0 else 1.0
        A0 = np.linalg.inv(B0 if abs(det) > 1e-9 else B0 + np.eye(2) * 1e-6)
        yaw0 = math.atan2(-float(A0[1, 0]), float(A0[1, 1]))
        cP = _cosP_of(A0)
        par[s] = [yaw0, _clamp(1.0 / cP if cP > 1e-6 else G_MAX, G_MIN, G_MAX),
                  float(d0[0]), float(d0[1])]

    # ---- 대응점을 배열로 미리 색인(잔차·야코비를 센서 단위 numpy 연산으로) ----------
    N = len(cons)
    Pi = np.array([c[1] for c in cons], float)
    Pj = np.array([c[3] for c in cons], float)
    rows = {}                       # sid → (i 쪽 행 인덱스, j 쪽 행 인덱스)
    for s in anchored:
        rows[s] = (np.array([n for n, c in enumerate(cons) if c[0] == s], dtype=int),
                   np.array([n for n, c in enumerate(cons) if c[2] == s], dtype=int))

    def residuals(pp):
        """(N,2) 잔차 = (i 쪽 방좌표) − (j 쪽 방좌표)."""
        R = np.zeros((N, 2))
        for s in anchored:
            ii, jj = rows[s]
            if s == ref:                                  # 게이지: B=I, d=0
                if ii.size:
                    R[ii] += Pi[ii]
                if jj.size:
                    R[jj] -= Pj[jj]
                continue
            yaw, g, dx, dy = pp[s]
            B = B_roll0(yaw, g, sgn[s]); d = np.array([dx, dy])
            if ii.size:
                R[ii] += Pi[ii] @ B.T + d
            if jj.size:
                R[jj] -= Pj[jj] @ B.T + d
        return R

    def hcost(R):
        n = np.linalg.norm(R, axis=1)
        big = n > huber_mm
        return float((n[~big] ** 2).sum() + (huber_mm * (2.0 * n[big] - huber_mm)).sum())

    def build(R):
        """IRLS(Huber) 가중 야코비 M 과 우변 rhs. 자유 해의 refine_affine_ba 와 같은 가중방식."""
        n = np.linalg.norm(R, axis=1)
        sw = np.sqrt(np.where(n > huber_mm, huber_mm / np.maximum(n, 1e-6), 1.0))
        M = np.zeros((2 * N, NP))
        for s in nonref:
            yaw, g = par[s][0], par[s][1]
            c, sn = math.cos(yaw), math.sin(yaw)
            base = 4 * idx[s]
            for ind, sign, P in ((rows[s][0], 1.0, Pi), (rows[s][1], -1.0, Pj)):
                if not ind.size:
                    continue
                px, py = P[ind, 0], P[ind, 1]
                q0, q1 = sgn[s] * px, g * py              # q = diag(s,g)·local
                f = sign * sw[ind]
                # ∂(B·p)/∂yaw = Rot′(yaw)·q,  ∂(B·p)/∂g = Rot(yaw)·(0, ly),  ∂/∂d = I
                M[2 * ind, base + 0] += (-sn * q0 - c * q1) * f
                M[2 * ind + 1, base + 0] += (c * q0 - sn * q1) * f
                M[2 * ind, base + 1] += (-sn * py) * f
                M[2 * ind + 1, base + 1] += (c * py) * f
                M[2 * ind, base + 2] += f
                M[2 * ind + 1, base + 3] += f
        rhs = np.empty(2 * N)
        rhs[0::2] = -R[:, 0] * sw
        rhs[1::2] = -R[:, 1] * sw
        return M, rhs

    def step_from(M, rhs, frozen):
        """frozen(고정) 열을 뺀 축소 최소제곱 해를 전체 길이 벡터로 되돌린다."""
        d = np.zeros(NP)
        cols = np.where(~frozen)[0]
        if not cols.size:
            return d
        sub, *_ = np.linalg.lstsq(M[:, cols], rhs, rcond=None)
        d[cols] = sub
        return d

    for _ in range(_GN_ITERS):
        R = residuals(par)
        cost0 = hcost(R)
        M, rhs = build(R)

        # 활성집합: 경계에 붙어 있고 스텝이 '밖으로' 미는 g 는 열을 빼고 재해석한다.
        frozen = np.zeros(NP, bool)
        delta = step_from(M, rhs, frozen)
        for _pass in range(len(nonref) + 1):
            newly = False
            for s in nonref:
                k = 4 * idx[s] + 1
                if frozen[k]:
                    continue
                g_now, dg = par[s][1], float(delta[k])
                if (g_now <= G_MIN + 1e-9 and dg < 0) or (g_now >= G_MAX - 1e-9 and dg > 0):
                    frozen[k] = True; newly = True
            if not newly:
                break
            delta = step_from(M, rhs, frozen)
        if not np.all(np.isfinite(delta)):
            break

        # 백트래킹 — 비용이 줄어드는 스텝만 채택(줄지 않으면 수렴/정체로 보고 종료)
        taken, step = None, 0.0
        st = 1.0
        for _ls in range(_GN_LS):
            trial = {}
            for s in nonref:
                b, k = par[s], 4 * idx[s]
                trial[s] = [b[0] + st * float(delta[k]),
                            _clamp(b[1] + st * float(delta[k + 1]), G_MIN, G_MAX),
                            b[2] + st * float(delta[k + 2]),
                            b[3] + st * float(delta[k + 3])]
            if hcost(residuals(trial)) <= cost0:
                taken, step = trial, st
                break
            st *= 0.5
        if taken is None:
            break
        par = taken
        if step * float(np.max(np.abs(delta))) < 1e-3:      # 최대 갱신폭 < 0.001(mm/rad) → 수렴
            break

    for s in nonref:
        yaw, g, dx, dy = par[s]
        out[s] = {"B": B_roll0(yaw, g, sgn[s]), "d": np.array([dx, dy]),
                  "g_at_bound": bool(g <= G_MIN + 1e-6)}   # pitch 0° 가 '추정' 이 아니라 경계 산물
    return out


def scale_consistency(edges, anchored, ref):
    """센서쌍 affine 의 |det| 은 **게이지와 무관한 상대 전방스케일** g_b/g_a 다
    (roll≡0 모형에서 det(A_edge)=det(A_a)·det(B_b)=±g_b/g_a). 따라서 사이클을 돌면 곱이 1 이어야
    한다. 크게 벗어나면 센서쌍 변환들이 서로 모순이라는 뜻 —  **어떤 --ref 를 골라도** 모든
    센서의 상대 Pitch 를 ≥0 으로 만들 수 없고 Pitch 는 확정되지 않는다(궤적·표본 문제).

    반환 (worst, detail): worst = 최악 불일치 배수(1.0=완전 일치), detail = 사람이 읽을 설명|None."""
    adj = collections.defaultdict(list)
    ratio = {}
    for (a, b), e in edges.items():
        if a not in anchored or b not in anchored:
            continue
        r = abs(float(np.linalg.det(e["A"])))
        if not (1e-6 < r < 1e6):
            continue
        ratio[(a, b)] = r                      # = g_b / g_a
        adj[a].append(b); adj[b].append(a)
    # ref 에서 신장트리를 펴서 각 센서의 예측 로그스케일을 정한다
    logs, order, seen = {ref: 0.0}, [ref], {ref}
    tree = set()
    while order:
        u = order.pop()
        for v in adj[u]:
            if v in seen:
                continue
            r = ratio.get((u, v))
            logs[v] = logs[u] + (math.log(r) if r else 0.0) if r else logs[u]
            if r is None:                       # (v,u) 방향으로 기록된 간선
                r2 = ratio.get((v, u))
                logs[v] = logs[u] - math.log(r2) if r2 else logs[u]
            seen.add(v); order.append(v); tree.add((u, v)); tree.add((v, u))
    worst, detail = 1.0, None
    for (a, b), r in ratio.items():
        if (a, b) in tree or a not in logs or b not in logs:
            continue                            # 트리 간선은 정의상 일치
        pred = math.exp(logs[b] - logs[a])
        m = max(r / pred, pred / r) if pred > 0 else float("inf")
        if m > worst:
            worst = m
            detail = f"{a}·{b} 측정 {r:.2f} vs 다른 경로 예측 {pred:.2f}"
    return worst, detail


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
                       ref_id=None, min_spread_mm=400.0, refine=True, fix_roll=False):
    """fix_roll=True 면 roll 을 0° 로 '제약' 한 채 x·y·Yaw·Pitch 만 최적화한다
    (사후 0 대입이 아니라 전단 없는 모형으로 재적합 → 저장값과 적합된 affine 이 일관)."""
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
                "global_rms": 0.0, "global_rms_free": 0.0, "roll_fixed": bool(fix_roll)}

    sol = refine_affine_ba(edges, frames, anchored, ref, inlier_mm=inlier_mm) if refine \
        else {s: {"B": np.eye(2), "d": np.zeros(2)} for s in anchored}
    g_rms = _ba_rms(sol, edges, frames, anchored, ref, inlier_mm=inlier_mm)
    rms_free = g_rms
    # 자유 해 진단 두 가지를 여기서 미리 뽑는다(고정 모드는 아래에서 sol 을 덮어쓴다):
    #  · flat_ratio: 전방압축비(기준센서=1). >1 이면 '기준센서보다 평평'(상대 pitch<0) → pitch 0° 처리.
    #  · roll_free : 자유 해가 그 센서에 붙였던 roll(°). 고정 모드가 '무엇을 버리는지' 보여준다.
    flat_ratio, roll_free = {}, {}
    for sid in anchored:
        Bf = sol[sid]["B"]
        detf = float(np.linalg.det(Bf))
        Af = np.linalg.inv(Bf if abs(detf) > 1e-9 else Bf + np.eye(2) * 1e-6)
        flat_ratio[sid] = _cosP_of(Af)
        _y, _p, _r, _f = decompose_Aloc(Af)
        roll_free[sid] = math.degrees(_r)
    # ★ 라벨·리포트는 '실제로 제약 최적화가 돌았는가' 로 판단한다 — fix_roll 만 보고 'roll 고정'
    #   이라고 쓰면 --no-refine(번들조정 자체를 건너뜀) 에서 하지 않은 최적화를 했다고 말하게 되고,
    #   기존의 roll 신뢰도 경고(low/n-a)까지 덮어 버린다.
    roll_is_fixed = bool(fix_roll and refine)
    g_bound = {}
    if roll_is_fixed:
        # 자유 해를 초기값으로 'roll≡0' 부분모형에서 재최적화 → 나머지(x,y,Yaw,Pitch)가
        # 제약과 일관되게 다시 맞춰진다. (사후 roll=0 대입은 적합된 affine 과 어긋남)
        sol = refine_affine_ba_roll0(edges, frames, anchored, ref, sol, inlier_mm=inlier_mm)
        g_bound = {sid: bool(sol[sid].get("g_at_bound")) for sid in anchored}
        g_rms = _ba_rms(sol, edges, frames, anchored, ref, inlier_mm=inlier_mm)
        # ★ RMS 증가만으로 '실제로 기울어졌다' 고 판정하면 안 된다 — 표본이 적거나 노이즈가
        #   크면 자유 해가 여분 자유도 2개로 '과적합' 해 RMS 만 낮게 만든다(자체검증: 위치오차는
        #   131→84mm 로 좋아지는데 RMS 는 96→146mm 로 나빠짐). 그래서 RMS 변화와 함께
        #   '자유 해가 쓰던 roll 각도' 를 같이 보여주고 판단 근거를 사람에게 넘긴다.
        worst_sid = max(roll_free, key=lambda k: abs(roll_free[k])) if roll_free else None
        worst_roll = abs(roll_free.get(worst_sid, 0.0)) if worst_sid else 0.0
        if g_rms > max(1.25 * rms_free, rms_free + 15.0):
            head = (f"Roll 0° 고정으로 전역 정합오차가 {rms_free:.0f}→{g_rms:.0f}mm 로 커졌습니다. "
                    f"자유 해가 쓰던 Roll 은 최대 {worst_roll:.1f}° ({worst_sid}) 였습니다 → ")
            if worst_roll < 5.0:
                # ★ 자유 해의 roll 이 애초에 0 에 가까웠다 = 이 RMS 격차는 roll 때문이 아니다.
                #   (자유 affine 의 좌우·전체 스케일 자유도가 흡수한 것) 이때 --free-roll 을
                #   권하면 더 나쁜 해로 유도한다 — 합성검증 35/35 에서 고정 해가 GT 에 더 가까웠다.
                warnings.append(head + "이 격차는 Roll 때문이 아닙니다(자유 affine 의 좌우·전체 "
                                "스케일 자유도가 흡수한 것). --free-roll 로 되돌리지 말고, "
                                "겹침 구역에서 곡선으로 더 크게 움직인 로그로 재측정하세요.")
            elif worst_roll <= 15.0:
                warnings.append(head + "실제 설치로 그럴듯한 각도이니 --free-roll 결과와 "
                                "비교해 보세요(RMS 는 자유도가 많은 쪽이 항상 낮아 정확도의 "
                                "척도가 아닙니다 — 두 해의 오버레이를 눈으로 비교하세요).")
            else:
                warnings.append(head + "그렇게 기울여 달지 않았다면 표본 부족·좌우 스케일 오차를 "
                                "roll 이 흡수한 것이라 0° 고정이 맞습니다 "
                                "(RMS 는 자유도가 많은 쪽이 항상 낮아 정확도의 척도가 아닙니다).")
        elif worst_roll >= 25.0:
            # RMS 가 별로 안 늘었는데 자유 해의 roll 이 거대했다 = 순수 노이즈 흡수였다는 증거.
            warnings.append(f"참고: 자유 해는 {worst_sid} 에 Roll {roll_free[worst_sid]:.1f}° 를 "
                            f"붙였는데 0° 로 고정해도 정합오차가 {rms_free:.0f}→{g_rms:.0f}mm 로 "
                            "거의 그대로입니다 — 그 각도는 실제 기울어짐이 아니라 노이즈였습니다.")
    elif fix_roll and not refine:
        warnings.append("--no-refine 이면 번들조정을 건너뛰므로 Roll 고정 최적화도 생략됩니다"
                        " (아래 Roll 값은 '고정' 이 아니라 항등해의 부산물입니다).")

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
        if roll_is_fixed:
            # 제약 모형이라 u01≈0(부동소수 오차만) → 값도 라벨도 '고정'으로 못 박는다.
            roll, roll_conf = 0.0, "fixed"
        elif sP > 5e-2:
            roll = math.atan2(_clamp((s * u01) / sP, -1.0, 1.0), cR)
            roll_conf = "ok" if pitch >= math.radians(15) else "low"
        else:
            roll = 0.0
            roll_conf = "n/a"                                    # pitch≈0 → roll 관측불가
        # Pitch 신뢰도: 상대 pitch 가 음수로 나오려 한 센서(=기준센서보다 평평)는 0° 로 잡힌다.
        # 고정 모드는 g 가 경계(G_MIN)에 붙었는지로 '직접' 안다 — 자유 해의 압축비로 판정하면
        # 자유 해가 그 스케일을 roll 로 흘려버린 센서(예: 실측 pia-1-1)를 놓친다(무성 고정).
        pitch_conf = "clamped" if (g_bound.get(sid) or flat_ratio.get(sid, 1.0) > 1.02) else "ok"
        pos = sol[sid]["d"]
        r = [e["rms"] for (a, b), e in edges.items() if sid in (a, b)]
        placements[sid] = {
            "anchored": True, "is_ref": (sid == ref),
            "x": float(pos[0]), "y": float(pos[1]),
            "heading_deg": math.degrees(yaw),      # Yaw (좌우방향 설치각도)
            "pitch_deg": math.degrees(pitch),      # Pitch (상하방향 설치각도)
            "roll_deg": math.degrees(roll),        # Roll (기울어짐 각도)
            "roll_conf": roll_conf, "pitch_conf": pitch_conf, "flip": bool(flip),
            "rms": (min(r) if r else None),
        }

    # Pitch 가 경계에 걸린 센서 안내 — '--ref 를 바꾸면 된다' 는 조언은 **그 해가 존재할 때만**
    # 유효하다. 센서쌍 전방스케일비가 서로 모순이면(사이클 곱 ≠ 1) 어떤 --ref 로도 못 고친다.
    clamped = [sid for sid, p in placements.items()
               if p.get("anchored") and p.get("pitch_conf") == "clamped"]
    if clamped:
        worst_c, detail = scale_consistency(edges, anchored, ref)
        head = (f"센서 {', '.join(clamped)} 의 Pitch 0° 는 '추정값' 이 아니라 제약 경계입니다"
                " (기준센서보다 평평 → 상대 Pitch 가 음수). ")
        if worst_c > 1.3:
            warnings.append(head + f"게다가 센서쌍 전방스케일비가 서로 모순입니다({detail}, "
                            f"불일치 {worst_c:.2f}배) → **어떤 --ref 를 골라도** Pitch 는 확정되지 "
                            "않습니다. Pitch 가 필요하면 겹침 구역에서 곡선으로 크게 움직인 로그로 "
                            "재측정하세요(x·y·Yaw 는 그대로 쓸 수 있습니다).")
        else:
            warnings.append(head + "가장 평평한(수평) 센서를 --ref 로 지정하면 Pitch 가 살아납니다.")
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
            "global_rms": g_rms, "global_rms_free": rms_free,
            "roll_fixed": roll_is_fixed}


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


_CONF_LABEL = {"ok": "", "low": " (신뢰낮음)", "n/a": " (관측불가)", "fixed": " (0°고정)"}
_PITCH_LABEL = {"ok": "", "clamped": " (경계·추정아님)"}


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
    if result.get("roll_fixed"):
        print(f"  · Roll 은 0° 로 '고정'하고 x·y·Yaw·Pitch 만 최적화했습니다"
              f" (참고: roll 까지 추정한 자유 해의 정합오차 {result.get('global_rms_free', 0.0):.0f} mm).")
        print("    ※ RMS 는 '얼마나 잘 맞췄나'가 아니라 '자유도가 몇 개냐'에 더 좌우됩니다 —"
              " 미지수를 뺀 고정 해가 RMS 는 커도 실제 배치는 더 정확할 수 있습니다.")
        print("  · 위치·Yaw 는 기준센서 상대,  Pitch 는 기준센서를 0°로 한 상대값.")
    else:
        print("  · 위치·Yaw 는 기준센서 상대,  Pitch 는 가장 평평한 센서를 0°로 한 상대값,  Roll 은 기준센서 상대.")
    print("\n  [추정된 설치자세]")
    for sid, p in result["placements"].items():
        if p.get("anchored"):
            tag = " (기준)" if p.get("is_ref") else ""
            rms = f"  rms {p['rms']:.0f}mm" if p.get("rms") is not None else ""
            rc = _CONF_LABEL.get(p.get("roll_conf", "ok"), "")
            pc = _PITCH_LABEL.get(p.get("pitch_conf", "ok"), "")
            fwd_sc = 1.0 / max(math.cos(math.radians(p["pitch_deg"])), 1e-3)   # 앞뒤 스케일
            lat_sc = 1.0 / max(math.cos(math.radians(p["roll_deg"])), 1e-3)    # 좌우 스케일
            print(f"    ✅ {nm(sid)}{tag}: 위치=({p['x']:.0f},{p['y']:.0f})mm  "
                  f"Yaw(좌우)={p['heading_deg']:.1f}°  Pitch(상하)={p['pitch_deg']:.1f}°{pc}  "
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

    # 0b) roll≡0 제약 파라미터화가 일반 모형의 roll=0 단면과 정확히 같은가
    #     (B_roll0 = pose_to_Aloc(yaw,pitch,0,flip)⁻¹ 이어야 제약 최적화 결과를 그대로 RQ 분해 가능)
    print("  [0b] roll≡0 파라미터화 ↔ 일반 모형 일치")
    c_ok = True
    for (y, p, f) in [(0, 0, False), (30, 20, False), (-40, 35, True), (170, 25, False), (95, 8, True)]:
        yaw, pitch = math.radians(y), math.radians(p)
        A = pose_to_Aloc(yaw, pitch, 0.0, f)
        B = B_roll0(yaw, 1.0 / math.cos(pitch), -1.0 if f else 1.0)
        if float(np.abs(A @ B - np.eye(2)).max()) > 1e-9:
            c_ok = False; print(f"      FAIL ({y},{p},{f}): A·B ≠ I")
        # 제약 해를 RQ 분해하면 roll 이 정확히 0, yaw/pitch 는 그대로 나와야 한다
        ry, rp, rr, rf = decompose_Aloc(np.linalg.inv(B))
        if abs(math.degrees(rr)) > 1e-6 or abs(math.degrees(rp) - p) > 1e-4 or rf != f \
                or abs(((math.degrees(ry) - y) + 180) % 360 - 180) > 1e-4:
            c_ok = False
            print(f"      FAIL 분해 ({y},{p},{f}) → yaw={math.degrees(ry):.3f} "
                  f"pitch={math.degrees(rp):.3f} roll={math.degrees(rr):.6f} flip={rf}")
    print("      " + ("PASS" if c_ok else "FAIL")); ok_all &= c_ok

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

    # ---- Roll 0° 고정 모드(--fix-roll) -------------------------------------------
    def _worst_pos_err(res, gts):
        e = 0.0
        for sid, gt in gts.items():
            p = res["placements"].get(sid, {})
            if not p.get("anchored"):
                return float("inf")
            e = max(e, math.hypot(p["x"] - gt["x"], p["y"] - gt["y"]))
        return e

    # I: 실제로 기울어짐이 없는 설치(gF: pitch 있음, roll 0) → 고정해도 GT 복원, roll 은 정확히 0
    frF = frames_of(gF)
    resI = estimate_positions(frF, list(gF), min_overlap=15, ref_id="s1", fix_roll=True)
    evaluate("I fix-roll (GT roll=0)", gF, resI)
    zero_ok = all(p["roll_deg"] == 0.0 and p.get("roll_conf") == "fixed"
                  for p in resI["placements"].values() if p.get("anchored"))
    print(f"  [{'PASS' if zero_ok else 'FAIL'}] I roll 정확히 0.0° + 라벨 fixed"); ok_all &= zero_ok
    # 제약이 맞는 상황이라 정합오차가 자유 해 대비 크게 나빠지지 않아야 한다(미지수만 줄어듦)
    resI_free = estimate_positions(frF, list(gF), min_overlap=15, ref_id="s1")
    rms_ok = resI["global_rms"] <= resI_free["global_rms"] * 1.25 + 5.0
    acc_ok = _worst_pos_err(resI, gF) <= _worst_pos_err(resI_free, gF) + 60.0
    print(f"  [{'PASS' if rms_ok and acc_ok else 'FAIL'}] I 제약 손실 없음: rms "
          f"{resI_free['global_rms']:.0f}→{resI['global_rms']:.0f}mm · 위치오차 "
          f"{_worst_pos_err(resI_free, gF):.0f}→{_worst_pos_err(resI, gF):.0f}mm")
    ok_all &= (rms_ok and acc_ok)

    # I-b: 저장되는 자세(x,y,Yaw,Pitch,roll=0,flip)로 재구성한 변환 == '적합된' 변환 인가.
    #      런타임(room_transform)은 저장값만 쓰므로 이게 어긋나면 오버레이가 그만큼 틀어진다.
    #      (그래서 g=1/cosPitch 를 ≥1 로 묶는다 — pitch clamp 로 스케일이 사라지지 않게)
    edgesI, _dg = build_edges(frF, list(gF), min_overlap=15, inlier_mm=300.0)
    anchI, refI, _cp = select_component_and_ref(edgesI, list(gF), ref_id="s1")
    freeI = refine_affine_ba(edgesI, frF, anchI, refI, inlier_mm=300.0)
    fixI = refine_affine_ba_roll0(edgesI, frF, anchI, refI, freeI, inlier_mm=300.0)
    worst = 0.0
    for _sid, so in fixI.items():
        A = np.linalg.inv(so["B"])
        yy, _pp, _rr, ff = decompose_Aloc(A)
        p_saved = math.acos(_clamp(_cosP_of(A), 0.0, 1.0))
        worst = max(worst, float(np.abs(pose_to_Aloc(yy, p_saved, 0.0, ff) - A).max()))
    cons_ok = worst < 1e-9
    print(f"  [{'PASS' if cons_ok else 'FAIL'}] I 저장값 = 적합값 일관성 (최대차 {worst:.1e})")
    ok_all &= cons_ok

    # I-c: '사후에 roll 만 0 으로 덮어쓰기' 보다 제약 재최적화가 실제로 더 잘 맞아야 한다
    #      (덮어쓰기는 나머지 값이 옛 전단을 전제로 맞춰져 있어 변환이 어긋난다)
    naive = {}
    for sid, so in freeI.items():
        A = np.linalg.inv(so["B"])
        yy, pp, _rr, ff = decompose_Aloc(A)
        naive[sid] = {"B": np.linalg.inv(pose_to_Aloc(yy, pp, 0.0, ff)), "d": so["d"]}
    rms_naive = _ba_rms(naive, edgesI, frF, anchI, refI, inlier_mm=300.0)
    rms_fix = _ba_rms(fixI, edgesI, frF, anchI, refI, inlier_mm=300.0)
    better = rms_fix <= rms_naive
    print(f"  [{'PASS' if better else 'FAIL'}] I 제약 재최적화 ≤ 사후 0 대입: "
          f"{rms_fix:.0f}mm ≤ {rms_naive:.0f}mm")
    ok_all &= better

    # I-d: ★ g 가 경계(G_MIN)에 붙는 배치 — 기준센서가 '가장 많이 숙여진' 센서라 다른 센서의
    #      상대 pitch 가 음수가 되는 경우. 여기서 예전 구현은 라인서치가 전부 실패해 GN 이
    #      3회에서 멈추고 최적점에서 수백 mm 벗어난 해를 냈다(clamp 만 하면 스텝 전체가
    #      상승방향이 되기 때문). 활성집합이 제대로 동작하는지 = '지역최적' 인지로 검증한다.
    gK = {"s1": {"x": 0, "y": 0, "yaw": 0, "pitch": 30, "roll": 0},       # 기준이 제일 숙여짐
          "s2": {"x": 5000, "y": 0, "yaw": 40, "pitch": 5},
          "s3": {"x": 2500, "y": 5600, "yaw": 180, "pitch": 0}}
    frK = frames_of(gK)
    edgesK, _dk = build_edges(frK, list(gK), min_overlap=15, inlier_mm=300.0)
    anchK, refK, _ck = select_component_and_ref(edgesK, list(gK), ref_id="s1")
    freeK = refine_affine_ba(edgesK, frK, anchK, refK, inlier_mm=300.0)
    fixK = refine_affine_ba_roll0(edgesK, frK, anchK, refK, freeK, inlier_mm=300.0)
    at_bound = [s for s in anchK if s != refK and fixK[s].get("g_at_bound")]
    rmsK = _ba_rms(fixK, edgesK, frK, anchK, refK, inlier_mm=300.0)
    # 지역최적성: yaw ±0.5°, 위치 ±40mm 를 흔들어도 정합오차가 줄지 않아야 한다(정체 아님)
    worse = True
    for s in [x for x in anchK if x != refK]:
        A = np.linalg.inv(fixK[s]["B"]); yy, _p, _r, ff = decompose_Aloc(A)
        gg = 1.0 / max(_cosP_of(A), 1e-9)
        for dy_ in (math.radians(0.5), -math.radians(0.5)):
            probe = {k: dict(v) for k, v in fixK.items()}
            probe[s] = {"B": B_roll0(yy + dy_, gg, -1.0 if ff else 1.0), "d": fixK[s]["d"]}
            if _ba_rms(probe, edgesK, frK, anchK, refK, inlier_mm=300.0) < rmsK - 1e-6:
                worse = False
        for dd in ((40.0, 0.0), (-40.0, 0.0), (0.0, 40.0), (0.0, -40.0)):
            probe = {k: dict(v) for k, v in fixK.items()}
            probe[s] = {"B": fixK[s]["B"], "d": fixK[s]["d"] + np.array(dd)}
            if _ba_rms(probe, edgesK, frK, anchK, refK, inlier_mm=300.0) < rmsK - 1e-6:
                worse = False
    p_bound = bool(at_bound) and worse
    print(f"  [{'PASS' if p_bound else 'FAIL'}] I-d 경계(g=G_MIN) 활성집합: 걸린센서={at_bound or '없음'}"
          f" · rms {rmsK:.0f}mm 가 지역최적={worse}")
    ok_all &= p_bound

    # J: 실제로 기울어진 설치(gG: roll ±)에 고정을 걸면 — 값은 0 이 되고, 모형 불일치를
    #    RMS 증가로 '경고' 해야 한다(조용히 틀린 값을 저장하지 않는다).
    resJ = estimate_positions(frames_of(gG), list(gG), min_overlap=15, ref_id="s1", fix_roll=True)
    warned = any("Roll 0° 고정" in w for w in resJ["warnings"])
    zeroJ = all(p["roll_deg"] == 0.0 for p in resJ["placements"].values() if p.get("anchored"))
    print(f"  [{'PASS' if warned and zeroJ else 'FAIL'}] J 실제 기울어진 설치에 고정 → 0° 저장 + "
          f"RMS 경고 (rms {resJ.get('global_rms_free', 0):.0f}→{resJ['global_rms']:.0f}mm)")
    ok_all &= (warned and zeroJ)

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
    from epl_config import DEFAULT_CAMERA_ID, DEFAULT_ORGANIZATION

    ap = argparse.ArgumentParser(description="다중 센서 자동 포지셔닝(위치 + Yaw·Pitch·Roll)")
    ap.add_argument("--host", nargs="+", default=None)
    ap.add_argument("--camera-id", default=DEFAULT_CAMERA_ID,
                    help=f"캘리브레이션할 카메라(stream) 식별자 (기본 {DEFAULT_CAMERA_ID}). "
                         "빈 문자열이면 카메라 구분 없이 등록된 전 센서를 쓴다.")
    ap.add_argument("--organization", default=DEFAULT_ORGANIZATION,
                    help=f"센서 id 의 organization 파트 (기본 {DEFAULT_ORGANIZATION})")
    ap.add_argument("--transport", choices=["api", "web"], default="api")
    ap.add_argument("--noise-psk", default=None)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--hz", type=float, default=15.0)
    ap.add_argument("--max-gap-ms", type=float, default=700.0)
    ap.add_argument("--min-overlap", type=int, default=20)
    ap.add_argument("--inlier-mm", type=float, default=300.0)
    ap.add_argument("--ref", default=None, help="기준센서 id(수평 설치로 아는 센서). 미지정 시 자동선택.")
    ap.add_argument("--fix-roll", action="store_true",
                    help="Roll 을 0° 로 고정(제약)하고 x·y·Yaw·Pitch 만 최적화")
    ap.add_argument("--free-roll", action="store_true",
                    help="--fix-roll 취소(Roll 도 추정). 뒤에 오는 인자가 우선이라 스크립트 기본값을 덮을 때 사용")
    ap.add_argument("--no-refine", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--start-delay", type=float, default=3.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    from epl_config import (
        CONFIG_PATH,
        assert_camera_id_registerable,
        get_camera_ids,
        get_sensors_for_camera,
        load_config,
    )
    from mmwave_reader import SensorHub
    from mmwave_wifi_reader import build_sources

    # 카메라 필터 — x/y/heading 은 '방 좌표계' 값이고 **카메라마다 별도의 방 좌표계**라
    # 카메라마다 따로 캘리브레이션해야 한다. 다른 카메라 센서를 섞으면 두 좌표계를 하나로
    # 억지로 맞춘 배치가 저장된다.
    # ★ 이 파일의 지역변수 `room`(610~717 의 합성기)은 **방 좌표 튜플(mm)** 이라 뜻이 다르다.
    camera_id = str(args.camera_id or "").strip()
    organization = str(args.organization or "").strip() or DEFAULT_ORGANIZATION
    specs = None
    if args.host:
        if camera_id:
            print(f"ℹ️  --host 를 직접 줬으므로 --camera-id '{camera_id}' 필터는 무시합니다.")
        camera_id = ""
    elif camera_id:
        # 제품이 등록할 수 없는 cameraId 로 캘리브레이션하면 존재할 수 없는 카메라의 방
        # 좌표를 맞추는 셈이다 — '센서 0개' 로 흘리지 않고 이유를 먼저 알린다.
        try:
            assert_camera_id_registerable(camera_id, source="--camera-id")
        except ValueError as e:
            print(f"❌ {e}")
            return 2
        cfg = load_config()
        try:
            specs = get_sensors_for_camera(cfg, organization, camera_id)
        except ValueError as e:      # 옛 rooms 스키마 / id 형식·중복 오류
            print(f"❌ 센서 설정 오류: {e}")
            print(f"   파일: {CONFIG_PATH}")
            return 2
        if len(specs) < 2:
            print(f"❌ 카메라 '{organization}-{camera_id}' 에 묶인 센서가 "
                  f"{len(specs)}개입니다 (최소 2개 필요).")
            print(f"   설정의 cameraId: {', '.join(get_camera_ids(cfg, organization)) or '(없음)'}"
                  f"   ·  파일: {CONFIG_PATH}")
            print("   → ./run_provision.sh 의 CAMERA_ID 로 센서를 등록하거나,"
                  " epl_config.json 의 센서 \"id\" 접두를 확인하세요.")
            return 2
        print(f"🎥 카메라: {organization}-{camera_id}  ·  센서 {len(specs)}개 — "
              f"{', '.join(s['name'] for s in specs)}")

    hub = SensorHub()
    workers, desc = build_sources(hub, hosts=args.host, specs=specs,
                                  transport=args.transport, noise_psk=args.noise_psk)
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
                                inlier_mm=args.inlier_mm, ref_id=args.ref,
                                refine=not args.no_refine,
                                fix_roll=args.fix_roll and not args.free_roll)
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
        if info["added"]:
            # --host 로 새 센서가 들어오면 id 가 규격을 벗어난다 → 제품이 등록을 거절한다.
            from epl_config import nonconforming_sensor_ids
            bad = nonconforming_sensor_ids()
            if bad:
                print(f"   ⚠ id 규격을 벗어난 센서: {', '.join(bad)}"
                      " — 제품이 스트림 등록을 거절합니다."
                      " \"id\" 를 '{organization}-{cameraId}-{sensorId}' 형식으로 고치세요.")
        print("\n다음: ./run_gui.sh 로 오버레이 확인.")
    else:
        print(f"\n(dry-run) 저장 안 함. 갱신 대상: {', '.join(names.get(s,s) for s in chg) or '없음'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
