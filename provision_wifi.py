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
  python provision_wifi.py --camera-id 7     # 이 센서를 카메라 7 에 귀속(기본 camera1)
  python provision_wifi.py --camera-id ''    # 카메라 귀속을 건드리지 않음(센서 id 유지)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from urllib.parse import urlparse

from improv_serial import ImprovSerial, ImprovError
from epl_config import (
    load_config, save_config, upsert_sensor, set_sensor_camera, nonconforming_sensor_ids,
    get_sensors_for_camera, CONFIG_PATH, DEFAULT_CAMERA_ID,
    DEFAULT_ORGANIZATION,
)
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
    ap.add_argument("--camera-id", default=DEFAULT_CAMERA_ID,
                    help=f"이 센서를 귀속시킬 카메라(stream) 식별자 (기본 {DEFAULT_CAMERA_ID}). "
                         "빈 문자열이면 센서 id 를 건드리지 않는다. "
                         "★ 현장에서는 병원이 부여한 숫자 cameraId 로 주세요 — 제품의 "
                         "AddStreamModel.cameraId 는 int 라 'camera1' 은 매칭되지 않습니다.")
    ap.add_argument("--organization", default=DEFAULT_ORGANIZATION,
                    help=f"센서 id 의 organization 파트 (기본 {DEFAULT_ORGANIZATION})")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    camera_id = str(args.camera_id or "").strip()
    organization = str(args.organization or "").strip() or DEFAULT_ORGANIZATION

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
            # ★ 파일은 있는데 파싱이 안 됐다(JSON 문법 오류 등) → 그대로 저장하면 기존 센서
            #   목록·캘리브레이션 좌표가 **전부 사라진다**. 손상 파일을 살려두고
            #   중단한다. Wi-Fi 연결 자체는 이미 성공했으니 센서를 다시 USB에 꽂을 필요는
            #   없고, 파일을 고친 뒤 이 스크립트를 다시 실행하면 된다.
            if not cfg and os.path.isfile(CONFIG_PATH):
                print(f"\n❌ 설정 파일을 읽을 수 없습니다(문법 오류로 보입니다): {CONFIG_PATH}")
                print("   그대로 저장하면 기존 센서·캘리브레이션이 모두 사라지므로 중단합니다.")
                print(f"   → 파일을 고친 뒤 다시 실행하세요. 이번 센서: node={node_name or '?'} "
                      f"host={host or '미확인'}")
                return 7
            # 최상위 ssid 는 계속 기록(마지막으로 프로비저닝한 Wi-Fi).
            cfg["ssid"] = ssid
            # 최상위 host 는 덮어쓰지 않는다. 대신 센서 목록(sensors)에 추가/갱신한다.
            # 배치값(x/y/heading/flip/color)은 upsert 기본값(원점/자동색)으로 채워지되,
            # 이미 등록된 센서면 기존 배치값을 보존한다.
            saved = upsert_sensor(cfg, {
                "node_name": node_name,
                "host": host,
            })
            # 카메라 귀속 = **센서 id 재작성**. 옛 버전은 rooms 목록만 고쳤지만, 이제
            # 귀속이 id 안에 있으므로 카메라를 정하려면 id 자체가 바뀐다(정체성 변경).
            # ★ 실패해도 센서 등록(=USB 재작업이 필요한 값)은 반드시 저장한다.
            #   set_sensor_camera 는 검증을 먼저 하므로 실패 시 id 를 건드리지 않는다.
            old_id = saved.get("id")
            new_id, camera_error, camera_sensor_count = old_id, None, 0
            if camera_id:
                try:
                    new_id = set_sensor_camera(cfg, old_id, camera_id, organization)
                except (ValueError, KeyError) as e:   # 옛 rooms 스키마 / id 충돌
                    camera_error = e
            save_config(cfg)
            total = len(cfg.get("sensors", []))
            print(f"💾 설정 저장: {CONFIG_PATH}")
            print(f"   등록 센서: {saved.get('name')}  ({saved.get('host') or 'host 미확인'})  ·  총 {total}개")
            if camera_error is not None:
                print("   ⚠ 카메라 귀속 실패 — 센서 등록은 저장했습니다(USB 재작업 불필요).")
                print(f"     설정 오류: {camera_error}")
                print(f"     → {CONFIG_PATH} 를 고친 뒤 이 스크립트를 다시 실행하세요.")
            elif camera_id:
                if new_id != old_id:
                    # ★★ id 는 단순 라벨이 아니다 — 융합 입력의 sid 이자 기록 JSONL 의
                    #    센서 식별자다. 바뀌면 이전 녹화와 대조가 끊긴다.
                    print(f"   ⚠ 센서 id 가 바뀌었습니다: {old_id!r} → {new_id!r}")
                    print("     id 는 융합 입력의 sid 이자 기록 JSONL 의 센서 식별자입니다 —")
                    print("     이전에 녹화한 로그(raw_*.jsonl)는 옛 id 로 남아 대조가 끊깁니다.")
                try:
                    mapped = get_sensors_for_camera(cfg, organization, camera_id)
                except ValueError as e:      # 손으로 고친 설정이 불변식을 깬 경우
                    mapped = None
                    print(f"   ⚠ 카메라 귀속을 검증하지 못했습니다: {e}")
                if mapped is not None:
                    camera_sensor_count = len(mapped)
                    names = ", ".join(str(s.get("name") or s.get("id")) for s in mapped)
                    print(f"   카메라: {organization}-{camera_id}  ·  "
                          f"이 카메라 센서 {camera_sensor_count}개 — {names}")
                    # 규격을 벗어난 id 는 제품이 등록을 거절한다(어느 카메라에도 안 붙는다)
                    bad = nonconforming_sensor_ids(cfg)
                    if bad:
                        print(f"   ⚠ id 규격을 벗어난 센서: {', '.join(str(o) for o in bad)}")
                        print("     → 각 센서를 USB 연결해 --camera-id 로 다시 실행하거나,")
                        print(f"       {CONFIG_PATH} 의 \"id\" 를 "
                              "'{organization}-{cameraId}-{sensorId}' 형식으로 고치세요.")
            else:
                print("   카메라: 지정 안 함 — 센서 id 를 건드리지 않았습니다.")
            if not host:
                print("   ⚠ 주소를 못 받았습니다. 공유기 관리페이지에서 IP 확인 후")
                print(f"     {CONFIG_PATH} 의 해당 센서 \"host\" 를 직접 채워주세요. (node: {node_name or '?'})")
            print("\n다음 단계:")
            print("  1) 센서를 USB에서 분리하고 다른 콘센트에 전원만 연결하세요.")
            print("  2) 시각화 실행:  ./run_gui.sh   또는   ./run.sh")
            if camera_id:
                # ★ 캘리브레이션은 센서쌍 대응점이 필요해 그 카메라 센서가 2대 이상일 때만
                #   된다. 1대뿐인 시점에 안내하면 반드시 실패하는 명령을 시키는 셈이다.
                if camera_sensor_count >= 2:
                    print("  3) 배치 캘리브레이션:  "
                          f"./run_auto_positioning_v2.sh --camera-id {camera_id!r}")
                    print("     (x/y/heading 은 **방 좌표계** 값이고 카메라마다 별도의 방"
                          " 좌표계이므로 카메라마다 따로 돌려야 합니다)")
                else:
                    print(f"  3) 배치 캘리브레이션은 카메라 '{camera_id}' 센서가 2대 이상일 때"
                          f" 가능합니다 (현재 {camera_sensor_count}대) — 나머지 센서를 먼저"
                          " 등록하세요.")
            print("\n➕ 센서를 더 추가하려면 각 센서를 USB로 연결해 이 스크립트를 다시 실행하세요.")
            if camera_id:
                print("   다른 카메라 센서라면 run_provision.sh 의 CAMERA_ID 를 바꾸거나"
                      " --camera-id <id> 를 주세요.")
            return 0

    except KeyboardInterrupt:
        print("\n중단됨.")
        return 130
    except Exception as e:
        print(f"❌ 오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
