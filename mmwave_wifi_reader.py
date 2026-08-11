"""
무선(Wi-Fi) 데이터 리더 ― ESPHome 센서 상태를 받아 SensorState 에 채운다.

USB 시리얼 로그 파싱(mmwave_reader.SerialReaderThread) 대신, 이미 Wi-Fi 에 연결된
센서로부터 네트워크로 데이터를 받는다. 표시 계층(SensorState, 시각화)은 그대로 재사용한다.

기본 경로(권장): ESPHome Native API (TCP 6053, aioesphomeapi)
    - EPL 기본 펌웨어에서 항상 켜져 있고 암호화 키도 없음 → 바로 연결됨
    - 상태가 push 로 들어오고(key→값), 단위 파싱이 필요 없음
폴백: web_server SSE (http://host/events)  ― 펌웨어에 web_server 가 켜진 경우만

두 경로 모두 백그라운드 스레드에서 돌며, 들어온 (이름, 값)을 mmwave_parser 와 동일한
업데이트 dict 로 변환해 state.apply_updates() 로 넣는다.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.request
from typing import Optional

# ESPHome 엔티티 이름 → 내부 모델 매핑
_TARGET_RE = re.compile(r"^Target\s+(\d+)\s+(X|Y|Speed|Angle|Distance|Resolution)$", re.I)
_ACTIVE_RE = re.compile(r"^Target\s+(\d+)\s+Active$", re.I)
_ZONE_RE = re.compile(r"^Zone\s+(\d+)\s+Target Count$", re.I)

# web SSE 폴백 전용 상수
_SSE_READ_TIMEOUT_SECOND = 10.0
_SSE_RECONNECT_BACKOFF_SECOND = 2.0
# 이 수만큼 이벤트를 받고도 타겟이 하나도 매칭되지 않으면 이름 규약이 어긋난 것으로 본다.
_SSE_NO_TARGET_EVENTS = 200


def name_to_update(name: str, value) -> Optional[dict]:
    """ESPHome 엔티티 이름+값을 SensorState.apply_updates 용 dict 로 변환.

    주의: Native API 의 Speed 는 이미 m/s 단위(펌웨어 변환)라 그대로 사용한다."""
    if name is None or value is None:
        return None
    name = name.strip()

    m = _TARGET_RE.match(name)
    if m:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return {"kind": "target", "target": int(m.group(1)),
                "field": m.group(2).lower(), "value": v}

    m = _ACTIVE_RE.match(name)
    if m:
        return {"kind": "target_active", "target": int(m.group(1)),
                "value": bool(value) if not isinstance(value, str)
                else value.strip().upper() in ("ON", "TRUE", "1")}

    if name.lower() == "illuminance":
        try:
            return {"kind": "illuminance", "value": float(value)}
        except (TypeError, ValueError):
            return None

    m = _ZONE_RE.match(name)
    if m:
        try:
            return {"kind": "zone_count", "zone": int(m.group(1)), "value": float(value)}
        except (TypeError, ValueError):
            return None

    # 그 외 숫자형 센서는 misc 로
    try:
        return {"kind": "misc", "name": name, "value": float(value), "unit": ""}
    except (TypeError, ValueError):
        return None


def _sse_entity_name(d: dict) -> str:
    """web_server SSE 의 state 이벤트 dict 에서 **친화명**을 뽑는다.

    ESPHome 은 `id` 에 object_id(`sensor-target_1_x`)를 싣고, 친화명(`name`,
    `"Target 1 X"`)은 접속 직후 전체 덤프에만 넣는다. 그래서 둘 중 어느 형태가 와도 같은
    이름으로 수렴시킨다 — 도메인 접두(`sensor-`/`binary_sensor-`)를 떼고 `_` 를 공백으로
    바꾸면 위의 세 정규식(전부 `re.I`)에 그대로 매칭된다.

    ★ object_id 를 그대로 name_to_update 에 넘기면 정규식이 하나도 안 맞고, 매칭 실패가
      예외가 아니라 `misc` 로 흘러가(mmwave_reader.apply_updates) **검출 0 인데 연결·수신
      지표만 정상**인 상태가 된다. mark_line 이 계속 불려 stale 판정에도 안 걸린다.
    """
    nm = str(d.get("name") or "").strip()
    if nm:
        return nm
    oid = str(d.get("id") or "").strip().split("/", 1)[-1]  # 옛 'sensor/xxx' 형식도 수용
    if "-" in oid:
        oid = oid.split("-", 1)[1]
    return oid.replace("_", " ").strip()


# ----------------------------------------------------------------- Native API
class WifiApiReader(threading.Thread):
    """ESPHome Native API(6053)로 센서 상태를 구독해 SensorState 에 채운다."""

    def __init__(self, state, host: str, port: int = 6053,
                 password: str = "", noise_psk: Optional[str] = None,
                 node_name: Optional[str] = None):
        super().__init__(daemon=True)
        self.state = state
        self.host = host
        self.port = port
        self.password = password or ""
        self.noise_psk = noise_psk or None
        self.node_name = node_name or None
        self._stop = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:  # noqa: BLE001
            self.state.set_conn(False, self.host, "disconnected")
            print(f"[wifi-api] 종료: {e}")

    async def _main(self):
        from aioesphomeapi import APIClient, ReconnectLogic

        self._loop = asyncio.get_running_loop()
        cli = APIClient(self.host, self.port, self.password or None,
                        noise_psk=self.noise_psk)
        keymap: dict[int, str] = {}

        async def on_connect():
            try:
                entities, _ = await cli.list_entities_services()
                keymap.clear()
                for e in entities:
                    k = getattr(e, "key", None)
                    nm = getattr(e, "name", None)
                    if k is not None and nm:
                        keymap[k] = nm

                def on_state(s):
                    if getattr(s, "missing_state", False):
                        return
                    nm = keymap.get(getattr(s, "key", None))
                    if nm is None:
                        return
                    u = name_to_update(nm, getattr(s, "state", None))
                    if u:
                        self.state.apply_updates([u], time.monotonic())
                    self.state.mark_line(time.monotonic())

                cli.subscribe_states(on_state)
                self.state.set_conn(True, self.host, "wifi")
                print(f"[wifi-api] 연결됨: {self.host} (엔티티 {len(keymap)}개)")
            except Exception as e:  # noqa: BLE001
                print(f"[wifi-api] on_connect 오류: {e}")

        async def on_disconnect(expected: bool):
            self.state.set_conn(False, self.host, "disconnected")

        rl = ReconnectLogic(
            client=cli, on_connect=on_connect, on_disconnect=on_disconnect,
            name=self.node_name,
        )
        await rl.start()
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            try:
                await rl.stop()
            except Exception:
                pass
            try:
                await cli.disconnect()
            except Exception:
                pass


# ----------------------------------------------------------------- web SSE 폴백
class WebSseReader(threading.Thread):
    """ESPHome web_server 가 켜져 있을 때 http://host/events (SSE)로 읽는 폴백."""

    def __init__(self, state, host: str, port: int = 80):
        super().__init__(daemon=True)
        self.state = state
        self.host = host
        self.port = port
        self._stop = threading.Event()
        # 재연결 알림 상태 — 진입 시 1회만 알리고 회복(=실제 수신) 시 리셋
        self._connected = False
        self._reconnects = 0
        self._reconnect_warned = False
        # 이름 규약 어긋남 감지 — 이벤트는 오는데 타겟이 0인 상태
        self._events = 0
        self._targets = 0
        self._no_target_warned = False

    def stop(self):
        self._stop.set()

    def _on_connected(self, url: str) -> None:
        """소켓 연결 성공 — 상태만 갱신하고, **첫 연결만** 알린다.

        ★ 연결 성공은 '회복' 이 아니다. 서버가 붙자마자 스트림을 닫는 flapping 에서는
          연결/종료가 반복되므로 여기서 래치를 리셋하면 경고가 매 회전 다시 찍힌다.
          회복 신호는 실제로 데이터가 들어온 시점(_on_event)이다."""
        self.state.set_conn(True, self.host, "wifi")
        if not self._connected:
            self._connected = True
            print(f"[wifi-web] 연결됨: {url}")

    def _on_event(self) -> None:
        """이벤트 1건 수신 — 재연결을 반복하던 상태였다면 회복을 알리고 상태를 초기화한다."""
        self._events += 1
        if not self._reconnect_warned:
            return
        print(f"[wifi-web] 수신 회복 — 재연결 {self._reconnects}회 후 재개")
        self._reconnect_warned = False
        self._reconnects = 0

    def _warn_reconnect(self, url: str, why: str) -> None:
        """재연결 대기 — 상태 진입 시 1회만 알린다(회복 시 _on_event 가 요약)."""
        self._reconnects += 1
        if self._reconnect_warned:
            return
        self._reconnect_warned = True
        print(f"[wifi-web] 재연결 대기: {url} ({why})")

    def _warn_if_no_target(self) -> None:
        """이벤트는 들어오는데 타겟이 하나도 매칭되지 않으면 **1회만** 경고한다.

        엔티티 이름 규약이 어긋나면 name_to_update 가 예외 없이 misc 로 흘려보내므로
        검출 0 인데 연결·수신 지표는 정상으로 보이고 mark_line 때문에 stale 판정에도 안
        걸린다. 타겟이 한 번이라도 매칭되면 규약이 맞다는 뜻이라 회복 리셋은 없다."""
        if self._targets or self._no_target_warned or self._events < _SSE_NO_TARGET_EVENTS:
            return
        self._no_target_warned = True
        print(
            f"[wifi-web] ⚠ 이벤트 {self._events}개를 받았지만 타겟이 하나도 매칭되지 "
            f"않았습니다: {self.host} — ESPHome 엔티티 이름 규약이 바뀐 것으로 보입니다. "
            "Native API 경로(transport=api)로 전환하거나 펌웨어 엔티티 이름을 확인하세요."
        )

    def run(self):
        url = f"http://{self.host}:{self.port}/events"
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=_SSE_READ_TIMEOUT_SECOND) as r:
                    self._on_connected(url)
                    event = None
                    for raw in r:
                        if self._stop.is_set():
                            break
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "state":
                            try:
                                d = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            val = d.get("value", d.get("state"))
                            u = name_to_update(_sse_entity_name(d), val)
                            if u:
                                if u["kind"] in ("target", "target_active"):
                                    self._targets += 1
                                self.state.apply_updates([u], time.monotonic())
                            self._on_event()
                            self.state.mark_line(time.monotonic())
                            self._warn_if_no_target()
                # ★ 예외 없이 여기 도달하면 서버가 스트림을 **정상 종료**한 것이다. 이 경우도
                #   재연결 사유이므로 아래 공통 경로로 보낸다 — 옛 코드는 backoff 가 except
                #   블록에만 있어서 tight loop 이 됐다(로그·CPU·장치 연타가 함께 폭주).
                why = "서버가 스트림을 닫음"
            except Exception as e:  # noqa: BLE001
                why = str(e)
            self.state.set_conn(False, self.host, "disconnected")
            if self._stop.is_set():
                break
            self._warn_reconnect(url, why)
            # sleep 대신 Event.wait — stop() 이 backoff 를 기다리지 않고 즉시 끊는다.
            self._stop.wait(_SSE_RECONNECT_BACKOFF_SECOND)


