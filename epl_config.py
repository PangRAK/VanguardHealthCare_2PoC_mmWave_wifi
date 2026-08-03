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
    않는다 → sensorId 를 1↔7 로 바꿔도 유일성만 지켜지면 동작이 같다. 비교·중복 판정은
    normalize_id_value(strip().lower()) 단일 기준이라 **대소문자만 다른 id 도 같은 id**
    이고, 숫자 정규화는 하지 않으므로 '01' 과 '1' 은 **다른** id 다.
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
  · sensorId 를 생략하면 assign_missing_sensor_ids 가 **결정적으로 순번을 배정**한다
    (기기 유래 spec_key 로 정렬 → 그 카메라에서 아직 안 쓰인 최소 순번). 배열 순서에
    의존하지 않으며, **명시된 id 는 절대 재번호화하지 않는다**. 비숫자('north')·비연속·
    역순 sensorId 도 정상 동작한다 — 순번은 사람이 읽기 쉬우라는 관례일 뿐이다.

  ★ cameraId 파트는 현장 설치 시 **병원이 부여한 실제 cameraId** 로 교체해야 하고,
    **선행 0 없는 숫자**여야 한다 — 제품의 AddStreamModel.cameraId 는 **int** 다
    (전 모듈 공용 DTO. DTO/stream_params.py). 비숫자('ward-a')는 등록 요청이 422 로
    거절되고, 선행 0('01')·밑줄('6_0')은 pydantic 이 조용히 1·60 으로 접어 그 센서가
    어느 카메라에도 붙지 않는다. 이 제약은 assert_camera_id_registerable() 이 id 를
    만드는 시점(make_sensor_id)과 파일을 읽는 시점(assert_sensor_id_format) 양쪽에서
    강제하므로, 규격 위반 cameraId 는 기록되지도 읽히지도 않는다.
    ※ '_' 금지는 위 int 제약에 흡수됐다(숫자에는 '_' 가 없다). 원래 이유는 제품 stream_id
      규약이 '{cameraId}_{organization}' 이라 'ward_a'+'pia' 가 'ward'+'a_pia' 로 갈리기
      때문이고, 그 가드는 제품 쪽 source.resolve_camera_id 에 그대로 남아 있다.

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
from typing import Any, Dict, List, Optional, Tuple, Union

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epl_config.json")

# 센서별 기본 색상 팔레트 (오버레이에서 센서를 색으로 구분)
DEFAULT_PALETTE = ["#27e0c8", "#ffb454", "#ff5d8f", "#6ea8ff", "#b18cff", "#7ce38b"]

# 도구 3종(run_provision / run_auto_positioning{,_v2})이 공유하는 카메라(stream) 기본값.
# 카메라를 나누지 않는 현장은 이 한 카메라에 전 센서가 들어간다.
# ★ 현장 설치 시 병원이 부여한 **실제 cameraId** 로 교체해야 한다. **선행 0 없는 숫자**여야
#   한다 — 제품의 AddStreamModel.cameraId 가 int 라서 'ward-a' 는 등록이 거절되고 '01' 은
#   조용히 1 로 접힌다(assert_camera_id_registerable 이 강제한다).
#   기본값이 '1' 인 것은 신규 설정의 관례가 cameraId '1' / sensorId '1','2','3' 이기 때문이고,
#   그래도 **현장 값으로 바꾸지 않으면 그 스트림에는 센서가 0개**다(id 접두가 안 맞는다).
DEFAULT_CAMERA_ID = "1"
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


# ---------------------------------------------------------------- 도구용 가드
def config_error_hint(exc: Exception) -> str:
    """설정 오류(ValueError)를 사람이 읽을 안내 문자열로 만든다.

    ★ get_sensors() 는 옛 rooms 스키마 / id 형식 위반 / id 누락 / id·host 중복을 전부
      ValueError 로 던진다. 진단 도구(diagnose·check_sensors)와 시각화(server·gui_qt·
      cli_monitor)가 그걸 그대로 흘리면 **원인을 알려주는 도구가 트레이스백으로 죽는다** —
      정작 그 도구를 돌리는 이유가 원인을 찾는 것인데. 그래서 문자열로 감싸 준다.

    Returns:
        str: "❌ 센서 설정 오류: …" 형태의 여러 줄 안내.
    """
    return (
        f"❌ 센서 설정 오류: {exc}\n"
        f"   파일: {CONFIG_PATH}\n"
        "   → 고친 뒤 다시 실행하세요. (id 규격은 이 파일 상단 [센서 id 규격] 참조)"
    )


# ----------------------------------------------------------------- 정규화 헬퍼
def normalize_id_value(value: Any) -> str:
    """식별자 비교용 정규화. 대소문자·앞뒤 공백 차이로 매칭이 갈리지 않게 한다.

    ★ 이 모듈에서 **식별자 비교의 단일 기준**이다 — 카메라 대조(get_sensors_for_camera /
      get_camera_ids)와 중복 판정(assert_unique_sensor_ids / assert_unique_sensor_hosts)이
      모두 이 함수를 쓴다. 두 계층이 서로 다른 기준을 쓰면 'PIA-1-A' 와 'pia-1-a' 처럼
      대소문자만 다른 id 가 중복 검사는 통과하면서 같은 카메라에 둘 다 붙어, 융합 입력의
      sid 가 갈려 한 사람이 두 명으로 세어진다(§중복 검사 주석 참조).
    ★ 숫자 정규화는 하지 않는다 — 세 파트는 불투명 문자열이라 '01' 과 '1' 은 다른 id 다.
    ※ None 은 빈 문자열로 접는다(str(None) 의 'none' 이 아니다). 호출측이 falsy 여부로
      '값이 없다' 를 판정하기 때문이다(get_sensors_for_camera 의 빈 인자 조기 반환 등).

    Args:
        value: 정규화할 식별자(문자열/정수/None).

    Returns:
        str: strip().lower() 된 문자열. None 이면 "".
    """
    return str(value if value is not None else "").strip().lower()


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


