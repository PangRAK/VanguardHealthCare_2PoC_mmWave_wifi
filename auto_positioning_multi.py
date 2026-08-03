#!/usr/bin/env python3
"""
다중 로그 기반 자동 포지셔닝 (v2) — 여러 debug 로그로 센서 배치를 한꺼번에 최적화
================================================================================

run_auto_positioning.sh(라이브 1회 측정) 와 목적은 같다: 각 센서가 방 안에서 어떻게
설치돼 있는지(x, y + 설치자세 Yaw·Pitch·Roll·flip)를 추정해 epl_config.json 에 저장.
다른 점은 '지금 걸어다니며 1회 측정'이 아니라, 이미 기록해 둔 debug 로그 N개를 한꺼번에
활용한다는 것. (run_optimization.sh 가 여러 로그를 겹쳐 최적화하는 것과 같은 발상)

전제(중요)
----------
  · 모든 로그는 '동일한 물리적 센서 배치'에서 기록됨(로그 사이 센서를 옮기지 않음).
  · 각 로그에는 '한 사람'만 등장(run_optimization.sh 용 단일-인물 로그와 동일 — 같은 폴더 재사용 가능).

원리
----
  센서 배치는 로그가 달라도 고정이므로, 두 센서가 같은 사람을 '동시에' 본 대응점
  (센서A local ↔ 센서B local)은 어느 로그에서든 '같은' 쌍별 affine 을 따른다.
  따라서 각 로그를 프레임(시간정렬 스냅샷)으로 만들고 그 프레임들을 전부 이어붙이면(pool),
  단일 측정보다 훨씬 많은 대응점으로 같은 최소제곱(번들조정)을 풀 수 있어 추정이 안정적이다.
  · 추정 알고리즘(estimate_positions)·저장(apply_to_config)·리포트(print_report)는
    auto_positioning 의 것을 '그대로' 재사용한다 → 라이브판과 동일 알고리즘.
  · 이 파일은 오직 '여러 로그(JSONL) → 프레임 풀' 조립만 담당한다.

입력(기록 JSONL) — run_debug_gui.sh 가 남긴 형식:
  · 헤더줄  {_type:"header", sensors:[{id,name,x,y,heading_deg,...}], fuse_hz, ...}
  · 프레임줄 {t, raw:[{sid, targets:[{tid, lx, ly, rx, ry, ...}]}], dets, tracks}
  auto-positioning 은 센서 로컬좌표(lx/ly)만 쓴다. lx/ly 는 캘리브레이션 적용 '전' 순수
  센서 출력이라, 기록 당시 epl_config 값이 틀렸어도 결과에 영향을 주지 않는다(순수 재추정).

사용법
    python auto_positioning_multi.py --logs-dir debug_logs/logs_for_optimization
    python auto_positioning_multi.py --gt-logs a.jsonl b.jsonl c.jsonl
    python auto_positioning_multi.py --logs-dir ... --dry-run
    python auto_positioning_multi.py --ref 98bd80
    python auto_positioning_multi.py --camera-id 7     # 그 카메라 센서만 캘리브레이션(기본 1)
    python auto_positioning_multi.py --selftest        # 합성 다중로그로 검증(하드웨어/파일 불필요)

★ 카메라마다 따로 돌려야 한다 — x/y/heading 은 '방 좌표계' 값이고 **카메라마다 별도의 방
  좌표계**라, 서로 다른 카메라의 센서를 한 번에 풀링하면 두 좌표계를 억지로 하나에 맞춘
  배치가 저장된다. --camera-id 는 로그 헤더의 센서 중 id 접두가 그 카메라를 가리키는 것만
  남긴다(로그가 섞여 있어도 안전).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys

try:
    import numpy as np  # noqa: F401  (auto_positioning 가 요구)
except ImportError:  # pragma: no cover
    print("❌ numpy 가 필요합니다.  ./run_auto_positioning_v2.sh 사용")
    sys.exit(1)

# 추정 파이프라인 전체를 재사용(라이브판과 100% 동일 알고리즘)
from auto_positioning import (
    resample_to_frames, estimate_positions, apply_to_config, print_report,
)
from epl_config import (
    CONFIG_PATH, DEFAULT_CAMERA_ID, DEFAULT_ORGANIZATION, get_camera_ids,
    get_sensors_for_camera, load_config, assert_camera_id_registerable,
)


# ============================================================================
# 로그(JSONL) 로딩 + 센서별 '한 사람' 로컬궤적 추출
# ============================================================================
def load_log(path):
    """기록 JSONL 1개 → (header|None, records[]).
    header = _type=='header' 첫 줄, records = raw 를 가진 프레임 줄들."""
    header, records = None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("_type") == "header":
                header = obj
            elif "raw" in obj:
                records.append(obj)
    return header, records


def series_from_records(records, ids):
    """기록 프레임들 → series[sid] = [(t, lx, ly), ...]  (센서별 '한 사람' 로컬 궤적).

    collect_series(라이브판) 와 동일한 '최근접-이전점' 추적으로, 슬롯(tid) 재배정이나 잡음
    타겟이 섞여도 한 사람을 안정적으로 골라 잇는다. 관측이 끊기면 last 를 리셋해 다른
    타겟으로 점프-매칭되는 것을 막는다.
    (라이브판의 age_ms<500ms 신선도 필터는 기록에 age 가 없어 재현 불가하나, 사라진 타겟은
     x=y=0 → present=False 로 애초에 기록되지 않아 결과에 유의미한 차이는 없다.)"""
    series = {sid: [] for sid in ids}
    last = {sid: None for sid in ids}
    for rec in records:
        t = rec.get("t")
        seen = set()
        for s in rec.get("raw", []):
            sid = s.get("sid")
            if sid not in series:
                continue
            seen.add(sid)
            cands = [(tt.get("lx"), tt.get("ly")) for tt in s.get("targets", [])
                     if tt.get("lx") is not None and tt.get("ly") is not None]
            if not cands:
                last[sid] = None
                continue
            lp = last[sid]
            if lp is None:
                cx, cy = cands[0]
            else:
                cx, cy = min(cands, key=lambda p: (p[0] - lp[0]) ** 2 + (p[1] - lp[1]) ** 2)
            series[sid].append((t, cx, cy))
            last[sid] = (cx, cy)
        for sid in ids:               # 이 프레임에 아예 안 나온 센서도 연속성 끊김 처리
            if sid not in seen:
                last[sid] = None
    return series


def collect_frames_from_logs(paths, *, hz=15.0, max_gap_ms=700.0, verbose=True, only_ids=None):
    """로그 N개 → (all_frames, ids, names, sensors_meta, per_log_info).

    각 로그를 개별적으로 프레임화(resample_to_frames)한 뒤 프레임 리스트를 이어붙인다.
    프레임은 각자 '한 시점의 동시관측 스냅샷'이라 로그 간 시각 불일치와 무관하게 풀링 가능.

    only_ids: 이 센서 id 집합만 사용한다(카메라 필터). None 이면 로그에 나온 전 센서.
      로그에 다른 카메라 센서가 섞여 있어도 여기서 걸러지므로, 서로 다른 방 좌표계가
      섞이지 않는다."""
    loaded = []
    for p in paths:
        try:
            header, records = load_log(p)
        except OSError as e:
            print(f"  ⚠  열기 실패, 건너뜀: {p} ({e})")
            continue
        if header is None:
            print(f"  ⚠  헤더 없음(형식 불명), 건너뜀: {os.path.basename(p)}")
            continue
        loaded.append((p, header, records))

    def _in_camera(sid):
        return only_ids is None or sid in only_ids

    # 센서 id 합집합(첫 등장 순) + 이름/메타 — 고정 배치 전제라 로그마다 같아야 정상
    ids, names, sensors_meta, excluded = [], {}, {}, []
    for _p, header, _r in loaded:
        for s in header.get("sensors", []):
            sid = s.get("id")
            if sid is None:
                continue
            if not _in_camera(sid):
                if sid not in excluded:
                    excluded.append(sid)
                continue
            if sid not in ids:
                ids.append(sid)
            names.setdefault(sid, s.get("name") or sid)
            sensors_meta.setdefault(sid, {"name": s.get("name", ""), "host": "",
                                          "node_name": "", "color": ""})
    if excluded and verbose:
        print(f"  ℹ  다른 카메라 센서 {len(excluded)}개 제외: {', '.join(excluded)}")

    # 로그별 센서 구성이 기준과 다르면 경고(센서를 옮겼거나 다른 카메라 로그 섞임 의심)
    base_set = None
    all_frames, per_log = [], []
    for p, header, records in loaded:
        log_ids = [s.get("id") for s in header.get("sensors", [])
                   if s.get("id") is not None and _in_camera(s.get("id"))]
        if not log_ids:
            # 다른 카메라 센서만 담긴 로그다 — 기준집합으로 삼으면 이후 로그가 전부 '불일치'로
            # 헛경고를 낸다. 표본 0으로 지나가되 무엇을 건너뛰는지 알린다.
            if verbose:
                print(f"  ℹ  {os.path.basename(p)}: 이 카메라 센서가 없어 건너뜁니다")
            continue
        if base_set is None:
            base_set = set(log_ids)
        elif set(log_ids) != base_set:
            print(f"  ⚠  센서 구성 불일치: {os.path.basename(p)} = {sorted(log_ids)} "
                  f"(기준 {sorted(base_set)}) — 같은 배치의 로그만 쓰세요")
        series = series_from_records(records, ids)
        frames = resample_to_frames(series, ids, hz=hz, max_gap_ms=max_gap_ms)
        all_frames.extend(frames)
        smp = {sid: len(series.get(sid, [])) for sid in ids}
        per_log.append((os.path.basename(p), len(records), len(frames), smp))
        if verbose:
            cnt = "  ".join(f"{names.get(sid, sid)}:{smp[sid]}" for sid in ids)
            print(f"  · {os.path.basename(p)}: 원시프레임 {len(records)} → 프레임 {len(frames)} · 표본 {cnt}")
    return all_frames, ids, names, sensors_meta, per_log


# ============================================================================
# 합성 자체검증 — 여러 짧은 로그를 풀링하면 배치를 복원한다(하드웨어/실제 파일 불필요)
# ============================================================================
def _synth_records(gts, *, n, seed, dt=0.06, noise=25.0, amp_x=1500.0, amp_y=1200.0):
    """설치자세 gts 인 센서들이 '한 사람'의 곡선 보행을 관측한 기록 프레임 목록(recorded 형식)."""
    import random as _random
    from auto_positioning import _synth_local
    rng = _random.Random(seed)
    recs = []
    for kk in range(n):
        frac = kk / max(n, 1)
        room = (2500.0 + amp_x * math.sin(2 * math.pi * frac * 1.5 + 0.6 * seed),
                2800.0 + amp_y * math.sin(2 * math.pi * frac * 0.9 + 0.7 + 0.6 * seed))
        raw = []
        for sid, gt in gts.items():
            targets = []
            loc = _synth_local(gt, room)
            if loc is not None:
                targets.append({"tid": 1,
                                "lx": loc[0] + rng.gauss(0, noise),
                                "ly": loc[1] + rng.gauss(0, noise),
                                "rx": 0.0, "ry": 0.0, "speed": 0.0,
                                "angle": 0.0, "dist": 0.0, "res": 0})
            raw.append({"sid": sid, "targets": targets})
        recs.append({"t": kk * dt, "raw": raw})
    return recs


def _write_log(path, gts, recs):
    with open(path, "w", encoding="utf-8") as fh:
        header = {"_type": "header", "fuse_hz": 15.0,
                  "sensors": [{"id": sid, "name": sid, "x": 0, "y": 0, "heading_deg": 0,
                               "pitch_deg": 0, "roll_deg": 0, "flip": False,
                               "fov_deg": 60, "range_mm": 6000} for sid in gts]}
        fh.write(json.dumps(header) + "\n")
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def selftest():
    import tempfile
    print("=== 다중 로그 자동 포지셔닝 자체검증 ===")
    ok_all = True
    # 기준 s1 = 수평·정면·원점 → 복원값을 GT 절대값과 직접 비교 가능
    GT = {"s1": {"x": 0, "y": 0, "yaw": 0, "pitch": 0, "roll": 0},
          "s2": {"x": 5000, "y": 0, "yaw": 40},
          "s3": {"x": 2500, "y": 5600, "yaw": 180}}

    tmp = tempfile.mkdtemp(prefix="apos_multi_")
    paths = []
    for li in range(3):                       # 서로 다른 위상의 3개 '짧은' 로그
        recs = _synth_records(GT, n=90, seed=li)
        f = os.path.join(tmp, f"log{li}.jsonl")
        _write_log(f, GT, recs)
        paths.append(f)

    # (1) 파일 파싱 왕복: load_log → series → frames
    frames_all, ids, names, smeta, per_log = collect_frames_from_logs(
        paths, hz=15.0, max_gap_ms=300.0, verbose=False)
    p1 = set(ids) == {"s1", "s2", "s3"} and len(frames_all) > 0
    print(f"  [{'PASS' if p1 else 'FAIL'}] 로그 파싱/프레임화 (ids={ids}, pool frames={len(frames_all)})")
    ok_all &= p1

    # (2) 풀링 추정이 GT 를 복원하는가
    res = estimate_positions(frames_all, ids, min_overlap=20, ref_id="s1")
    fails = []
    if res["ref"] is None:
        fails.append("ref=None")
    for sid, gt in GT.items():
        p = res["placements"].get(sid, {})
        if not p.get("anchored"):
            fails.append(f"{sid} 미앵커"); continue
        dpos = math.hypot(p["x"] - gt["x"], p["y"] - gt["y"])
        dyaw = abs(((p["heading_deg"] - gt["yaw"]) + 180) % 360 - 180)
        if dpos >= 400:
            fails.append(f"{sid} pos {dpos:.0f}mm")
        if dyaw >= 6:
            fails.append(f"{sid} yaw {dyaw:.1f}°")
    p2 = not fails
    print(f"  [{'PASS' if p2 else 'FAIL'}] 풀링(3로그) GT 복원  rms={res['global_rms']:.0f}mm"
          + ("" if p2 else "  ← " + "; ".join(fails)))
    ok_all &= p2

    # (3) 가설 검증(핵심): '여러 로그 풀링'이 '1개 로그'보다 배치를 더 정확히 복원한다.
    #     지표는 fit 잔차(rms)가 아니라 GT 대비 '정확도'(위치오차) — rms 는 표본이 많을수록
    #     노이즈 바닥으로 수렴할 뿐 정확도가 아니다. 노이즈를 키운 로그 3개로,
    #     풀링 오차 ≤ 개별 로그 오차 평균 임을 보인다(노이즈 평균화 효과, 시드 고정=결정적).
    def _pos_err(r):
        e = 0.0
        for sid, gt in GT.items():
            p = r["placements"].get(sid, {})
            if not p.get("anchored"):
                return float("inf")
            e = max(e, math.hypot(p["x"] - gt["x"], p["y"] - gt["y"]))
        return e

    npaths = []
    for li in range(3):
        recs = _synth_records(GT, n=60, seed=li, noise=60.0)
        f = os.path.join(tmp, f"noisy{li}.jsonl")
        _write_log(f, GT, recs)
        npaths.append(f)
    fp, idp, *_ = collect_frames_from_logs(npaths, hz=15.0, max_gap_ms=300.0, verbose=False)
    res_pool = estimate_positions(fp, idp, min_overlap=20, ref_id="s1")
    singles = []
    for f in npaths:
        fs, ids1, *_ = collect_frames_from_logs([f], hz=15.0, max_gap_ms=300.0, verbose=False)
        singles.append(estimate_positions(fs, ids1, min_overlap=20, ref_id="s1"))
    e_pool = _pos_err(res_pool)
    e_singles = [_pos_err(s) for s in singles]
    e_mean = sum(e_singles) / len(e_singles)
    p3 = (len(fp) > max(len(collect_frames_from_logs([f], hz=15.0, max_gap_ms=300.0,
                                                     verbose=False)[0]) for f in npaths)) \
        and (e_pool <= e_mean)
    print(f"  [{'PASS' if p3 else 'FAIL'}] 다중>단일 (정확도): 풀링 위치오차 {e_pool:.0f}mm "
          f"≤ 개별 평균 {e_mean:.0f}mm  (개별={[round(x) for x in e_singles]})")
    ok_all &= p3

    # 정리
    for f in paths + npaths:
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    print("=== 결과:", "전부 PASS ✅" if ok_all else "일부 FAIL ❌", "===")
    return 0 if ok_all else 1


# ============================================================================
# CLI
# ============================================================================
def _resolve_paths(args):
    if args.gt_logs:                                   # 명시 파일이 최우선
        return list(args.gt_logs)
    if args.logs_dir:
        return sorted(glob.glob(os.path.join(args.logs_dir, "*.jsonl")))
    return []


def main() -> int:
    ap = argparse.ArgumentParser(
        description="다중 로그 기반 자동 포지셔닝(위치 + Yaw·Pitch·Roll) — 여러 로그를 풀링")
    ap.add_argument("--logs-dir", default=None, help="이 폴더의 모든 *.jsonl 을 사용")
    ap.add_argument("--gt-logs", nargs="+", default=None, help="특정 로그 파일들(폴더보다 우선)")
    ap.add_argument("--hz", type=float, default=15.0, help="프레임화 주파수(Hz)")
    ap.add_argument("--max-gap-ms", type=float, default=700.0, help="보간 허용 최대 공백(ms)")
    ap.add_argument("--min-overlap", type=int, default=20, help="센서쌍 최소 동시관측 표본")
    ap.add_argument("--inlier-mm", type=float, default=300.0, help="RANSAC 인라이어 임계(mm)")
    ap.add_argument("--ref", default=None, help="기준센서 id(수평 설치로 아는 센서). 미지정 시 자동선택.")
    ap.add_argument("--no-refine", action="store_true", help="번들조정 생략(쌍 affine 만)")
    ap.add_argument("--dry-run", action="store_true", help="계산만, epl_config.json 저장 안 함")
    ap.add_argument("--camera-id", default=DEFAULT_CAMERA_ID,
                    help=f"캘리브레이션할 카메라(stream) 식별자 (기본 {DEFAULT_CAMERA_ID}). "
                         "빈 문자열이면 카메라 구분 없이 로그의 전 센서를 쓴다.")
    ap.add_argument("--organization", default=DEFAULT_ORGANIZATION,
                    help=f"센서 id 의 organization 파트 (기본 {DEFAULT_ORGANIZATION})")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # 카메라 필터 — 서로 다른 방 좌표계가 섞이지 않게 그 카메라 센서만 남긴다.
    # ★ 아래 지역변수 camera_id 는 '그룹 키' 다. 이 파일의 다른 곳(_synth_local 계열)에 있는
    #   지역변수 `room` 은 **방 좌표 튜플(mm)** 이므로 이름이 겹치지 않게 구분해 둔다.
    camera_id = str(args.camera_id or "").strip()
    organization = str(args.organization or "").strip() or DEFAULT_ORGANIZATION
    only_ids = None
    if not camera_id:
        # ★ 카메라를 지정하지 않으면 로그의 전 센서를 한 좌표계로 맞춘다. 설정에 카메라가
        #   2개 이상이면 그건 **다른 카메라의 캘리브레이션을 덮어쓰는** 것이므로 거절한다
        #   (경고만 하면 그 방 좌표가 조용히 망가져 재실 판정이 어긋난다).
        try:
            defined = get_camera_ids(load_config())
        except ValueError as e:
            print(f"❌ 센서 설정 오류: {e}")
            print(f"   파일: {CONFIG_PATH}")
            return 2
        if len(defined) > 1:
            print(f"❌ 설정에 카메라가 {len(defined)}개({', '.join(defined)}) 인데 카메라를 지정하지 "
                  "않았습니다.")
            print("   카메라마다 별도의 방 좌표계라 한 번에 캘리브레이션하면 다른 카메라의"
                  " 방 좌표를 덮어씁니다.")
            print("   → --camera-id <id> 또는 스크립트의 CAMERA_ID 를 지정해 카메라마다"
                  " 따로 실행하세요.")
            return 2
    if camera_id:
        # 제품이 등록할 수 없는 cameraId 로 캘리브레이션하면 존재할 수 없는 카메라의 방
        # 좌표를 맞추는 셈이다 — '센서 0개' 로 흘리지 않고 이유를 먼저 알린다.
        try:
            assert_camera_id_registerable(camera_id, source="--camera-id")
        except ValueError as e:
            print(f"❌ {e}")
            return 2
        cfg = load_config()
        try:
            camera_sensors = get_sensors_for_camera(cfg, organization, camera_id)
        except ValueError as e:      # 옛 rooms 스키마 / id 형식·중복 오류
            print(f"❌ 센서 설정 오류: {e}")
            print(f"   파일: {CONFIG_PATH}")
            return 2
        if len(camera_sensors) < 2:
            print(f"❌ 카메라 '{organization}-{camera_id}' 에 묶인 센서가 "
                  f"{len(camera_sensors)}개입니다 (최소 2개 필요).")
            print(f"   설정의 cameraId: {', '.join(get_camera_ids(cfg, organization)) or '(없음)'}"
                  f"   ·  파일: {CONFIG_PATH}")
            print("   → ./run_provision.sh 의 CAMERA_ID 로 센서를 등록하거나,"
                  " epl_config.json 의 센서 \"id\" 접두를 확인하세요.")
            return 2
        only_ids = {s["id"] for s in camera_sensors}
        print(f"🎥 카메라: {organization}-{camera_id}  ·  센서 {len(camera_sensors)}개 — "
              f"{', '.join(s['name'] for s in camera_sensors)}")

    paths = _resolve_paths(args)
    if not paths:
        print("❌ 로그가 없습니다. --logs-dir <폴더> 에 .jsonl 을 넣거나 --gt-logs <파일…> 로 지정하세요.")
        return 2
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        print("❌ 다음 로그를 찾을 수 없습니다:")
        for m in missing:
            print(f"    - {m}")
        return 2

    print(f"[multi-apos] 로그 {len(paths)}개 풀링:")
    frames, ids, names, sensors_meta, per_log = collect_frames_from_logs(
        paths, hz=args.hz, max_gap_ms=args.max_gap_ms, only_ids=only_ids)
    if len(ids) < 2:
        print(f"❌ 최소 2개 센서 필요(로그 헤더에서 발견: {len(ids)}개)")
        if only_ids:
            print(f"   카메라 '{camera_id}' 센서: {', '.join(sorted(only_ids))}"
                  " — 이 센서들이 담긴 로그가 필요합니다(다른 카메라 로그를 지정했는지 확인).")
        return 2
    if only_ids:
        # ★ 그 카메라 센서 일부만 로그에 있으면, 저장되는 좌표계는 '로그에 있는 센서들'
        #   기준이다. 빠진 센서는 옛 좌표계 값을 유지하므로 같은 방 안에서 좌표계가 섞인다
        #   → 융합이 어긋난다. 조용히 두지 않고 명시적으로 알린다.
        missing = sorted(only_ids - set(ids))
        if missing:
            print(f"  ⚠  카메라 '{camera_id}' 센서 중 {len(missing)}개가 로그에 없습니다: "
                  f"{', '.join(missing)}")
            print("     이 센서들의 x/y/heading 은 예전 값으로 남습니다 → 같은 방 안에서 "
                  "좌표계가 어긋날 수 있습니다.")
            print("     그 카메라 센서 전부가 함께 기록된 로그로 다시 돌리는 것을 권합니다.")
    print(f"  → 총 프레임(pool): {len(frames)}  ·  센서 {len(ids)}개")

    result = estimate_positions(frames, ids, min_overlap=args.min_overlap,
                                inlier_mm=args.inlier_mm, ref_id=args.ref,
                                refine=not args.no_refine)
    print_report(result, names)
    if result["warnings"]:
        print("\n  [경고]")
        for w in result["warnings"]:
            print(f"    - {w}")
    if result["ref"] is None:
        print("\n저장할 배치가 없습니다.")
        return 1

    # 저장 안전장치: 로그 헤더에는 접속주소(host/node_name)가 없다. epl_config.json 에 '이미
    # 등록된' 센서는 주소를 보존한 채 배치값만 갱신되지만, 등록 안 된 센서를 로그만으로 새로
    # 추가하면 host/node_name 이 비어 get_sensors() 가 걸러내 라이브에서 보이지 않는다(조용한
    # 유실). 그래서 미등록 앵커 센서는 저장에서 제외하고 명확히 경고한다.
    known = {s.get("id") for s in (load_config().get("sensors") or [])}
    orphan = [sid for sid, p in result["placements"].items()
              if p.get("anchored") and sid not in known]
    if orphan:
        print("\n  ⚠  epl_config.json 에 등록되지 않은 센서는 저장에서 제외합니다"
              " (로그엔 접속주소가 없어 저장해도 라이브 화면에서 보이지 않음):")
        for sid in orphan:
            print(f"      - {names.get(sid, sid)} ({sid}) → 먼저 ./run_provision.sh 로 등록 후 다시 실행")
        result = {**result, "placements": {k: v for k, v in result["placements"].items()
                                           if k not in orphan}}
        if not any(p.get("anchored") for p in result["placements"].values()):
            print("\n  저장할(등록된) 센서가 없습니다.")
            return 1

    info = apply_to_config(result, sensors_meta, dry_run=args.dry_run)
    chg = info["updated"] + info["added"]
    if info["written"]:
        print(f"\n💾 저장 완료: {info['path']}  (백업: {info['path']}.bak)")
        print(f"   갱신/추가: {', '.join(names.get(s, s) for s in chg) or '없음'}")
        if info["skipped"]:
            print(f"   유지(미고정): {', '.join(names.get(s, s) for s in info['skipped'])}")
        print("\n다음: ./run_gui.sh 로 오버레이 확인.")
    else:
        print(f"\n(dry-run) 저장 안 함. 갱신 대상: {', '.join(names.get(s, s) for s in chg) or '없음'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