def make_wifi_reader(state, host: str, transport: str = "api",
                     noise_psk: Optional[str] = None, password: str = "",
                     node_name: Optional[str] = None):
    """transport 에 따라 적절한 리더 스레드를 생성."""
    if transport == "web":
        return WebSseReader(state, host)
    return WifiApiReader(state, host, password=password, noise_psk=noise_psk,
                         node_name=node_name)


# ----------------------------------------------------------------- mDNS 자동 탐색
# ESPHome 장치는 "_esphomelib._tcp.local." 서비스를 mDNS 로 광고한다.
# 이를 브라우징해 EPL 센서(everything-presence-lite-*)를 자동 발견한다.
_ESPHOME_SERVICE = "_esphomelib._tcp.local."


def discover_sensors(timeout: float = 4.0,
                     name_prefix: str = "everything-presence-lite") -> list:
    """네트워크의 EPL 센서를 mDNS 로 탐색. [{node_name, host, address}, ...] 반환.

    zeroconf 미설치/오류 시 빈 리스트를 반환한다(탐색 실패는 치명적이지 않음)."""
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
    except Exception:  # noqa: BLE001
        return []

    seen = []   # (service_type, name)

    def _on_change(zeroconf, service_type, name, state_change, **_kw):
        if state_change is ServiceStateChange.Added:
            seen.append((service_type, name))

    results: dict = {}
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, _ESPHOME_SERVICE, handlers=[_on_change])
        time.sleep(max(0.5, timeout))
        # 브라우저 콜백 스레드가 아닌 여기(메인)에서 resolve → 교착 회피
        for service_type, name in seen:
            node = name.split("._esphomelib", 1)[0].rstrip(".")
            if name_prefix and not node.lower().startswith(name_prefix.lower()):
                continue
            address = ""
            try:
                info = zc.get_service_info(service_type, name, timeout=1500)
                if info:
                    addrs = info.parsed_addresses()
                    if addrs:
                        address = addrs[0]
            except Exception:  # noqa: BLE001
                pass
            results[node.lower()] = {
                "node_name": node,
                # host 에는 mDNS 이름 대신 **방금 조회로 알아낸 실제 IP** 를 쓴다
                # (프로비저닝의 기록 규칙과 같다 — provision_wifi.py §5 주석).
                # 이름을 쓰면 접속할 때마다 mDNS 를 한 번 더 타서, mDNS 가 막힌
                # 네트워크에서는 '탐색은 됐는데 접속은 안 되는' 상태가 된다.
                # ★ 중복 판정(_spec_key)과 id 배정(short_id)은 node_name 을 먼저 보므로
                #   host 가 IP 로 바뀌어도 키가 흔들리지 않는다.
                "host": address or f"{node}.local",
                "address": address,
            }
    finally:
        try:
            zc.close()
        except Exception:  # noqa: BLE001
            pass
    return list(results.values())


