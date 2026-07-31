"""
프로비저닝 결과(센서들의 네트워크 주소 + 오버레이 배치)를 저장/로드하는 헬퍼.

provision_wifi.py 가 Wi-Fi 연결 성공 후 여기에 센서를 등록하고,
시각화(server.py / gui_qt.py / cli_monitor.py)가 이 목록을 읽어 무선으로 접속한다.

[다중 센서 스키마 (현재)]
{
  "sensors": [
    {
      "id": "pia-1-1",                                 # ★ {organization}-{cameraId}-{sensorId}
      "host": "10.201.31.120",                         # 접속 주소(고정 IP 권장, mDNS 이름도 가능)
      "mac": "A4:F0:0F:98:BD:80",                      # 물리 센서 대조용(코드는 읽지 않는다)
      "node_name": "everything-presence-lite-98bd80",
      "name": "Sensor 1",                              # 화면 표시 이름
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
  "ssid": "RAK",
  "discovery": true            # 실행 시 mDNS 자동 탐색 사용 여부
}

[센서 id 규격 — 카메라 귀속의 유일한 표현]
제품(Product-AI-mono)의 알람은 **카메라 단위**로 발화하고, MQ 봉투의 cameraId 는
백엔드가 등록한 AddStreamModel.cameraId 를 그대로 싣는다. 그래서 이 파일이 답할 것은
하나뿐이다 — *그 카메라(스트림)에 묶인 센서가 누구인가*. 그 답을 별도 매핑 테이블이 아니라
센서 id 자체에 담는다. get_sensors_for_camera() 는 **제품 쪽과 같은 로직**이어야 한다.

    id = "{organization}-{cameraId}-{sensorId}"        예: "pia-1-1"

  · 파싱은 split("-", 2) 3-파트. organization/cameraId 파트에는 '-' 를 쓸 수 없고
    (앞의 두 구분자가 경계라 구조적으로 불가능), **sensorId 파트에는 '-' 를 허용**한다
    — host 유래 자동 id '10-201-31-120' 을 그대로 담기 위해서다.
  · 세 파트 전부 **불투명 문자열**로 다룬다. 숫자로 파싱하지 않고 순서에서 의미를 끌어내지
    않는다 → sensorId 를 1↔7 로 바꿔도 유일성만 지켜지면 동작이 같다.
  · 같은 cameraId 파트를 쓰는 센서들 = 한 카메라(한 스트림)에 묶인 센서들. organization 도
    함께 대조한다(다른 조직의 센서를 빌리지 않도록).
  · 매칭 센서가 0개면 **빈 목록**을 준다 — 남의 카메라 센서를 빌려 쓰면 엉뚱한 카메라의
    재실을 보고하기 때문이다(폴백 금지).
  · id 형식 위반 / 중복 / 같은 host 중복 / 옛 'rooms' 스키마는 ValueError 로 실패시킨다.
  · x/y/heading_deg 는 **방 좌표계** 값이다 → 카메라를 나누면 카메라마다 별도의 방
    좌표계이므로 카메라마다 따로 ./run_auto_positioning{,_v2}.sh --camera-id <id> 를
    돌려야 한다.
  · mac 은 코드가 읽지 않는다(normalize_sensor 반환에 없다). sensorId 가 순번이라 id
    만으로는 어느 물리 센서인지 알 수 없으므로 **파일의 mac 이 유일한 대조 수단**이다.

  ★ cameraId 파트는 현장 설치 시 **병원이 부여한 실제 숫자 cameraId** 로 교체해야 한다.
    제품의 AddStreamModel.cameraId 는 int 라서 기본값 'camera1' 은 어떤 스트림과도
    매칭되지 않는다(= 그 스트림은 센서 0개).

[구버전 스키마 (단일 host)]
    {"host": "...", "url": "...", "node_name": "...", "ssid": "...",
     "api_password": "", "noise_psk": null}
  → get_sensors() 가 자동으로 1개짜리 sensors 목록으로 변환해 읽는다(파일은 안 건드림).
    단 그 항목에도 규격 id 가 있어야 한다(자동 생성 id 는 3-파트가 아니라 형식 오류다).
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epl_config.json")

# 센서별 기본 색상 팔레트 (오버레이에서 센서를 색으로 구분)
DEFAULT_PALETTE = ["#27e0c8", "#ffb454", "#ff5d8f", "#6ea8ff", "#b18cff", "#7ce38b"]

# 도구 3종(run_provision / run_auto_positioning{,_v2})이 공유하는 카메라(stream) 기본값.
# 카메라를 나누지 않는 현장은 이 한 카메라에 전 센서가 들어간다.
# ★ 현장 설치 시 병원이 부여한 **숫자 cameraId** 로 교체해야 한다 — 제품의
#   AddStreamModel.cameraId 는 int 라 'camera1' 은 어떤 스트림과도 매칭되지 않는다.
DEFAULT_CAMERA_ID = "camera1"
DEFAULT_ORGANIZATION = "pia"


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
# host 끝에서 잘라낼 mDNS/로컬 DNS 접미사.
# ★ '첫 점에서 자르기' 는 금지 — IP(10.201.31.120)가 첫 옥텟('10')으로 뭉개져 같은 대역
#   센서 전부가 같은 id 가 된다(아래 short_id 주석 참조).
_DNS_SUFFIXES = (".local", ".lan", ".home")


def strip_dns_suffix(base: str) -> str:
    """host 끝의 DNS 접미사(.local 등)만 제거한다. 그 밖의 점은 보존."""
    low = base.lower()
    for suffix in _DNS_SUFFIXES:
        if low.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _is_ipv4(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def short_id(node_name: str = "", host: str = "") -> str:
    """node_name/host 에서 짧은 식별자를 뽑는다. (예: everything-presence-lite-98bd80 -> 98bd80)

    ★ IP 는 점으로 자르지 않고 전체를 쓴다(10.201.31.120 -> 10-201-31-120).
      첫 옥텟만 남기면 같은 대역 센서 3대가 모두 '10' 이 되고, 그 id 가 융합 입력의
      sid 로 쓰이므로 (sid, tid) 가 충돌한다 → 같은 tid 면 두 사람이 중간 지점 한 명으로
      합쳐지고(median), 다른 tid 면 '센서 겹침' 판정에 걸려 한 사람이 두 명으로 세어진다.
      짧음보다 유일성이 우선이다 — 화면 표시는 name 이 담당한다."""
    base = (node_name or host or "").strip()
    if _is_ipv4(base):
        return base.replace(".", "-")
    base = strip_dns_suffix(base).split(".", 1)[0]
    if "-" in base:
        tail = base.rsplit("-", 1)[-1]
        if tail:
            return tail
    return base or "sensor"


# --------------------------------------------------------------- 센서 id 규격
# id = "{organization}-{cameraId}-{sensorId}" — 모듈 독스트링 [센서 id 규격] 참조.
SENSOR_ID_SEPARATOR = "-"

# 제품 알람 키가 f"{stream_id}__{category}" 이고 그쪽이 "__" 로 split 한다. id 안에 "__" 가
# 들어가면 카테고리 추출이 오염되고 그 스트림 알람이 통째로 사라진다 → id 단계에서 금지.
_FORBIDDEN_IN_ID = "__"

_ID_FORMAT_HINT = (
    "센서 id 는 '{organization}-{cameraId}-{sensorId}' 형식이어야 합니다"
    " (예: 'pia-1-1'). organization/cameraId 파트에는 '-' 를 쓸 수 없고,"
    " sensorId 파트에는 쓸 수 있습니다(예: 'pia-1-10-201-31-120')."
)


def _norm(value: Any) -> str:
    """식별자 비교용 정규화. 대소문자·앞뒤 공백 차이로 매칭이 갈리지 않게 한다."""
    return str(value if value is not None else "").strip().lower()


def parse_sensor_id(sid: str) -> Tuple[str, str, str]:
    """센서 id 를 (organization, cameraId, sensorId) 로 분해한다.

    세 파트 전부 **불투명 문자열**로 돌려준다 — 숫자로 바꾸지 않고 순서에서 의미를 끌어내지
    않는다. 앞의 두 구분자만 경계로 쓰므로(split maxsplit=2) sensorId 파트에는 '-' 가 남는다
    (host 유래 자동 id '10-201-31-120' 을 그대로 담기 위한 규격이다).

    Raises:
        ValueError: 파트가 3개 미만이거나 빈 파트가 있거나 '__' 를 포함할 때."""
    raw = str(sid or "").strip()
    if _FORBIDDEN_IN_ID in raw:
        raise ValueError(
            f"센서 id 에 '{_FORBIDDEN_IN_ID}' 를 쓸 수 없습니다: '{raw}'. "
            "제품 알람 키가 '{stream_id}__{category}' 라서 카테고리 추출이 깨지고 "
            "그 스트림 알람이 통째로 사라집니다."
        )
    parts = raw.split(SENSOR_ID_SEPARATOR, 2)
    if len(parts) < 3 or not all(p.strip() for p in parts):
        raise ValueError(f"센서 id 형식 오류: '{raw}'. {_ID_FORMAT_HINT}")
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def make_sensor_id(organization: Any, camera_id: Any, sensor_id: Any) -> str:
    """(organization, cameraId, sensorId) 를 규격 id 문자열로 조립한다.

    Raises:
        ValueError: 어떤 파트가 비었거나, organization/cameraId 에 '-' 또는 '__' 가 있을 때."""
    org = str(organization if organization is not None else "").strip()
    cam = str(camera_id if camera_id is not None else "").strip()
    sen = str(sensor_id if sensor_id is not None else "").strip()
    if not org or not cam or not sen:
        raise ValueError(
            "센서 id 를 만들 수 없습니다 — organization / cameraId / sensorId 가 모두 "
            f"필요합니다 (지금은 '{org}' / '{cam}' / '{sen}'). {_ID_FORMAT_HINT}"
        )
    for label, part in (("organization", org), ("cameraId", cam)):
        if SENSOR_ID_SEPARATOR in part:
            raise ValueError(
                f"{label} 에 '{SENSOR_ID_SEPARATOR}' 를 쓸 수 없습니다: '{part}'. {_ID_FORMAT_HINT}"
            )
    for label, part in (("organization", org), ("cameraId", cam), ("sensorId", sen)):
        if _FORBIDDEN_IN_ID in part:
            raise ValueError(f"{label} 에 '{_FORBIDDEN_IN_ID}' 를 쓸 수 없습니다: '{part}'.")
    return SENSOR_ID_SEPARATOR.join((org, cam, sen))


def assert_sensor_id_format(sensors: List[Dict[str, Any]], source: str = "") -> None:
    """모든 센서 id 가 3-파트 규격을 따르는지 검증. 하나라도 어긋나면 ValueError.

    ★ 조용히 건너뛰지 않는다 — 규격 위반 센서를 무시하면 그 센서가 어느 카메라에도 붙지
      않아 재실 인원이 조용히 줄고, 운영자에게는 '센서가 없습니다' 로만 보인다.
    ※ 3-파트라고 뜻까지 맞는 건 아니다. host 유래 자동 id '10-201-31-120' 은 형식은
      통과하지만 organization 파트가 '10' 이라 실제 스트림('pia')과 대조되지 않는다 →
      get_sensors_for_camera 의 organization 대조가 최종 안전장치다."""
    bad: List[str] = []
    for s in sensors:
        try:
            parse_sensor_id(s.get("id", ""))
        except ValueError:
            bad.append(str(s.get("id") or "(빈 id)"))
    if bad:
        where = f" ({source})" if source else ""
        raise ValueError(
            f"센서 id 가 규격에 맞지 않습니다{where}: "
            f"{', '.join(repr(b) for b in bad)}. {_ID_FORMAT_HINT}"
        )


def assert_no_legacy_schema(cfg: Optional[Dict[str, Any]], source: str = "") -> None:
    """폐기된 방(rooms) 스키마가 남아 있으면 ValueError.

    ★ 자동 마이그레이션하지 않는다 — 옛 rooms 를 새 id 로 옮기려면 코드가 센서 id 를
      바꿔야 하는데, id 는 융합 입력의 sid 이자 화면·기록 JSONL 의 센서 식별자다.
      코드가 조용히 바꾸면 화면과 로그가 실동작과 어긋난다."""
    cfg = cfg or {}
    where = f" ({source})" if source else ""
    if "rooms" in cfg:
        raise ValueError(
            f"'rooms' 스키마는 폐기됐습니다{where}. 방↔센서 매핑 대신 센서 id 에 카메라를 "
            f"담습니다. {_ID_FORMAT_HINT} 'rooms' 키를 지우고 각 센서의 'id' 를 새 형식으로 "
            "다시 적으세요."
        )
    tagged = [
        str(s.get("id") or s.get("host") or "?")
        for s in (cfg.get("sensors") or [])
        if isinstance(s, dict) and str(s.get("room") or "").strip()
    ]
    if tagged:
        raise ValueError(
            f"센서별 'room' 태그는 폐기됐습니다{where}: "
            f"{', '.join(repr(t) for t in tagged)}. {_ID_FORMAT_HINT} "
            "'room' 키를 지우고 'id' 를 새 형식으로 다시 적으세요."
        )


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


def normalize_sensor(
    raw: Dict[str, Any],
    index: int = 0,
    organization: Any = None,
    camera_id: Any = None,
) -> Dict[str, Any]:
    """부분 정의된 센서 dict 를 모든 필드가 채워진 형태로 정규화한다.

    ★ id 자동 생성은 **호출측이 org/cameraId 를 줄 수 있을 때만** 완전한 3-파트 id 를
      만든다. 설정 파일 경로에는 그 문맥이 없으므로 자동 생성 id 는 3-파트가 되지 못하고
      assert_sensor_id_format 에서 형식 오류로 걸린다 — 파일에는 id 를 명시해야 한다."""
    raw = dict(raw or {})
    node_name = str(raw.get("node_name") or "").strip()
    host = str(raw.get("host") or "").strip()
    sid = str(raw.get("id") or "").strip()
    if not sid:
        base = short_id(node_name, host)
        sid = (
            make_sensor_id(organization, camera_id, base)
            if organization is not None and camera_id is not None
            else base
        )
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


def assert_unique_sensor_ids(sensors: List[Dict[str, Any]], source: str = "") -> None:
    """센서 id 유일성 검증. 한 id 가 서로 다른 host 를 가리키면 ValueError.

    ★ 자동 개명하지 않는다 — 'a, a, a-1' 처럼 명시된 id 와 다시 충돌할 수 있고, 사용자가
      적은 id 를 코드가 바꾸면 화면·로그의 센서 식별자가 실동작과 달라진다. 그대로 진행하면
      융합 입력이 (sid, tid) 로 합쳐져 재실 인원이 틀어지므로(같은 tid → 2인이 1인,
      다른 tid → 1인이 2인) 설정 오류로 즉시 실패시킨다.
    ※ 완전히 동일한 항목(같은 id + 같은 host)이 두 번 적힌 경우는 중복 기입일 뿐
      모호함이 없어 허용한다(중복 제거는 호출측 _spec_key 가 담당)."""
    by_id: Dict[str, set] = {}
    for s in sensors:
        by_id.setdefault(s["id"], set()).add(s.get("host") or s.get("node_name") or "?")
    dups = {sid: sorted(hosts) for sid, hosts in by_id.items() if len(hosts) > 1}
    if dups:
        detail = "; ".join(f"'{sid}' -> hosts={hosts}" for sid, hosts in sorted(dups.items()))
        where = f" ({source})" if source else ""
        raise ValueError(
            f"센서 id 가 중복입니다{where}: {detail}. 센서마다 다른 'id' 를 지정하거나, "
            "'id' 를 비워 host/node_name 에서 자동 생성되게 하세요."
        )


def get_sensors(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """config 에서 정규화된 센서 목록을 반환. 구버전(단일 host) 스키마도 처리한다."""
    if cfg is None:
        cfg = load_config()
    assert_no_legacy_schema(cfg, source="센서 설정 파일")
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
    missing_id: List[str] = []
    for i, raw in enumerate(raw_list):
        s = normalize_sensor(raw, i)
        if not (s["host"] or s["node_name"]):
            continue
        if not str((raw or {}).get("id") or "").strip():
            missing_id.append(s["host"] or s["node_name"])
        out.append(s)
    if missing_id:
        # ★ 파일 경로에는 org/cameraId 문맥이 없어 자동 생성 id 가 귀속을 담지 못한다.
        #   더 나쁜 것은 host 유래 id 가 '형식은 통과' 한다는 점이다 — '10.0.0.13' →
        #   '10-0-0-13' 은 3파트로 파싱돼(org='10') 조용히 아무 카메라에도 안 붙는다.
        raise ValueError(
            f"'id' 가 없는 센서 항목이 있습니다(센서 설정 파일): "
            f"{', '.join(repr(h) for h in missing_id)}. {_ID_FORMAT_HINT} "
            "설정 파일에는 자동 생성이 불가능하므로 id 를 직접 적어야 합니다."
        )
    # 파일 전체 기준 규격·유일성. id 가 곧 카메라 귀속이므로 형식을 먼저 본다.
    assert_sensor_id_format(out, source="센서 설정 파일")
    assert_unique_sensor_ids(out, source="센서 설정 파일")
    assert_unique_sensor_hosts(out, source="센서 설정 파일")
    return out


def assert_unique_sensor_hosts(sensors: List[Dict[str, Any]], source: str = "") -> None:
    """같은 접속 주소(host)를 서로 다른 id 로 두 번 적으면 ValueError.

    ★ 옛 assert_one_room_per_sensor 가 막던 사고를 새 규격에서 이어받는다. id 가 카메라를
      인코딩하므로 '한 센서가 두 카메라' 는 이제 **host 중복**으로만 나타난다: 'pia-1-1' 과
      'pia-2-1' 이 같은 host 를 가리키면 두 카메라가 같은 기기에 각각 붙어(TCP 6053 x2),
      물리적으로 카메라 1 쪽에 있는 사람을 카메라 2 도 재실로 세고 알람이 두 번 나간다."""
    by_host: Dict[str, set] = {}
    for s in sensors:
        host = _norm(s.get("host") or s.get("node_name"))
        if host:
            by_host.setdefault(host, set()).add(s["id"])
    dups = {host: sorted(ids) for host, ids in by_host.items() if len(ids) > 1}
    if dups:
        detail = "; ".join(f"'{host}' -> ids={ids}" for host, ids in sorted(dups.items()))
        where = f" ({source})" if source else ""
        raise ValueError(
            f"같은 host 를 여러 센서 id 가 가리킵니다{where}: {detail}. 한 기기는 한 번만 "
            "등록하세요 — 두 카메라가 같은 센서에 붙으면 같은 사람을 각각 재실로 세어 "
            "알람이 중복 발화합니다."
        )


# --------------------------------------------------------- 카메라 ↔ 센서 귀속
#
# ★ assert_one_room_per_sensor 는 삭제했다 — 구조적으로 불필요해졌다. 센서 id 하나가
#   정확히 한 cameraId 를 가리키므로 '한 센서가 두 카메라에 걸침' 을 표현할 수 없다.
#   같은 기기를 두 번 적는 사고는 assert_unique_sensor_hosts 가 이어받는다.
# ★ _sensor_keys / _rooms_of / get_room_ids / get_sensors_for_room 도 삭제했다 —
#   rooms 목록 대조가 사라져 존재 이유가 없다. _sensor_keys 가 허용하던 관용성(rooms 에
#   id 대신 host/node_name 을 적어도 매칭)도 함께 사라진다: 귀속은 이제 id 에만 있다.


def get_camera_ids(cfg: Optional[Dict[str, Any]] = None, organization: Any = None) -> List[str]:
    """설정에 등장하는 cameraId 목록(센서 id 등장 순, 중복 제거).

    organization 을 주면 그 조직의 카메라만 센다 — 운영자에게 보여줄 목록이라 조직을
    섞으면 안 된다(acme 의 '1' 과 pia 의 '1' 이 같은 '1' 로 뭉개진다).

    빈 목록이면 '이 설정 파일에 (그 조직의) 센서가 없다'는 뜻이다."""
    if cfg is None:
        cfg = load_config()
    want_org = _norm(organization) if organization is not None else None
    out: List[str] = []
    seen = set()
    for s in get_sensors(cfg):
        org, camera_id, _sensor = parse_sensor_id(s["id"])
        if want_org is not None and _norm(org) != want_org:
            continue
        key = _norm(camera_id)
        if key not in seen:
            seen.add(key)
            out.append(camera_id)
    return out


def get_sensors_for_camera(
    cfg: Optional[Dict[str, Any]], organization: Any, camera_id: Any
) -> List[Dict[str, Any]]:
    """카메라(=stream) 하나에 묶인 센서 목록.

    ★ 제품 Product-AI-mono/…/mmwave_core/epl_config.py 의 동명 함수와 **같은 로직**이어야
      한다. 도구가 고른 센서 집합과 제품이 실제로 붙는 집합이 달라지면, 엉뚱한 조합으로
      캘리브레이션한 좌표를 저장하게 된다.

    센서 id 의 organization/cameraId 파트가 인자와 모두 일치하는 센서만 돌려준다. 비교는
    문자열 정규화(strip().lower())이며 cameraId 는 int 로 와도 문자열로 다룬다.

    ★ 매칭이 0개여도 **절대 폴백하지 않는다**(전 센서도, 다른 카메라 센서도). 엉뚱한
      카메라의 재실을 보고하면 간호사가 없는 곳으로 출동한다. 빈 목록을 주고 호출측이
      경고/오류를 낸다.
    ★ organization 대조가 '형식은 맞고 뜻은 틀린' id 를 걸러내는 최종 안전장치다."""
    if cfg is None:
        cfg = load_config()
    want_org = _norm(organization)
    want_camera = _norm(camera_id)
    if not want_org or not want_camera:
        return []
    out: List[Dict[str, Any]] = []
    for s in get_sensors(cfg):
        org, camera, _sensor = parse_sensor_id(s["id"])
        if _norm(org) == want_org and _norm(camera) == want_camera:
            out.append(s)
    return out


def set_sensor_camera(
    cfg: Dict[str, Any],
    sensor_id: str,
    camera_id: str,
    organization: str = DEFAULT_ORGANIZATION,
) -> str:
    """센서를 그 카메라 소속으로 만든다 — **센서 id 자체를 다시 쓴다**.

    ★★ 이것은 목록 편집이 아니라 **정체성 변경**이다. 옛 set_sensor_room 은 cfg["rooms"]
      목록만 고쳤지만, 이제 귀속은 id 안에 있으므로 카메라를 바꾸려면 id 를 바꿔야 한다.
      그 id 는 융합 입력의 sid 이자 기록 JSONL(header.sensors[].id / raw[].sid /
      dets[].sid)의 센서 식별자다 → **바꾸는 순간 이전 녹화와 대조가 끊긴다**.
      호출측은 사용자에게 이 사실을 반드시 출력으로 알려야 한다.

    sensorId 파트는 보존한다(순번이라 의미는 없지만 바꿀 이유도 없다). 규격을 벗어난 옛
    id('98bd80')는 그 전체를 sensorId 파트로 삼는다 → 'pia-1-98bd80'.

    Args:
        cfg: 설정 dict(제자리 수정).
        sensor_id: 현재 센서 id(또는 id 를 생략한 항목의 자동 생성 id).
        camera_id: 새 cameraId 파트.
        organization: 새 organization 파트. 기존 id 가 규격이면 그쪽을 우선한다.

    Returns:
        새 센서 id. 바뀌지 않았으면 기존 id 와 같다(멱등).

    Raises:
        KeyError: 그 센서가 sensors 목록에 없을 때.
        ValueError: 옛 rooms 스키마가 남아 있거나, 새 id 가 다른 센서와 충돌할 때.
    """
    camera_id = str(camera_id or "").strip()
    if not camera_id:
        raise ValueError("camera_id 가 비어 있습니다.")
    assert_no_legacy_schema(cfg)  # 깨진 파일이면 아무것도 바꾸기 전에 멈춘다

    sensor_id = str(sensor_id or "").strip()
    raw_sensors = cfg.get("sensors") or []
    target = next(
        (s for s in raw_sensors if str(s.get("id") or "").strip() == sensor_id),
        None,
    )
    if target is None:
        # "id" 를 생략한 항목(최소 입력 {"host": ...})도 찾을 수 있어야 한다
        target = next(
            (s for i, s in enumerate(raw_sensors) if normalize_sensor(s, i)["id"] == sensor_id),
            None,
        )
    if target is None:
        raise KeyError(f"센서 id '{sensor_id}' 가 설정의 sensors 목록에 없습니다.")

    current = str(target.get("id") or "").strip() or sensor_id
    try:
        current_org, _current_camera, sensor_part = parse_sensor_id(current)
    except ValueError:
        current_org, sensor_part = "", current  # 규격 밖 옛 id → 전체를 sensorId 로
    new_id = make_sensor_id(current_org or organization, camera_id, sensor_part)

    if new_id != current:
        clash = [
            s
            for s in raw_sensors
            if s is not target and _norm(s.get("id")) == _norm(new_id)
        ]
        if clash:
            raise ValueError(
                f"센서 id '{new_id}' 가 이미 다른 센서에 있습니다 — 융합 입력의 sid 가 겹쳐 "
                "재실 인원이 조용히 틀어집니다. sensorId 파트를 다르게 정하세요."
            )
    target["id"] = new_id
    target.pop("room", None)  # 폐기된 태그가 남아 있으면 여기서 정리한다
    return new_id


def nonconforming_sensor_ids(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """id 형식이 규격에 맞지 않는 센서 id 목록.

    규격을 벗어난 센서는 어느 카메라에도 붙지 않는다(제품이 등록을 거절한다). 도구가
    프로비저닝 직후 경고로 알리기 위한 헬퍼다.
    ※ get_sensors() 는 규격 위반을 ValueError 로 던지므로 여기서는 파일을 직접 읽는다."""
    if cfg is None:
        cfg = load_config()
    out: List[str] = []
    for i, raw in enumerate(cfg.get("sensors") or []):
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("id") or "").strip()
        if not sid:
            out.append(f"(id 없음: {raw.get('host') or raw.get('node_name') or '?'})")
            continue
        try:
            parse_sensor_id(sid)
        except ValueError:
            out.append(sid)
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
