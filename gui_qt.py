#!/usr/bin/env python3
"""
네이티브 GUI 실시간 시각화 (PySide6 + pyqtgraph) — 다중 센서 오버레이

표시 계층은 그대로 고성능 네이티브 플롯(pyqtgraph)이고, 데이터 소스만 USB→Wi-Fi 로 교체.
여러 센서(최대 3개)를 하나의 레이더 화면에 겹쳐 그린다. 각 센서는 자기 로컬 좌표로
타겟을 보고하고, 코어(SensorHub)가 이를 방(room) 좌표로 변환해준다. 화면은 변환된
방 좌표(rx, ry)만 그린다.

    python gui_qt.py                         # epl_config.json 의 센서들에 무선 접속
    python gui_qt.py --host 192.168.1.42 192.168.1.43   # 여러 개 지정 가능
    python gui_qt.py --transport web         # web_server SSE 폴백
    python gui_qt.py --demo                  # 하드웨어 없이 합성 센서 3개 오버레이
    python gui_qt.py --screenshot out.png    # (검증용) 잠깐 띄운 뒤 캡처하고 종료

구조:
    [WifiApiReader × N(asyncio)] --(lock)--> [SensorHub] <--(QTimer 60fps 폴링)-- [GUI]
    리더들은 백그라운드 스레드, GUI 는 메인 스레드에서 hub.snapshot() 만 읽으므로 스레드 안전.
"""
from __future__ import annotations

import argparse
import math
import sys
import time

import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from mmwave_reader import SensorHub, room_transform, RANGE_MM, FOV_DEG
from mmwave_wifi_reader import build_sources
from fusion import add_fusion_args, apply_fusion_opts

# 색상 팔레트 (웹 버전과 통일)
C_BG = "#0a0f14"; C_PANEL = "#0f1620"; C_PANEL2 = "#131d29"; C_LINE = "#1d2b3a"
C_TXT = "#dce6f0"; C_MUTED = "#7e93a8"; C_ACCENT = "#27e0c8"; C_ACCENT2 = "#ffb454"
C_OK = "#39d98a"; C_BAD = "#ff5d6c"
C_ALERT = "#ff3b30"    # 장기체류 경보(빨강) — 테두리·마커·패널 강조

LERP = 0.30            # 위치 보간 계수 (1=즉시, 작을수록 부드럽게 느림)
VEC_SCALE = 700.0      # 속도 m/s -> mm 시각화 배율


def _fmt_dwell(sec):
    """Dwell seconds → '5m00s' / '42s'."""
    sec = int(max(0.0, sec or 0.0))
    return f"{sec}s" if sec < 60 else f"{sec // 60}m{sec % 60:02d}s"