def spec_key(sensor: Dict[str, Any]) -> str:
    """센서의 **안정적 물리 식별 키**(node_name 우선, 없으면 host, 없으면 id).

    같은 기기를 두 번 세지 않기 위한 키이자, sensorId 자동 배정의 **결정적 정렬 기준**이다
    (assign_missing_sensor_ids). id 는 배정 결과이므로 정렬 기준이 될 수 없고, 배열 순서는
    호출측 사정이라 기준이 되면 안 된다 — 그래서 기기에 붙어 있는 값만 쓴다.

    ★ 첫 점에서 자르지 않는다 — IP 호스트가 첫 옥텟으로 뭉개지면 같은 대역 센서들이 한
      키로 겹쳐 조용히 버려진다(3대 등록 → 1대만 남는 사고). strip_dns_suffix 로 mDNS
      접미사만 떼고 나머지 점은 보존한다.
    ※ mmwave_wifi_reader._spec_key 가 이 함수를 그대로 쓴다 — 중복 판정 키와 자동 배정
      정렬 키가 갈라지면 '주소로는 같은 기기, 순번으로는 다른 센서' 가 되어 한 기기에
      리더 스레드가 두 개 붙는다.

    Args:
        sensor: 센서 dict(정규화 전/후 무관).

    Returns:
        str: 정규화된 물리 식별 키. 세 필드가 모두 비면 "".
    """
    base = normalize_id_value(sensor.get("node_name") or sensor.get("host") or sensor.get("id"))
    return strip_dns_suffix(base)


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
    " cameraId 파트는 **선행 0 없는 숫자**여야 합니다(제품 AddStreamModel.cameraId 가 int)."
)


def assert_camera_id_registerable(camera_id: Union[str, int], source: str = "") -> str:
    """cameraId 파트가 제품에 **등록 가능한** 값인지 검증한다 — int 왕복 여부.

    제품의 `AddStreamModel.cameraId` 는 **int** 다(전 모듈 공용 DTO). 센서 id 규격 자체는
    세 파트를 불투명 문자열로 다루지만, cameraId 파트에 int 로 왕복하지 않는 값을 쓰면
    **대조될 스트림이 존재할 수 없다** → 그 센서는 영원히 어느 카메라에도 붙지 않고,
    운영자에게는 'NO SENSOR' 로만 보인다. 그래서 id 를 만드는·읽는 시점에 막는다.

    거절되는 값과 이유(제품 DTO 에서 실측):
      · 비숫자('ward-a', 'alpha') → pydantic 이 등록 요청을 422 로 거절한다.
      · 선행 0('01')            → pydantic 이 조용히 1 로 접는다. **거절보다 위험하다** —
                                  등록은 성공하고 'pia-01-*' 센서만 매칭 0개가 된다.
      · 밑줄('6_0')             → Python int() 가 밑줄을 허용해 60 이 된다(같은 함정).
    '0' 은 허용한다(int 왕복이 성립한다).

    Args:
        camera_id (Union[str, int]): 검사할 cameraId 파트.
        source (str): 오류 메시지에 표시할 출처(파일명·CLI 인자명 등).

    Returns:
        str: strip() 된 cameraId 문자열(그대로 id 조립에 쓸 수 있다).

    Raises:
        ValueError: int 로 왕복하지 않는 값일 때.
    """
    cam = str(camera_id if camera_id is not None else "").strip()
    if cam.isascii() and cam.isdigit() and str(int(cam)) == cam:
        return cam
    where = f" ({source})" if source else ""
    raise ValueError(
        f"cameraId 파트를 제품에 등록할 수 없습니다{where}: '{cam}'. "
        "제품의 AddStreamModel.cameraId 는 int 라서 비숫자('ward-a')는 등록 요청이 거절되고, "
        "선행 0('01')·밑줄('6_0')은 조용히 다른 숫자로 접혀 그 센서가 어느 카메라에도 "
        "붙지 않습니다(영원히 'NO SENSOR'). 선행 0 없는 숫자로 바꾸세요(예: '1', '42')."
    )


