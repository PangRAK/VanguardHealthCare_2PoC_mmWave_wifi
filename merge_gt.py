#!/usr/bin/env python3
"""
단일-인물 debug 로그 N개를 같은 공간·시간축에 겹쳐 '다중-인물 GT' 를 만든다.
================================================================================

각 debug 로그는 "1명만 출현" 가정으로 기록됐으므로, 그 사람의 정답 ID 는 자명(=로그 자체).
서로 다른 시도의 단일-인물 로그 N개를 "같은 방에서 N명이 동시에 움직인 것"처럼 중첩하면,
사람별 정답 ID 를 아는 N-인물 장면(GT)이 만들어진다. 이 GT 로 채점하면 "병합이 공격적일수록
좋아 보이는" 1인 최적화의 맹점(두 사람을 하나로 합쳐도 못 잡음)을 막을 수 있다.

검증/처리 (리뷰 포인트):
  R1. 헤더의 센서 배치(id·x·y·heading·pitch·roll·flip)와 fuse_hz 가 모두 같아야 좌표계가 일치.
      불일치 시 중단.
  R3. 병합 후 한 센서 타깃 수가 LD2450 한계(~3)를 넘으면 경고(융합층 스트레스로는 유효).
  R4. (sid,tid) 를 사람별로 유일 리맵(new_tid = orig_tid + g*1000). 안 하면 서로 다른 사람이
      입력단에서 (sid,tid) 평균으로 하나가 돼버림(fusion.py _aggregate_window).
  R5. 로그마다 t 절대값이 다르므로 프레임 인덱스로 정렬하고, 단일 타임라인(로그0의 t)을 채택.
  R6. 센서 병합 모사(collision_mm>0, merge_collisions): 각 로그는 따로 기록돼 두 사람이 가까워져도
      입력단에는 '별개의 점 2개'로 남는다(비현실적 — 실제 센서는 한 명으로 측정). 그래서 두 사람의
      참 위치가 collision_mm 안이면 그들의 '센서별' 검출을 한 점(평균)으로 합쳐 실제 병합을 모사한다.
      → 다시 멀어지면 점이 둘로 갈라지고, 되살아난 점이 새 ID 가 되는지/ReID 로 옛 ID 를 지키는지까지
      채점 가능해진다. (같은 collision_mm 로 채점 시 그 겹침 구간은 개인별 채점에서 제외 — R2)
"""
from __future__ import annotations

import json
import math
import os

from optimize_fusion import load_log, presence_class


def _collision_groups(present_gs, positions, collision_mm):
    """present 사람들 중 참 위치가 collision_mm 안(추이적)으로 묶이는 그룹들(크기≥2만)."""
    parent = {g: g for g in present_gs}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    for ia in range(len(present_gs)):
        for ib in range(ia + 1, len(present_gs)):
            g1, g2 = present_gs[ia], present_gs[ib]
            p1, p2 = positions[g1], positions[g2]
            if p1 and p2 and math.hypot(p1[0] - p2[0], p1[1] - p2[1]) < collision_mm:
                ra, rb = find(g1), find(g2)
                if ra != rb:
                    parent[ra] = rb
    groups = {}
    for g in present_gs:
        groups.setdefault(find(g), []).append(g)
    return [grp for grp in groups.values() if len(grp) >= 2]


def _collapse_collisions(dets, present_gs, positions, collision_mm):
    """collision_mm 안 그룹의 검출을 '센서별'로 한 점(평균)으로 합침(=센서가 한 명으로 측정).
    대표 tid=그룹 내 최소(이미 tid2gt 에 매핑됨). 겹치지 않은 사람 검출은 그대로."""
    groups = _collision_groups(present_gs, positions, collision_mm)
    if not groups:
        return dets
    gid = {}
    for i, grp in enumerate(groups):
        for g in grp:
            gid[g] = i
    out, bucket = [], {}
    for d in dets:
        g = d.get("gt")
        if g in gid:
            bucket.setdefault((gid[g], d["sid"]), []).append(d)
        else:
            out.append(d)
    for (_, sid), ds in bucket.items():
        if len(ds) == 1:
            out.append(ds[0]); continue
        mx = sum(x["x"] for x in ds) / len(ds); my = sum(x["y"] for x in ds) / len(ds)
        rep = min(ds, key=lambda x: x["tid"])       # 대표(최소) tid — tid2gt 이미 매핑됨
        out.append({"sid": sid, "tid": rep["tid"], "x": mx, "y": my, "dir": None,
                    "gt": rep["gt"], "merged_gts": sorted({x["gt"] for x in ds})})
    return out


def _sensor_geom(header):
    """헤더 sensors → {id: (x,y,heading,pitch,roll,flip)} (좌표계 동일성 비교용)."""
    g = {}
    for s in header.get("sensors", []):
        g[s["id"]] = (round(s.get("x", 0.0), 1), round(s.get("y", 0.0), 1),
                      round(s.get("heading_deg", 0.0), 2), round(s.get("pitch_deg", 0.0), 2),
                      round(s.get("roll_deg", 0.0), 2), bool(s.get("flip", False)))
    return g