class RadarPlot(pg.PlotWidget):
    """위에서 내려다본 레이더 플롯 (top-down). 방(room) 좌표계로 여러 센서를 오버레이.

    - 뷰 범위는 스냅샷의 world bbox(+여백 8%)에 맞춘다.
    - 센서별 FOV 부채꼴 + 거리링 + 각도선 + 마커를 각 센서의 위치/방향/flip 으로 그린다.
    - 타겟은 방 좌표(rx, ry / rtrail)로, 색은 소속 센서의 color 로 그린다.
    """

    def __init__(self):
        super().__init__(background=C_BG)
        self.show_trail = True
        self.show_vec = True
        self.show_raw = False     # 원시(센서별) 점 표시 여부 — 기본 OFF, 융합점만 보임
        self.show_links = False   # 원시점→융합트랙 묶음선(디버그) — 기본 OFF
        self.alert_ids = set()    # 장기체류 경보 대상 트랙 id(빨간 마커) — MainWindow 가 매틱 갱신
        self.render = {}          # 보간 위치 {키: [x, y]} (키=(sensor_id,target_id) 또는 ("trk",id))
        self.items = {}           # 그래픽 {키: {...}} (per-sensor 타겟 + 융합 트랙)
        self._bg = []             # 배경/커버리지 아이템 [(item, is_vb), ...]
        self._layout_sig = None   # 센서 배치 서명(변경 시에만 오버레이 재구성)

        pi = self.getPlotItem()
        self._vb = pi.getViewBox()
        pi.setAspectLocked(True)               # 원이 찌그러지지 않게 1:1 스케일
        # 화면 좌우를 실제 공간과 일치시킨다(실제로 왼쪽 이동 → 화면에서도 왼쪽).
        # LD2450 계열은 보고 X 부호가 관찰자 기준과 반대(거울상)라, 방 좌표계 자체는
        # 센서끼리 자기일관적(융합/캘리브레이션은 거울대칭에 불변)이지만 사람이 보는
        # 좌우만 뒤집혀 보인다. 융합·기록·좌표수식은 그대로 두고 '표시'만 좌우 반전한다.
        # (장면 전체 - 융합점·센서부채꼴·거리링·궤적·속도벡터·노이즈원 - 이 함께 반전돼
        #  내부 기하는 그대로 유지된다.)
        self._vb.invertX(True)
        pi.hideButtons()
        pi.setMenuEnabled(False)
        pi.setMouseEnabled(x=False, y=False)   # 고정 시점
        for ax in ("left", "bottom"):
            a = pi.getAxis(ax)
            a.setPen(pg.mkPen(C_LINE)); a.setTextPen(pg.mkPen(C_MUTED))
        pi.getAxis("left").setLabel("Room Y · forward (mm)", color=C_MUTED)
        pi.getAxis("bottom").setLabel("Room X · lateral (mm)", color=C_MUTED)
        pi.showGrid(x=True, y=True, alpha=0.08)

        # 초기 뷰(센서 없을 때 기본값)
        self.setXRange(-RANGE_MM, RANGE_MM, padding=0)
        self.setYRange(0, RANGE_MM, padding=0)

    # ---- 배경/커버리지 아이템 관리 ----
    def _add_bg(self, item, vb=False):
        if vb:
            self._vb.addItem(item)
        else:
            self.addItem(item)
        self._bg.append((item, vb))

    def _clear_bg(self):
        for item, vb in self._bg:
            try:
                (self._vb.removeItem if vb else self.removeItem)(item)
            except Exception:
                pass
        self._bg = []

    def _layout_signature(self, snap):
        parts = []
        for s in snap["sensors"]:
            parts.append((s["id"], s["x"], s["y"], s["heading_deg"],
                          s.get("pitch_deg", 0.0), s.get("roll_deg", 0.0),
                          bool(s["flip"]), s["fov_deg"], s["range_mm"], s["color"]))
        w = snap["world"]
        return (tuple(parts), w["x_min"], w["x_max"], w["y_min"], w["y_max"])

    # ---- 센서별 FOV 부채꼴 / 거리링 / 각도선 / 마커 (배치 변경 시 재구성) ----
    def _rebuild_overlay(self, snap):
        self._clear_bg()
        for s in snap["sensors"]:
            self._draw_sensor_coverage(s)
        self._fit_view(snap["world"])

    def _draw_sensor_coverage(self, s):
        R = float(s["range_mm"]); F = int(s["fov_deg"])
        col = pg.mkColor(s["color"])
        sx, sy = room_transform(s, 0.0, 0.0)   # 센서 원점(방 좌표)

        # FOV 부채꼴 채우기 (센서 색, 은은하게)
        poly = QtGui.QPolygonF([QtCore.QPointF(sx, sy)])
        d = -F
        while d <= F:
            rx, ry = room_transform(s, R * math.sin(math.radians(d)),
                                    R * math.cos(math.radians(d)))
            poly.append(QtCore.QPointF(rx, ry))
            d += 2
        sector = QtWidgets.QGraphicsPolygonItem(poly)
        sector.setBrush(pg.mkBrush(col.red(), col.green(), col.blue(), 16))
        sector.setPen(pg.mkPen(None))
        self._add_bg(sector, vb=True)

        # 거리 동심원 (1m 간격, 방 좌표로 변환한 호)
        r = 1000
        while r <= R + 1:
            xs, ys = [], []
            for dd in range(-F, F + 1, 3):
                rx, ry = room_transform(s, r * math.sin(math.radians(dd)),
                                        r * math.cos(math.radians(dd)))
                xs.append(rx); ys.append(ry)
            self._add_bg(pg.PlotDataItem(xs, ys, pen=pg.mkPen(120, 150, 180, 45, width=1)))
            r += 1000

        # 각도선(시야 경계 ±F, 중심 0) + 경계 라벨
        for dd in (-F, 0, F):
            ex, ey = room_transform(s, R * math.sin(math.radians(dd)),
                                    R * math.cos(math.radians(dd)))
            edge = abs(dd) == F
            self._add_bg(pg.PlotDataItem(
                [sx, ex], [sy, ey],
                pen=pg.mkPen(120, 150, 180, 90 if edge else 30, width=1.4 if edge else 1)))

        # 센서 마커 + 이름
        self._add_bg(pg.ScatterPlotItem(
            [sx], [sy], size=13, brush=pg.mkBrush(col), pen=pg.mkPen("w", width=1.4)))
        lbl = pg.TextItem(s["name"], color=s["color"], anchor=(0.5, 1.4))
        lbl.setPos(sx, sy)
        self._add_bg(lbl)

    def _fit_view(self, world):
        x0, x1 = world["x_min"], world["x_max"]
        y0, y1 = world["y_min"], world["y_max"]
        wpad = max((x1 - x0) * 0.08, 300.0)
        hpad = max((y1 - y0) * 0.08, 300.0)
        self.setXRange(x0 - wpad, x1 + wpad, padding=0)
        self.setYRange(y0 - hpad, y1 + hpad, padding=0)

    # ---- 타겟용 동적 아이템 ((sensor_id, target_id) 키로 생성/재사용) ----
    def _ensure_item(self, key, col):
        it = self.items.get(key)
        if it is not None and it["col"] == col:
            return it
        if it is not None:
            self._remove_item(key)
        c = pg.mkColor(col)
        glow = pg.ScatterPlotItem([], [], size=30, pen=pg.mkPen(None),
                                  brush=pg.mkBrush(c.red(), c.green(), c.blue(), 70))
        trail = pg.PlotDataItem([], [], pen=pg.mkPen(col, width=2)); trail.setOpacity(0.55)
        vec = pg.PlotDataItem([], [], pen=pg.mkPen(col, width=2.2))
        vtip = pg.ScatterPlotItem([], [], size=7, brush=pg.mkBrush(col), pen=pg.mkPen(None))
        dot = pg.ScatterPlotItem([], [], size=15, brush=pg.mkBrush(col),
                                 pen=pg.mkPen("w", width=1.5))
        label = pg.TextItem("", color=col, anchor=(0, 1.2))
        for g in (glow, trail, vec, vtip, dot, label):
            self.addItem(g)
        it = dict(col=col, glow=glow, trail=trail, vec=vec, vtip=vtip, dot=dot, label=label)
        self.items[key] = it
        return it

    # ---- 융합 트랙용 동적 아이템 (("trk", track_id) 키). 사람별 큰 점 하나. ----
    def _ensure_track_item(self, tid, col):
        key = ("trk", tid)
        it = self.items.get(key)
        if it is not None and it["col"] == col:
            return it
        if it is not None:
            self._remove_item(key)
        c = pg.mkColor(col)
        # 흡수 반경(noise_radius) 시각화용 반투명 채워진 원 — 데이터 좌표(mm) 반지름.
        # ScatterPlotItem(size=px)로는 데이터좌표 반지름 원을 못 그리므로, FOV 부채꼴과
        # 동일하게 QGraphicsEllipseItem 을 뷰박스에 직접 얹어 방 좌표(mm)로 그린다.
        zone = QtWidgets.QGraphicsEllipseItem()
        zone.setBrush(pg.mkBrush(c.red(), c.green(), c.blue(), 20))  # 알파≈0.08
        zone.setPen(pg.mkPen(None))
        zone.setZValue(4)          # 트랙 점(dot=10)보다 아래 레이어
        zone.setVisible(False)
        self._vb.addItem(zone)
        # 묶음선: 이 트랙에 묶인 원시점(members)들을 트랙 점과 잇는 얇은 선(디버그).
        # connect='pairs' 로 (원시점, 트랙) 쌍마다 한 선분씩 그린다.
        links = pg.PlotDataItem([], [], connect="pairs",
                                pen=pg.mkPen(c.red(), c.green(), c.blue(), 130, width=1))
        links.setZValue(3)         # 원시점 위, 트랙 glow(5) 아래
        self.addItem(links)
        glow = pg.ScatterPlotItem([], [], size=46, pen=pg.mkPen(None),
                                  brush=pg.mkBrush(c.red(), c.green(), c.blue(), 60))
        trail = pg.PlotDataItem([], [], pen=pg.mkPen(col, width=2.4)); trail.setOpacity(0.6)
        vec = pg.PlotDataItem([], [], pen=pg.mkPen(col, width=2.6))
        vtip = pg.ScatterPlotItem([], [], size=8, brush=pg.mkBrush(col), pen=pg.mkPen(None))
        dot = pg.ScatterPlotItem([], [], size=23, brush=pg.mkBrush(col),
                                 pen=pg.mkPen("w", width=2))
        label = pg.TextItem("", color=col, anchor=(0, 1.25))
        # 융합점은 원시점 위에 올라오도록 z 값을 높인다
        for z, g in ((5, glow), (6, trail), (7, vec), (8, vtip), (10, dot), (11, label)):
            g.setZValue(z)
            self.addItem(g)
        it = dict(col=col, zone=zone, links=links, glow=glow, trail=trail, vec=vec,
                  vtip=vtip, dot=dot, label=label)
        self.items[key] = it
        return it

    def _remove_item(self, key):
        it = self.items.pop(key, None)
        if not it:
            return
        z = it.get("zone")   # 트랙 흡수반경 원(뷰박스 직접 얹은 아이템)
        if z is not None:
            try:
                self._vb.removeItem(z)
            except Exception:
                pass
        for k in ("links", "glow", "trail", "vec", "vtip", "dot", "label"):
            try:
                self.removeItem(it[k])
            except Exception:
                pass
        self.render.pop(key, None)

    def _clear_item(self, key):
        it = self.items.get(key)
        if not it:
            return
        z = it.get("zone")   # 사라진 트랙이면 흡수반경 원도 숨긴다
        if z is not None:
            z.setVisible(False)
        for k in ("glow", "dot", "vtip"):
            it[k].setData([], [])
        if "links" in it:
            it["links"].setData([], [])
        it["trail"].setData([], [])
        it["vec"].setData([], [])
        it["label"].setText("")
        self.render[key] = None

    def render_snapshot(self, snap):
        # 센서 배치가 바뀌었을 때만 배경/커버리지 재구성 + 뷰 범위 재설정
        # (주의: QWidget.update() 를 덮어쓰지 않도록 별도 이름 사용)
        sig = self._layout_signature(snap)
        if sig != self._layout_sig:
            self._layout_sig = sig
            self._rebuild_overlay(snap)
        self._update_targets(snap)   # 원시(센서별) 점 — show_raw OFF 면 내부에서 비움
        self._update_tracks(snap)    # 융합 점(주) — 사람 하나당 점 하나

    def _update_targets(self, snap):
        seen = set()
        # 원시점 토글이 꺼져 있으면 per-sensor 타겟은 아무 것도 그리지 않는다(기본 동작)
        if self.show_raw:
          for s in snap["sensors"]:
            sx, sy = s["x"], s["y"]
            col = s["color"]; name = s["name"]
            for t in s["targets"]:
                key = (s["id"], t["id"])
                if not t["present"]:
                    self._clear_item(key)
                    continue
                seen.add(key)
                it = self._ensure_item(key, col)
                # 궤적 (방 좌표)
                if self.show_trail and t["rtrail"]:
                    it["trail"].setData([p[0] for p in t["rtrail"]],
                                        [p[1] for p in t["rtrail"]])
                else:
                    it["trail"].setData([], [])
                # 위치 보간 (스냅샷 ~15Hz, 화면 60fps)
                rs = self.render.get(key)
                if rs is None:
                    rs = [t["rx"], t["ry"]]; self.render[key] = rs
                rs[0] += (t["rx"] - rs[0]) * LERP
                rs[1] += (t["ry"] - rs[1]) * LERP
                px, py = rs[0], rs[1]
                it["glow"].setData([px], [py])
                it["dot"].setData([px], [py])
                it["label"].setText(
                    f"{name}·T{t['id']}\n{t['distance']/1000:.2f} m  {t['speed']:+.2f} m/s")
                it["label"].setPos(px, py)
                # 속도 벡터 (센서→타겟 시선방향 단위벡터 × 부호있는 속도)
                if self.show_vec and abs(t["speed"]) > 0.03:
                    dx, dy = rs[0] - sx, rs[1] - sy
                    norm = math.hypot(dx, dy) or 1.0
                    ux, uy = dx / norm, dy / norm
                    ex = rs[0] + ux * t["speed"] * VEC_SCALE
                    ey = rs[1] + uy * t["speed"] * VEC_SCALE
                    it["vec"].setData([px, ex], [py, ey])
                    it["vtip"].setData([ex], [ey])
                else:
                    it["vec"].setData([], [])
                    it["vtip"].setData([], [])
        # 이번 프레임에 없는(사라진/타 센서) 원시 타겟은 비운다. 트랙 아이템은 건드리지 않음.
        for key in list(self.items.keys()):
            if key[0] == "trk":
                continue
            if key not in seen:
                self._clear_item(key)

    def _update_tracks(self, snap):
        """융합 트랙(사람별) 렌더링 — confirmed 만 큰 점 하나로. coasting 은 외곽선만."""
        seen = set()
        # 흡수 반경(mm) — 각 confirmed 트랙 둘레에 반투명 원으로 표시. 0/음수면 원 없음.
        zone_r = float(snap.get("config", {}).get("noise_radius_mm", 0) or 0)
        # 원시점 위치 조회표 {(sensor_id, target_id): (rx, ry)} — 묶음선 끝점용(스냅샷 폴백)
        raw_pos = {}
        for s in snap["sensors"]:
            for t in s["targets"]:
                if t.get("present"):
                    raw_pos[(s["id"], t["id"])] = (t["rx"], t["ry"])
        for tr in snap.get("tracks", []):
            if not tr.get("confirmed"):
                continue           # 후보(tentative) 트랙은 그리지 않는다
            tid = tr["id"]; col = tr["color"]
            key = ("trk", tid)
            seen.add(key)
            it = self._ensure_track_item(tid, col)
            coasting = bool(tr.get("coasting"))
            c = pg.mkColor(col)
            # 궤적 (방 좌표 히스토리)
            trail = tr.get("trail") or []
            if self.show_trail and len(trail) > 1:
                it["trail"].setData([p[0] for p in trail], [p[1] for p in trail])
            else:
                it["trail"].setData([], [])
            # 위치 보간 (스냅샷 ~15Hz, 화면 60fps) — 원시 타겟과 동일 방식
            rs = self.render.get(key)
            if rs is None:
                rs = [tr["x"], tr["y"]]; self.render[key] = rs
            rs[0] += (tr["x"] - rs[0]) * LERP
            rs[1] += (tr["y"] - rs[1]) * LERP
            px, py = rs[0], rs[1]
            # 흡수 반경 원 — 트랙 보간 위치(px,py) 중심, 반지름 zone_r(mm). R<=0 이면 숨김.
            zone = it.get("zone")
            if zone is not None:
                if zone_r > 0:
                    zone.setRect(px - zone_r, py - zone_r, 2 * zone_r, 2 * zone_r)
                    zone.setVisible(True)
                else:
                    zone.setVisible(False)
            # 장기체류 경보 대상이면 빨간 굵은 테두리 + 빨간 glow 로 강조.
            alerting = tid in self.alert_ids
            # coasting=관측 없이 예측만(가려짐) → 속 빈 외곽선 + glow 끔
            if coasting:
                dot_brush = pg.mkBrush(c.red(), c.green(), c.blue(), 0)
                dot_pen = pg.mkPen(C_ALERT, width=3.5) if alerting else pg.mkPen(col, width=2)
            else:
                dot_brush = pg.mkBrush(col)
                dot_pen = pg.mkPen(C_ALERT, width=3.5) if alerting else pg.mkPen("w", width=2)
            it["dot"].setData([px], [py], brush=dot_brush, pen=dot_pen)
            if alerting:
                ac = pg.mkColor(C_ALERT)
                it["glow"].setData([px], [py],
                                   brush=pg.mkBrush(ac.red(), ac.green(), ac.blue(), 110))
            elif coasting:
                it["glow"].setData([], [])
            else:
                it["glow"].setData([px], [py],
                                   brush=pg.mkBrush(c.red(), c.green(), c.blue(), 60))
            # 묶음선(디버그): 이 트랙에 묶인 원시점(members)들을 트랙 점과 얇은 선으로 연결.
            # 원시점 끝은 보간 위치(render) 우선, 없으면 스냅샷 원시 좌표(raw_pos) 사용.
            lk = it.get("links")
            if lk is not None:
                if self.show_links:
                    lxs, lys = [], []
                    for m in (tr.get("members") or []):
                        mk = (m[0], m[1])
                        rp = self.render.get(mk) or raw_pos.get(mk)
                        if rp is None:
                            continue
                        lxs += [rp[0], px]; lys += [rp[1], py]
                    lk.setData(lxs, lys, connect="pairs")
                else:
                    lk.setData([], [])
            # 라벨: #id · 속력 · 센서수 · 체류시간 (재식별 직후엔 ↩, 장기체류 경보면 ⚠)
            reid_mark = " ↩ReID" if tr.get("reid") else ""
            alert_mark = " ⚠OVERSTAY" if alerting else ""
            _dw = float(tr.get("dwell_sec", 0.0) or 0.0)     # 체류시간(초): ReID 시 공백 포함 지속
            dwell_str = _fmt_dwell(_dw)
            it["label"].setText(f"#{tid}{reid_mark}{alert_mark} · {tr['speed']:.1f}m/s · "
                                f"{tr['n_sensors']} sensors · ⏱{dwell_str}")
            it["label"].setColor(C_ALERT if alerting else col)
            it["label"].setPos(px, py)
            # 속도 벡터 (vx,vy 는 mm/s → (vx,vy)*K/1000 만큼 mm 로 표시)
            if self.show_vec and tr["speed"] > 0.05:
                ex = px + tr["vx"] * VEC_SCALE / 1000.0
                ey = py + tr["vy"] * VEC_SCALE / 1000.0
                it["vec"].setData([px, ex], [py, ey])
                it["vtip"].setData([ex], [ey])
            else:
                it["vec"].setData([], [])
                it["vtip"].setData([], [])
        # 사라진(죽은) 트랙 아이템은 완전 제거. 트랙 id 는 재사용되지 않으므로(단조 증가)
        # _clear_item(숨김만)으로 두면 그래픽 아이템이 씬에 무한 누적된다 → _remove_item 로 detach.
        for key in list(self.items.keys()):
            if key[0] == "trk" and key not in seen:
                self._remove_item(key)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, hub: SensorHub, desc: str = "", dwell_alert_sec: float = 300.0):
        super().__init__()
        self.hub = hub
        self.desc = desc
        self.dwell_alert_sec = dwell_alert_sec   # 이 시간(초) 이상 체류=경보. 0=끔
        self._panel_div = 0
        self._cards_sig = None
        self.sensor_cards = {}     # {sensor_id: {"frame","title","body"}}
        # 장기체류 경보 상태
        self._alert_active = False
        self._alerted_ids = set()  # 이미 알림음을 울린 대상(중복 방지)
        self._last_beep = 0.0
        self._beep_period = 5.0    # 경보 지속 시 재알림 간격(초)
        self.setWindowTitle("VanguardHealthCare · mmWave Occupancy Detection PoC")
        self.resize(1400, 880)
        self.setStyleSheet(self._qss())

        # 중앙 영역을 테두리 있는 프레임으로 — 경보 시 이 테두리를 빨갛게 켠다(항상 4px 확보→깜빡임 없음)
        self.central = QtWidgets.QFrame(); self.central.setObjectName("central")
        self.setCentralWidget(self.central)
        root = QtWidgets.QVBoxLayout(self.central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QtWidgets.QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0)
        self.radar = RadarPlot()
        body.addWidget(self.radar, 1)
        body.addWidget(self._build_panel())
        bw = QtWidgets.QWidget(); bw.setLayout(body)
        root.addWidget(bw, 1)

        # 60fps 렌더 타이머
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    # ---- 헤더 (제목 + 상태 + 토글) ----
    def _build_header(self):
        h = QtWidgets.QFrame(); h.setObjectName("header")
        lay = QtWidgets.QHBoxLayout(h); lay.setContentsMargins(18, 10, 18, 10); lay.setSpacing(12)

        self.dot = QtWidgets.QLabel("●"); self.dot.setObjectName("dot")
        title = QtWidgets.QLabel(
            "<b style='font-size:15px'>VanguardHealthCare · mmWave Occupancy Detection PoC</b><br>"
            "<span style='color:#7e93a8;font-size:11px'>Everything Presence Lite — "
            "HLK-LD2450 24GHz Radar · Multi-sensor overlay · PySide6 + pyqtgraph</span>")
        lay.addWidget(self.dot); lay.addWidget(title); lay.addStretch(1)

        # 좌우반전 토글은 제거(각 센서 flip 이 이미 rx 에 반영됨). 궤적/속도벡터만 유지.
        self.cb_trail = QtWidgets.QCheckBox("Trails"); self.cb_trail.setChecked(True)
        self.cb_vec = QtWidgets.QCheckBox("Velocity"); self.cb_vec.setChecked(True)
        # 원시점(센서별 점) — 기본 OFF. 켜면 융합 전 원본 검출을 함께 본다.
        self.cb_raw = QtWidgets.QCheckBox("Raw"); self.cb_raw.setChecked(False)
        # 묶음선 — 원시점이 어느 융합 ID 로 묶였는지 얇은 선으로 표시(디버그).
        self.cb_links = QtWidgets.QCheckBox("Links"); self.cb_links.setChecked(False)
        for cb in (self.cb_trail, self.cb_vec, self.cb_raw, self.cb_links):
            lay.addWidget(cb)
        self.status = QtWidgets.QLabel(self.desc or "Waiting for connection…"); self.status.setObjectName("status")
        lay.addWidget(self.status)
        return h

    # ---- 우측 정보 패널 ----
    def _build_panel(self):
        p = QtWidgets.QFrame(); p.setObjectName("panel"); p.setFixedWidth(360)
        lay = QtWidgets.QVBoxLayout(p); lay.setContentsMargins(16, 16, 16, 16); lay.setSpacing(13)

        # 추적 인원 (융합) — 중복 제거한 실제 사람 수를 주 카운트로
        c1 = self._card("People tracked · Fusion (FUSION)")
        self.lbl_count = QtWidgets.QLabel("0")
        self.lbl_count.setStyleSheet(f"font-size:40px;font-weight:700;color:{C_ACCENT}")
        row = QtWidgets.QHBoxLayout(); row.addWidget(self.lbl_count)
        self.lbl_count_unit = QtWidgets.QLabel("people")
        self.lbl_count_unit.setStyleSheet(f"color:{C_MUTED}")
        self.lbl_count_unit.setAlignment(QtCore.Qt.AlignBottom)
        row.addWidget(self.lbl_count_unit); row.addStretch(1)
        c1.layout().addLayout(row); lay.addWidget(c1)

        # 추적 중인 사람 (융합 트랙 목록)
        c_trk = self._card("Tracked people (TRACKS)")
        self.lbl_tracks = QtWidgets.QLabel(); self.lbl_tracks.setTextFormat(QtCore.Qt.RichText)
        self.lbl_tracks.setWordWrap(True)
        c_trk.layout().addWidget(self.lbl_tracks); lay.addWidget(c_trk)

        # 센서별 카드 (동적)
        title = QtWidgets.QLabel("Sensor status (SENSORS)"); title.setObjectName("ctitle")
        lay.addWidget(title)
        self.cards_host = QtWidgets.QWidget()
        self.cards_lay = QtWidgets.QVBoxLayout(self.cards_host)
        self.cards_lay.setContentsMargins(0, 0, 0, 0); self.cards_lay.setSpacing(10)
        lay.addWidget(self.cards_host)

        # 조도 (첫 연결 센서 기준)
        c3 = self._card("Illuminance (ILLUMINANCE)")
        self.bar_lux = QtWidgets.QProgressBar(); self.bar_lux.setRange(0, 1000)
        self.bar_lux.setTextVisible(True); self.bar_lux.setFormat("%v lx")
        c3.layout().addWidget(self.bar_lux); lay.addWidget(c3)

        lay.addStretch(1)
        legend = QtWidgets.QLabel(
            "Coords: room (mm), X=lateral · Y=forward · fused dot=person (track color) · "
            "raw points=per-sensor (toggle 'Raw' in header; large dot=fused ID) · "
            "'Links'=fused ID each raw point belongs to · range 6m · data=Wi-Fi (ESPHome Native API)")
        legend.setWordWrap(True); legend.setStyleSheet(f"color:{C_MUTED};font-size:10px")
        lay.addWidget(legend)
        return p

    def _card(self, title):
        f = QtWidgets.QFrame(); f.setObjectName("card")
        v = QtWidgets.QVBoxLayout(f); v.setContentsMargins(13, 11, 13, 12); v.setSpacing(7)
        h = QtWidgets.QLabel(title); h.setObjectName("ctitle"); v.addWidget(h)
        return f

    def _rebuild_cards(self, sensors):
        sig = tuple((s["id"], s["name"], s["color"]) for s in sensors)
        if sig == self._cards_sig:
            return
        self._cards_sig = sig
        # 기존 카드 제거
        while self.cards_lay.count():
            item = self.cards_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self.sensor_cards = {}
        for s in sensors:
            f = QtWidgets.QFrame(); f.setObjectName("card")
            f.setStyleSheet(f"border-left:3px solid {s['color']}")
            v = QtWidgets.QVBoxLayout(f); v.setContentsMargins(11, 9, 11, 10); v.setSpacing(4)
            tl = QtWidgets.QLabel(); tl.setTextFormat(QtCore.Qt.RichText)
            bd = QtWidgets.QLabel(); bd.setTextFormat(QtCore.Qt.RichText); bd.setWordWrap(True)
            v.addWidget(tl); v.addWidget(bd)
            self.cards_lay.addWidget(f)
            self.sensor_cards[s["id"]] = {"frame": f, "title": tl, "body": bd}

    # ---- 주기적 갱신 ----
    def tick(self):
        snap = self.hub.snapshot()
        self.radar.show_trail = self.cb_trail.isChecked()
        self.radar.show_vec = self.cb_vec.isChecked()
        self.radar.show_raw = self.cb_raw.isChecked()
        self.radar.show_links = self.cb_links.isChecked()
        # 장기체류 경보: dwell_sec 이 임계 이상인 confirmed 트랙 id 집합(0=끔)
        alert_ids = self._alert_id_set(snap)
        self.radar.alert_ids = alert_ids
        self.radar.render_snapshot(snap)
        self._handle_alert(alert_ids)
        # 패널은 10Hz 로만 갱신 (텍스트 업데이트 비용 절감)
        self._panel_div = (self._panel_div + 1) % 6
        if self._panel_div == 0:
            self._update_panel(snap)

    def _alert_id_set(self, snap):
        """임계 이상 체류한 confirmed 트랙 id 집합. dwell_alert_sec<=0 이면 항상 빈 집합(경보 끔)."""
        thr = self.dwell_alert_sec
        if not thr or thr <= 0:
            return set()
        return {t["id"] for t in snap.get("tracks", [])
                if t.get("confirmed") and float(t.get("dwell_sec") or 0.0) >= thr}

    def _handle_alert(self, alert_ids):
        """경보 대상 유무에 따라 빨간 테두리 토글 + 알림음(신규 진입 즉시, 지속 시 주기적)."""
        active = bool(alert_ids)
        if active != self._alert_active:
            self._alert_active = active
            self.central.setStyleSheet(
                f"#central {{ border: 4px solid {C_ALERT if active else 'transparent'}; }}")
        if active:
            now = time.monotonic()
            if (alert_ids - self._alerted_ids) or (now - self._last_beep >= self._beep_period):
                try:
                    QtWidgets.QApplication.beep()
                except Exception:
                    pass
                self._last_beep = now
        self._alerted_ids = set(alert_ids)

    _MODE_MAP = {"wifi": "Wi-Fi", "serial": "USB Serial",
                 "demo": "Demo (synthetic)", "disconnected": "Disconnected"}

    def _update_panel(self, snap):
        sensors = snap["sensors"]
        n_sensor = snap["sensor_count"]
        # 주 카운트 = 융합 인원(track_count), 원시 합계는 작게 병기
        self.lbl_count.setText(str(snap["track_count"]))
        self.lbl_count_unit.setText(
            f"people  ·  raw {snap['present_total']}  ·  {n_sensor} sensors")

        # 추적 중인 사람 목록 (confirmed 트랙만) + 장기체류 경보 강조
        tracks = [t for t in snap.get("tracks", []) if t.get("confirmed")]
        thr = self.dwell_alert_sec
        if tracks:
            rows, alerted = [], []
            for t in tracks:
                dw = float(t.get("dwell_sec") or 0.0)
                over = bool(thr and thr > 0 and dw >= thr)
                coast = (" ·<span style='color:#7e93a8'>coast</span>"
                         if t.get("coasting") else "")
                if over:
                    alerted.append(t["id"])
                    id_cell = f"<span style='color:{C_ALERT}'>⚠ <b>#{t['id']}</b></span>"
                    dwell_cell = f"<span style='color:{C_ALERT}'>⏱{_fmt_dwell(dw)}</span>"
                else:
                    id_cell = (f"<span style='color:{t['color']}'>■</span> <b>#{t['id']}</b>")
                    dwell_cell = f"⏱{_fmt_dwell(dw)}"
                rows.append(
                    f"<tr><td>{id_cell}</td>"
                    f"<td align='right'>({t['x']/1000:.1f}, {t['y']/1000:.1f}) m</td>"
                    f"<td align='right'>{t['speed']:.2f} m/s</td>"
                    f"<td align='right'>{dwell_cell}{coast}</td></tr>")
            banner = ""
            if alerted:
                who = ", ".join(f"#{i}" for i in sorted(alerted))
                banner = (f"<div style='color:{C_ALERT};font-weight:700;padding:2px 0 6px'>"
                          f"⚠ OVERSTAY ALERT · {who} (over {_fmt_dwell(thr)})</div>")
            self.lbl_tracks.setText(
                f"{banner}<table width='100%' cellspacing='4'>{''.join(rows)}</table>")
        else:
            self.lbl_tracks.setText(
                "<span style='color:#5d6b7a'>No one tracked</span>")

        on = bool(snap["connected"])
        conn_n = sum(1 for s in sensors if s["connected"])
        self.dot.setStyleSheet(f"color:{C_OK if on else C_BAD};font-size:13px")
        self.status.setText(("● " if on else "○ ") + f"{conn_n}/{n_sensor} sensors connected")

        # 센서별 카드
        self._rebuild_cards(sensors)
        for s in sensors:
            card = self.sensor_cards.get(s["id"])
            if not card:
                continue
            sc = bool(s["connected"])
            badge = (f"<span style='color:{C_OK}'>● connected</span>" if sc
                     else f"<span style='color:{C_BAD}'>○ offline</span>")
            card["title"].setText(
                f"<span style='color:{s['color']}'>■</span> "
                f"<b>{s['name']}</b> &nbsp; {badge}")
            # 타겟 요약
            present = [t for t in s["targets"] if t["present"]]
            if present:
                summ = "  ".join(f"T{t['id']}·{t['distance']/1000:.1f}m" for t in present)
            else:
                summ = "<span style='color:#5d6b7a'>none</span>"
            st = s["stats"]
            mode = self._MODE_MAP.get(s["mode"], s["mode"])
            card["body"].setText(
                f"<table width='100%' cellspacing='3'>"
                f"<tr><td style='color:{C_MUTED}'>People</td>"
                f"<td align='right'><b>{s['present_count']}</b> / 3</td></tr>"
                f"<tr><td style='color:{C_MUTED}'>Targets</td><td align='right'>{summ}</td></tr>"
                f"<tr><td style='color:{C_MUTED}'>Rate</td>"
                f"<td align='right'>{st['line_rate']} /s</td></tr>"
                f"<tr><td style='color:{C_MUTED}'>Source</td>"
                f"<td align='right'>{mode} · {s['host'] or s['port'] or '--'}</td></tr>"
                f"</table>")

        # 조도: 첫 연결 센서(없으면 첫 센서) 기준
        lux = None
        for s in sensors:
            if s["connected"] and s["illuminance"] is not None:
                lux = s["illuminance"]; break
        if lux is None and sensors:
            lux = sensors[0]["illuminance"]
        if lux is not None:
            self.bar_lux.setValue(int(max(0, min(1000, lux))))
            self.bar_lux.setFormat("%v lx")
        else:
            self.bar_lux.setValue(0)
            self.bar_lux.setFormat("-- lx")

    def _qss(self):
        return f"""
        QWidget {{ background:{C_BG}; color:{C_TXT};
            font-family:-apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif; font-size:12px; }}
        #header {{ background:{C_PANEL}; border-bottom:1px solid {C_LINE}; }}
        #central {{ border: 4px solid transparent; }}   /* 장기체류 경보 시 빨강 */
        #panel {{ background:{C_PANEL}; border-left:1px solid {C_LINE}; }}
        #card {{ background:{C_PANEL2}; border:1px solid {C_LINE}; border-radius:11px; }}
        #ctitle {{ color:{C_MUTED}; font-size:10px; font-weight:600; }}
        #status {{ color:{C_MUTED}; border:1px solid {C_LINE}; border-radius:13px; padding:4px 11px; }}
        QCheckBox {{ color:{C_MUTED}; padding:3px 6px; }}
        QCheckBox::indicator:checked {{ background:{C_ACCENT}; border-radius:3px; }}
        QCheckBox::indicator:unchecked {{ background:{C_PANEL2}; border:1px solid {C_LINE}; border-radius:3px; }}
        QProgressBar {{ background:{C_PANEL}; border:1px solid {C_LINE}; border-radius:6px;
            height:16px; text-align:center; color:{C_TXT}; }}
        QProgressBar::chunk {{ background:{C_ACCENT2}; border-radius:5px; }}
        """


