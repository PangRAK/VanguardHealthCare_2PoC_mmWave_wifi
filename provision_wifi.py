#!/usr/bin/env python3
"""
센서 Wi-Fi 연결(프로비저닝) 도구  ―  USB로 1회만 실행

[전체 사용 흐름]
  1) 센서를 노트북에 USB로 연결
  2) 이 스크립트로 Wi-Fi 자격증명 전송  →  센서가 무선 연결되고 IP 를 알려줌
  3) 센서를 USB에서 분리, 다른 콘센트에 전원만 연결  →  등록된 Wi-Fi 에 자동 접속
  4) 시각화 실행(server.py / gui_qt.py)  →  유선 없이 무선으로 데이터 수신

[사용법]
  python provision_wifi.py                 # 대화형: 포트 자동탐지 → AP 스캔 → SSID/PW 입력 → 연결
  python provision_wifi.py --scan          # 주변 Wi-Fi 목록만 출력
  python provision_wifi.py --probe         # 장치 상태/펌웨어 정보만 확인(읽기전용)
  python provision_wifi.py --ssid MyAP --password 's3cret'   # 비대화형
  python provision_wifi.py --port /dev/cu.usbserial-2140
"""
from __future__ import annotations

import argparse
import getpass
import sys
from urllib.parse import urlparse

from improv_serial import ImprovSerial, ImprovError
from epl_config import load_config, save_config, upsert_sensor, CONFIG_PATH
from mmwave_reader import autodetect_port


def _pick_port(arg_port: str | None) -> str | None:
    if arg_port:
        return arg_port
    return autodetect_port()