def build_gt(paths, *, margin_deg, margin_mm, collision_mm=0.0,
             merge_collisions=True, gt_out=None):
    """단일-인물 로그 경로 N개 → GT dict. (검증 실패 시 SystemExit)
    collision_mm>0 & merge_collisions=True 이면 R6(센서 병합 모사) 적용."""
    if len(paths) < 2:
        raise SystemExit("--gt-logs 에는 최소 2개의 로그가 필요합니다.")
    logs = []
    for p in paths:
        h, fr = load_log(p)
        if not fr:
            raise SystemExit(f"빈 기록: {p}")
        logs.append((p, h or {}, fr))

    # R1: fuse_hz + 센서 배치 동일성
    fz0 = logs[0][1].get("fuse_hz")
    geom0 = _sensor_geom(logs[0][1])
    for p, h, _ in logs[1:]:
        if h.get("fuse_hz") != fz0:
            raise SystemExit(f"fuse_hz 불일치: {os.path.basename(paths[0])}={fz0} vs "
                             f"{os.path.basename(p)}={h.get('fuse_hz')} — 중첩 불가")
        if _sensor_geom(h) != geom0:
            raise SystemExit(f"센서 배치 불일치: {os.path.basename(p)} — "
                             f"같은 방/배치로 기록한 로그만 겹칠 수 있습니다")

    N = len(logs)
    L = min(len(fr) for _, _, fr in logs)          # R5: 최단 길이로 절단
    labels = [os.path.basename(p) for p, _, _ in logs]
    print(f"[GT] {N}인 합성 · 길이 {L}프레임(최단 기준) · fuse_hz={fz0}")
    for g, (p, _, fr) in enumerate(logs):
        print(f"     사람{g}: {labels[g]} ({len(fr)}프레임 → {L}로 절단)")

    # R4: 사람별 tid 오프셋 = (전 로그 최대 tid + 1) → tid 크기와 무관하게 항상 충돌 없음
    maxtid = 0
    for _, _, fr in logs:
        for f in fr[:L]:
            for d in f["dets"]:
                if d["tid"] > maxtid:
                    maxtid = d["tid"]
    off = maxtid + 1

    do_merge = merge_collisions and collision_mm > 0
    tid2gt = {}
    frames, cls, positions = [], [], []
    max_per_sensor = 0
    n_collapsed = 0                                                    # 센서 병합이 일어난 프레임 수
    for i in range(L):
        merged_dets, row_cls, row_pos = [], [], []
        for g, (_, _, fr) in enumerate(logs):
            f = fr[i]
            row_cls.append(presence_class(f, margin_deg, margin_mm))   # 사람 g presence(그 로그 raw)
            ds = f["dets"]
            if ds:
                cx = sum(d["x"] for d in ds) / len(ds)
                cy = sum(d["y"] for d in ds) / len(ds)
                row_pos.append((cx, cy))
            else:
                row_pos.append(None)
            for d in ds:
                nt = d["tid"] + g * off                                # R4: 사람별 tid 유일화
                tid2gt[(d["sid"], nt)] = g
                merged_dets.append({"sid": d["sid"], "tid": nt, "x": d["x"], "y": d["y"],
                                    "dir": d.get("dir"), "gt": g})
        if do_merge:                                                   # R6: 센서 병합 모사
            present_gs = [g for g in range(N) if row_cls[g] != "OUT" and row_pos[g] is not None]
            collapsed = _collapse_collisions(merged_dets, present_gs, row_pos, collision_mm)
            if len(collapsed) != len(merged_dets):
                n_collapsed += 1
            merged_dets = collapsed
        cnt = {}
        for d in merged_dets:                                          # 병합 후 실제 센서당 타깃 수
            cnt[d["sid"]] = cnt.get(d["sid"], 0) + 1
        if cnt:
            max_per_sensor = max(max_per_sensor, max(cnt.values()))
        frames.append({"t": logs[0][2][i]["t"], "frame": i + 1, "dets": merged_dets})  # R5 타임라인
        cls.append(row_cls)
        positions.append(row_pos)

    if do_merge:
        print(f"[GT] 센서 병합 모사(R6): collision_mm={collision_mm:.0f} · 병합 발생 프레임 {n_collapsed}/{L}")
    else:
        print("[GT] 센서 병합 모사 꺼짐(--no-collision-merge 또는 collision_mm=0) — 겹쳐도 점 2개 유지")
    if max_per_sensor > 3:                                             # R3
        print(f"[경고 R3] 병합 후 한 센서에 최대 {max_per_sensor}타깃 — LD2450 실제 한계(~3) 초과. "
              "융합층 스트레스 테스트로는 유효하나 센서 단 현실성은 벗어남.")

    gt = {"n": N, "labels": labels, "fuse_hz": fz0, "length": L,
          "frames": frames, "cls": cls, "positions": positions, "tid2gt": tid2gt,
          "base_hp": dict(logs[0][1].get("hyperparams_after_record") or {}),
          "sensors": logs[0][1].get("sensors", [])}   # 배치(replay 영상 렌더용)

    if gt_out:                                                         # (선택) 주석 붙은 merged jsonl
        with open(gt_out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"_type": "header", "gt_persons": N, "labels": labels,
                                 "fuse_hz": fz0, "length": L,
                                 "note": "합성 다중-인물 GT. 각 det 의 gt=정답 사람, cls[g]=사람별 presence"},
                                ensure_ascii=False) + "\n")
            for i in range(L):
                fh.write(json.dumps({"t": frames[i]["t"], "frame": i + 1,
                                     "cls": cls[i], "positions": positions[i],
                                     "dets": frames[i]["dets"]}, ensure_ascii=False) + "\n")
        print(f"[GT] 병합 저장: {gt_out}")
    return gt
