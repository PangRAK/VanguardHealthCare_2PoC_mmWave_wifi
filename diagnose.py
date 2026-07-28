#!/usr/bin/env python3
"""
무선 연결 진단 도구 (Wi-Fi 판, 다중 센서)

등록/발견된 센서들이 Wi-Fi 에 붙어 노트북에서 네트워크로 보이는지 확인한다.
시각화가 안 될 때 가장 먼저 실행해 본다.

    python diagnose.py                         # epl_config.json 의 센서들 ∪ mDNS 자동탐색
    python diagnose.py --host 192.168.1.42      # 특정 주소만 강제 점검
    python diagnose.py --host epl-a.local epl-b.local
"""
import argparse
import socket
import urllib.request

from epl_config import load_config, get_sensors, CONFIG_PATH
from mmwave_wifi_reader import discover_sensors


def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ok(host: str, port: int = 80, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def _dedup_key(node_name: str = "", host: str = "") -> str:
    """중복 판정용 키(node_name 우선, 없으면 host). .local/도메인 꼬리 제거 후 소문자."""
    base = (node_name or host or "").strip()
    return base.split(".", 1)[0].lower()


def _host_to_spec(host: str) -> dict:
    """--host 로 넘어온 주소 문자열을 점검용 spec 으로 변환."""
    host = host.strip()
    node = host[:-6] if host.endswith(".local") else ""
    return {
        "name": node or host,
        "host": host,
        "node_name": node,
        "source": "--host",
    }


def collect_targets(cfg, hosts):
    """점검 대상 센서 목록을 만든다.

    --host 가 주어지면 그 주소들만 강제로 점검한다.
    그렇지 않으면 epl_config 의 등록 센서 ∪ mDNS 자동탐색 결과를 합쳐
    node_name/host 기준으로 중복을 제거한다.
    반환 항목: {name, host, node_name, source}
    """
    if hosts:
        by_key = {}
        for h in hosts:
            spec = _host_to_spec(h)
            by_key[_dedup_key(spec["node_name"], spec["host"])] = spec
        return list(by_key.values())

    by_key = {}
    # 1) 등록 센서(config)
    for s in get_sensors(cfg):
        key = _dedup_key(s.get("node_name", ""), s.get("host", ""))
        by_key[key] = {
            "name": s.get("name") or s.get("host") or key,
            "host": s.get("host", ""),
            "node_name": s.get("node_name", ""),
            "source": "config",
        }
    # 2) mDNS 자동탐색(중복 제거)
    for f in discover_sensors():
        key = _dedup_key(f.get("node_name", ""), f.get("host", ""))
        if key in by_key:
            continue
        by_key[key] = {
            "name": f.get("node_name") or f.get("host") or key,
            "host": f.get("host", ""),
            "node_name": f.get("node_name", ""),
            "source": "mDNS",
        }
    return list(by_key.values())


def diagnose_one(idx: int, spec: dict) -> bool:
    """센서 1개 도달성 점검 + 판정 출력. API 도달 가능하면 True."""
    name = spec.get("name") or "센서"
    node = spec.get("node_name") or ""
    # 접속 주소: host 우선, 없으면 node_name.local
    host = spec.get("host") or (f"{node}.local" if node else "")

    print(f"\n----- [{idx}] {name} -----")
    print(f"  주소: {host or '(없음)'}"
          + (f"   node_name: {node}" if node else "")
          + f"   [출처: {spec.get('source', '?')}]")

    if not host:
        print("  ⚠ 접속 주소가 없습니다(host/node_name 미기록). 프로비저닝을 다시 하세요.")
        return False

    try:
        ip = socket.gethostbyname(host)
        print(f"  이름 해석: {host} -> {ip}")
    except OSError:
        print(f"  ⚠ 이름 해석 실패({host}). IP 로 직접 지정하거나 공유기에서 IP 확인.")

    api = tcp_open(host, 6053)
    web = http_ok(host, 80)
    print(f"  Native API (6053) : {'✅ 열림' if api else '❌ 닫힘/도달불가'}")
    print(f"  web_server (80)   : {'✅ 응답' if web else '❌ 없음/도달불가'}")

    if api:
        print("  ✅ 무선 데이터 수신 가능 (--transport api, 기본값)")
        if not web:
            print("     (web_server 는 꺼져 있음 → Native API 사용. 정상)")
    elif web:
        print("  △ Native API 막힘 / web_server 응답 → --transport web 으로 실행하세요.")
    else:
        print("  ❌ 센서에 도달 못함. 점검: 같은 Wi-Fi/서브넷인지, 센서 전원/부팅,")
        print("     공유기 클라이언트격리(AP isolation) 해제, IP 변경 여부.")
    return api


def main():
    ap = argparse.ArgumentParser(description="EPL mmWave 센서 무선 연결 진단(다중 센서)")
    ap.add_argument("--host", nargs="+", default=None,
                    help="점검할 주소를 직접 지정(여러 개 가능). 지정 시 config/mDNS 무시.")
    args = ap.parse_args()

    cfg = load_config()
    print("===== 1) 저장된 설정 (epl_config.json) =====")
    if cfg:
        print(f"  경로: {CONFIG_PATH}")
        reg = get_sensors(cfg)
        print(f"  등록 센서: {len(reg)}개")
        for s in reg:
            print(f"    - {s.get('name')} ({s.get('host') or s.get('node_name')})")
        if cfg.get("ssid"):
            print(f"  ssid: {cfg.get('ssid')}")
        print(f"  discovery(자동탐색): {cfg.get('discovery', True)}")
    else:
        print(f"  (없음) {CONFIG_PATH}")
        print("  → 먼저 USB로 연결 후 ./run_provision.sh 로 Wi-Fi 등록을 하세요.")

    # 점검 대상 확정
    targets = collect_targets(cfg, args.host)

    print("\n===== 2) 점검 대상 센서 목록 =====")
    if args.host:
        print(f"  --host 강제 지정: {len(targets)}개 (config/mDNS 무시)")
    else:
        print(f"  등록(config) ∪ mDNS 자동탐색: {len(targets)}개")
    if not targets:
        print("\n❌ 점검할 센서가 하나도 없습니다.")
        print("   --host <IP/이름> 으로 직접 지정하거나, USB 연결 후 ./run_provision.sh 로")
        print("   Wi-Fi 등록을 먼저 하세요. (자동탐색은 같은 서브넷 + zeroconf 설치 필요)")
        return 2
    for i, spec in enumerate(targets, 1):
        print(f"  [{i}] {spec.get('name')}  "
              f"{spec.get('host') or spec.get('node_name')}  "
              f"[{spec.get('source')}]")

    # 센서별 도달성 점검
    print("\n===== 3) 센서별 도달성 점검 =====")
    reachable = 0
    for i, spec in enumerate(targets, 1):
        if diagnose_one(i, spec):
            reachable += 1

    print("\n===== 4) 종합 판정 =====")
    print(f"  점검 {len(targets)}개 중 무선 수신 가능(Native API): {reachable}개")
    if reachable == len(targets):
        print("  ✅ 모든 센서 도달 가능. `./run_gui.sh` 또는 `./run.sh` 실행하세요.")
    elif reachable > 0:
        print("  △ 일부 센서만 도달 가능. 위 개별 판정을 확인하세요.")
    else:
        print("  ❌ 도달 가능한 센서가 없습니다. 네트워크/전원/서브넷을 점검하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
