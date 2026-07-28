"""
상태 모델 + 스레드 안전 상태 저장소 (Everything Presence Lite / LD2450)

- SensorState/SensorHub 로 타겟 상태를 스레드 안전하게 보관·갱신한다.
  (mmwave_parser 가 만든 업데이트를 apply_updates() 로 반영)
- room_transform 등으로 센서 로컬 좌표를 방(room) 좌표계로 변환한다.
- --demo 모드면 합성 센서(DemoSensorThread)로 하드웨어 없이 시각화를 시연한다.
- snapshot() 으로 JSON 직렬화 가능한 현재 상태 스냅샷을 얻는다.
- autodetect_port() 는 프로비저닝용 USB-시리얼 포트 자동 탐지에만 쓰인다.

무선(Wi-Fi) 데이터 수신 자체는 mmwave_wifi_reader.py 가 담당한다.
"""

from __future__ import annotations

import glob
import json
import math
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

import mmwave_parser as P
from fusion import FusionTracker, tracker_params

# ---- 설정 상수 -------------------------------------------------------------
DEFAULT_BAUD = 115200          # EPL ESPHome 로그 기본 baud
MAX_TARGETS = 3                # LD2450 는 최대 3 타겟 추적
STALE_SEC = 1.0               # 이 시간 이상 갱신 없으면 타겟을 '비활성'으로 간주
TRAIL_LEN = 48                 # 타겟 이동 궤적 보관 점 개수
# LD2450 물리 범위 (시각화 스케일 기준값)
RANGE_MM = 6000                # 최대 탐지 거리(mm)
FOV_DEG = 60                   # 수평 반시야각(±60°)

# macOS 에서 흔한 USB-시리얼 포트 패턴 (CH340 / CP210x / 기타)
_PORT_GLOBS = [
    "/dev/cu.usbserial-*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.usbmodem*",
]


def autodetect_port() -> Optional[str]:
    """연결된 USB-시리얼 포트를 자동 탐지. 첫 번째 매치를 반환."""
    for pattern in _PORT_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


class Target:
    """단일 추적 타겟의 현재 상태."""

    __slots__ = ("id", "x", "y", "speed", "angle", "distance",
                 "resolution", "active", "last_update", "trail")

    def __init__(self, tid: int):
        self.id = tid
        self.x = 0.0
        self.y = 0.0
        self.speed = 0.0
        self.angle = 0.0
        self.distance = 0.0
        self.resolution = 0.0
        self.active = False
        self.last_update = 0.0
        self.trail: Deque[Tuple[float, float, float]] = deque(maxlen=TRAIL_LEN)

    def is_present(self, now: float) -> bool:
        """최근에 갱신되었고 좌표가 0이 아니면 present.

        주의: ESPHome 의 'Target N Active' binary_sensor 는 값이 바뀔 때만
        로그를 남기므로(희소) 존재 판정의 기준으로 쓰지 않는다. 대신 X/Y 가
        주기적으로 갱신되는지(최근성) + 좌표가 0이 아닌지로 판정한다.
        타겟이 사라지면 LD2450/ESPHome 이 X=Y=0 을 보내므로 nonzero 로 걸러진다."""
        recent = self.last_update > 0 and (now - self.last_update) < STALE_SEC
        nonzero = abs(self.x) > 1.0 or abs(self.y) > 1.0  # mm
        return recent and nonzero

    def to_dict(self, now: float) -> Dict:
        return {
            "id": self.id,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "speed": round(self.speed, 3),
            "angle": round(self.angle, 2),
            "distance": round(self.distance, 1),
            "resolution": round(self.resolution, 1),
            "active_flag": self.active,
            "present": self.is_present(now),
            "age_ms": int(max(0.0, now - self.last_update) * 1000) if self.last_update else None,
            "trail": [[round(tx, 1), round(ty, 1)] for tx, ty, _ in self.trail],
        }


