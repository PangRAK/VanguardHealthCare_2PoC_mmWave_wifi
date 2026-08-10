#!/usr/bin/env python3
"""
기록 로그를 융합 재생해 MP4 로 저장 (단일 재생 / 최적화 전·후 비교)
================================================================================

로그 N개(≥2)면 merge_gt 로 겹쳐 다중-인물 GT 를, 1개면 그 로그 그대로 장면을 만들고,
그 dets 스트림을 하이퍼파라미터로 replay 해 레이더 화면(gui_qt.RadarPlot)에 프레임별로
그려 grab → 시스템 ffmpeg 로 H.264 MP4 저장.

두 가지 모드 (--best-params 유무로 결정):
  · 단일 재생 (기본, run_replay.sh)      : 한 화면. HP = --params 파일 → --set 로 덮어씀
                                           (둘 다 없으면 기록 당시 HP). 최적화 없이 '지금 세팅' 확인용
  · 좌우 비교 (--best-params, run_optimization.sh) : 왼쪽 = 최적화 '전'(base = 기록 당시 HP),
                                           오른쪽 = 최적화 '후'(best = optimize_fusion 결과)

장기체류 경보(--dwell-alert-sec, 기본 300초 = run_gui.sh 와 같은 규칙)
  한 ID 의 dwell_sec 가 임계를 넘으면 라이브 GUI 와 똑같이 ① 화면 빨간 테두리 ② 그 사람 빨간
  마커(RadarPlot.alert_ids) ③ 알림음 — 을 재현한다. 영상은 실시간이 아니므로 ③은 그 시각에
  '삐' 소리를 넣은 오디오 트랙으로 MP4 에 굽는다(gui_qt 와 같은 재알림 간격 5초). 0 = 끔.

렌더는 기존 GUI 컴포넌트를 그대로 재사용하고, 인코딩만 ffmpeg(rawvideo stdin)로 처리한다
(파이썬 외부 인코더 의존성 없음). ffmpeg 가 없으면 안내 후 종료.

용법:
  python replay_video.py --logs a.jsonl b.jsonl --params debug_logs/best_params.yaml
  python replay_video.py --gt-logs a.jsonl b.jsonl --best-params debug_logs/best_params.yaml
"""
from __future__ import annotations

import argparse
import array
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave

from fusion import NAME2KW, replay_frames
from merge_gt import build_gt
from mmwave_reader import room_transform
from optimize_fusion import cast, describe_resample, load_log, resample_frames

PALETTE = ["#27e0c8", "#ff6b9d", "#ffb454", "#4ea3ff", "#b18cff", "#5be37a"]
BEEP_HZ = 880.0        # 알림음 주파수(Hz) — 시스템 beep 대용(영상엔 오디오 트랙으로 굽는다)
BEEP_SEC = 0.18        # 알림음 1회 길이(초)
BEEP_AMP = 0.35        # 알림음 진폭(0~1)


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


def apply_sets(params, sets):
    """--set NAME=VAL 목록을 params(FusionTracker kwargs) 에 덮어씀. 모르는 이름은 안내 후 무시."""
    for kv in sets or []:
        if "=" not in kv:
            print(f"[warn] --set 형식은 NAME=VALUE 입니다: {kv}"); continue
        k, v = kv.split("=", 1)
        k = k.strip().upper()
        if k not in NAME2KW:
            print(f"[warn] 모르는 파라미터 이름 무시: {k}"); continue
        params[NAME2KW[k]] = cast(v)
    return params


def build_single(path, *, interval_sec):
    """로그 1개 → build_gt 와 같은 모양의 장면 dict(겹치기 없이 기록 그대로 재생).
    ≥2개일 때의 다중-인물 합성(merge_gt.build_gt)과 달리 GT 채점용 필드(cls/positions/tid2gt)는
    없다 — 영상 렌더는 frames/sensors/step_hz 만 쓰므로 충분하다."""
    header, frames = load_log(path)
    if not frames:
        raise SystemExit(f"빈 기록: {path}")
    header = header or {}
    if interval_sec and interval_sec > 0:
        rs = resample_frames(frames, interval_sec)
        print("[scene] 재샘플 " + describe_resample(os.path.basename(path), frames, rs, interval_sec))
        frames = rs
    fz = header.get("fuse_hz")
    step_hz = (1.0 / interval_sec) if interval_sec and interval_sec > 0 else fz
    print(f"[scene] 단일 로그 재생 · {os.path.basename(path)} · {len(frames)}프레임 · fuse_hz={fz}")
    return {"n": 1, "labels": [os.path.basename(path)], "fuse_hz": fz, "length": len(frames),
            "interval_sec": (interval_sec if interval_sec and interval_sec > 0 else None),
            "step_hz": step_hz, "frames": frames,
            "base_hp": dict(header.get("hyperparams_after_record") or {}),
            "sensors": header.get("sensors", [])}


