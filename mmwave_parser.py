"""
ESPHome 로그 라인 파서 (Everything Presence Lite / HLK-LD2450)

Everything Presence Lite 는 ESP32 + HLK-LD2450 24GHz mmWave 레이더 기반 센서다.
기본 ESPHome 펌웨어 상태에서 USB 로 연결하면, USB-시리얼(CH340)로 디버그 로그가
115200 baud 로 흘러나온다. 이 로그 안에 LD2450 가 추적한 타겟 좌표가 텍스트로 들어있다.

예) 실제 관측된 로그 라인:
    [D][sensor:098]: 'Target 1 X': Sending state 268.00000 mm with 0 decimals of accuracy
    [D][sensor:098]: 'Target 1 Y': Sending state 491.00000 mm with 0 decimals of accuracy
    [D][sensor:098]: 'Target 1 Speed': Sending state 0.08000 m/s with 2 decimals of accuracy
    [D][sensor:098]: 'Target 1 Angle': Sending state -28.62681 ° with 0 decimals of accuracy
    [D][sensor:098]: 'Target 1 Distance': Sending state 559.37915 mm with 0 decimals of accuracy
    [D][binary_sensor:...]: 'Target 2 Active': Sending state OFF
    [D][bh1750.sensor:159]: 'Illuminance': Got illuminance=257.5lx
    [D][sensor:098]: 'Zone 1 Target Count': Sending state 1.00000

이 모듈은 한 줄을 입력받아 구조화된 업데이트(dict)들의 리스트로 변환한다.
하드웨어/시리얼과 무관한 순수 함수라서 단위 테스트가 쉽다.

LD2450 좌표계 (위에서 내려다본 top-down 기준):
    - 원점: 센서 위치
    - X: 좌/우 (mm). 센서 정면 기준 한쪽이 +, 반대쪽이 -.
    - Y: 센서로부터의 전방 거리 (mm), 항상 +.
    - Distance = sqrt(X^2 + Y^2)
    - Angle: 센서 정면(0°) 기준 각도(°). 펌웨어 부호는 atan2(X,Y) 와 반대.
    - Speed: 시선방향 속도(m/s). +는 멀어짐 / -는 가까워짐 (펌웨어 정의에 따름).
    - 탐지 범위: 약 6m, 수평 시야각(FOV) 약 ±60°.
"""

from __future__ import annotations

import math
import re
from typing import List, Optional, Dict, Any

# ANSI 컬러 escape 코드 제거용 (로그에 \x1b[0;36m 등이 섞여 있음)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# 'Target N <Field>': Sending state <value> <unit>
_TARGET_STATE = re.compile(
    r"'Target\s+(\d+)\s+"
    r"(X|Y|Speed|Angle|Distance|Resolution)'"
    r"\s*:\s*Sending state\s*(-?[0-9]+(?:\.[0-9]+)?|nan|NaN|inf)"
)

# 'Target N Active': Sending state ON/OFF  (binary_sensor)
_TARGET_ACTIVE = re.compile(
    r"'Target\s+(\d+)\s+Active'\s*:\s*Sending state\s*(ON|OFF)"
)

# 'Illuminance': Got illuminance=257.5lx   (bh1750 컴포넌트 전용 포맷)
_ILLUM_GOT = re.compile(r"'Illuminance'\s*:\s*Got illuminance=\s*(-?[0-9.]+)\s*lx")
# 'Illuminance': Sending state 257.5 lx   (일반 포맷도 대비)
_ILLUM_STATE = re.compile(
    r"'Illuminance'\s*:\s*Sending state\s*(-?[0-9.]+)\s*lx"
)

# 'Zone N Target Count': Sending state <value>
_ZONE_COUNT = re.compile(
    r"'Zone\s+(\d+)\s+Target Count'\s*:\s*Sending state\s*(-?[0-9.]+)"
)

# 그 외 임의의 'Name': Sending state <num> <unit>  (기타 센서를 일반 수집)
_GENERIC_STATE = re.compile(
    r"'([^']+)'\s*:\s*Sending state\s*(-?[0-9]+(?:\.[0-9]+)?|nan|NaN|inf)\s*(\S*)"
)

# 위에서 이미 전용 처리하는 이름들 (generic 수집에서 제외)
_FIELD_SUFFIXES = ("X", "Y", "Speed", "Angle", "Distance", "Resolution", "Active")


def strip_ansi(line: str) -> str:
    return _ANSI.sub("", line)


def _to_float(token: str) -> float:
    t = token.lower()
    if t in ("nan", "inf"):
        return float("nan")
    try:
        return float(token)
    except ValueError:
        return float("nan")


def parse_line(line: str) -> List[Dict[str, Any]]:
    """ESPHome 로그 한 줄을 파싱해 업데이트 dict 리스트를 반환.

    반환되는 dict 종류:
      {"kind": "target", "target": 1, "field": "x", "value": 268.0}
      {"kind": "target_active", "target": 2, "value": False}
      {"kind": "illuminance", "value": 257.5}
      {"kind": "zone_count", "zone": 1, "value": 1.0}
      {"kind": "misc", "name": "...", "value": 12.3, "unit": "°C"}
    """
    line = strip_ansi(line).strip()
    if not line:
        return []

    updates: List[Dict[str, Any]] = []

    m = _TARGET_STATE.search(line)
    if m:
        updates.append({
            "kind": "target",
            "target": int(m.group(1)),
            "field": m.group(2).lower(),
            "value": _to_float(m.group(3)),
        })
        return updates

    m = _TARGET_ACTIVE.search(line)
    if m:
        updates.append({
            "kind": "target_active",
            "target": int(m.group(1)),
            "value": m.group(2) == "ON",
        })
        return updates

    m = _ILLUM_GOT.search(line) or _ILLUM_STATE.search(line)
    if m:
        updates.append({"kind": "illuminance", "value": _to_float(m.group(1))})
        return updates

    m = _ZONE_COUNT.search(line)
    if m:
        updates.append({
            "kind": "zone_count",
            "zone": int(m.group(1)),
            "value": _to_float(m.group(2)),
        })
        return updates

    # 기타 센서(온도, PIR 등)는 일반 수집 (Target 전용 필드는 제외)
    m = _GENERIC_STATE.search(line)
    if m:
        name = m.group(1)
        if not name.startswith("Target ") or not name.endswith(_FIELD_SUFFIXES):
            updates.append({
                "kind": "misc",
                "name": name,
                "value": _to_float(m.group(2)),
                "unit": m.group(3),
            })
    return updates


def angle_from_xy(x: float, y: float) -> float:
    """X,Y(mm)로부터 센서 정면 기준 각도(°)를 계산. (디스플레이 보조용)"""
    return math.degrees(math.atan2(x, y)) if (x or y) else 0.0


def distance_from_xy(x: float, y: float) -> float:
    return math.hypot(x, y)