class SensorState:
    """전체 센서 상태(타겟 + 부가 센서)를 보관하는 스레드 안전 저장소."""

    def __init__(self):
        self._lock = threading.Lock()
        self.targets: Dict[int, Target] = {i: Target(i) for i in range(1, MAX_TARGETS + 1)}
        self.illuminance: Optional[float] = None
        self.zones: Dict[int, float] = {}
        self.misc: Dict[str, Dict] = {}
        self.connected = False
        self.port: Optional[str] = None
        self.mode = "disconnected"   # serial | demo | disconnected
        self.last_line_ts = 0.0
        self.lines_total = 0
        self._line_times: Deque[float] = deque(maxlen=120)  # 라인 수신 시각(레이트 계산)

    # ---- 업데이트 진입점 (리더 스레드에서 호출) ----
    def apply_updates(self, updates: List[Dict], now: float):
        if not updates:
            return
        with self._lock:
            for u in updates:
                kind = u["kind"]
                if kind == "target":
                    t = self.targets.get(u["target"])
                    if t is None:
                        continue
                    field, val = u["field"], u["value"]
                    if math.isnan(val):
                        continue
                    setattr(t, field, val)
                    t.last_update = now
                    # X/Y 가 갱신되면 거리 보정. 로그는 X 다음 Y 순서로 오므로
                    # 궤적은 Y 갱신 시점(= X/Y 쌍이 완성된 시점)에만 1점 기록한다.
                    if field in ("x", "y"):
                        t.distance = P.distance_from_xy(t.x, t.y)
                        if abs(t.x) < 1.0 and abs(t.y) < 1.0:
                            t.trail.clear()   # 타겟이 사라지면 잔상 궤적 제거
                        elif field == "y" and abs(t.x) > 1.0 and abs(t.y) > 1.0:
                            t.trail.append((t.x, t.y, now))
                elif kind == "target_active":
                    t = self.targets.get(u["target"])
                    if t is not None:
                        t.active = u["value"]
                        t.last_update = now
                        if not u["value"]:
                            t.x = t.y = t.speed = 0.0
                elif kind == "illuminance":
                    self.illuminance = u["value"]
                elif kind == "zone_count":
                    self.zones[u["zone"]] = u["value"]
                elif kind == "misc":
                    self.misc[u["name"]] = {"value": u["value"], "unit": u["unit"]}

    def mark_line(self, now: float):
        with self._lock:
            self.lines_total += 1
            self.last_line_ts = now
            self._line_times.append(now)

    def set_conn(self, connected: bool, port: Optional[str], mode: str):
        with self._lock:
            self.connected = connected
            self.port = port
            self.mode = mode

    def _line_rate(self, now: float) -> float:
        times = [t for t in self._line_times if now - t <= 2.0]
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return (len(times) - 1) / span if span > 0 else 0.0

    def snapshot(self) -> Dict:
        now = time.monotonic()
        with self._lock:
            targets = [t.to_dict(now) for t in self.targets.values()]
            present = [t for t in targets if t["present"]]
            return {
                "ts": now,
                "connected": self.connected,
                "mode": self.mode,
                "port": self.port,
                "targets": targets,
                "present_count": len(present),
                "illuminance": self.illuminance,
                "zones": dict(self.zones),
                "misc": dict(self.misc),
                "stats": {
                    "lines_total": self.lines_total,
                    "line_rate": round(self._line_rate(now), 1),
                    "stale_ms": int((now - self.last_line_ts) * 1000) if self.last_line_ts else None,
                },
                "config": {"range_mm": RANGE_MM, "fov_deg": FOV_DEG, "max_targets": MAX_TARGETS},
            }


def _demo_people(t: float):
    """방(room) 좌표에서 걷는 합성 인원 목록 [(px, py, vx, vy)] (mm, mm/s).
    여러 데모 센서가 '같은 사람'을 각자 시야에서 보고하도록, 공통 공간에서 정의한다."""
    p0 = (1200 * math.sin(0.5 * t), 2500 + 900 * math.sin(0.35 * t + 0.6),
          600 * math.cos(0.5 * t), 315 * math.cos(0.35 * t + 0.6))
    p1 = (-1000 + 900 * math.cos(0.4 * t), 2300 + 800 * math.sin(0.45 * t + 1.2),
          -360 * math.sin(0.4 * t), 360 * math.cos(0.45 * t + 1.2))
    return [p0, p1]