def build_scene(paths, **kw):
    """로그 1개 → 그대로 / ≥2개 → 다중-인물 합성(merge_gt). 이후 처리는 동일한 dict 모양."""
    if len(paths) == 1:
        return build_single(paths[0], interval_sec=kw["interval_sec"])
    return build_gt(paths, **kw)


def replay_full(frames, params):
    """dets 스트림을 재생 → 프레임별 confirmed 트랙 전체 dict 목록(렌더용).
    공용 드라이버 fusion.replay_frames 사용(optimize 와 동일한 step → 채점과 영상이 같은 결과)."""
    return replay_frames(frames, params, confirmed_only=True)


def alert_ids_per_frame(tracks_per_frame, thr):
    """프레임별 '장기체류 경보' 대상 트랙 id 집합.
    규칙은 라이브 GUI(gui_qt.MainWindow._alert_id_set)와 동일 — dwell_sec ≥ 임계인 confirmed 트랙.
    (replay_full 이 이미 confirmed 만 돌려주므로 여기선 dwell_sec 만 본다). thr≤0 이면 경보 끔."""
    if not thr or thr <= 0:                             # 0/음수 = 경보 끔
        return [set() for _ in tracks_per_frame]
    return [{t["id"] for t in trs if float(t.get("dwell_sec") or 0.0) >= thr}
            for trs in tracks_per_frame]


def beep_times(alert_seq, fps, period):
    """경보 활성 프레임열 → 알림음을 넣을 시각(초, 영상 타임라인) 목록.
    gui_qt.MainWindow._handle_alert 와 같은 규칙: 경보가 새로 켜지거나 새 대상이 추가되면 즉시,
    계속 켜져 있으면 period 초마다 다시 울린다(그쪽은 실시간, 여기는 영상 시각 기준)."""
    times, prev, last = [], set(), None
    for k, ids in enumerate(alert_seq):
        if not ids:
            prev, last = set(), None
            continue
        t = k / fps
        if last is None or (ids - prev) or (t - last) >= period:
            times.append(t); last = t
        prev = ids
    return times