def parse_sensor_id(sid: str) -> Tuple[str, str, str]:
    """센서 id 를 (organization, cameraId, sensorId) 로 분해한다.

    세 파트 전부 **불투명 문자열**로 돌려준다 — 숫자로 바꾸지 않고 순서에서 의미를 끌어내지
    않는다. 앞의 두 구분자만 경계로 쓰므로(split maxsplit=2) sensorId 파트에는 '-' 가 남는다
    (host 유래 자동 id '10-201-31-120' 을 그대로 담기 위한 규격이다).

    Returns:
        Tuple[str, str, str]: (organization, cameraId, sensorId). strip() 만 적용된다.

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


def make_sensor_id(
    organization: Union[str, int], camera_id: Union[str, int], sensor_id: Union[str, int]
) -> str:
    """(organization, cameraId, sensorId) 를 규격 id 문자열로 조립한다.

    Returns:
        str: 규격 id 문자열.

    Raises:
        ValueError: 어떤 파트가 비었거나, organization/cameraId 에 '-' 또는 '__' 가 있을 때,
            또는 cameraId 가 제품에 등록 불가한 값일 때(assert_camera_id_registerable)."""
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
    # ★ 쓰기 경로의 단일 관문이다. 이 함수를 타는 모든 id 생성(프로비저닝의
    #   set_sensor_camera / assign_camera_sensor_ids / 순번 자동 배정)이 여기서 걸러진다 →
    #   제품이 등록할 수 없는 cameraId 로는 애초에 파일에 기록되지 않는다.
    assert_camera_id_registerable(cam)
    return SENSOR_ID_SEPARATOR.join((org, cam, sen))


def assert_sensor_id_format(sensors: List[Dict[str, Any]], source: str = "") -> None:
    """모든 센서 id 가 3-파트 규격을 따르는지 검증. 하나라도 어긋나면 ValueError.

    ★ 조용히 건너뛰지 않는다 — 규격 위반 센서를 무시하면 그 센서가 어느 카메라에도 붙지
      않아 재실 인원이 조용히 줄고, 운영자에게는 '센서가 없습니다' 로만 보인다.
    ※ 3-파트라고 뜻까지 맞는 건 아니다. host 유래 자동 id '10-201-31-120' 은 형식은
      통과하지만 organization 파트가 '10' 이라 실제 스트림('pia')과 대조되지 않는다 →
      get_sensors_for_camera 의 organization 대조가 최종 안전장치다.

    Args:
        sensors (List[Dict[str, Any]]): normalize_sensor 를 거친 센서 목록.
        source (str): 오류 메시지에 표시할 출처.

    Raises:
        ValueError: 규격을 따르지 않는 id 가 있을 때(위반 목록을 전부 담는다).
    """
    bad: List[str] = []
    for s in sensors:
        try:
            # 형식(3-파트)뿐 아니라 cameraId 파트의 **등록 가능성**까지 본다 — 손으로 적은
            # 'pia-01-1' 은 형식은 통과하지만 제품이 그 카메라를 등록할 수 없어 영원히
            # 매칭 0개다. 조용한 'NO SENSOR' 보다 파일을 읽는 시점의 실패가 낫다.
            assert_camera_id_registerable(parse_sensor_id(s.get("id", ""))[1])
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
      코드가 조용히 바꾸면 화면과 로그가 실동작과 어긋난다.

    Raises:
        ValueError: cfg 에 'rooms' 키가 있거나 센서에 'room' 태그가 남아 있을 때.
    """
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


