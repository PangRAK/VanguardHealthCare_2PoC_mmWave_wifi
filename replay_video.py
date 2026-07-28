#!/usr/bin/env python3
"""
최적화 전(base) / 후(best) 융합 결과를 좌우 비교 MP4 로 저장
================================================================================

단일-인물 debug 로그 N개를 겹친 다중-인물 GT(merge_gt) 의 dets 스트림을, 두 벌의
하이퍼파라미터로 각각 replay 해서:
  · 왼쪽  = 최적화 '전' (base = 기록 당시 HP)
  · 오른쪽 = 최적화 '후' (best = optimize_fusion 이 찾은 HP, best_params.sh)
동일한 레이더 화면(gui_qt.RadarPlot)에 프레임별로 그려 grab → 시스템 ffmpeg 로 H.264 MP4 저장.

렌더는 기존 GUI 컴포넌트를 그대로 재사용하고, 인코딩만 ffmpeg(rawvideo stdin)로 처리한다
(파이썬 외부 인코더 의존성 없음). ffmpeg 가 없으면 안내 후 종료.

용법:  python replay_video.py --gt-logs a.jsonl b.jsonl [c.jsonl] --best-params debug_logs/best_params.sh
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys

from fusion import NAME2KW, replay_frames
from merge_gt import build_gt
from mmwave_reader import room_transform
from optimize_fusion import cast

PALETTE = ["#27e0c8", "#ff6b9d", "#ffb454", "#4ea3ff", "#b18cff", "#5be37a"]


def load_params(path):
    """best_params(YAML 'KEY: v' 또는 sh 'KEY=v'; 주석/빈줄 무시) → FusionTracker kwargs dict."""
    p = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:                       # sh 형식(하위호환)
            k, v = line.split("=", 1)
        elif ":" in line:                     # YAML 플랫 형식
            k, v = line.split(":", 1)
        else:
            continue
        k = k.strip().upper()
        v = v.split("#", 1)[0].strip()        # 인라인 주석 제거
        if k in NAME2KW:
            p[NAME2KW[k]] = cast(v)
    return p


def replay_full(frames, params):
    """dets 스트림을 재생 → 프레임별 confirmed 트랙 전체 dict 목록(렌더용).
    공용 드라이버 fusion.replay_frames 사용(optimize 와 동일한 step → 채점과 영상이 같은 결과)."""
    return replay_frames(frames, params, confirmed_only=True)


def build_sensors(gt):
    out = []
    for i, s in enumerate(gt["sensors"]):
        m = dict(s)
        m.setdefault("color", PALETTE[i % len(PALETTE)])
        m.setdefault("fov_deg", 60)
        m.setdefault("range_mm", 6000)
        m.setdefault("name", s.get("id", f"S{i+1}"))
        m.setdefault("targets", [])   # RadarPlot 이 원시점/링크 조회에 참조(영상은 빈 목록)
        out.append(m)
    return out


def _sensor_bounds(meta):
    """센서 FOV 부채꼴을 방 좌표로 변환해 (xmin,xmax,ymin,ymax)."""
    R = meta.get("range_mm", 6000); F = meta.get("fov_deg", 60)
    xs, ys = [], []
    d = -F
    while d <= F:
        rx, ry = room_transform(meta, R * math.sin(math.radians(d)), R * math.cos(math.radians(d)))
        xs.append(rx); ys.append(ry); d += 6
    rx, ry = room_transform(meta, 0.0, 0.0)
    xs.append(rx); ys.append(ry)
    return min(xs), max(xs), min(ys), max(ys)


def make_snap(sensors, world, tracks, noise_radius, ts):
    return {"ts": ts, "sensors": sensors, "world": world, "tracks": tracks,
            "config": {"noise_radius_mm": noise_radius}}


def main():
    ap = argparse.ArgumentParser(description="최적화 전/후 좌우 비교 MP4 저장")
    ap.add_argument("--gt-logs", nargs="+", required=True, help="단일-인물 로그 N개(≥2)")
    ap.add_argument("--best-params", required=True, help="best_params.yaml/.sh (오른쪽=최적화 후)")
    ap.add_argument("--out", default="debug_logs/compare.mp4")
    ap.add_argument("--fps", type=float, default=None, help="영상 fps(기본=fuse_hz)")
    ap.add_argument("--frame-stride", type=int, default=1, help="N프레임마다 1장 렌더(↑빠름·거침)")
    ap.add_argument("--margin-deg", type=float, default=55.0)
    ap.add_argument("--margin-mm", type=float, default=5000.0)
    ap.add_argument("--collision-mm", type=float, default=700.0,
                    help="센서 병합 모사 반경(mm) — 최적화와 같은 값을 줘야 영상이 실제 입력과 일치")
    ap.add_argument("--no-collision-merge", dest="merge_collisions", action="store_false",
                    default=True, help="센서 병합 모사 끄기(겹쳐도 점 2개 유지)")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=860)
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg 가 없어 MP4 저장 불가. (예: brew install ffmpeg)"); return 1
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)          # 분석 폴더 등 없으면 생성

    gt = build_gt(args.gt_logs, margin_deg=args.margin_deg, margin_mm=args.margin_mm,
                  collision_mm=args.collision_mm, merge_collisions=args.merge_collisions)
    base = dict(gt["base_hp"]); base.pop("fuse_hz", None)
    best = load_params(args.best_params)
    fps = args.fps or gt.get("fuse_hz") or 15.0
    W = args.width - (args.width % 2); H = args.height - (args.height % 2)   # libx264: 짝수

    print(f"[video] replay: base(전) / best(후) · {gt['length']}프레임 · {fps}fps")
    tracks_base = replay_full(gt["frames"], base)
    tracks_best = replay_full(gt["frames"], best)

    # Qt 는 오프스크린으로 (pyqtgraph/gui_qt import 전에 설정)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pyqtgraph.Qt import QtWidgets, QtGui, QtCore
    from gui_qt import RadarPlot, C_BG, C_TXT, C_PANEL, C_LINE

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    sensors = build_sensors(gt)
    xs, ys = [], []
    for s in sensors:
        a, b, c, d = _sensor_bounds(s); xs += [a, b]; ys += [c, d]
    world = {"x_min": min(xs), "x_max": max(xs), "y_min": min(ys), "y_max": max(ys)}
    nr_base = float(base.get("noise_radius_mm", 0) or 0)
    nr_best = float(best.get("noise_radius_mm", 0) or 0)

    def column(text):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
        lab = QtWidgets.QLabel(text); lab.setAlignment(QtCore.Qt.AlignCenter)
        lab.setStyleSheet(f"color:{C_TXT};background:{C_PANEL};font-size:16px;"
                          f"font-weight:bold;padding:7px;border-bottom:1px solid {C_LINE}")
        plot = RadarPlot(); plot.show_raw = False; plot.show_links = False
        v.addWidget(lab); v.addWidget(plot, 1)
        return w, plot

    root = QtWidgets.QWidget(); root.resize(W, H)
    root.setStyleSheet(f"background:{C_BG};")
    row = QtWidgets.QHBoxLayout(root); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(2)
    lw, left = column("◀ Before optimization (base HP)")
    rw, right = column("After optimization (best HP) ▶")
    row.addWidget(lw); row.addWidget(rw)
    root.show(); app.processEvents()

    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pixel_format", "rgba",
         "-video_size", f"{W}x{H}", "-framerate", str(fps), "-i", "-",
         "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
         "-loglevel", "error", args.out],
        stdin=subprocess.PIPE)

    n = len(gt["frames"])
    written = 0
    try:
        for i in range(0, n, max(1, args.frame_stride)):
            ts = gt["frames"][i]["t"]
            left.render_snapshot(make_snap(sensors, world, tracks_base[i], nr_base, ts))
            right.render_snapshot(make_snap(sensors, world, tracks_best[i], nr_best, ts))
            app.processEvents()
            img = root.grab().toImage().convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            if img.width() != W or img.height() != H:
                img = img.scaled(W, H)
            proc.stdin.write(bytes(img.constBits()))
            written += 1
            if written % 100 == 0:
                print(f"  … {written} 프레임")
    finally:
        proc.stdin.close(); proc.wait()
    print(f"[video] 저장: {args.out}  ({written}프레임 @ {fps}fps ≈ {written/fps:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