def write_beep_wav(path, duration_sec, times, sr=44100):
    """times(초)마다 '삐' 하는 모노 16bit WAV 생성 — ffmpeg 오디오 입력용(외부 의존성 없음)."""
    n = int(duration_sec * sr) + sr // 2
    buf = array.array("h", bytes(2 * n))                # 전 구간 무음으로 시작
    blen = int(BEEP_SEC * sr)
    fade = max(1, int(0.008 * sr))                      # 클릭음 방지용 페이드 인/아웃
    for t0 in times:
        s0 = int(t0 * sr)
        for i in range(blen):
            j = s0 + i
            if j >= n:
                break
            env = min(1.0, i / fade, (blen - i) / fade)
            buf[j] = int(32767 * BEEP_AMP * env * math.sin(2 * math.pi * BEEP_HZ * i / sr))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(buf.tobytes())


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
    ap = argparse.ArgumentParser(description="기록 로그 융합 재생 MP4 저장(단일 / 최적화 전·후 비교)")
    ap.add_argument("--logs", "--gt-logs", dest="logs", nargs="+", required=True,
                    help="재생할 로그. 1개=그대로, ≥2개=단일-인물 로그를 겹쳐 다중-인물 장면")
    ap.add_argument("--best-params", default=None,
                    help="주면 '좌우 비교' 모드: 왼쪽=기록 당시 HP, 오른쪽=이 파일의 HP(최적화 후)")
    ap.add_argument("--params", default=None,
                    help="단일 재생 모드의 HP 파일(best_params.yaml/.sh). 없으면 기록 당시 HP")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="NAME=VAL",
                    help="HP 개별 지정(--params 보다 우선, 반복 가능). 예: --set MERGE_MM=0")
    ap.add_argument("--label", default=None, help="단일 재생 모드 화면 상단 제목")
    ap.add_argument("--out", default="debug_logs/replay.mp4")
    ap.add_argument("--fps", type=float, default=None,
                    help="영상 fps(기본=replay 스텝 주파수 = 1/interval-sec, 재샘플 없으면 fuse_hz)")
    ap.add_argument("--interval-sec", type=float, default=0.1,
                    help="샘플링(융합 스텝) 간격(초) — 최적화와 같은 값을 줘야 영상이 채점된 "
                         "입력과 일치. 기본 0.1. 0=재샘플 없음(기록 그대로)")
    ap.add_argument("--frame-stride", type=int, default=1, help="N프레임마다 1장 렌더(↑빠름·거침)")
    ap.add_argument("--margin-deg", type=float, default=55.0)
    ap.add_argument("--margin-mm", type=float, default=5000.0)
    ap.add_argument("--collision-mm", type=float, default=700.0,
                    help="센서 병합 모사 반경(mm) — 최적화와 같은 값을 줘야 영상이 실제 입력과 일치")
    ap.add_argument("--no-collision-merge", dest="merge_collisions", action="store_false",
                    default=True, help="센서 병합 모사 끄기(겹쳐도 점 2개 유지)")
    ap.add_argument("--width", type=int, default=None, help="기본: 비교 1600 / 단일 960")
    ap.add_argument("--height", type=int, default=860)
    ap.add_argument("--dwell-alert-sec", type=float, default=300.0,
                    help="장기체류 경보 임계(초) — 한 ID 가 이 시간 이상 체류하면 빨간 테두리+빨간 "
                         "마커+알림음. 0=끔, 기본 300(=5분, run_gui.sh 와 동일)")
    ap.add_argument("--alert-beep-period", type=float, default=5.0,
                    help="경보가 계속될 때 알림음 재생 간격(초). 기본 5(gui_qt 와 동일)")
    ap.add_argument("--no-alert-beep", dest="alert_beep", action="store_false", default=True,
                    help="알림음(오디오 트랙) 없이 화면 표시만")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg 가 없어 MP4 저장 불가. (예: brew install ffmpeg)"); return 1
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)          # 분석 폴더 등 없으면 생성

    gt = build_scene(args.logs, margin_deg=args.margin_deg, margin_mm=args.margin_mm,
                     collision_mm=args.collision_mm, merge_collisions=args.merge_collisions,
                     interval_sec=args.interval_sec)
    base = dict(gt["base_hp"]); base.pop("fuse_hz", None)   # 기록 당시 HP
    # 화면(= 패널) 구성: --best-params 있으면 전/후 2개, 없으면 '현재 세팅' 1개
    if args.best_params:
        panes = [("◀ Before optimization (base HP)", base),
                 ("After optimization (best HP) ▶", load_params(args.best_params))]
    else:
        cur = load_params(args.params) if args.params else dict(base)
        panes = [(args.label or ("Current settings" if args.params or args.sets
                                 else "Recorded HP (기록 당시 설정)"), apply_sets(cur, args.sets))]
    # 재샘플했으면 프레임 간격이 interval-sec 이므로 fps 는 step_hz(=1/interval) 여야 실시간 속도가 맞다
    fps = args.fps or gt.get("step_hz") or gt.get("fuse_hz") or 15.0
    width = args.width or (1600 if len(panes) > 1 else 960)
    W = width - (width % 2); H = args.height - (args.height % 2)   # libx264: 짝수

    print(f"[video] replay: {' / '.join(lb for lb, _ in panes)} · {gt['length']}프레임 · {fps}fps")
    pane_tracks = [replay_full(gt["frames"], hp) for _, hp in panes]

    # ── 장기체류 경보(라이브 GUI 와 같은 규칙) ───────────────────────────────────────
    thr = args.dwell_alert_sec
    stride = max(1, args.frame_stride)
    rendered = list(range(0, len(gt["frames"]), stride))          # 실제로 렌더할 프레임 인덱스
    # 경보는 '렌더된 프레임' 기준으로 계산해야 화면/소리/영상 시각이 정확히 맞는다
    pane_alerts = [alert_ids_per_frame([trs[i] for i in rendered], thr) for trs in pane_tracks]
    max_dwell = max((float(t.get("dwell_sec") or 0.0)
                     for trs in pane_tracks for fr in trs for t in fr), default=0.0)
    if thr and thr > 0:
        any_alert = [set().union(*ids) for ids in zip(*pane_alerts)]
        n_alert_fr = sum(1 for s in any_alert if s)
        beeps = beep_times(any_alert, fps, args.alert_beep_period) if args.alert_beep else []
        print(f"[alert] 장기체류 경보 임계 {thr:g}s · 경보 프레임 {n_alert_fr}/{len(rendered)} "
              f"({n_alert_fr / fps:.1f}s) · 알림음 {len(beeps)}회 · 최대 체류 {max_dwell:.1f}s")
        if not n_alert_fr:
            print(f"       ⚠ 이 기록의 최대 체류가 {max_dwell:.1f}s 라 임계 {thr:g}s 를 넘지 않습니다 "
                  f"— 경보를 보려면 임계를 낮추세요(run_replay.sh 의 DWELL_ALERT_SEC, "
                  f"또는 --dwell-alert-sec {max(10, int(max_dwell * 0.5))}).")
    else:
        any_alert = [set() for _ in rendered]
        beeps = []
        print(f"[alert] 장기체류 경보 꺼짐(--dwell-alert-sec 0) · 이 기록의 최대 체류 {max_dwell:.1f}s")

    # Qt 는 오프스크린으로 (pyqtgraph/gui_qt import 전에 설정)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pyqtgraph.Qt import QtWidgets, QtGui, QtCore
    from gui_qt import RadarPlot, C_BG, C_TXT, C_PANEL, C_LINE, C_ALERT, _fmt_dwell

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    sensors = build_sensors(gt)
    xs, ys = [], []
    for s in sensors:
        a, b, c, d = _sensor_bounds(s); xs += [a, b]; ys += [c, d]
    world = {"x_min": min(xs), "x_max": max(xs), "y_min": min(ys), "y_max": max(ys)}
    pane_nr = [float(hp.get("noise_radius_mm", 0) or 0) for _, hp in panes]   # GUI 흡수반경 원

    def label_qss(alerting):
        return (f"color:{C_ALERT if alerting else C_TXT};background:{C_PANEL};font-size:16px;"
                f"font-weight:bold;padding:7px;border-bottom:1px solid {C_LINE}")

    def column(text, idx):
        # 경보 시 빨간 테두리를 켤 수 있게 QFrame + objectName (gui_qt 의 #central 과 같은 방식).
        # 평소에도 4px 투명 테두리를 잡아 둬 경보 켜질 때 레이아웃이 흔들리지 않는다.
        w = QtWidgets.QFrame(); w.setObjectName(f"pane{idx}")
        w.setStyleSheet(f"#pane{idx} {{ border: 4px solid transparent; }}")
        v = QtWidgets.QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
        lab = QtWidgets.QLabel(text); lab.setAlignment(QtCore.Qt.AlignCenter)
        lab.setStyleSheet(label_qss(False))
        plot = RadarPlot(); plot.show_raw = False; plot.show_links = False
        v.addWidget(lab); v.addWidget(plot, 1)
        return w, plot, lab

    root = QtWidgets.QWidget(); root.resize(W, H)
    root.setStyleSheet(f"background:{C_BG};")
    row = QtWidgets.QHBoxLayout(root); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(2)
    plots, cols, labels = [], [], []
    for idx, (label, _) in enumerate(panes):
        w, plot, lab = column(label, idx)
        row.addWidget(w); plots.append(plot); cols.append(w); labels.append(lab)
    root.show(); app.processEvents()

    # 알림음: 경보 시각마다 '삐' 하는 WAV 를 만들어 오디오 트랙으로 함께 인코딩
    wav_path = None
    if beeps:
        fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="replay_alert_")
        os.close(fd)
        write_beep_wav(wav_path, len(rendered) / fps, beeps)

    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pixel_format", "rgba",
           "-video_size", f"{W}x{H}", "-framerate", str(fps), "-i", "-"]
    if wav_path:
        cmd += ["-i", wav_path, "-map", "0:v:0", "-map", "1:a:0",
                "-c:a", "aac", "-b:a", "96k", "-shortest"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-loglevel", "error", args.out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    written = 0
    prev_alert = [None] * len(panes)          # 패널별 경보 on/off — 바뀔 때만 스타일 갱신
    try:
        for k, i in enumerate(rendered):
            ts = gt["frames"][i]["t"]
            for p, (plot, tracks, nr) in enumerate(zip(plots, pane_tracks, pane_nr)):
                ids = pane_alerts[p][k]
                plot.alert_ids = ids                       # RadarPlot: 빨간 마커 + glow
                on = bool(ids)
                if on != prev_alert[p]:                    # 화면 빨간 테두리 + 제목 강조
                    cols[p].setStyleSheet(f"#pane{p} {{ border: 4px solid "
                                          f"{C_ALERT if on else 'transparent'}; }}")
                    labels[p].setStyleSheet(label_qss(on))
                    prev_alert[p] = on
                    if not on:
                        labels[p].setText(panes[p][0])
                if on:                                     # 대상이 바뀔 수 있어 매 프레임 갱신
                    who = ", ".join(f"#{t}" for t in sorted(ids))
                    labels[p].setText(f"⚠ OVERSTAY {who} · over {_fmt_dwell(thr)}  —  {panes[p][0]}")
                plot.render_snapshot(make_snap(sensors, world, tracks[i], nr, ts))
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
        if wav_path:
            os.unlink(wav_path)
    snd = f" · 알림음 {len(beeps)}회" if beeps else ""
    print(f"[video] 저장: {args.out}  ({written}프레임 @ {fps}fps ≈ {written/fps:.1f}s{snd})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