def _print_networks(nets):
    if not nets:
        print("  (스캔된 네트워크 없음)")
        return
    print(f"  {'#':>2}  {'SSID':<28} {'RSSI':>5}  보안")
    for i, (ssid, rssi, secured) in enumerate(nets):
        print(f"  {i:>2}  {ssid:<28} {rssi:>5}  {'🔒' if secured else 'open'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="EPL 센서 Wi-Fi 프로비저닝 (Improv Serial)")
    ap.add_argument("--port", default=None, help="USB 시리얼 포트 (미지정 시 자동탐지)")
    ap.add_argument("--ssid", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--scan", action="store_true", help="주변 Wi-Fi 스캔만")
    ap.add_argument("--probe", action="store_true", help="장치 상태/정보만 확인(읽기전용)")
    ap.add_argument("--force", action="store_true",
                    help="SSID 가 스캔 목록에 없어도 강제 진행(숨김 SSID 용)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    port = _pick_port(args.port)
    if not port:
        print("❌ USB 시리얼 포트를 찾지 못했습니다. 센서를 USB로 연결했는지 확인하세요.")
        print("   (포트를 직접 지정: --port /dev/cu.usbserial-XXXX)")
        return 2
    print(f"🔌 포트: {port}")

    try:
        with ImprovSerial(port, verbose=args.verbose) as imp:
            # 1) 상태 확인
            state = imp.request_state()
            if state is None:
                print("❌ 센서가 Improv 응답을 하지 않습니다. (펌웨어/포트 확인)")
                print("   USB가 맞는지, 다른 프로그램이 포트를 점유 중은 아닌지 확인하세요.")
                return 3
            print(f"📡 장치 상태: {state}")

            info = imp.device_info()
            node_name = info[3] if len(info) >= 4 else ""
            if info:
                print(f"ℹ️  펌웨어: {' / '.join(info)}")

            if args.probe:
                print("✅ probe 완료 (읽기전용). 장치가 Improv 프로비저닝을 지원합니다.")
                return 0

            # 2) 네트워크 스캔
            print("\n🔍 주변 Wi-Fi 스캔 중…")
            nets = imp.scan_networks()
            _print_networks(nets)
            if args.scan:
                return 0

            # 3) SSID/PW 결정 (인자 → 없으면 대화형)
            ssid = args.ssid
            password = args.password
            if not ssid:
                if nets:
                    sel = input("\n연결할 네트워크 번호(또는 SSID 직접 입력): ").strip()
                    if sel.isdigit() and int(sel) < len(nets):
                        ssid = nets[int(sel)][0]
                    else:
                        ssid = sel
                else:
                    ssid = input("\nSSID 입력: ").strip()
            if not ssid:
                print("❌ SSID 가 비어있습니다.")
                return 4

            # 스캔 목록에 없는 SSID 는 거짓 성공(오탐) 위험 → 기본적으로 중단.
            # 이미 다른 Wi-Fi 에 연결된 센서는, 존재하지 않는 SSID 를 줘도
            # 아직 살아있는 기존 연결을 근거로 improv 성공을 돌려주기 때문이다.
            if nets and ssid not in {n[0] for n in nets}:
                print(f"❌ '{ssid}' 을(를) 주변 Wi-Fi 스캔 목록에서 찾지 못했습니다.")
                print("   AP 가 켜져 있는지 / 2.4GHz 인지 확인 후 다시 시도하세요.")
                print("   (숨김 SSID 라 스캔에 안 뜨는 경우: --force 로 강제 진행)")
                if not args.force:
                    return 6
                print("   ⚠ --force 지정됨 → 검증 없이 강제로 진행합니다.")

            if password is None:
                password = getpass.getpass(f"'{ssid}' 비밀번호(없으면 Enter): ")

            # 4) 프로비저닝
            print(f"\n📶 '{ssid}' 에 연결 시도 중… (최대 30초)")
            try:
                urls = imp.provision(ssid, password)
            except ImprovError as e:
                hint = {
                    "UNABLE_TO_CONNECT": "비밀번호 오류 또는 신호 약함. SSID/PW 확인 후 재시도.",
                    "NOT_AUTHORIZED": "장치가 인증 대기 상태입니다.",
                    "BAD_HOSTNAME": "호스트명 문제.",
                }.get(str(e), "")
                print(f"❌ 연결 실패: {e}  {('— ' + hint) if hint else ''}")
                return 5

            # 5) 결과 저장
            url = urls[0] if urls else ""
            host = urlparse(url).hostname if url else ""
            # 센서가 URL 을 안 돌려줘도(web_server 꺼진 경우) mDNS 이름으로 접속 가능.
            if not host and node_name:
                host = f"{node_name}.local"
                url = f"http://{host}"
            print("✅ Wi-Fi 연결 성공!")
            if url:
                print(f"   장치 주소: {url}")
            cfg = load_config()
            # 최상위 ssid 는 계속 기록(마지막으로 프로비저닝한 Wi-Fi).
            cfg["ssid"] = ssid
            # 최상위 host 는 덮어쓰지 않는다. 대신 센서 목록(sensors)에 추가/갱신한다.
            # 배치값(x/y/heading/flip/color)은 upsert 기본값(원점/자동색)으로 채워지되,
            # 이미 등록된 센서면 기존 배치값을 보존한다.
            saved = upsert_sensor(cfg, {
                "node_name": node_name,
                "host": host,
            })
            save_config(cfg)
            total = len(cfg.get("sensors", []))
            print(f"💾 설정 저장: {CONFIG_PATH}")
            print(f"   등록 센서: {saved.get('name')}  ({saved.get('host') or 'host 미확인'})  ·  총 {total}개")
            if not host:
                print("   ⚠ 주소를 못 받았습니다. 공유기 관리페이지에서 IP 확인 후")
                print(f"     {CONFIG_PATH} 의 해당 센서 \"host\" 를 직접 채워주세요. (node: {node_name or '?'})")
            print("\n다음 단계:")
            print("  1) 센서를 USB에서 분리하고 다른 콘센트에 전원만 연결하세요.")
            print("  2) 시각화 실행:  ./run_gui.sh   또는   ./run.sh")
            print("\n➕ 센서를 더 추가하려면 각 센서를 USB로 연결해 이 스크립트를 다시 실행하세요.")
            return 0

    except KeyboardInterrupt:
        print("\n중단됨.")
        return 130
    except Exception as e:
        print(f"❌ 오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