class DemoSensorThread(threading.Thread):
    """융합 시연용 합성 센서. 방 안의 '사람들'(_demo_people)을 이 센서의 설치자세로
    역변환(sensor_local)해 로컬 좌표로 보고한다. 시야(FOV/range) 밖이면 안 보고한다.
    → 여러 센서가 같은 사람을 각자 보고 → SensorHub 융합이 하나의 트랙으로 묶는다."""

    def __init__(self, state: "SensorState", meta: Dict, label: str = "DEMO"):
        super().__init__(daemon=True)
        self.state = state
        self.meta = dict(meta)
        self.label = label
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        self.state.set_conn(True, self.meta.get("name", self.label), "demo")
        fov = self.meta.get("fov_deg", FOV_DEG)
        rng = self.meta.get("range_mm", RANGE_MM)
        sx, sy = self.meta.get("x", 0.0), self.meta.get("y", 0.0)
        t0 = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            t = now - t0
            people = _demo_people(t)
            updates = []
            seen = set()
            for pi, (px, py, vx, vy) in enumerate(people):
                lx, ly = sensor_local(self.meta, px, py)
                if ly <= 1.0 or math.hypot(lx, ly) > rng:
                    continue
                if abs(math.degrees(math.atan2(lx, ly))) > fov:
                    continue
                tid = pi + 1
                seen.add(tid)
                dxr, dyr = px - sx, py - sy
                nr = math.hypot(dxr, dyr) or 1.0
                v_radial = (vx * dxr + vy * dyr) / nr / 1000.0   # m/s (시선방향 성분)
                updates += [
                    {"kind": "target_active", "target": tid, "value": True},
                    {"kind": "target", "target": tid, "field": "x", "value": lx},
                    {"kind": "target", "target": tid, "field": "y", "value": ly},
                    {"kind": "target", "target": tid, "field": "speed", "value": v_radial},
                    {"kind": "target", "target": tid, "field": "angle",
                     "value": P.angle_from_xy(lx, ly)},
                ]
            for tid in (1, 2, 3):
                if tid not in seen:
                    updates.append({"kind": "target_active", "target": tid, "value": False})
            updates += [
                {"kind": "illuminance", "value": 250 + 50 * math.sin(t * 0.2)},
                {"kind": "zone_count", "zone": 1, "value": len(seen)},
            ]
            self.state.apply_updates(updates, now)
            self.state.mark_line(now)
            time.sleep(0.05)


# ============================================================================
# 다중 센서(오버레이) 지원
# ============================================================================
#
# 각 센서는 자기만의 로컬 좌표계(원점=센서, X=좌우, Y=전방)로 타겟을 보고한다.
# 여러 센서를 한 화면에 겹쳐 그리려면 각 센서의 방(room) 안 위치/방향을 알아야 한다.
# meta 에 담긴 배치값(x, y, heading_deg, flip)으로 로컬 좌표를 방 좌표로 변환한다.
#
#   meta["x"], meta["y"]  : 방 좌표계에서 센서의 위치 (mm)
#   meta["heading_deg"]   : 센서 정면(로컬 +Y)이 방 +Y 축에서 반시계로 회전한 각도(°)
#   meta["flip"]          : 좌우반전(로컬 X 부호 반전) ─ 센서 장착 방향 보정
#
def room_transform(meta: Dict, lx: float, ly: float) -> Tuple[float, float]:
    """센서 로컬 좌표(lx=좌우, ly=전방, mm) → 방 좌표계(rx, ry, mm).

    설치자세(Yaw·Pitch·Roll)로 왜곡된 보고좌표를 실제 바닥거리로 복원한다.
    2D 레이더라 설치각에 따라 축이 압축돼 보고되므로 그 역수로 되돌린다:
      · Pitch(상하) → 전방(Y) 스케일 1/cos(pitch)  (아래로 숙일수록 전방이 압축)
      · Roll(기울)  → 좌우(X) 스케일 1/cos(roll)   (갸우뚱할수록 좌우가 압축)
      · Pitch·Roll 동시 → shear(sinR·sinP) 결합항까지 보정
    room = pos + A⁻¹·local,  A = U·Rot(−yaw),  U=[[s·cosR, s·sinR·sinP],[0, cosP]].
    (기본 pitch=roll=0 → 기존 강체 동작과 동일.)"""
    yaw = math.radians(meta.get("heading_deg", 0.0))
    pit = math.radians(meta.get("pitch_deg", 0.0))
    rol = math.radians(meta.get("roll_deg", 0.0))
    s = -1.0 if meta.get("flip") else 1.0
    cP = max(math.cos(pit), 1e-3)
    cR = max(math.cos(rol), 1e-3)
    sP, sR = math.sin(pit), math.sin(rol)
    # U⁻¹·local  (secR 좌우 확대 + shear, secP 전방 확대)
    ux = s * lx / cR - ly * sR * sP / (cR * cP)
    uy = ly / cP
    c, sn = math.cos(yaw), math.sin(yaw)
    rx = meta.get("x", 0.0) + c * ux - sn * uy
    ry = meta.get("y", 0.0) + sn * ux + c * uy
    return rx, ry