# ----------------------------------------------------------------- 다중 소스 빌더
def _host_to_spec(host: str, index: int) -> dict:
    """--host 로 넘어온 주소 문자열을 센서 spec 으로 정규화.

    ★ 이 경로에는 카메라 문맥(organization/cameraId)이 없어 규격 3-파트 id 를 만들 수 없다.
      그래도 id 는 융합 입력의 sid 라 비워둘 수 없으므로 host 유래 로컬 식별자(short_id)를
      쓴다 — 자동 sensorId 순번(assign_missing_sensor_ids)은 카메라를 아는 경로 전용이다.
      비우면 여러 센서의 sid 가 전부 ""로 겹쳐 한 사람이 여러 명으로 세어진다."""
    from epl_config import normalize_sensor, short_id
    node = host[:-6] if host.endswith(".local") else ""
    spec = normalize_sensor({"host": host, "node_name": node}, index)
    spec["id"] = short_id(node, host)
    return spec


def _spec_key(s: dict) -> str:
    """중복 판정용 키(node_name 우선, 없으면 host).

    ★ 첫 점에서 자르지 않는다 — IP 호스트가 첫 옥텟으로 뭉개지면 같은 대역 센서들이
      한 키로 겹쳐 조용히 버려진다(3대 등록 → 1대만 남는 사고).
    ※ 구현은 epl_config.spec_key 하나뿐이다 — 여기의 중복 판정 키와 sensorId 자동 배정의
      정렬 키(assign_missing_sensor_ids)가 갈라지면 '주소로는 같은 기기, 순번으로는 다른
      센서' 가 되어 한 기기에 리더 스레드가 두 개 붙는다(같은 사람을 두 번 센다)."""
    from epl_config import spec_key
    return spec_key(s)