def main():
    ap = argparse.ArgumentParser(description="EPL mmWave 네이티브 GUI 시각화 (Wi-Fi · 다중 센서)")
    ap.add_argument("--host", nargs="+", default=None,
                    help="센서 IP/호스트 (여러 개 가능, 미지정 시 epl_config.json ∪ 자동탐색)")
    ap.add_argument("--transport", choices=["api", "web"], default="api")
    ap.add_argument("--noise-psk", default=None, help="API 암호화 키(설정된 경우)")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--screenshot", default=None,
                    help="검증용: 지정 경로로 캡처 후 종료")
    ap.add_argument("--shot-delay", type=float, default=2.5)
    ap.add_argument("--duration", type=float, default=None,
                    help="지정 초 후 자동 종료(기록 파일 정상 마감). 미지정/0 이면 수동 종료까지 계속")
    # GUI 표시 조정값 (이 파일 전용)
    ap.add_argument("--lerp", type=float, default=None,
                    help="화면 위치 보간 계수 0~1 (작을수록 부드럽고 느림, 기본 0.30)")
    ap.add_argument("--vec-scale", type=float, default=None,
                    help="속도벡터 길이 배율 m/s→mm (기본 700)")
    ap.add_argument("--dwell-alert-sec", type=float, default=300.0,
                    help="이 시간(초) 이상 체류한 ID 경보: 빨간 테두리+마커+알림음. 0=끔. 기본 300(=5분)")
    # 디버그 모드 (run_debug_gui.sh) — 융합 전 원시점/묶음선 표시 + 원시 스트림 기록
    ap.add_argument("--show-raw", action="store_true",
                    help="시작 시 원시점(센서별 검출) 표시 ON")
    ap.add_argument("--show-links", action="store_true",
                    help="시작 시 묶음선(원시점→융합 ID) 표시 ON")
    ap.add_argument("--record", default=None,
                    help="융합 전 원시 검출 스트림을 이 경로에 JSONL 로 기록(분석/HPO)")
    add_fusion_args(ap)
    args = ap.parse_args()

    if args.lerp is not None:
        globals()["LERP"] = args.lerp           # RadarPlot 메서드가 참조하는 모듈 전역
    if args.vec_scale is not None:
        globals()["VEC_SCALE"] = args.vec_scale

    hub = SensorHub()
    apply_fusion_opts(hub, args)
    workers, desc = build_sources(hub, demo=args.demo, hosts=args.host,
                                  transport=args.transport, noise_psk=args.noise_psk)
    print(f"Data source: {desc}")
    # 기록은 트래커 설정(apply_fusion_opts)과 센서 등록(build_sources) '후'에 켠다
    # → 헤더에 HP 전체 + 센서 외부보정이 함께 남는다.
    if args.record:
        try:
            hub.enable_recording(args.record)
            print(f"Recording raw data: {args.record}")
        except Exception as e:
            print(f"[warn] Failed to start recording ({args.record}): {e}")
    for w in workers:
        w.start()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow(hub, desc, dwell_alert_sec=args.dwell_alert_sec)
    if args.show_raw:
        win.cb_raw.setChecked(True)
    if args.show_links:
        win.cb_links.setChecked(True)
    win.show()

    if args.screenshot:
        def _grab():
            win.grab().save(args.screenshot)
            print(f"Screenshot saved: {args.screenshot}")
            for w in workers:
                w.stop()
            app.quit()
        QtCore.QTimer.singleShot(int(args.shot_delay * 1000), _grab)

    if args.duration and args.duration > 0:
        # 지정 시간 후 워커를 멈추고 app 종료 → 아래 finally 가 기록을 정상 마감한다.
        def _timeup():
            print(f"[duration] {args.duration:.0f}s elapsed — auto-exit")
            for w in workers:
                w.stop()
            app.quit()
        QtCore.QTimer.singleShot(int(args.duration * 1000), _timeup)

    try:
        app.exec()
    finally:
        for w in workers:
            w.stop()
        hub.close_recording()


if __name__ == "__main__":
    main()
