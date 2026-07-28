#!/usr/bin/env python3
"""
센서 통신 빠른 점검 ― 각 센서의 Native API(6053) 도달성을 병렬로 1~2초 안에 확인.

diagnose.py 는 자동탐색(4s)+상세 진단이라 느리다. 이 스크립트는 "지금 통신 되나?"만
빠르게 본다. epl_config.json 의 센서들을 병렬로 TCP 접속 시도한다.

    ./.venv/bin/python check_sensors.py                 # config 의 센서들 점검
    ./.venv/bin/python check_sensors.py --host 172.168.45.214 abc.local   # 직접 지정
    ./.venv/bin/python check_sensors.py --discover      # mDNS 자동탐색 결과도 포함(+약4초)
"""
from __future__ import annotations

import argparse
import socket
import threading
import time

from epl_config import get_sensors

API_PORT = 6053
TIMEOUT = 1.5   # 센서당 접속 타임아웃(초)


def check_one(host: str, port: int) -> dict:
    """host:port 에 TCP 접속 시도. (ip, ok, err, ms) 반환."""
    t0 = time.monotonic()
    ip = ""
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        ip = infos[0][4][0] if infos else ""
    except OSError:
        return {"ip": "", "ok": False, "err": "이름해석 실패", "ms": None}
    try:
        with socket.create_connection((ip or host, port), timeout=TIMEOUT):
            return {"ip": ip, "ok": True, "err": "", "ms": int((time.monotonic() - t0) * 1000)}
    except OSError:
        return {"ip": ip, "ok": False, "err": "도달 불가", "ms": None}


def main() -> int:
    ap = argparse.ArgumentParser(description="센서 통신 빠른 점검 (Native API 6053)")
    ap.add_argument("--host", nargs="+", default=None, help="점검할 host 직접 지정")
    ap.add_argument("--discover", action="store_true", help="mDNS 자동탐색 결과도 포함(+약4초)")
    ap.add_argument("--port", type=int, default=API_PORT)
    args = ap.parse_args()

    # 점검 대상 목록: (표시이름, host)
    targets: list[tuple[str, str]] = []
    if args.host:
        targets = [(h, h) for h in args.host]
    else:
        for s in get_sensors():
            targets.append((s["name"], s["host"]))
        if args.discover:
            from mmwave_wifi_reader import discover_sensors
            known = {h.split(".", 1)[0].lower() for _, h in targets}
            for f in discover_sensors():
                if f["host"].split(".", 1)[0].lower() not in known:
                    targets.append((f["node_name"], f["host"]))

    if not targets:
        print("점검할 센서가 없습니다. epl_config.json 에 등록하거나 --host 로 지정하세요.")
        return 2

    print(f"센서 통신 점검 (Native API :{args.port}, 타임아웃 {TIMEOUT}s, 병렬)\n")

    # 병렬 점검 (데몬 스레드 → 하나가 느려도 전체가 막히지 않음)
    results: dict[str, dict] = {}

    def worker(host: str):
        results[host] = check_one(host, args.port)

    threads = []
    for _, host in targets:
        th = threading.Thread(target=worker, args=(host,), daemon=True)
        th.start()
        threads.append(th)
    deadline = time.monotonic() + TIMEOUT + 1.0
    for th in threads:
        th.join(timeout=max(0.05, deadline - time.monotonic()))

    ok_n = 0
    for name, host in targets:
        r = results.get(host) or {"ip": "", "ok": False, "err": "시간초과", "ms": None}
        if r["ok"]:
            ok_n += 1
            print(f"  ✅ {name:<16} {host:<42} {r['ip']:<16} 열림 ({r['ms']}ms)")
        else:
            print(f"  ❌ {name:<16} {host:<42} {(r['ip'] or '—'):<16} {r['err']}")

    tail = "" if ok_n == len(targets) else "   → 끊긴 센서: 전원/Wi‑Fi/신호 확인"
    print(f"\n요약: {ok_n}/{len(targets)} 통신 가능{tail}")
    return 0 if ok_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
