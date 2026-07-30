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

    def stop(self):
        self._stop.set()

    def run(self):
        url = f"http://{self.host}:{self.port}/events"
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    self.state.set_conn(True, self.host, "wifi")
                    print(f"[wifi-web] 연결됨: {url}")
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
                            name = str(d.get("id", "")).split("/", 1)[-1]
                            val = d.get("value", d.get("state"))
                            u = name_to_update(name, val)
                            if u:
                                self.state.apply_updates([u], time.monotonic())
                            self.state.mark_line(time.monotonic())
            except Exception as e:  # noqa: BLE001
                self.state.set_conn(False, self.host, "disconnected")
                if not self._stop.is_set():
                    print(f"[wifi-web] 재연결 대기({e})")
                    time.sleep(2.0)


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
                "host": f"{node}.local",
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
    """--host 로 넘어온 주소 문자열을 센서 spec 으로 정규화."""
    from epl_config import normalize_sensor
    node = host[:-6] if host.endswith(".local") else ""
    return normalize_sensor({"host": host, "node_name": node}, index)


def _spec_key(s: dict) -> str:
    """중복 판정용 키(node_name 우선, 없으면 host).

    ★ 첫 점에서 자르지 않는다 — IP 호스트가 첫 옥텟으로 뭉개지면 같은 대역 센서들이
      한 키로 겹쳐 조용히 버려진다(3대 등록 → 1대만 남는 사고)."""
    from epl_config import strip_dns_suffix
    base = (s.get("node_name") or s.get("host") or s.get("id") or "").strip().lower()
    return strip_dns_suffix(base)


def build_sources(hub, *, demo: bool = False, hosts: Optional[list] = None,
                  transport: str = "api", noise_psk: Optional[str] = None,
                  password: str = "", discover: Optional[bool] = None):
    """여러 센서용 리더 스레드 목록과 설명 문자열을 만든다.

    센서 소스 결정 우선순위:
      --demo            → 합성 데이터 센서 3개(오버레이 시연)
      hosts=[...]       → 명시된 주소들만
      그 외             → epl_config.json 의 sensors ∪ mDNS 자동탐색(중복 제거)

    반환: (workers: list[Thread], desc: str)
    각 worker 는 hub.add_sensor() 로 만든 SensorState 에 데이터를 채운다.
    server.py / gui_qt.py / cli_monitor.py 가 공통으로 사용한다."""
    from mmwave_reader import DemoSensorThread       # 지연 import (순환 방지)
    from epl_config import (load_config, get_sensors, normalize_sensor, DEFAULT_PALETTE,
                            assert_unique_sensor_ids)

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

    # 명시 hosts 가 있으면 그것만, 없으면 config ∪ 자동탐색
    if hosts:
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
                # 같은 기기가 설정+탐색으로 두 번 들어오는 경우(키가 달라도 id 가 같다) 건너뛴다
                if k in by_key or cand["id"] in known_ids:
                    continue
                by_key[k] = cand
                known_ids.add(cand["id"])
        specs = list(by_key.values())

    if not specs:
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