def build_sources(hub, *, demo: bool = False, hosts: Optional[list] = None,
                  transport: str = "api", noise_psk: Optional[str] = None,
                  password: str = "", discover: Optional[bool] = None,
                  specs: Optional[list] = None):
    """여러 센서용 리더 스레드 목록과 설명 문자열을 만든다.

    센서 소스 결정 우선순위:
      --demo            → 합성 데이터 센서 3개(오버레이 시연)
      specs=[{...}]     → 센서 dict 목록(센서 id 접두로 고른 카메라 귀속 결과 등)
      hosts=[...]       → 명시된 주소들만
      그 외             → epl_config.json 의 sensors ∪ mDNS 자동탐색(중복 제거)

    ★ specs 의 None 과 [] 는 다른 뜻이다(제품 build_sources 와 동일 계약):
      · None → **미지정**. 설정 파일(∪ 자동탐색)에서 찾는다.
      · []   → **지정됐고 0개**. 그 카메라에 붙일 센서가 없다는 뜻이므로 전 센서/데모로
               폴백하지 않는다. 폴백하면 남의 카메라 센서(또는 합성 인원)로 캘리브레이션한
               좌표를 저장하는 사고가 난다.

    반환: (workers: list[Thread], desc: str)
    각 worker 는 hub.add_sensor() 로 만든 SensorState 에 데이터를 채운다.
    server.py / gui_qt.py / cli_monitor.py 가 공통으로 사용한다."""
    from mmwave_reader import DemoSensorThread       # 지연 import (순환 방지)
    from epl_config import (load_config, get_sensors, normalize_sensor, short_id,
                            DEFAULT_PALETTE, assert_unique_sensor_ids)

    workers = []

    if demo:
        # 서로 다른 위치/방향에서 방 중앙을 겹쳐 바라보는 3센서 → 같은 사람을 여러 센서가 관측
        placements = [
            {"id": "demo1", "name": "Demo 1 (L)", "x": -2500, "y": 0, "heading_deg": -25,
             "color": DEFAULT_PALETTE[0]},
            {"id": "demo2", "name": "Demo 2 (R)", "x": 2500, "y": 0, "heading_deg": 25,
             "color": DEFAULT_PALETTE[1]},
            {"id": "demo3", "name": "Demo 3 (top)", "x": 0, "y": 5000, "heading_deg": 180,
             "color": DEFAULT_PALETTE[2]},
        ]
        for p in placements:
            st = hub.add_sensor(p)
            workers.append(DemoSensorThread(st, p, label=p["name"]))
        return workers, f"DEMO (2 synthetic people · {len(placements)} sensors overlap · fusion)"

    cfg = load_config()

    # 명시 specs(카메라 귀속) > 명시 hosts > config ∪ 자동탐색
    # ★ `specs is not None` — 빈 목록([])은 '센서 0개로 지정됨'이라 폴백 대상이 아니다.
    explicit_specs = specs is not None
    if explicit_specs:
        specs = [normalize_sensor(s, i) for i, s in enumerate(specs)]
    elif hosts:
        specs = [_host_to_spec(h, i) for i, h in enumerate(hosts)]
    else:
        by_key = {}
        for s in get_sensors(cfg):
            by_key[_spec_key(s)] = s
        use_disc = cfg.get("discovery", True) if discover is None else discover
        if use_disc:
            known_ids = {s["id"] for s in by_key.values()}
            for f in discover_sensors():
                k = _spec_key(f)
                cand = normalize_sensor({"node_name": f["node_name"], "host": f["host"]},
                                        len(by_key))
                # ★ 탐색된 센서에는 카메라 문맥이 없어 규격 id 를 만들 수 없다 → host 유래
                #   로컬 id 를 붙인다(sid 가 비면 융합 입력 키가 겹친다). 이 분기는 설치
                #   편의용이고, 규격 id 는 프로비저닝(--camera-id)에서 확정된다.
                cand["id"] = short_id(f["node_name"], f["host"])
                # 같은 기기가 설정+탐색으로 두 번 들어오는 경우(키가 달라도 id 가 같다) 건너뛴다
                if k in by_key or cand["id"] in known_ids:
                    continue
                by_key[k] = cand
                known_ids.add(cand["id"])
        specs = list(by_key.values())

    if not specs:
        if explicit_specs:
            # 카메라 귀속 결과가 0개 — 데모(합성 인원)로 폴백하면 가짜 사람으로 캘리브레이션
            # 하거나 화면에 없는 재실을 그리게 된다. 빈 목록으로 정직하게 돌려준다.
            print(
                "⚠  지정된 센서가 0개입니다(센서 id 접두 대조 결과). "
                "데모로 폴백하지 않습니다."
            )
            return [], "NO SENSOR (카메라에 묶인 센서 없음)"
        print("⚠  등록/발견된 센서가 없습니다.")
        print("   먼저 USB로 연결한 뒤  ./run_provision.sh  로 Wi-Fi 연결을 하거나,")
        print("   --host <IP/이름> 으로 직접 지정하세요.  지금은 데모 모드로 표시합니다.")
        return build_sources(hub, demo=True)

    # hub.add_sensor 직전 — 명시 hosts/설정파일/탐색 어느 경로로 왔든 여기서 한 번 막는다.
    assert_unique_sensor_ids(specs, source="센서 목록")

    for s in specs:
        meta = {
            "id": s["id"], "name": s["name"], "host": s["host"],
            "node_name": s.get("node_name", ""), "color": s["color"],
            "x": s["x"], "y": s["y"], "heading_deg": s["heading_deg"], "flip": s["flip"],
            "pitch_deg": s.get("pitch_deg", 0.0), "roll_deg": s.get("roll_deg", 0.0),
        }
        st = hub.add_sensor(meta)
        workers.append(make_wifi_reader(
            st, s["host"], transport=transport,
            noise_psk=(noise_psk or s.get("noise_psk")),
            password=(password or s.get("api_password", "")),
            node_name=s.get("node_name") or None))

    names = ", ".join(s["name"] for s in specs)
    return workers, f"WIFI/{transport.upper()} · {len(specs)} sensors ({names})"
