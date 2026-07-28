#!/usr/bin/env python3
"""
실시간 시각화 서버 — Wi-Fi 판 (Everything Presence Lite / mmWave)

구조(무선):
    [LD2450] --UART--> [ESP32(ESPHome)] --Wi-Fi(Native API 6053)-->
        [이 PC] --(mmwave_wifi_reader)--> [SensorState]
            --(HTTP/SSE)--> [브라우저 레이더 대시보드 (index.html)]

전제: 센서가 이미 Wi-Fi 에 연결돼 있어야 한다(최초 1회 ./run_provision.sh 로 설정).
      접속 주소는 epl_config.json 의 host 를 사용하며, --host 로 덮어쓸 수 있다.

사용법:
    python server.py                         # epl_config.json 의 센서들에 무선 접속(+mDNS 자동탐색)
    python server.py --host 192.168.1.42     # 주소 직접 지정(여러 개 가능)
    python server.py --host a.local b.local  # 여러 센서를 한 화면에 오버레이
    python server.py --transport web         # web_server SSE 폴백 사용
    python server.py --demo                  # 합성 데이터 3개 오버레이로 시연
    python server.py --http-port 8000 --no-browser
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mmwave_reader import SensorHub
from mmwave_wifi_reader import build_sources
from fusion import add_fusion_args, apply_fusion_opts

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")

# 핸들러가 접근할 전역 상태 (서버 시작 시 주입)
# 단일 SensorState → 여러 센서를 모으는 SensorHub 로 확장(오버레이)
HUB: SensorHub = None  # type: ignore
SSE_HZ = 20.0


class Handler(BaseHTTPRequestHandler):
    # 로그를 조용히 (요청마다 stderr 출력 방지)
    def log_message(self, fmt, *args):
        pass

    def _send_headers(self, code=200, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._serve_index()
        elif path == "/events":
            self._serve_sse()
        elif path == "/state":
            self._serve_state()
        else:
            self._send_headers(404, "text/plain; charset=utf-8")
            self.wfile.write(b"not found")

    def _serve_index(self):
        try:
            with open(INDEX_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self._send_headers(500, "text/plain; charset=utf-8")
            self.wfile.write(b"index.html not found")
            return
        self._send_headers(200, extra={"Cache-Control": "no-cache"})
        self.wfile.write(body)

    def _serve_state(self):
        body = json.dumps(HUB.snapshot()).encode("utf-8")
        self._send_headers(200, "application/json", {"Access-Control-Allow-Origin": "*"})
        self.wfile.write(body)

    def _serve_sse(self):
        self._send_headers(200, "text/event-stream", {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        })
        interval = 1.0 / SSE_HZ
        try:
            # 연결 직후 1회 즉시 전송
            while True:
                snap = HUB.snapshot()
                payload = "data: " + json.dumps(snap) + "\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 클라이언트가 탭을 닫음 — 정상 종료
            return


def main():
    global HUB
    ap = argparse.ArgumentParser(description="EPL mmWave 실시간 시각화 서버 (Wi-Fi)")
    ap.add_argument("--host", nargs="+", default=None,
                    help="센서 IP/호스트 (여러 개 가능, 미지정 시 epl_config.json ∪ mDNS 자동탐색)")
    ap.add_argument("--transport", choices=["api", "web"], default="api",
                    help="api=Native API(기본), web=web_server SSE 폴백")
    ap.add_argument("--noise-psk", default=None, help="API 암호화 키(설정된 경우)")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="하드웨어 없이 합성 데이터")
    ap.add_argument("--no-browser", action="store_true", help="브라우저 자동 실행 안 함")
    add_fusion_args(ap)
    args = ap.parse_args()

    HUB = SensorHub()
    apply_fusion_opts(HUB, args)
    workers, mode_desc = build_sources(
        HUB, demo=args.demo, hosts=args.host, transport=args.transport,
        noise_psk=args.noise_psk)
    for w in workers:
        w.start()

    url = f"http://127.0.0.1:{args.http_port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)

    print("=" * 60)
    print("  Everything Presence Lite · mmWave 실시간 시각화")
    print("=" * 60)
    print(f"  데이터 소스 : {mode_desc}")
    print(f"  대시보드    : {url}")
    print(f"  종료        : Ctrl+C")
    print("=" * 60)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다…")
    finally:
        for w in workers:
            w.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
