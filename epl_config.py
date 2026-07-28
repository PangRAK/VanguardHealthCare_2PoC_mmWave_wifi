"""
프로비저닝 결과(센서들의 네트워크 주소 + 오버레이 배치)를 저장/로드하는 헬퍼.

provision_wifi.py 가 Wi-Fi 연결 성공 후 여기에 센서를 등록하고,
시각화(server.py / gui_qt.py / cli_monitor.py)가 이 목록을 읽어 무선으로 접속한다.

[다중 센서 스키마 (현재)]
{
  "sensors": [
    {
      "id": "98bd80",                                  # 짧은 식별자
      "host": "everything-presence-lite-98bd80.local", # 접속 주소(IP 또는 mDNS)
      "node_name": "everything-presence-lite-98bd80",
      "name": "Sensor 98bd80",                         # 화면 표시 이름
      "x": 0, "y": 0,          # 방(room) 좌표계에서 센서 위치 (mm) ─ 오버레이 배치
      "heading_deg": 0,        # Yaw(좌우 설치각): 정면이 방 +Y축에서 반시계로 돈 각(°) → 동서남북 방향
      "pitch_deg": 0,          # Pitch(상하 설치각): 아래로 숙인 각(°) → 전방 스케일 1/cos
      "roll_deg": 0,           # Roll(기울어짐): 정면축 기준 갸우뚱(°) → 좌우 스케일 1/cos
      "flip": false,           # 좌우반전(거울 장착 보정)
      "color": "#27e0c8",      # 이 센서 타겟의 오버레이 색상
      "api_password": "",
      "noise_psk": null
    }, ...
  ],
  "ssid": "seoulaihub1",
  "discovery": true            # 실행 시 mDNS 자동 탐색 사용 여부
}

[구버전 스키마 (단일 host)]
    {"host": "...", "url": "...", "node_name": "...", "ssid": "...",
     "api_password": "", "noise_psk": null}
  → get_sensors() 가 자동으로 1개짜리 sensors 목록으로 변환해 읽는다(파일은 안 건드림).
    provision 이 다시 저장할 때 새 스키마로 마이그레이션된다.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epl_config.json")

# 센서별 기본 색상 팔레트 (오버레이에서 센서를 색으로 구분)
DEFAULT_PALETTE = ["#27e0c8", "#ffb454", "#ff5d8f", "#6ea8ff", "#b18cff", "#7ce38b"]


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(data: Dict[str, Any], path: str = CONFIG_PATH) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


# ----------------------------------------------------------------- 정규화 헬퍼
def short_id(node_name: str = "", host: str = "") -> str:
    """node_name/host 에서 짧은 식별자를 뽑는다. (예: everything-presence-lite-98bd80 -> 98bd80)"""
    base = (node_name or host or "").strip()
    base = base.split(".", 1)[0]           # host 의 .local 등 제거
    if "-" in base:
        tail = base.rsplit("-", 1)[-1]
        if tail:
            return tail
    return base or "sensor"


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pitch_from_legacy(raw: Dict[str, Any]) -> float:
    """구 필드 fwd_scale(=1/cos(pitch)) 이 있으면 pitch(°)로 환산."""
    if "fwd_scale" not in raw:
        return 0.0
    try:
        k = max(1.0, float(raw["fwd_scale"]))
    except (TypeError, ValueError):
        return 0.0
    return math.degrees(math.acos(1.0 / k)) if k > 0 else 0.0


def normalize_sensor(raw: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    """부분 정의된 센서 dict 를 모든 필드가 채워진 형태로 정규화한다."""
    raw = dict(raw or {})
    node_name = str(raw.get("node_name") or "").strip()
    host = str(raw.get("host") or "").strip()
    sid = str(raw.get("id") or "").strip() or short_id(node_name, host)
    name = str(raw.get("name") or "").strip() or f"Sensor {sid}"
    color = str(raw.get("color") or "").strip() or DEFAULT_PALETTE[index % len(DEFAULT_PALETTE)]
    return {
        "id": sid,
        "host": host,
        "node_name": node_name,
        "name": name,
        "x": _as_float(raw.get("x"), 0.0),
        "y": _as_float(raw.get("y"), 0.0),
        "heading_deg": _as_float(raw.get("heading_deg"), 0.0),   # Yaw (좌우 설치각)
        "pitch_deg": _as_float(raw.get("pitch_deg"), _pitch_from_legacy(raw)),  # Pitch (상하)
        "roll_deg": _as_float(raw.get("roll_deg"), 0.0),         # Roll (기울어짐)
        "flip": bool(raw.get("flip", False)),
        "color": color,
        "api_password": str(raw.get("api_password") or ""),
        "noise_psk": raw.get("noise_psk") or None,
    }


def get_sensors(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """config 에서 정규화된 센서 목록을 반환. 구버전(단일 host) 스키마도 처리한다."""
    if cfg is None:
        cfg = load_config()
    raw_list = cfg.get("sensors")
    if not raw_list:
        # 구버전: 최상위 host 한 개 → 1개짜리 목록으로 변환
        if cfg.get("host") or cfg.get("node_name"):
            raw_list = [{
                "host": cfg.get("host", ""),
                "node_name": cfg.get("node_name", ""),
                "api_password": cfg.get("api_password", ""),
                "noise_psk": cfg.get("noise_psk"),
            }]
        else:
            raw_list = []
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(raw_list):
        s = normalize_sensor(raw, i)
        if s["host"] or s["node_name"]:
            out.append(s)
    return out


def upsert_sensor(cfg: Dict[str, Any], sensor: Dict[str, Any]) -> Dict[str, Any]:
    """센서를 config['sensors'] 에 추가하거나(같은 id/node_name/host 면) 갱신한다.

    이미 있는 센서의 배치값(x/y/heading_deg/flip/color/name)은 보존하고,
    주소(host/node_name)와 인증정보만 새 값으로 덮어쓴다. 저장된 sensor dict 반환."""
    sensors = cfg.setdefault("sensors", [])
    incoming = normalize_sensor(sensor, len(sensors))

    def _same(a: Dict[str, Any]) -> bool:
        return (a.get("id") and a.get("id") == incoming["id"]) or \
               (a.get("node_name") and a.get("node_name") == incoming["node_name"]) or \
               (a.get("host") and a.get("host") == incoming["host"])

    for existing in sensors:
        if _same(existing):
            existing.update({
                "id": incoming["id"],
                "host": incoming["host"] or existing.get("host", ""),
                "node_name": incoming["node_name"] or existing.get("node_name", ""),
                "api_password": incoming["api_password"] or existing.get("api_password", ""),
                "noise_psk": incoming["noise_psk"] if incoming["noise_psk"] is not None
                             else existing.get("noise_psk"),
            })
            # 배치/표시 필드는 이미 있으면 보존, 없으면 기본값 채움
            for k in ("name", "x", "y", "heading_deg", "pitch_deg", "roll_deg", "flip", "color"):
                existing.setdefault(k, incoming[k])
            existing.pop("fwd_scale", None)   # 구 필드 정리
            return existing

    sensors.append(incoming)
    return incoming
