#!/usr/bin/env python3
"""
터미널 실시간 모니터 (디버그/검증용) ― Wi-Fi 판 · 다중 센서 오버레이

GUI/웹 없이도 무선 데이터 수신을 빠르게 확인하기 위한 도구.
SensorHub 통합 스냅샷을 주기적으로 출력한다(센서별 블록).

사용법:
    python cli_monitor.py                       # epl_config.json 의 센서에 무선 접속
    python cli_monitor.py --host 192.168.1.42   # 호스트 직접 지정(여러 개 가능)
    python cli_monitor.py --host a.local b.local
    python cli_monitor.py --demo                # 하드웨어 없이 합성 데이터(센서 3개)
    python cli_monitor.py --seconds 5           # 5초만 실행 후 종료
"""
import argparse
import time

from mmwave_reader import SensorHub
from mmwave_wifi_reader import build_sources
from fusion import add_fusion_args, apply_fusion_opts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", nargs="+", default=None,
                    help="센서 IP/호스트 (여러 개 가능, 미지정 시 epl_config.json)")
    ap.add_argument("--transport", choices=["api", "web"], default="api")
    ap.add_argument("--noise-psk", default=None)
    ap.add_argument("--demo", action="store_true", help="하드웨어 없이 합성 데이터")
    ap.add_argument("--seconds", type=float, default=0.0, help="실행 시간(0=무한)")
    ap.add_argument("--hz", type=float, default=4.0, help="출력 주기(Hz)")
    add_fusion_args(ap)
    args = ap.parse_args()

    hub = SensorHub()
    apply_fusion_opts(hub, args)
    try:
        workers, desc = build_sources(hub, demo=args.demo, hosts=args.host,
                                      transport=args.transport, noise_psk=args.noise_psk)
    except ValueError as e:                      # 설정 오류(id 규격/중복/옛 rooms)
        from epl_config import config_error_hint
        print(config_error_hint(e))
        return 2
    print(f"[source] {desc}")
    for w in workers:
        w.start()

    t0 = time.monotonic()
    try:
        while True:
            snap = hub.snapshot()
            gconn = "●" if snap["connected"] else "○"
            track_count = snap.get("track_count", 0)
            tracks = snap.get("tracks", [])
            print(f"\033[2J\033[H", end="")  # clear screen
            print(f"  Everything Presence Lite · mmWave 실시간 모니터 (Wi-Fi 다중 센서)")
            # 주 인원수 = 융합 후 실제 인원(중복 제거). 원시 합계는 참고용으로 병기.
            print(f"  {gconn} \033[1m추적 인원(융합): {track_count}\033[0m   "
                  f"(원시 합계 {snap['present_total']})   "
                  f"센서 수: {snap['sensor_count']}")
            print("  " + "=" * 64)

            # [추적 중인 사람] — confirmed 트랙만(생성 중 후보 제외)
            people = [t for t in tracks if t.get("confirmed")]
            print("  [추적 중인 사람]")
            if not people:
                print("      (융합 트랙 없음)")
            else:
                print(f"      {'#id':<5}{'X(mm)':>9}{'Y(mm)':>9}"
                      f"{'속력(m/s)':>11}{'방향(°)':>9}  기여센서")
                for t in people:
                    coast = " ~" if t.get("coasting") else ""
                    sids = ",".join(str(s) for s in t.get("sensors", []))
                    print(f"      {'#' + str(t['id']):<5}{t['x']:>9.0f}{t['y']:>9.0f}"
                          f"{t['speed']:>11.2f}{t['heading_deg']:>9.1f}"
                          f"  [{sids}]{coast}")
            print("  " + "=" * 64)

            sensors = snap["sensors"]
            if not sensors:
                print("  (등록/발견된 센서 없음)")
            for i, s in enumerate(sensors):
                conn = "●" if s["connected"] else "○"
                stats = s["stats"]
                print(f"  [{i + 1}] {conn} {s['name']}  ({s['mode']})  "
                      f"port={s['port']}")
                illum = s["illuminance"]
                illum_txt = f"{illum:.1f} lx" if illum is not None else "--"
                print(f"      인원={s['present_count']}  조도={illum_txt}  "
                      f"line_rate={stats['line_rate']}/s  lines={stats['lines_total']}  "
                      f"Zone={s['zones']}")
                print(f"      {'ID':<3}{'present':<9}{'X(mm)':>9}{'Y(mm)':>9}"
                      f"{'dist':>8}{'angle':>8}{'speed':>8}")
                for t in s["targets"]:
                    mark = "YES" if t["present"] else "-"
                    print(f"      {t['id']:<3}{mark:<9}{t['x']:>9.0f}{t['y']:>9.0f}"
                          f"{t['distance']:>8.0f}{t['angle']:>8.1f}{t['speed']:>8.2f}")
                print("  " + "-" * 64)

            time.sleep(1.0 / max(0.5, args.hz))
            if args.seconds and (time.monotonic() - t0) >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        for w in workers:
            w.stop()


if __name__ == "__main__":
    main()