def sensor_local(meta: Dict, rx: float, ry: float) -> Tuple[float, float]:
    """방 좌표(rx, ry) → 센서 로컬 좌표(lx, ly). room_transform 의 역변환.
    (합성 데모: 방 안 '사람'을 각 센서가 어떻게 보고할지 만들 때 사용.)
    local = A·(room−pos),  A = U·Rot(−yaw)."""
    yaw = math.radians(meta.get("heading_deg", 0.0))
    pit = math.radians(meta.get("pitch_deg", 0.0))
    rol = math.radians(meta.get("roll_deg", 0.0))
    s = -1.0 if meta.get("flip") else 1.0
    dx = rx - meta.get("x", 0.0); dy = ry - meta.get("y", 0.0)
    c, sn = math.cos(yaw), math.sin(yaw)
    ex = c * dx + sn * dy          # Rot(−yaw)·(dx,dy)
    ey = -sn * dx + c * dy
    # room_transform 과 동일한 클램프(cos≥1e-3)를 써야 극단 설치각(±90°근처)에서도 정확한 역
    cP = max(math.cos(pit), 1e-3)
    cR = max(math.cos(rol), 1e-3)
    lx = s * cR * ex + s * math.sin(rol) * math.sin(pit) * ey
    ly = cP * ey
    return lx, ly