def normalize_sensor(raw: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    """부분 정의된 센서 **한 개**의 필드를 모두 채워진 형태로 정규화한다.

    ★ id 를 만들지 않는다 — 없으면 `""` 로 남긴다. 최종 id 배정은 그 목록에 이미 쓰인
      sensorId 가 무엇인지 **여러 센서를 함께 봐야** 정할 수 있어서, 한 항목만 보는 이
      함수의 호출 순서에 맡길 수 없다(순서에 따라 같은 기기가 다른 순번을 받는다).
      배정은 assign_missing_sensor_ids 가 담당한다. 카메라 문맥(org/cameraId)이 없는
      도구 경로(--host 즉석 지정 / mDNS 탐색)는 호출측이 short_id 로 로컬 id 를 채운다.

    Args:
        raw (Dict[str, Any]): 부분 정의된 센서 dict.
        index (int): 기본 색상 팔레트 인덱스(표시 색만 정한다 — id 와 무관하다).

    Returns:
        Dict[str, Any]: 모든 필드가 채워진 센서 dict. id 는 명시값 또는 "".
    """
    raw = dict(raw or {})
    node_name = str(raw.get("node_name") or "").strip()
    host = str(raw.get("host") or "").strip()
    sid = str(raw.get("id") or "").strip()
    # 표시 이름은 id 가 아니라 **기기** 를 가리켜야 한다 — sensorId 가 순번이 되면 id 만으로
    # 어느 물리 센서인지 알 수 없어서, 이름이 유일한 화면상 단서다(파일에서는 mac).
    name = str(raw.get("name") or "").strip() or f"Sensor {sid or short_id(node_name, host)}"
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


def assign_missing_sensor_ids(
    sensors: List[Dict[str, Any]],
    organization: Union[str, int],
    camera_id: Union[str, int],
) -> List[Dict[str, Any]]:
    """id 를 생략한 센서에 **결정적으로** 순번 sensorId 를 배정해 규격 id 를 완성한다.

    이 목록은 카메라 하나(=스트림 하나)에 묶인 센서들이다. 그 카메라의 organization/
    cameraId 는 인자로 받는다 — 배정이 추측이 아닌 이유가 그것이다(스트림 자체가 귀속이다).

    배정 규칙:
      1. **명시된 id 를 먼저 예약한다.** 그 sensorId 파트(정규화 기준)는 순번 후보에서
         빠진다 → 사용자가 적은 id 는 절대 재번호화되지 않고, 새 센서가 그 번호를
         빼앗지도 않는다.
      2. 나머지는 `spec_key`(node_name/host — 기기에 붙어 있는 값)로 **정렬**한 뒤
         `"1"`, `"2"`, `"3"` … 중 **아직 쓰이지 않은 가장 작은 값**을 받는다.
      3. `spec_key` 를 만들 수 없거나(주소·이름이 전부 빈 센서) 둘이 같은 키를 가지면
         **조용히 배열 순서로 떨어지지 않고 ValueError** 다. 순서로 배정하면 같은 기기가
         재등록마다 다른 순번을 받아, 융합 입력의 sid 와 기록 JSONL 의 센서 식별자가
         말없이 바뀐다(이전 녹화와 대조가 끊긴다).

    ★ 배열 순서·호출 순서에 의존하는 전역 카운터를 쓰지 않는다. 같은 `spec_key` 집합이면
      입력 순서가 어떻든 기기→sensorId 대응이 같다 — 배열 순서를 바꿔 다시 읽어도 센서
      정체성이 흔들리지 않아야 하기 때문이다.
    ※ 순번은 **관례일 뿐 기능적 순서가 아니다.** 세 파트는 불투명 문자열이라 비숫자
      sensorId('north')·비연속·역순도 정상 동작한다. 여기서 숫자를 쓰는 건 사람이 읽기
      쉬워서일 뿐이고, 어떤 읽기 경로도 sensorId 를 숫자로 파싱하지 않는다.
    ※ 설정 **파일을 읽는** 경로(get_sensors)에서는 부르지 않는다 — 파일에는 org/cameraId
      문맥이 없고, id 누락을 명시적 오류로 잡는 것이 의도된 동작이다(자동으로 채우면
      어느 카메라에도 안 붙는 센서를 조용히 만들어낸다). 파일에 **쓰는** 프로비저닝
      경로에서는 assign_camera_sensor_ids 가 이 함수를 카메라 단위로 감싸 쓴다.

    Args:
        sensors: normalize_sensor 를 거친 센서 목록(원본은 수정하지 않는다).
        organization: 이 카메라의 organization.
        camera_id: 이 카메라의 cameraId.

    Returns:
        List[Dict[str, Any]]: 모든 항목의 id 가 채워진 새 목록(입력 순서 보존).

    Raises:
        ValueError: organization/cameraId 로 규격 id 를 만들 수 없을 때, 또는 배정 대상의
            `spec_key` 가 비었거나 서로 겹쳐 결정적 배정이 불가능할 때.
    """
    org = str(organization if organization is not None else "").strip()
    cam = str(camera_id if camera_id is not None else "").strip()
    if not org or not cam:
        # ★ organization 에 임의 기본값을 두지 않는다 — 틀린 조직으로 조용히 붙는 것보다
        #   배정을 거절하는 편이 낫다(멀티테넌트에서 남의 카메라 재실을 보고하게 된다).
        raise ValueError(
            "sensorId 를 배정할 수 없습니다 — 이 카메라의 organization / cameraId 가 모두 "
            f"필요합니다 (지금은 '{org}' / '{cam}'). {_ID_FORMAT_HINT}"
        )

    out = [dict(s) for s in sensors]
    pending = [s for s in out if not str(s.get("id") or "").strip()]
    if not pending:
        return out

    # 1) 명시된 id 의 sensorId 파트를 예약. 비규격 id 는 여기서 건너뛴다 —
    #    assert_sensor_id_format 이 별도로 실패시키므로 조용히 삼키는 게 아니다.
    taken = set()
    for s in out:
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        try:
            _org, _cam, sensor_part = parse_sensor_id(sid)
        except ValueError:
            continue
        taken.add(normalize_id_value(sensor_part))

    # 2) 기기 유래 키로 결정적 정렬. 키가 없거나 겹치면 실패시킨다(§3 위 주석).
    keyed = []
    for s in pending:
        key = spec_key(s)
        if not key:
            raise ValueError(
                "id 를 생략한 센서에 순번을 배정할 수 없습니다 — node_name 이나 host 중 "
                "하나는 있어야 합니다(그 값이 기기를 가리키는 유일한 안정 식별자이고, "
                "순번 배정의 결정적 기준입니다). "
                f"문제 항목: {s.get('name') or '(이름 없음)'}"
            )
        keyed.append((key, s))
    seen_keys, duplicated = set(), set()
    for key, _s in keyed:
        if key in seen_keys:
            duplicated.add(key)
        seen_keys.add(key)
    if duplicated:
        raise ValueError(
            "id 를 생략한 센서들의 물리 식별 키가 겹쳐 순번을 결정적으로 배정할 수 "
            f"없습니다: {', '.join(repr(k) for k in sorted(duplicated))}. "
            "같은 기기를 두 번 적었거나 node_name/host 가 같습니다 — 한 기기는 한 번만 "
            "등록하거나, 각 항목에 'id' 를 직접 적어 주세요."
        )

    # 3) 정렬 순서대로 미사용 최소 순번을 배정.
    next_ordinal = 1
    for _key, s in sorted(keyed, key=lambda item: item[0]):
        while str(next_ordinal) in taken:
            next_ordinal += 1
        s["id"] = make_sensor_id(org, cam, str(next_ordinal))
        taken.add(str(next_ordinal))
    return out


def assert_unique_sensor_ids(sensors: List[Dict[str, Any]], source: str = "") -> None:
    """센서 id 유일성 검증. 한 id 가 서로 다른 host 를 가리키면 ValueError.

    ★ 자동 개명하지 않는다 — 'a, a, a-1' 처럼 명시된 id 와 다시 충돌할 수 있고, 사용자가
      적은 id 를 코드가 바꾸면 화면·로그의 센서 식별자가 실동작과 달라진다. 그대로 진행하면
      융합 입력이 (sid, tid) 로 합쳐져 재실 인원이 틀어지므로(같은 tid → 2인이 1인,
      다른 tid → 1인이 2인) 설정 오류로 즉시 실패시킨다.
    ※ 완전히 동일한 항목(같은 id + 같은 host)이 두 번 적힌 경우는 중복 기입일 뿐
      모호함이 없어 허용한다(중복 제거는 호출측 _spec_key 가 담당).

    ★ 중복 판정 키는 **normalize_id_value 기준**이다(원문 id 가 아니다). 카메라 대조
      (get_sensors_for_camera)가 같은 기준으로 비교하기 때문이다 — 원문을 키로 쓰면
      'PIA-1-A' 와 'pia-1-a' 가 서로 다른 host 를 가리켜도 각각 host 1개짜리 버킷이 되어
      여기서는 통과하고, 정작 매칭에서는 **둘 다 같은 카메라에 붙는다**. 그러면 융합 입력의
      sid 가 갈려(fusion 이 sid 집합으로 '센서 겹침' 을 판정한다) 한 사람이 두 명으로
      세어진다 — 이 함수가 막으려던 바로 그 사고가 조용히 통과한다.
    ※ 표시는 **원문 그대로** 한다. 운영자가 설정 파일에서 그 문자열을 찾아야 하므로,
      정규화 키 하나에 여러 원문 스펠링이 접히면 그 스펠링을 모두 보여준다.
    ※ host 비교도 같은 기준으로 정규화한다 — 대소문자만 다른 같은 주소가 서로 다른 host 로
      집계되면 중복 기입(허용해야 하는 경우)이 오류로 뒤집힌다.

    Args:
        sensors (List[Dict[str, Any]]): normalize_sensor 를 거친 센서 목록.
        source (str): 오류 메시지에 표시할 출처.

    Raises:
        ValueError: 한 id(정규화 기준)가 두 개 이상의 서로 다른 host 를 가리킬 때.
    """
    # 정규화 id -> ({정규화 host}, [원문 id 등장순])
    by_id: Dict[str, Tuple[set, List[str]]] = {}
    for s in sensors:
        raw_id = str(s["id"])
        hosts, spellings = by_id.setdefault(normalize_id_value(raw_id), (set(), []))
        hosts.add(normalize_id_value(s.get("host") or s.get("node_name")) or "?")
        if raw_id not in spellings:
            spellings.append(raw_id)
    dups = {key: value for key, value in by_id.items() if len(value[0]) > 1}
    if dups:
        detail = "; ".join(
            f"{'/'.join(repr(x) for x in spellings)} -> hosts={sorted(hosts)}"
            for _key, (hosts, spellings) in sorted(dups.items())
        )
        where = f" ({source})" if source else ""
        raise ValueError(
            f"센서 id 가 중복입니다{where}: {detail}. "
            "센서마다 다른 'id' 를 지정하세요 — 대소문자만 다른 id 도 같은 id 로 봅니다"
            "(카메라 대조가 대소문자를 구분하지 않기 때문입니다)."
        )


def assert_unique_sensor_hosts(sensors: List[Dict[str, Any]], source: str = "") -> None:
    """같은 접속 주소(host)를 서로 다른 id 로 두 번 적으면 ValueError.

    ★ 옛 assert_one_room_per_sensor 가 막던 사고를 새 규격에서 이어받는다. id 가 카메라를
      인코딩하므로 '한 센서가 두 카메라' 는 이제 **host 중복**으로만 나타난다: 'pia-1-1' 과
      'pia-2-1' 이 같은 host 를 가리키면 두 카메라가 같은 기기에 각각 붙어(TCP 6053 x2),
      물리적으로 카메라 1 쪽에 있는 사람을 카메라 2 도 재실로 세고 알람이 두 번 나간다.

    Args:
        sensors (List[Dict[str, Any]]): normalize_sensor 를 거친 센서 목록.
        source (str): 오류 메시지에 표시할 출처.

    Raises:
        ValueError: 한 host 를 두 개 이상의 서로 다른 id 가 가리킬 때.
    """
    by_host: Dict[str, set] = {}
    for s in sensors:
        host = normalize_id_value(s.get("host") or s.get("node_name"))
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


def get_sensors(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """config 에서 정규화된 센서 목록을 반환. 구버전(단일 host) 스키마도 처리한다.

    ★ 설정 파일을 읽는 **단일 관문**이라 네 검증을 여기서 한 번에 건다: 폐기된 rooms
      스키마(assert_no_legacy_schema) → id 규격(assert_sensor_id_format) → id 유일성
      (assert_unique_sensor_ids) → host 유일성(assert_unique_sensor_hosts).
      전부 ValueError 다(도구는 config_error_hint 로 감싸 사람이 읽는 안내로 바꾼다).

    Raises:
        ValueError: 옛 rooms 스키마 / id 형식 위반 / id 누락 / id 중복 / host 중복.
    """
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
        #   그러면 운영자에게는 '센서 0개' 로만 보이므로 여기서 원인을 직접 알린다.
        #   (프로비저닝처럼 카메라를 아는 **쓰기** 경로는 assign_camera_sensor_ids 로
        #    저장 전에 순번을 확정하므로 여기까지 빈 id 가 오지 않는다.)
        raise ValueError(
            f"'id' 가 없는 센서 항목이 있습니다(센서 설정 파일): "
            f"{', '.join(repr(h) for h in missing_id)}. {_ID_FORMAT_HINT} "
            "설정 파일에는 자동 생성이 불가능하므로 id 를 직접 적어야 합니다."
        )
    # 파일 전체 기준 id 규격 + 유일성. id 가 곧 카메라 귀속이므로 형식을 먼저 보고,
    # 같은 id 가 두 번 나오면 융합 입력의 sid 가 겹쳐 재실 인원이 조용히 틀어진다.
    assert_sensor_id_format(out, source="센서 설정 파일")
    assert_unique_sensor_ids(out, source="센서 설정 파일")
    assert_unique_sensor_hosts(out, source="센서 설정 파일")
    return out


# --------------------------------------------------------- 카메라 ↔ 센서 귀속
#
# ★ assert_one_room_per_sensor 는 삭제했다 — 구조적으로 불필요해졌다. 센서 id 하나가
#   정확히 한 cameraId 를 가리키므로 '한 센서가 두 카메라에 걸침' 을 표현할 수 없다.
#   같은 기기를 두 번 적는 사고는 assert_unique_sensor_hosts 가 이어받는다.
# ★ _sensor_keys / _rooms_of / get_room_ids / get_sensors_for_room 도 삭제했다 —
#   rooms 목록 대조가 사라져 존재 이유가 없다. _sensor_keys 가 허용하던 관용성(rooms 에
#   id 대신 host/node_name 을 적어도 매칭)도 함께 사라진다: 귀속은 이제 id 에만 있다.


def get_camera_ids(
    cfg: Optional[Dict[str, Any]] = None, organization: Optional[Union[str, int]] = None
) -> List[str]:
    """설정에 등장하는 cameraId 목록(센서 id 등장 순, 중복 제거).

    organization 을 주면 그 조직의 카메라만 센다 — 운영자에게 보여줄 목록이라 조직을
    섞으면 안 된다(acme 의 '1' 과 pia 의 '1' 이 같은 '1' 로 뭉개진다).

    Returns:
        List[str]: cameraId 목록. 빈 목록이면 '이 설정 파일에 (그 조직의) 센서가 없다'는 뜻이다.
    """
    if cfg is None:
        cfg = load_config()
    want_org = normalize_id_value(organization) if organization is not None else None
    out: List[str] = []
    seen = set()
    for s in get_sensors(cfg):
        org, camera_id, _sensor = parse_sensor_id(s["id"])
        if want_org is not None and normalize_id_value(org) != want_org:
            continue
        key = normalize_id_value(camera_id)
        if key not in seen:
            seen.add(key)
            out.append(camera_id)
    return out


def get_sensors_for_camera(
    cfg: Optional[Dict[str, Any]], organization: Union[str, int], camera_id: Union[str, int]
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
    ★ organization 대조가 '형식은 맞고 뜻은 틀린' id 를 걸러내는 최종 안전장치다.

    Args:
        cfg (Optional[Dict[str, Any]]): 설정 dict. None 이면 파일에서 읽는다.
        organization (Union[str, int]): 스트림의 organization.
        camera_id (Union[str, int]): 스트림의 cameraId (int 가능).

    Returns:
        List[Dict[str, Any]]: 그 카메라에 묶인 정규화 센서 목록.
            매칭 0개면 빈 목록이다(폴백 없음).

    Raises:
        ValueError: 옛 rooms 스키마 / id 형식 위반 / id 중복 / host 중복 (get_sensors 경유).
    """
    if cfg is None:
        cfg = load_config()
    want_org = normalize_id_value(organization)
    want_camera = normalize_id_value(camera_id)
    if not want_org or not want_camera:
        return []
    out: List[Dict[str, Any]] = []
    for s in get_sensors(cfg):
        org, camera, _sensor = parse_sensor_id(s["id"])
        if normalize_id_value(org) == want_org and normalize_id_value(camera) == want_camera:
            out.append(s)
    return out


# ------------------------------------------------------- 설정 파일 쓰기 경로
#
# [PoC 전용 — 제품(mmwave_core/epl_config.py)에는 이 절이 통째로 없다]
# 제품은 설정 파일을 **읽기만** 하고(런타임이 현장 캘리브레이션 산출물을 덮어쓰는 사고를
# 원천 차단), PoC 는 프로비저닝·캘리브레이션 도구라 **쓴다**. 그래서 아래 함수들은 의도된
# 미벤더링 항목이다: save_config / assign_camera_sensor_ids / set_sensor_camera /
# nonconforming_sensor_ids / upsert_sensor / DEFAULT_CAMERA_ID / config_error_hint.
#
# ★ 쓰기 경로의 불변식: **파일에 빈 id 를 남기지 않는다.** normalize_sensor 는 더 이상
#   id 를 만들지 않으므로(목록 전체를 봐야 순번이 정해진다) 저장 전에 순번을 확정해야
#   한다. 빈 id 가 저장되면 다음 실행의 get_sensors() 가 'id 없음' ValueError 로 죽어
#   시각화·진단·캘리브레이션 도구가 **전부** 멈춘다.


def assign_camera_sensor_ids(
    cfg: Dict[str, Any],
    organization: Union[str, int],
    camera_id: Union[str, int],
) -> List[str]:
    """설정 파일 안의 **id 생략 항목**에 그 카메라의 순번 sensorId 를 붙인다(제자리 수정).

    assign_missing_sensor_ids(카메라 하나의 목록을 받는 벤더링 함수)를 설정 **파일** 에
    맞게 감싼 것이다. 두 가지를 더 한다:

      1. **카메라 단위로 좁힌다.** 파일에는 여러 카메라의 센서가 섞여 있는데, 순번 예약을
         파일 전체로 하면 카메라 2 의 '1' 이 카메라 1 의 '1' 을 막아 'pia-2-3' 처럼
         이유 없이 건너뛴 번호가 나온다. 순번은 카메라마다 1부터다.
      2. **원본 dict 를 제자리에서 갱신한다**(id 키만 쓴다). assign_missing_sensor_ids 는
         복사본을 돌려주므로 그대로 쓰면 호출측이 들고 있던 센서 참조(upsert_sensor 반환값)가
         끊긴다. 여기서 id 만 되써서 mac/x/y 등 파일의 나머지 필드와 참조를 그대로 둔다.

    ★ 배정 결과는 호출측이 save_config 로 **JSON 에 영속화**한다. 그래야 다음 실행에서
      배열 순서가 바뀌어도 다시 계산하지 않고(=재번호화 없음) 같은 센서가 같은 id 를
      유지한다 — id 는 융합 입력의 sid 이자 기록 JSONL 의 센서 식별자다.
    ※ 비규격 id(옛 '98bd80')는 순번을 **예약하지 않는다** — 그 항목은 어차피
      set_sensor_camera 가 순번으로 옮길 대상이라, 예약하면 자기 자신 때문에 번호가 밀린다.

    Args:
        cfg: 설정 dict(제자리 수정).
        organization: 이 카메라의 organization.
        camera_id: 이 카메라의 cameraId.

    Returns:
        List[str]: 이번에 새로 붙은 id 목록(사용자에게 보여줄 용도. 없으면 빈 목록).

    Raises:
        ValueError: organization/cameraId 가 비었거나, id 를 생략한 센서의 spec_key 가
            비었거나 서로 겹칠 때(assign_missing_sensor_ids 경유).
    """
    raw_sensors = cfg.get("sensors") or []
    want_org = normalize_id_value(organization)
    want_camera = normalize_id_value(camera_id)

    scoped: List[Dict[str, Any]] = []
    positions: List[int] = []
    for i, s in enumerate(raw_sensors):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        if not sid:
            scoped.append(s)
            positions.append(i)
            continue
        try:
            org, camera, _sensor = parse_sensor_id(sid)
        except ValueError:
            continue  # 비규격 id → 이 카메라의 순번 체계 밖이다(위 ※ 참조)
        if normalize_id_value(org) == want_org and normalize_id_value(camera) == want_camera:
            scoped.append(s)
            positions.append(i)

    if not any(not str(s.get("id") or "").strip() for s in scoped):
        return []

    assigned = assign_missing_sensor_ids(scoped, organization, camera_id)
    out: List[str] = []
    for pos, new in zip(positions, assigned):
        if not str(raw_sensors[pos].get("id") or "").strip():
            raw_sensors[pos]["id"] = new["id"]
            out.append(new["id"])
    return out


def _next_free_ordinal(
    raw_sensors: List[Dict[str, Any]],
    organization: Union[str, int],
    camera_id: Union[str, int],
    exclude: Optional[Dict[str, Any]] = None,
) -> str:
    """그 카메라에서 아직 쓰이지 않은 가장 작은 순번 sensorId('1', '2', …).

    assign_missing_sensor_ids 의 3단계(명시된 sensorId 를 예약 → 미사용 최소 순번)와 **같은
    규칙**이다. 둘이 갈라지면 프로비저닝 경로(신규 센서)와 카메라 이동 경로(옛 id 를 옮기는
    센서)가 같은 카메라에 서로 다른 번호 체계를 만든다.
    한 항목만 옮기는 set_sensor_camera 전용이라 정렬(spec_key)은 필요 없다 — 배정 대상이
    하나뿐이면 정렬 결과도 하나뿐이다.

    Args:
        raw_sensors: 설정 파일의 센서 목록(원본, 수정하지 않는다).
        organization: 대상 카메라의 organization.
        camera_id: 대상 카메라의 cameraId.
        exclude: 예약에서 제외할 항목(옮기는 당사자 — 자기 옛 id 때문에 밀리지 않게).

    Returns:
        str: 미사용 최소 순번 문자열.
    """
    want_org = normalize_id_value(organization)
    want_camera = normalize_id_value(camera_id)
    taken = set()
    for s in raw_sensors:
        if not isinstance(s, dict) or s is exclude:
            continue
        try:
            org, camera, sensor_part = parse_sensor_id(str(s.get("id") or "").strip())
        except ValueError:
            continue
        if normalize_id_value(org) == want_org and normalize_id_value(camera) == want_camera:
            taken.add(normalize_id_value(sensor_part))
    ordinal = 1
    while str(ordinal) in taken:
        ordinal += 1
    return str(ordinal)


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

    **규격 id 의 sensorId 파트는 보존한다** — 명시된 id 는 재번호화하지 않는다는 규약이고,
    카메라만 바뀌는 이동에서 번호까지 흔들 이유가 없다. 반면 규격을 벗어난 옛 id('98bd80')는
    그 카메라의 **미사용 최소 순번**을 받는다(_next_free_ordinal). 옛 id 를 통째로 sensorId
    파트로 삼으면('pia-1-98bd80') 형식만 맞고 그 카메라의 순번 체계 밖에 남아, 다음 센서가
    받을 번호와 사람이 읽는 순서가 모두 어긋난다.

    Args:
        cfg: 설정 dict(제자리 수정).
        sensor_id: 현재 센서 id. id 로 못 찾으면 기기 키(spec_key — node_name/host)로도
            찾는다(id 를 생략한 항목이나, 운영자가 주소를 넘긴 경우).
        camera_id: 새 cameraId 파트.
        organization: 새 organization 파트. 기존 id 가 규격이면 그쪽을 우선한다.

    Returns:
        새 센서 id. 바뀌지 않았으면 기존 id 와 같다(멱등).

    Raises:
        KeyError: 그 센서가 sensors 목록에 없을 때.
        ValueError: sensor_id/camera_id 가 비었거나, 옛 rooms 스키마가 남아 있거나,
            새 id 가 다른 센서와 충돌할 때.
    """
    camera_id = str(camera_id or "").strip()
    if not camera_id:
        raise ValueError("camera_id 가 비어 있습니다.")
    assert_no_legacy_schema(cfg)  # 깨진 파일이면 아무것도 바꾸기 전에 멈춘다

    sensor_id = str(sensor_id or "").strip()
    if not sensor_id:
        # ★ 빈 값을 그대로 두면 아래 조회가 '있는 그대로 빈 id 인 항목'을 집어 **엉뚱한
        #   센서**를 옮긴다. normalize_sensor 가 id 를 만들지 않게 되면서 빈 id 가 실제로
        #   존재할 수 있게 됐으므로(저장 전 상태) 여기서 명시적으로 막는다.
        raise ValueError(
            "옮길 센서를 지정하지 않았습니다(sensor_id 가 비어 있습니다). "
            "assign_camera_sensor_ids 로 순번을 확정한 뒤 그 id 를 넘기세요."
        )
    raw_sensors = cfg.get("sensors") or []
    target = next(
        (s for s in raw_sensors if str(s.get("id") or "").strip() == sensor_id),
        None,
    )
    if target is None:
        # id 로 못 찾으면 **기기 키**로 찾는다. 옛 코드는 normalize_sensor 가 만들어 주던
        # 자동 id(short_id)로 역조회했지만, 이제 그 함수는 id 를 만들지 않아 그 경로가 죽는다.
        # spec_key 는 자동 배정의 정렬 키와 같은 값이라 '이 기기' 를 가리키는 표현이 하나다.
        want_key = spec_key({"id": sensor_id})
        target = next((s for s in raw_sensors if spec_key(s) == want_key), None)
    if target is None:
        raise KeyError(f"센서 id '{sensor_id}' 가 설정의 sensors 목록에 없습니다.")

    current = str(target.get("id") or "").strip() or sensor_id
    try:
        current_org, _current_camera, sensor_part = parse_sensor_id(current)
    except ValueError:
        # 규격 밖 옛 id → 그 카메라의 미사용 최소 순번으로 배정한다(위 독스트링 참조).
        current_org = ""
        sensor_part = _next_free_ordinal(raw_sensors, organization, camera_id, exclude=target)
    new_id = make_sensor_id(current_org or organization, camera_id, sensor_part)

    if new_id != current:
        clash = [
            s
            for s in raw_sensors
            if s is not target
            and normalize_id_value(s.get("id")) == normalize_id_value(new_id)
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
    """id 형식이 규격에 맞지 않거나 cameraId 파트를 제품에 등록할 수 없는 센서 id 목록.

    규격을 벗어난 센서는 어느 카메라에도 붙지 않는다(제품이 등록을 거절한다). 도구가
    프로비저닝 직후 경고로 알리기 위한 헬퍼다. 3-파트 형식은 맞지만 cameraId 파트가
    'ward-a'/'01' 처럼 제품 DTO(int)로 왕복하지 않는 것도 여기 잡힌다 — 형식만 보면
    정상으로 보이는데 매칭은 영원히 0개인 가장 찾기 어려운 사고다.
    ※ get_sensors() 는 규격 위반을 ValueError 로 던지므로 여기서는 파일을 직접 읽는다.

    Returns:
        List[str]: 규격을 벗어난 센서 id 목록(id 가 없는 항목은 host 로 표시).
    """
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
            assert_camera_id_registerable(parse_sensor_id(sid)[1])
        except ValueError:
            out.append(sid)
    return out


def upsert_sensor(cfg: Dict[str, Any], sensor: Dict[str, Any]) -> Dict[str, Any]:
    """센서를 config['sensors'] 에 추가하거나(같은 id/node_name/host 면) 갱신한다.

    이미 있는 센서의 배치값(x/y/heading_deg/flip/color/name)은 보존하고,
    주소(host/node_name)와 인증정보만 새 값으로 덮어쓴다. 저장된 sensor dict 반환.

    ★ id 는 여기서 만들지 않는다. normalize_sensor 가 id 를 만들지 않게 되면서, id 를
      주지 않고 부르면 **id 가 빈 항목**이 목록에 들어간다 — 호출측이 저장 전에
      assign_camera_sensor_ids 로 순번을 확정해야 한다(§쓰기 경로 불변식).
      매칭 자체는 영향이 없다: 아래 _same() 은 빈 id 를 falsy 로 걸러 node_name/host 로
      떨어지므로 재프로비저닝은 그대로 기존 항목을 찾는다.
    ★ 갱신 시 **기존 id 를 빈 값으로 덮어쓰지 않는다.** 이미 등록된 센서를 다시
      프로비저닝하면 incoming 의 id 가 "" 인데, 그대로 쓰면 확정돼 있던 규격 id 가
      지워져 그 센서가 카메라에서 떨어져 나간다(재실 인원이 조용히 준다)."""
    sensors = cfg.setdefault("sensors", [])
    incoming = normalize_sensor(sensor, len(sensors))

    def _same(a: Dict[str, Any]) -> bool:
        return (a.get("id") and a.get("id") == incoming["id"]) or \
               (a.get("node_name") and a.get("node_name") == incoming["node_name"]) or \
               (a.get("host") and a.get("host") == incoming["host"])

    for existing in sensors:
        if _same(existing):
            existing.update({
                "id": incoming["id"] or existing.get("id", ""),
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