class SensorHub:
    """여러 센서의 SensorState 를 모아 하나의 통합 스냅샷(오버레이용)으로 제공한다.

    각 센서마다 SensorState 하나 + 배치 meta 하나를 갖는다. 리더 스레드(WifiApiReader
    등)는 add_sensor() 가 돌려준 SensorState 에 개별적으로 데이터를 채우고, 화면 계층은
    snapshot() 하나로 전체 센서 + 방 좌표로 변환된 타겟을 읽는다."""

    def __init__(self, fusion: bool = True):
        self._lock = threading.Lock()
        self._sensors: List[Dict] = []   # [{"meta": {...}, "state": SensorState}, ...]
        # 융합 트래킹 (여러 센서의 검출점을 공통 공간에서 동일인으로 묶어 하나의 ID로)
        self._fuse_lock = threading.Lock()
        self._tracker: Optional[FusionTracker] = None
        self._tracks: List[Dict] = []
        self._last_fuse = 0.0
        self._fuse_interval = 1.0 / 15.0    # 융합 스텝 상한(렌더 호출 빈도와 무관)
        # 원시(융합 전) 데이터 기록 — 디버그/분석/HPO 용 (기본 꺼짐)
        self._rec_fh = None
        self._rec_frame = 0
        if fusion:
            self.enable_fusion()

    def enable_fusion(self, **opts) -> None:
        """멀티센서 융합 트래킹을 켠다(기본 on). opts 는 FusionTracker 파라미터."""
        self._tracker = FusionTracker(**opts)

    # ---- 원시 데이터 기록 (디버그/HPO) ----------------------------------------
    def enable_recording(self, path: str) -> None:
        """융합 '전' 원시 검출 스트림을 JSONL 로 기록한다(분석·하이퍼파라미터 최적화용).

        각 줄 = 융합 프레임 1개:
          · raw    : 센서별 원시 타겟(로컬 lx/ly + 방 rx/ry + speed/angle/dist/res)
          · dets   : 융합 트래커 입력(방 좌표 x/y + 방향 dir)
          · tracks : 현재 HP 로 산출된 융합 결과(참고값 — 원시→ID 매핑 확인용)
        raw/dets 는 FusionTracker.step 의 '입력'(step 전 계산값)이라 융합 HP 의 영향을 전혀
        받지 않는다(오직 FUSE_HZ 만 기록 주기를 결정). tracks 는 그 프레임의 결과라 step 후
        같은 줄에 함께 적는다. → 이 파일을 다른 HP 로 재생(replay)하면
        동일 원시 입력에 대한 서로 다른 ID 매칭 결과를 오프라인으로 비교/최적화할 수 있다.
        헤더 줄(_type=header)에 '기록 후' 적용되는 HP 전체와 센서 외부보정을 함께 남긴다."""
        self.close_recording()                # 이미 열려 있으면 먼저 닫는다(fd 누수 방지)
        fh = open(path, "w", encoding="utf-8")
        tk = self._tracker
        # 기록 헤더의 HP 는 fusion.tracker_params(SSOT 파생)로 얻는다 → CLI/optimize 와 항상 동일 집합.
        hp = tracker_params(tk) if tk is not None else {}
        with self._lock:
            sensors_meta = [{
                "id": e["meta"]["id"], "name": e["meta"].get("name"),
                "x": e["meta"].get("x", 0.0), "y": e["meta"].get("y", 0.0),
                "heading_deg": e["meta"].get("heading_deg", 0.0),
                "pitch_deg": e["meta"].get("pitch_deg", 0.0),
                "roll_deg": e["meta"].get("roll_deg", 0.0),
                "flip": bool(e["meta"].get("flip", False)),
                "fov_deg": e["meta"].get("fov_deg", FOV_DEG),
                "range_mm": e["meta"].get("range_mm", RANGE_MM),
            } for e in self._sensors]
        header = {
            "_type": "header",
            "created_wall": round(time.time(), 3),
            "fuse_hz": round(1.0 / self._fuse_interval, 3) if self._fuse_interval else None,
            "note": "raw/dets 는 융합 HP 적용 전 원시값. tracks 는 현재 HP 기준 참고 결과.",
            "hyperparams_after_record": hp,   # 기록 후(=ID 매칭)부터 적용되는 HP 전체
            "sensors": sensors_meta,          # 외부보정(heading/pitch/roll/flip/pos)=기록 전 적용
            "schema": {
                "raw": "센서별 원시 타겟: tid, lx/ly(로컬mm), rx/ry(방mm), speed, angle, dist, res",
                "dets": "융합 트래커 입력: sid, tid, x/y(방mm), dir[dx,dy]|null",
                "tracks": "현재 HP 융합 결과: id, x/y, members[[sid,tid]], confirmed, counted, coasting, reid, dwell_sec(체류초)",
            },
        }
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        fh.flush()
        self._rec_fh = fh
        self._rec_frame = 0

    def close_recording(self) -> None:
        # _rec_fh 를 null 로 만드는 것은 _fuse_lock 안에서 — _record 가 같은 락에서 write 하므로
        # 쓰기 도중 닫히는 경합을 원천 차단(닫은 뒤엔 어떤 _record 도 이 핸들을 만지지 않음).
        with self._fuse_lock:
            fh = self._rec_fh
            self._rec_fh = None
        if fh is not None:
            try:
                fh.flush(); fh.close()
            except Exception:
                pass

    def _record(self, now: float, sensors_out: List[Dict],
                dets: List[Dict], tracks: List[Dict]) -> None:
        """융합 스텝 직전의 원시 입력 + 결과를 한 줄(JSON) 기록. _fuse_lock 안에서 호출."""
        self._rec_frame += 1
        rec = {
            "t": round(now, 4),
            "wall": round(time.time(), 3),
            "frame": self._rec_frame,
            "raw": [{
                "sid": s["id"],
                "targets": [{
                    "tid": t["id"],
                    "lx": t["x"], "ly": t["y"],          # 센서 로컬 좌표(mm) — 가장 raw
                    "rx": t["rx"], "ry": t["ry"],        # 방(room) 좌표(mm)
                    "speed": t["speed"], "angle": t["angle"],
                    "dist": t["distance"], "res": t["resolution"],
                } for t in s["targets"] if t.get("present")],
            } for s in sensors_out],
            "dets": [{
                "sid": d["sid"], "tid": d["tid"],
                "x": round(d["x"], 1), "y": round(d["y"], 1),
                "dir": ([round(d["dir"][0], 3), round(d["dir"][1], 3)]
                        if d.get("dir") else None),
            } for d in dets],
            "tracks": [{
                "id": tr["id"], "x": tr["x"], "y": tr["y"],
                "members": tr["members"], "confirmed": tr["confirmed"],
                "counted": tr.get("counted"), "coasting": tr.get("coasting"),
                "reid": tr.get("reid"), "dwell_sec": tr.get("dwell_sec"),  # ReID 표식 + 체류시간(초)
            } for tr in tracks],
        }
        try:
            self._rec_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._rec_fh.flush()
        except Exception:
            pass

    def disable_fusion(self) -> None:
        with self._fuse_lock:
            self._tracker = None
            self._tracks = []

    def add_sensor(self, meta: Dict) -> SensorState:
        """센서 하나를 등록하고 그 SensorState 를 반환(리더가 여기에 데이터를 채움)."""
        st = SensorState()
        m = dict(meta)
        m.setdefault("id", short_id_fallback(len(self._sensors)))
        m.setdefault("name", f"Sensor {m['id']}")
        m.setdefault("color", "#27e0c8")
        m.setdefault("x", 0.0)
        m.setdefault("y", 0.0)
        m.setdefault("heading_deg", 0.0)
        m.setdefault("flip", False)
        m.setdefault("pitch_deg", 0.0)   # 상하 설치각(Pitch) → 전방 스케일
        m.setdefault("roll_deg", 0.0)    # 기울어짐(Roll) → 좌우 스케일
        m.setdefault("fov_deg", FOV_DEG)
        m.setdefault("range_mm", RANGE_MM)
        with self._lock:
            self._sensors.append({"meta": m, "state": st})
        return st

    def sensor_states(self) -> List[Tuple[Dict, SensorState]]:
        with self._lock:
            return [(e["meta"], e["state"]) for e in self._sensors]

    @staticmethod
    def _sensor_bounds(meta: Dict) -> Tuple[float, float, float, float]:
        """센서 FOV 부채꼴을 방 좌표로 변환해 (x_min, x_max, y_min, y_max) 를 구한다."""
        R = meta.get("range_mm", RANGE_MM)
        F = meta.get("fov_deg", FOV_DEG)
        pts = [(0.0, 0.0)]
        d = -F
        while d <= F:
            a = math.radians(d)
            pts.append((R * math.sin(a), R * math.cos(a)))
            d += 6
        xs, ys = [], []
        for lx, ly in pts:
            rx, ry = room_transform(meta, lx, ly)
            xs.append(rx); ys.append(ry)
        return min(xs), max(xs), min(ys), max(ys)

    @staticmethod
    def _dets_from_sensors(sensors_out: List[Dict]) -> List[Dict]:
        """스냅샷의 센서별 타겟(방 좌표)에서 융합용 검출점을 만든다.
        방향(dir)은 방 좌표 궤적(rtrail)의 최근 변위에서 추정(움직일 때만)."""
        dets = []
        for s in sensors_out:
            for t in s["targets"]:
                if not t.get("present"):
                    continue
                rt = t.get("rtrail") or []
                dirv = None
                if len(rt) >= 2:
                    j = max(0, len(rt) - 4)
                    dx = rt[-1][0] - rt[j][0]; dy = rt[-1][1] - rt[j][1]
                    n = math.hypot(dx, dy)
                    if n >= 45.0:                       # 최소 이동량 이상일 때만 방향 사용
                        dirv = (dx / n, dy / n)
                dets.append({"sid": s["id"], "tid": t["id"],
                             "x": t["rx"], "y": t["ry"], "dir": dirv})
        return dets

    def _maybe_fuse(self, sensors_out: List[Dict], now: float) -> List[Dict]:
        """융합 스텝을 상한 빈도로 실행하고 최신 트랙을 반환(스레드 안전).
        None 검사와 step 을 같은 락 안에서 원자적으로 수행(동시 disable_fusion 대비)."""
        with self._fuse_lock:
            tracker = self._tracker
            if tracker is None:
                return []
            if now - self._last_fuse >= self._fuse_interval:
                self._last_fuse = now
                dets = self._dets_from_sensors(sensors_out)   # 융합 HP 적용 전 원시 검출(step 입력)
                self._tracks = tracker.step(dets, now)
                if self._rec_fh is not None:                  # 원시 입력(dets) + 그 프레임 결과 기록
                    self._record(now, sensors_out, dets, self._tracks)
            return list(self._tracks)

    def snapshot(self) -> Dict:
        now = time.monotonic()
        with self._lock:
            entries = list(self._sensors)

        sensors_out: List[Dict] = []
        present_total = 0
        any_conn = False
        xmin = ymin = float("inf")
        xmax = ymax = float("-inf")

        for e in entries:
            meta, st = e["meta"], e["state"]
            snap = st.snapshot()
            any_conn = any_conn or snap["connected"]
            present_total += snap["present_count"]

            # 각 타겟에 방 좌표(rx, ry)와 방 좌표 궤적(rtrail)을 추가
            for t in snap["targets"]:
                rx, ry = room_transform(meta, t["x"], t["y"])
                t["rx"] = round(rx, 1)
                t["ry"] = round(ry, 1)
                t["rtrail"] = [
                    [round(px, 1), round(py, 1)]
                    for px, py in (room_transform(meta, p[0], p[1]) for p in t["trail"])
                ]

            bx0, bx1, by0, by1 = self._sensor_bounds(meta)
            xmin = min(xmin, bx0); xmax = max(xmax, bx1)
            ymin = min(ymin, by0); ymax = max(ymax, by1)

            sensors_out.append({
                "id": meta["id"],
                "name": meta["name"],
                "host": meta.get("host", ""),
                "color": meta["color"],
                "x": round(meta.get("x", 0.0), 1),
                "y": round(meta.get("y", 0.0), 1),
                "heading_deg": meta.get("heading_deg", 0.0),
                "flip": bool(meta.get("flip", False)),
                "pitch_deg": meta.get("pitch_deg", 0.0),
                "roll_deg": meta.get("roll_deg", 0.0),
                "fov_deg": meta.get("fov_deg", FOV_DEG),
                "range_mm": meta.get("range_mm", RANGE_MM),
                "connected": snap["connected"],
                "mode": snap["mode"],
                "port": snap["port"],
                "present_count": snap["present_count"],
                "illuminance": snap["illuminance"],
                "zones": snap["zones"],
                "misc": snap["misc"],
                "stats": snap["stats"],
                "targets": snap["targets"],
            })

        if not entries:
            xmin, xmax, ymin, ymax = -RANGE_MM, RANGE_MM, 0.0, RANGE_MM

        # 멀티센서 융합: 여러 센서 검출점을 공통 공간에서 동일인으로 묶은 전역 트랙
        # track_count = '카운트 대상'(counted) 수 — dwell 켜면 진입지연 반영, 끄면 confirmed 와 동일
        tracks = self._maybe_fuse(sensors_out, now)
        track_count = sum(1 for t in tracks if t.get("counted", t.get("confirmed")))
        noise_radius = self._tracker.noise_radius if self._tracker else 0.0

        return {
            "ts": now,
            "connected": any_conn,
            "present_total": present_total,
            "track_count": track_count,        # 융합 후 실제 인원(중복 제거)
            "sensor_count": len(sensors_out),
            "world": {
                "x_min": round(xmin, 1), "x_max": round(xmax, 1),
                "y_min": round(ymin, 1), "y_max": round(ymax, 1),
            },
            "sensors": sensors_out,
            "tracks": tracks,
            "config": {"range_mm": RANGE_MM, "fov_deg": FOV_DEG, "max_targets": MAX_TARGETS,
                       "noise_radius_mm": round(noise_radius, 1)},
        }


def short_id_fallback(index: int) -> str:
    return f"s{index + 1}"
