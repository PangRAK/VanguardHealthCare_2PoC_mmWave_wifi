# Vanguard mmWave PoC — 배포·네트워크·운영 지식 로그 (skill.md)

> 밴가드 요양병원 **화장실 재실 감지 PoC**의 배포/네트워크/운영 지식을 누적 기록하는 **리빙 문서**입니다.
> 새 작업·결정·수치가 생기면 맨 아래 **[변경 로그]** 에 날짜와 함께 추가하세요.
> (개인 메모리와 별개로, 레포에 남겨 팀이 함께 보기 위한 문서)

---

## ★ 핵심 설계 요구사항 (모든 코드 수정이 만족해야 함)
**최종 목표: "다른 서브넷이지만 라우팅으로 연결된 내부망"에서도 Mac ↔ 센서 통신이 되어야 한다.**
- **프로비저닝은 병실(센서와 같은 서브넷)** 에서, **실사용 접속은 서버실(다른 서브넷)** 에서 일어난다 → §11 현장 시나리오.
- 따라서:
  - **R1. 연결은 IP 직결(mDNS/ARP 비의존)** 이어야 한다. 서버실은 센서와 다른 서브넷이라 mDNS/ARP가 라우터를 못 넘는다.
  - **R2. 센서 IP는 고정**이어야 한다(병실 공유기 DHCP 예약 또는 firmware static). 안 그러면 IP가 바뀔 때 다른 서브넷에서 재발견 불가.
  - **R3. 센서 IP 캡처는 "병실(같은 서브넷)에 있을 때"** 해야 한다(mDNS/ARP 가능한 유일한 순간) → 이후 서버실에서 그 IP로 접속.
  - **R4. 내부망 라우팅 + 방화벽**이 서버실 서브넷 ↔ 병실 서브넷, TCP 6053 을 허용해야 한다(IT).
- ⚠️ **어떤 코드 변경도 이 3-phase 시나리오(§11)를 깨면 안 된다.** (예: 접속을 mDNS에 의존하게 만들면 서버실에서 동작 불가)

---

## 1. 목표 / 개요
- 화장실에 **mmWave 재실 감지 센서 3대** 설치 → 병실 Wi-Fi → 병원 내부망 → **FCC룸 관제 PC**에서 실시간 모니터링(재실 여부·체류시간).
- 전송 데이터: **위치 좌표값만** (영상·음성 없음) → 개인정보 부담 낮음.

## 2. 하드웨어 / 데이터 경로
- 센서: **Everything Presence Lite(EPL)** = ESP32-WROOM + **HLK-LD2450 24GHz mmWave 레이더** + CH340 USB.
  - **24GHz = 레이더(사람 감지)**, **2.4GHz = Wi-Fi(통신)** — 서로 다른 주파수 (혼동 주의).
  - LD2450: 최대 3타겟, 2D 평면(X/Y mm, 높이 없음), FOV ±60°, ~6m.
- 펌웨어: **ESPHome**. 데이터 경로:
  ```
  [LD2450] --UART--> [ESP32/ESPHome] --Wi-Fi(Native API TCP 6053)--> [PC 리더(aioesphomeapi)]
  ```
- **Native API = TCP 6053**, EPL 기본 펌웨어에서 항상 켜짐, 기본 **무암호**(noise_psk 없음).

## 3. 센서 3대 (확정값)
| id (규격) | 옛 id | 이름 | **MAC (고정 앵커)** | node_name | 색 | 현재 로컬 IP(가변) |
|---|---|------|----------------------|-----------|----|--------------------|
| `pia-1-1` | 98bd80 | Sensor 1 | `A4:F0:0F:98:BD:80` | everything-presence-lite-98bd80 | #27e0c8 | 10.201.31.120 |
| `pia-1-2` | bc0e00 | Sensor 2 | `20:E7:C8:BC:0E:00` | everything-presence-lite-bc0e00 | #ffb454 | 10.201.31.153 |
| `pia-1-3` | b9a07c | Sensor 3 | `00:70:07:B9:A0:7C` | everything-presence-lite-b9a07c | #ff5d8f | 10.201.31.76 |

- **id 규격 = `{organization}-{cameraId}-{sensorId}`** (§5.1). 옛 id(MAC 뒤 6자리)는
  `sensorId` 를 순번으로 바꾸면서 사라졌다 → **물리 센서 대조는 이제 `mac` 이 유일한 단서**다.
- 2026-07-31 이전에 기록한 JSONL(`raw_*.jsonl`)의 `sid` 는 **옛 id** 다. 재생은 그대로 되지만
  (`replay_frames` 는 sid 를 불투명 키로만 쓴다) 새 설정과 센서 대조는 되지 않는다.
- MAC은 **ARP + ESPHome API `device_info` 이중 검증**됨. **IP는 DHCP라 가변**(§4 참고).
- 프로비저닝 SSID: **`RAK`** (현재 로컬 데모망, 10.201.31.0/24, 단말격리 없음).

## 4. 네트워크 모델 (핵심 개념)
- **mDNS(`.local`) · ARP = 같은 서브넷(브로드캐스트 도메인)에서만** 동작 → **라우터를 못 넘음**.
  - 같은 서브넷 → 이름/자동탐색으로 **IP 몰라도 접속 가능**.
  - 다른 서브넷(라우팅된 내부망) → mDNS 불가 → **예약 고정 IP 직접접속** 또는 대역 스캔 필요.
- **같은 대역 ≠ 같은 IP** (같은 건물/다른 호실). 공유기=게이트웨이(예: .1), 기기는 각자 IP.
- **NAT**: 인터넷 나갈 때만 공인 IP 하나로 뭉쳐 보임. 로컬 통신은 각자 사설 IP.
- **방화벽 규칙**: source=FCC PC IP, dest=센서 IP, TCP 6053. **공유기는 경유지**(규칙 대상 아님).
- **단말격리(client isolation)**: 포트 무관하게 기기간 차단 → **6053 열어도 안 됨**(격리 해제 필요). → IT에 "포트 차단인지 단말격리인지" 반드시 확인.

## 5. 현재 코드/설정 상태
- `epl_config.json`: 다중 센서, 각 센서 `mac` 등록, **`host`=고정 IP**, **`discovery:false`**(mDNS 끔).
  - mDNS로 되돌리려면 각 host를 `<node_name>.local`로 바꾸면 됨(node_name 보존됨).
- **카메라 귀속 = 센서 id 접두** (2026-07-31 전환, 옛 `rooms` 필드 폐기). 현재 `pia-1-1/2/3`.

### 5.1 카메라 모델 — 센서 id 가 귀속의 유일한 표현
- **카메라 1개 = 제품(Product-AI-mono)의 stream 1개 = 융합 좌표계 1개**.
  `x/y/heading_deg` 는 **방 좌표계** 값이고 카메라마다 별도의 방 좌표계다.
- 귀속은 **센서 id 하나로만** 표현한다(**PoC 도구와 제품이 동일 로직**):

  ```
  id = "{organization}-{cameraId}-{sensorId}"        예: "pia-1-1"
  ```

  · 파싱은 `split("-", 2)` 3파트. `organization`/`cameraId` 파트에는 `-` 불가,
    **`sensorId` 파트에는 허용**(host 유래 자동 id `10-201-31-120` 수용).
  · 세 파트 모두 **불투명 문자열**. 숫자로 파싱하지 않고 순서에서 의미를 끌어내지 않는다
    → `sensorId` 를 바꿔도 유일성만 지키면 동작이 같다.
  · `organization` 도 함께 대조한다(멀티테넌트에서 다른 조직 센서를 빌리지 않도록).
    이 대조가 **'형식은 맞고 뜻은 틀린' id 를 걸러내는 최종 안전장치**다 — host 유래
    `10-201-31-120` 은 3파트를 통과하지만 org 파트가 `10` 이라 아무 카메라에도 안 붙는다.
  · 매칭 0개면 **빈 목록**(전 센서로도, 남의 카메라 센서로도 폴백하지 않음).
- **아래는 전부 설정 오류로 즉시 실패**한다(제품이 스트림 등록을 거절):
  id 형식 위반 / `id` 누락 / id 중복 / **같은 host 를 다른 id 로 두 번** /
  옛 `rooms` 키·센서 `room` 태그 / `organization`·`cameraId` 에 `-`.
  · `id` 누락이 오류인 이유: 설정 파일에는 org·cameraId 문맥이 없어 자동 생성 id 가 귀속을
    담지 못하는데, host 유래 id 는 **형식만은 통과**해서 조용히 아무 카메라에도 안 붙는다.
  · host 중복이 오류인 이유: 옛 `assert_one_room_per_sensor` 가 막던 사고를 이어받는다 —
    두 카메라가 같은 기기에 붙으면(TCP 6053 ×2) 같은 사람을 두 번 센다.
- **옛 `rooms` 스키마는 자동 마이그레이션하지 않는다.** 옮기려면 코드가 센서 id 를 바꿔야
  하는데, id 는 융합 입력의 `sid` 이자 관제 화면·기록 JSONL 의 센서 식별자다. 코드가
  조용히 바꾸면 화면·로그가 실동작과 어긋난다.
- 도구 3종이 `--camera-id`(기본 `1`, `epl_config.DEFAULT_CAMERA_ID`)를 받는다:

  | 스크립트 | 설정 변수 | 카메라를 쓰는 방식 |
  |---|---|---|
  | `run_provision.sh` | `CAMERA_ID=1` | 이번에 연결한 센서의 **id 를 다시 써서** 그 카메라에 귀속 |
  | `run_auto_positioning.sh` | `CAMERA_ID=1` | 그 카메라 센서에만 접속해 측정·저장 |
  | `run_auto_positioning_v2.sh` | `CAMERA_ID=1` | 로그 헤더 센서 중 그 카메라 것만 풀링 |

  세 스크립트 모두 `CAMERA_ID=` (빈 값) → 센서 id 를 안 건드림. CLI `--camera-id` 가 우선.
- ★★ **`set_sensor_camera()` 는 목록 편집이 아니라 정체성 변경이다.** 카메라를 바꾸면
  센서 id 가 바뀌고, 그 id 는 융합 입력의 `sid` 이자 기록 JSONL(`header.sensors[].id` /
  `raw[].sid` / `dets[].sid`)의 센서 식별자다 → **이전 녹화와 대조가 끊긴다.**
  `provision_wifi.py` 가 바뀐 id 를 출력으로 경고한다.
- ★ **현장 설치 시 `cameraId` 파트를 병원이 부여한 실제 cameraId 로 교체해야 한다.**
  숫자일 필요는 없다 — 제품의 `AddStreamModel.cameraId` 는 **문자열**이고, 백엔드가 JSON
  숫자로 보내도 DTO 경계(`coerce_camera_id`)에서 문자열로 승격된다. 교체하지 않으면 기본값
  `1` 이 현장 카메라와 안 맞아 그 스트림은 센서 0개가 된다(= 체류 알람이 아예 안 나감).
  · **`cameraId` 에 `_` 금지** — 제품 `stream_id` 가 `{cameraId}_{organization}` 이고 폴백이
    첫 `_` 1회 분해라 `ward_a`+`pia` 가 `ward`+`a_pia` 로 갈린다(카메라·테넌트가 동시에
    틀리며 예외도 로그도 없다). `-` 도 센서 id 구분자라 금지. `organization` 쪽 `_` 는 허용.
- **`sensorId` 는 생략하면 자동 배정된다** — 그 카메라에서 아직 안 쓰인 최소 순번을 기기
  유래 `spec_key`(node_name/host) 정렬로 **결정적으로** 준다(배열 순서 무관, 전역 카운터
  없음). **명시한 id 는 절대 재번호화하지 않는다.** 대조는 `normalize_id_value`
  (`strip().lower()`) 단일 기준이라 **대소문자만 다른 id 도 중복**이고, 숫자 정규화는 없어
  `"01"` 과 `"1"` 은 다른 id 다. 비숫자(`north`)·비연속·역순 sensorId 도 정상 동작한다.
- **카메라를 나누면 카메라마다 따로** `./run_auto_positioning_v2.sh --camera-id <id>` 를
  돌려야 한다(한 번에 돌리면 서로 다른 방 좌표계를 억지로 하나에 맞춘 배치가 저장된다).
- ⚠ **시각화·진단 도구는 아직 카메라를 모른다** (`server.py` / `gui_qt.py` / `cli_monitor.py` /
  `diagnose.py` / `check_sensors.py`). 전 센서를 **한 SensorHub 에** 넣으므로 카메라가 2개면
  오버레이만 뒤섞이는 게 아니라 **서로 다른 카메라의 두 사람이 한 트랙으로 융합**된다
  (좌표계가 다른 값을 같은 평면으로 취급 → 거리·병합 판정이 무의미). 카메라를 나눈 뒤에는
  이 도구들의 화면·인원수를 신뢰하지 말 것 — 제품 경로(카메라별 SensorHub)만 정확하다.
- ⚠ `run_auto_positioning{,_v2}.sh` 의 `CAMERA_ID=` 를 **비우면** 카메라가 2개 이상인
  설정에서 **실행이 거부된다** — 여러 카메라 센서를 한 좌표계로 맞춰 저장하면 다른 카메라의
  캘리브레이션을 덮어쓰기 때문이다. 카메라마다 `--camera-id <id>` 로 따로 돌릴 것.
- 리더: `mmwave_wifi_reader.py` `WifiApiReader` → `APIClient(host,6053)` + `ReconnectLogic(login=True)` + `subscribe_states`.
  - 접속엔 **`host`만 사용**(IP/이름 무관). `mac`은 참조·resolve 앵커용(리더가 직접 안 씀).
  - `discovery` 플래그는 **자동탐색(browse)만** 제어 — host의 `.local` 조회와는 별개.
- 파서/융합/시각화 로직은 배포 이관 시 **무변경**(값만 교체).

### 5.2 제품(Product-AI-mono)이 카메라를 정하는 순서
제품은 **카메라 식별자를 백엔드 등록값에서 받는다** — 추측 계층이 없다.
`source.resolve_camera_id()` 의 출처는 단 하나다:

```
user_param["user_param"]["cameraId"]  /  ["organization"]
   └ 없으면 stream_id("{cameraId}_{organization}")를 첫 '_' 기준 1회 분해해 폴백
```

- **왜 추측이 필요 없어졌나** — MQ 봉투의 `cameraId` 는
  `DTO/output_handler.make_alarm_message` 가 그 등록값을 **그대로** 싣는다. 즉 같은 값으로
  센서를 고르면 "센서가 본 사람"과 "알람이 가리키는 카메라"가 정의상 일치한다.
- `cameraUrl` / `room` / `roomId` / `mmwave://` 는 **더 이상 보지 않는다.** 방 이름이라는
  근거가 없는 부산물이었고, 그걸 추측하던 계층(옛 `resolve_effective_room`)이 조용히 엉뚱한
  센서를 붙일 수 있는 유일한 통로였다. `cameraUrl` 은 카메라 기준 규약의 필수 필드지만
  mmWave 스트림에는 대응하는 카메라 주소가 없어 빈 문자열이 정상이다.
- `organization`·`cameraId` 에 `-` 가 있으면 **등록을 거절**한다 — 그 카메라를 가리키는
  규격 id 를 만들 수 없어 조용히 '센서 0개' 로 떨어지기 때문이다.
- **한 카메라 = 한 스트림은 구조적으로 보장된다.** `stream_id` 가
  `{cameraId}_{organization}` 이라 같은 카메라를 가리키는 서로 다른 stream_id 가 존재할 수
  없다 → 옛 claim 충돌 검사는 삭제했다(같은 stream_id 재등록은 '교체').
- 알람 키는 레포 표준 `{stream_id}__{category}` 를 유지한다(백엔드가 stream_id 로 대조).
  카메라↔스트림이 1:1 이라 그게 곧 카메라 단위 알람이다.
  ※ 그래서 센서 id 에 `__` 를 금지한다 — 끼면 카테고리 추출이 깨져 알람이 통째로 사라진다.

## 6. 로컬 테스트 vs 실제 배포 (같은 코드, host 값만 다름)
| | 로컬 테스트 | 실제 배포(병원) |
|---|---|---|
| host | 현재 DHCP IP 또는 `.local` | **병원 예약 고정 IP** |
| discovery | false(권장) 또는 true(mDNS 편의) | false |
| 안정화 | 공유기에 MAC별 **DHCP 예약** 권장 | 예약 IP 필수 |
- ⚠️ **로컬 IP는 DHCP라 재부팅/임대만료 시 변함** → 그때 config stale → 갱신 필요.

## 7. 실행 / 검증
```bash
./.venv/bin/python check_sensors.py     # 통신 점검(6053 도달성)
./run.sh                                 # 웹 시각화 (융합 파라미터는 run.sh 상단)
./run_gui.sh                             # PyQt GUI
./run_provision.sh --ssid <SSID> --password <PW>   # USB로 Wi-Fi 프로비저닝
```

## 8. 병원(현장) 배포 체크리스트
- **IT 요청**: TCP 6053 개방(FCC PC→센서 3대) · **각 MAC에 고정 IP 예약** · **FCC PC 고정 IP** · **차단 방식(포트 vs 단말격리) 확인**.
- 화장실 Wi-Fi 약하면 **유선 AP(액세스포인트 모드)** 추가(WAN 아님, LAN↔LAN).
- 현장 설치 후 **`auto_positioning` 재캘리브레이션**(x/y/heading).
- 산출물(레포 내): `Vanguard_network_diagram.png/pdf`, `Vanguard_IT_port_request.docx`(간소화·기입란), `Vanguard_IT_port_request_full.docx`(전체판).
  - docx 표에 IT가 채울 칸: **FCC PC IP**, **센서별 예약 IP ×3**. (Monitoring IP·Inference server IP = MACS 솔루션 필요 IP로 명시)

## 9. 계획(미구현): IP 자동 resolve
- **MAC을 앵커**로 매 실행 시 현재 IP 찾기: `resolve_by_mac()` (mmwave_wifi_reader.py) + `run_resolve.sh`(또는 `run.sh --auto-resolve`).
- 계층 전략: **예약IP → mDNS(`.local`) → ARP 매칭 → 서브넷 스캔(6053+`device_info.mac` 대조)** → config `host` 갱신.
- 가능성: **같은 서브넷 = 완전 자동**. **다른 서브넷(라우팅) = 예약 IP 또는 탐색 대역 지정 필요**(mDNS/ARP는 라우터 못 넘음). 라우팅 없으면 불가.

## 10. 열린 항목 / TODO
- [ ] RAK 공유기 **DHCP 예약**(로컬 안정화)
- [ ] 병원 **예약 IP · SSID/PW · 방화벽 개방/격리 확인** 수령
- [ ] `resolve_by_mac()` + `run_resolve.sh` 구현
- [ ] **암호화(noise_psk)** 병원 정책 확인
- [ ] **FCC PC IP/MAC** 확보
- [ ] 현장 설치 후 **캘리브레이션**
- [ ] **`best_params.yaml` 0.1초 기준 재튜닝** → PoC `debug_logs/` + 제품 `assets/config/` 동시 갱신
- [ ] (제품) `fusion_params` 가 `_interval_second` 를 읽어 실제 스냅샷 주기와 다르면 **경고**
      (지금은 PoC 가 값만 동봉. 조용히 어긋나면 같은 HP 가 다른 시간 폭을 뜻해 성능이 안 재현됨)
- [ ] `server.py`/`gui` 실제 실행 렌더 확인(고정 IP 전환 후 잔여 검증)
- [ ] `run_provision.sh`: 프로비저닝 직후 **센서 IP 자동 캡처 → config `host` 기록** (R3)
- [ ] 병실 공유기 **DHCP 예약**(또는 firmware static IP)로 센서 IP 고정 (R2)
- [ ] 서버실 ↔ 병실 **서브넷 라우팅 + TCP 6053 방화벽** 확인 (R4, IT)
- [ ] **[센서 id 규격 후속 · 배포 게이트]** `rooms` 폐기 → id 규격화(2026-07-31)에 딸린 것들:
  - [x] **제품·PoC 동시 전환 완료** — `parse_sensor_id`/`make_sensor_id`/
        `assert_sensor_id_format`/`assert_no_legacy_schema`/`assert_unique_sensor_hosts`/
        `get_camera_ids`/`get_sensors_for_camera` 를 양쪽에 넣고, 카메라 해석을
        `source.resolve_camera_id()` 단일 경로로 통합했다(§5.1·§5.2).
  - [x] **★ HF(`PIA-SPACE-LAB/SensorData`)의 `epl_config.json` 을 새 id 규격으로 교체**
        → **완료(2026-08-02)**. 제품 `assets/` 는 **git 무시 대상**(`.gitignore:73`)이라
        HF 가 유일한 배포 경로였고, 교체 전까지 다른 머신/CI 는 옛 `rooms` 파일을 받아
        스트림 등록이 ValueError 로 거절됐다. 올린 내용은 이 레포의 `epl_config.json`
        과 같다(`pia-1-1/2/3`).
        · 실자산 테스트 2개(`test_real_epl_config` / `test_real_epl_config_ids_conform`)의
          **skip 분기는 그대로 남긴다** — 없앨 게 아니라 안 걸리는 게 정상이다. 누군가
          옛 리비전을 가리키거나 다음 스키마 변경이 오면 다시 그 사실을 알려야 한다.
          (로컬 자산이 옛 것이면 skip 이 아니라 **fail** 이다 — 방침 무변경.)
  - [x] ~~제품의 `AddStreamModel.cameraId` 가 `int` 라 비숫자 cameraId 가 매칭되지 않던
        문제~~ → **해소됨(2026-08-02)**: `cameraId` 는 문자열이고 백엔드가 JSON 숫자로
        보내도 DTO 경계(`coerce_camera_id`)에서 승격된다. 이제 `ward-a` 같은 값도 등록된다.
  - [ ] **현장 설치 시 `cameraId` 파트를 병원이 부여한 실제 cameraId 로 교체**(값 자체는
        여전히 현장 값이어야 한다 — 기본값 `1` 은 그 병원 카메라가 아니다).
        `./run_provision.sh` 의 `CAMERA_ID` 또는 `--camera-id <id>`. 숫자 제약은 없고,
        `_` 와 `-` 만 쓸 수 없다.
  - [ ] **백엔드가 `cameraId`/`organization` 을 정확히 내려주는지 확인** — 이제 이 둘이
        카메라 식별의 **단일 출처**다(`cameraUrl`/`room`/`roomId` 는 읽지 않는다).
        없으면 `stream_id`(`{cameraId}_{organization}`) 분해로 폴백한다.
  - [ ] 시각화·진단 5개 도구에 `--camera-id` 도입(§5.1 마지막 ⚠ — 카메라 2개면 트랙이 융합된다)
  - [ ] `organization` 에 `-` 가 들어가는 테넌트가 생기면 규격을 다시 봐야 한다 — 지금은
        그런 스트림을 **등록 거절**한다(그 카메라를 가리키는 id 를 만들 수 없으므로).

---

## 11. 현장 시나리오 (PoC On-site Runbook)
> 전제: **서버실 LAN 과 병실 Wi-Fi 는 내부망으로 라우팅 연결**(서로 다른 서브넷). 각 Phase의 서브넷·mDNS 가용성에 주목. (설계 기준 §★ 참조)

### Phase A — 병실에서 프로비저닝  *(Mac·센서 = 같은 서브넷, mDNS 가능)*
1. MacBook 들고 **병실로 이동**.
2. Mac을 **병실 Wi-Fi에 연결**.
3. **센서를 Mac에 USB 연결**.
4. `./run_provision.sh --ssid <병실SSID> --password <PW>` → 센서를 병실 Wi-Fi에 등록.
   - **[카메라 배정] 스크립트의 `CAMERA_ID`**(기본 `1`)가 이 센서가 귀속될 카메라다.
     ★ 현장에서는 **병원이 부여한 실제 cameraId** 로 바꿔야 한다(숫자가 아니어도 된다 —
     제품의 `cameraId` 는 문자열이다. 단 `_`·`-` 는 쓸 수 없다).
     카메라가 여러 개면 그 카메라 센서를 다 끝낸 뒤 `CAMERA_ID` 를 바꿔 다음 카메라 센서를
     프로비저닝한다(또는 이번만 `--camera-id 8`). 출력의 `카메라: pia-7 · 이 카메라 센서 N개`
     로 확인.
   - ★★ 이때 **센서 id 자체가 다시 쓰인다**(`pia-1-1` → `pia-7-1`). id 는 융합 입력의 `sid`
     이자 기록 JSONL 의 센서 식별자라 **이전 녹화와 대조가 끊긴다** — 스크립트가 바뀐 id 를
     경고로 출력하니 반드시 확인할 것.
   - "id 규격을 벗어난 센서" 경고가 뜨면 그 센서는 **제품이 스트림 등록을 거절한다** → 반드시 수정.
5. **[중요·R3] 프로비저닝 직후 같은 서브넷에서 센서의 현재 IP를 캡처**해 `epl_config.json` 의 `host`에 기록.
   - mDNS/ARP가 되는 **유일한 순간**이 지금(같은 서브넷). 서버실 가면 못 함.
   - **[R2] IT가 병실 공유기에 해당 MAC DHCP 예약** → IP 고정(가장 안전).

### Phase B — 센서 설치  *(화장실, 전원 공급)*
6. 센서를 Mac에서 분리 → **화장실에 부착 + 전원 공급**.
7. 프로비전돼 있으므로 전원 시 **병실 Wi-Fi에 자동 접속** → (예약)IP 획득.

### Phase C — 서버실에서 관제  *(Mac = 다른 서브넷, 유선 LAN)*
8. MacBook을 **서버실로 이동**, 서버실 **LAN 포트에 유선 연결**(병실과 다른 서브넷).
9. 시각화 실행(`./run.sh` / `./run_gui.sh`) → **캡처해둔 센서 고정 IP로 크로스 서브넷 TCP 6053 접속**.
   - 이 시점 **mDNS/ARP 불가**(라우터 못 넘음) → **IP 직결만 유효**[R1].
   - 전제: 내부망 라우팅 + 방화벽이 **서버실↔병실 서브넷, TCP 6053** 허용[R4].

### 실패 모드 (반드시 방지)
- **센서 IP가 Phase A~C 사이 바뀜**(재부팅/임대만료) → 서버실(다른 서브넷)에서 **재발견 불가** → **예약/Static IP 필수**[R2].
- **라우팅/방화벽 차단 또는 단말격리** → 6053 열어도 불가 → IT 확인[R4].
- **접속 로직이 mDNS에 의존** → 서버실에서 동작 안 함 → **IP 직결 유지**[R1].

---

## 12. MACS 시스템 개발 범위 (Scope)
**MACS에 개발할 것 — `run_gui.sh` 실행 시 쓰이는 런타임 기능만:**
- 센서 연결
- 사람 트래킹
- 실시간 재실 카운팅
- 알람 발생

**MACS에 개발할 필요 없음 — 아래는 설치·개발용 보조 스크립트(제품에 통합 X):**
- `run_provision.sh` — Wi-Fi 프로비저닝 (설치 시 1회, 별도 도구)
- `run_auto_positioning.sh` / `run_auto_positioning_v2.sh` — 센서 위치 자동 캘리브레이션 (설치 시)
- `run_optimization.sh` — 하이퍼파라미터 튜닝(오프라인 최적화)
- `run_debug_gui.sh` — 개발/디버그용 GUI

---

## [변경 로그]

- **2026-08-02 (순번 sensorId 자동 배정 · `cameraId` 문자열화 · 정규화 단일 기준)**
  - **왜**: 2026-07-31 전환은 id 규격을 세웠지만 **id 를 누가 정하는가**는 남겨뒀다. 그
    결과 (a) id 를 생략하면 host 유래 값(`98bd80` / `10-201-31-120`)이 sensorId 파트가 돼
    사람이 읽을 수 없고 카메라 순번 체계 밖에 남았고, (b) 제품 `AddStreamModel.cameraId`
    가 `int` 라 비숫자 cameraId 를 아예 못 받았으며, (c) 중복 검사와 카메라 대조가 서로
    다른 기준(원문 vs 정규화)을 써서 **대소문자만 다른 id 가 중복 검사를 통과한 뒤 같은
    카메라에 둘 다 붙는** 구멍이 있었다.
  - **순번 자동 배정**(`assign_missing_sensor_ids`): id 를 생략하면 그 카메라에서 아직 안
    쓰인 **최소 순번**을 받는다. 정렬 기준은 기기에 붙어 있는 값(`spec_key` — node_name >
    host)이라 **배열 순서·호출 순서와 무관하게 결정적**이다(전역 카운터 없음).
    **명시된 id 는 예약만 되고 절대 재번호화되지 않는다.** `spec_key` 가 비었거나 서로
    겹치면 조용히 순서로 떨어지지 않고 ValueError — 순서로 배정하면 같은 기기가 재등록
    때마다 다른 순번을 받아 융합 `sid` 와 기록 JSONL 식별자가 말없이 바뀐다.
  - **`normalize_sensor` 가 더 이상 id 를 만들지 않는다**(없으면 `""`). 순번은 목록 전체를
    봐야 정해지는데 이 함수는 한 항목만 보기 때문이다. 카메라 문맥이 없는 도구 경로
    (`--host` 즉석 지정 / mDNS 탐색)만 호출측이 `short_id` 로 로컬 id 를 채운다.
  - **쓰기 경로(이 레포 전용 — 제품은 설정을 읽기만 한다)**: `assign_camera_sensor_ids` 를
    새로 뒀다. 프로비저닝은 `upsert_sensor` → **순번 확정** → `set_sensor_camera` 순서로
    돌고, 확정된 id 를 그대로 JSON 에 **영속화**한다. 이 단계가 없으면 `id: ""` 가 저장되고
    다음 실행의 `get_sensors()` 가 ValueError 로 죽어 **도구 전체가 멈춘다**.
    · 순번 예약을 **카메라 단위로 좁힌다** — 파일에는 여러 카메라가 섞여 있어서 전체로
      예약하면 카메라 2 의 첫 센서가 `pia-2-3` 처럼 이유 없이 밀린다. 순번은 카메라마다 1부터.
    · `upsert_sensor` 가 기존 id 를 빈 값으로 덮어쓰지 않게 막았다(재프로비저닝 시 확정된
      규격 id 가 지워져 그 센서가 카메라에서 떨어져 나가던 경로).
  - **`set_sensor_camera`**: 규격 id 의 sensorId 파트는 계속 보존하되, **비규격 옛 id 만**
    그 카메라의 미사용 최소 순번으로 옮긴다(`98bd80` → `pia-1-3`). 옛 동작(`pia-1-98bd80`)은
    형식만 맞고 순번 체계 밖이라 폐기했다. id 를 생략한 항목 역조회는 `spec_key` 대조로
    바꿨다(`normalize_sensor` 가 자동 id 를 만들어주던 옛 폴백이 죽었으므로).
  - **`cameraId` 는 문자열**(제품 `DTO/stream_params.py`): 세 모델(`AddStreamModel` /
    `DeleteStreamModel` / `RTSPErrorModel`)의 선언을 `str` 로 바꾸고, 백엔드가 아직 JSON
    숫자로 보내는 값은 **DTO 경계 한 곳**(`coerce_camera_id`)에서만 승격한다(소비 모듈에서
    `str()` 로 봉합하지 않는다). `strip` 만 하고 `"06"→6` 같은 숫자 해석은 하지 않으며
    `bool` 은 거절한다. MQ 봉투의 `cameraId` 도 **JSON 문자열**로 나간다.
    · **새 제약: `cameraId` 에 `_` 금지** — `stream_id` 가 `{cameraId}_{organization}` 이고
      폴백이 첫 `_` 1회 분해라 `ward_a`+`pia` 가 `ward`+`a_pia` 로 갈린다(카메라·테넌트가
      동시에 틀리며 예외도 로그도 없다). `organization` 쪽 `_` 는 계속 허용.
    · 이 레포 기본값 `DEFAULT_CAMERA_ID` 를 `"camera1"` → **`"1"`** 로 바꿨고, 문서·CLI
      도움말의 "제품 cameraId 는 int 라 비숫자는 매칭 안 됨" 문구를 전부 걷어냈다(거짓이 됨).
  - **정규화 단일 기준**(`normalize_id_value` 공개, `_norm` 완전 제거 — 별칭 없음):
    카메라 대조와 중복 판정이 같은 기준(`strip().lower()`)을 쓴다. 중복 오류 메시지는
    정규화 키에 접힌 **원문 스펠링을 전부** 보여준다(`'PIA-1-A'/'pia-1-a' -> hosts=[…]`).
    숫자 정규화는 하지 않으므로 `"01"` ≠ `"1"` 이다.
  - **⚠ 옛 id 범위가 넓어졌다**: **2026-07-31 전환분으로 생성된 host 유래 자동 id
    (`98bd80` / `10-201-31-120`)도 이제 "옛 id" 다.** 그때 녹화한 JSONL 의 `sid` 는 그 값으로
    남아 새 설정과 센서 대조가 안 된다(재생 자체는 된다 — `replay_frames` 는 `sid` 를
    불투명 키로 취급). `--camera-id` 로 다시 프로비저닝하면 순번 id 로 옮겨진다.
  - **마이그레이션**: `pia-camera1-*` 로 프로비저닝된 설정이 있으면 `--camera-id 1` 로 다시
    실행해 옮긴다. 이 레포 작업트리의 `epl_config.json` 은 배포 기준 그대로다(`pia-1-1/2/3`,
    3대) — 로컬 재프로비저닝본은 `epl_config.backup.json`(untracked)에만 있고 커밋 대상이 아니다.
  - **검증**: 제품 `test_vanguard_mmwave.py` **81 passed / 0 skipped**(실자산 리플레이 포함),
    black·flake8 clean. 리플레이 회귀 **3/3 발화 유지**(peak 116.7 / 117.0 / 94.6s).
    이 레포는 프로비저닝 왕복 스모크(`upsert` → 순번 배정 → `save` → `load`/`get_sensors()`)로
    A-2 회귀를 막았다.
  - **✅ HF 자산 교체 완료**: `PIA-SPACE-LAB/SensorData` 의 `epl_config.json` 을 새 id 규격
    (`pia-1-1/2/3`)으로 올렸다 — 2026-07-31 부터 열려 있던 배포 게이트가 닫혔다(§10).
    이제 다른 머신·CI 도 새 스키마를 받으므로 실자산 테스트가 skip 없이 돈다.
    ※ skip 분기 자체는 남겨 둔다(옛 리비전을 가리키거나 다음 스키마 변경 때 다시 필요하다).
  - **남은 게이트**: 현장 cameraId 로 교체 / 백엔드가 MQ `cameraId` 를 **문자열**로 받는지
    확인(이번에 숫자 → 문자열로 바뀐 외부 계약이다) (§10).

- **2026-07-31 (`rooms` 폐기 → 센서 id 규격화 `{organization}-{cameraId}-{sensorId}`)**
  - **왜**: 알람은 기존 레포 컨벤션대로 **카메라 단위**로 발화하고, MQ 봉투의 `cameraId` 는
    `DTO/output_handler.make_alarm_message` 가 `user_param["user_param"]["cameraId"]` 에서
    **그대로** 읽는다. 즉 장기체류를 측정하는 공간을 비추는 카메라의 id 로 스트림이
    등록되면 MQ 는 이미 맞다. 그러면 모듈이 답할 것은 하나뿐이다 — *그 카메라에 묶인
    센서가 누구인가*. 그 답을 별도 매핑 테이블(`rooms`)이 아니라 **센서 id 자체**에 담아
    "방 이름을 추측하는 계층"을 통째로 없앴다.
  - **없어진 것**: `rooms` 필드 · 센서 `room` 태그 · `mmwave://` 스킴(`SENSOR_URL_SCHEME`) ·
    제품 `resolve_room_id`/`is_room_explicit`/`room_candidates`/`resolve_effective_room` ·
    양쪽 `_sensor_keys`/`_rooms_of`/`get_room_ids`/`get_sensors_for_room`/
    `assert_one_room_per_sensor` · `add_stream` 의 '한 방을 두 스트림이 claim' 검사
    (stream_id 가 `{cameraId}_{organization}` 이라 **구조적으로 불가능**해졌다).
  - **생긴 것**(양쪽 동일): `parse_sensor_id` / `make_sensor_id` / `assert_sensor_id_format` /
    `assert_no_legacy_schema` / `assert_unique_sensor_hosts` / `get_camera_ids` /
    `get_sensors_for_camera`, 제품 `resolve_camera_id`(카메라 해석 단일 경로),
    PoC `set_sensor_camera`(**센서 id 재작성**) / `nonconforming_sensor_ids` /
    `DEFAULT_CAMERA_ID`("camera1" — **2026-08-02 에 "1" 로 바뀜**) / `DEFAULT_ORGANIZATION`("pia").
    클래스·필드도 개명: `MmwaveRoomSource`→`MmwaveCameraSource`,
    `MmwaveSourceManager.rooms`→`.streams`, 도구 `--room`→`--camera-id`, `ROOM=`→`CAMERA_ID=`.
  - **용어 규칙(중요)**: `room` 은 **좌표계 전용**으로 남겼다 — `room_transform`/`sensor_local`/
    "방 좌표계 mm"/`rx`,`ry` 는 무변경이다. 그룹핑 문맥의 "방"만 "카메라"로 바꿨다.
    · '카메라 좌표계' 라는 개념은 만들지 않았다(카메라 광학 좌표계와 혼동) —
      "카메라마다 별도의 방 좌표계" 로 표기한다.
    · `mmwave_reader.py` / `fusion.py` / `gui_qt.py` / `replay_video.py` / `index.html` 은
      grouping 히트가 0이라 **무변경**이 정답이었다.
    · `auto_positioning{,_multi}.py` 의 지역변수 `room` 은 **방 좌표 튜플(mm)** 이다
      (`room[0]` 을 X 로 인덱싱) → 일괄 치환하면 셀프테스트 합성기가 조용히 깨진다.
      한글 오탐(방화벽/전방/방침/방지/방어/방향)도 많아 **행 단위 수동 편집**만 했다.
  - **실패 방침**(전부 ValueError = 스트림 등록 거절): id 형식 위반 / `id` 누락 / id 중복 /
    **같은 host 를 다른 id 로 두 번** / 옛 `rooms`·`room` 잔재 / `organization`·`cameraId` 에
    `-`. 매칭 센서 0개만 예외로 error 로그 + 데이터 없음(안전한 축소).
    · `id` 누락이 오류인 이유: 설정 파일에는 org·cameraId 문맥이 없어 자동 생성 id 가 귀속을
      담지 못하는데, host 유래 id(`10.0.0.13`→`10-0-0-13`)는 **3파트 형식만은 통과**해
      조용히 아무 카메라에도 안 붙는다.
    · host 중복 검사는 삭제한 `assert_one_room_per_sensor` 의 성질을 이어받은 것이다.
    · `__` 금지: 알람 키가 `{stream_id}__{category}` 라 끼면 카테고리 추출이 깨지고
      `bases/service_base.py` 의 batch 조회가 IndexError → **그 스트림 알람이 통째로 소실**.
  - **M1 회귀 방지 재확인**: 설정 검증을 `MmwaveCameraSource.__init__` 으로 되돌렸다.
    한 번 `start()` 로 밀렸더니 재등록 실패 시 **잘 돌던 스트림이 사라졌다**(테스트가 잡음).
  - **마이그레이션**: 자동 변환 없음(코드가 센서 id 를 바꾸면 관제 화면·기록과 어긋난다).
    `epl_config.json` 3대를 `98bd80/bc0e00/b9a07c` → `pia-1-1/2/3` 로 수동 전환했고
    `rooms` 키를 삭제했다. **옛 녹화 JSONL 의 `sid` 는 옛 id 로 남는다** — 재생은 되지만
    (`replay_frames` 는 sid 를 불투명 키로 취급) 새 설정과 센서 대조는 안 된다.
  - **검증**: 제품 **71 passed**(0 failed / 0 skipped, 실자산 포함), black·flake8 clean.
    현장 로그 3개 리플레이 회귀 **3/3 발화 유지**(peak 116.7 / 117.0 / 94.6s) — 이번 변경은
    센서 그룹핑만 건드리므로 융합 결과가 바뀌면 안 되는데 실제로 안 바뀌었다.
    신규 e2e 5종으로 **설정 → 귀속 → 허브 → 융합 → 이벤트 → 알람**을 한 줄로 꿰었고,
    레포 최초로 `make_alarm_message` MQ 봉투 계약을 테스트로 못박았다
    (센서 id 의 cameraId 파트 == 스트림 cameraId == MQ `cameraId`).
  - **남은 게이트**: HF 자산 교체 / 현장 cameraId 로 교체 / 백엔드가 cameraId·
    organization 을 내려주는지 확인 (§10).
    (당시엔 "**숫자** cameraId 로 교체" 였다 — 2026-08-02 에 `cameraId` 가 문자열이 되어
     숫자 제약이 사라졌다. 현장 값으로 바꿔야 한다는 것 자체는 그대로다.
     **HF 자산 교체도 2026-08-02 에 완료**됐다 — §10 참조.)
- **2026-07-30 (방(room)↔센서 매핑 도입 — 설치 도구 3종 + 설정 파일)**
  - 왜: 제품은 **방 단위(stream)로 재실/체류를 판정**하는데, `epl_config.json` 에 방 정보가
    없어 `get_sensors_for_room` 의 3순위 폴백(전 센서)이 걸리고 있었다. 방이 1곳인 지금은
    무해하지만, 방을 2곳으로 늘리면 **두 방이 같은 센서 3대를 공유해 같은 사람을 각각
    재실로 세고**, 한 센서에 방 수만큼 TCP 6053 연결이 생긴다. 최상위 `rooms` 매핑
    (§5.1)을 도입해 막았다.
  - `epl_config.json`(이 레포 + 제품 `assets/config/`) 에 `"rooms": {"room_1": [98bd80,
    bc0e00, b9a07c]}` 추가. **현재 배치 그대로 한 방**이라 동작 변화 없음(실측: room_1
    필터 결과 == 방 구분 없이 돌린 결과, 자동 포지셔닝 출력 diff 0).
  - `epl_config.py`: `DEFAULT_ROOM="room_1"`, `get_sensors_for_room()`(**제품과 동일 로직**),
    `set_sensor_room()`(등록/이동·멱등), `get_room_ids()`, `unmapped_sensor_ids()`,
    `_rooms_of()`(스키마 검증), `normalize_sensor` 에 `room` 필드(제품 파리티).
  - 도구 3종에 `--room` + 스크립트 `ROOM=room_1`(§5.1 표). `run_provision.sh` 는 센서를
    그 방에 등록하고, 두 auto_positioning 은 **그 방 센서만** 접속/풀링한다.
  - `mmwave_wifi_reader.build_sources` 에 `specs=` 추가(제품과 같은 `None`≠`[]` 계약).
    `specs=[]` 는 **데모 폴백 금지** — 합성 인원으로 캘리브레이션한 좌표가 저장되면 안 된다.
  - 적대적 리뷰에서 잡아 고친 것 3건:
    · `set_sensor_room` 이 remove-then-append 라 **재프로비저닝마다 목록 순서가 바뀌던** 문제
      → 이미 그 방 소속이면 목록을 건드리지 않음(멱등·표기 보존).
    · 깨진 `rooms`(dict 아님 / `"room_1": "98bd80"` 처럼 대괄호 누락)가 `AttributeError`
      또는 **조용한 0개**가 되던 문제 → `_rooms_of()` 가 설정 오류로 실패(`assert_unique_
      sensor_ids` 와 같은 방침). 도구는 `❌ 센서 설정 오류: …` + exit 2.
    · 방 매핑이 실패하면 **성공한 Wi-Fi 프로비저닝 기록까지 저장되지 않던** 문제
      → 센서는 먼저 저장하고 매핑 실패만 경고(USB 재작업 불필요).
  - 검증: 방 로직 100개 체크 PASS(제품 함수와 40케이스 출력 대조 포함),
    v1/v2 `--selftest` PASS, 실제 로그 3개 `--room room_1 --dry-run` 회귀 0, 제품 52 passed.
  - ⚠ **배포 게이트는 §10 TODO 참조** — 제품 `assets/` 는 **git 무시 대상**이라 HF 업로드가
    유일한 배포 경로다.
- **2026-07-30 (알람 카테고리명 변경: 재실체류 → 장기체류)**
  - 제품 카테고리를 `재실체류_cv`/`presence_dwell_cv` → **`장기체류_cv`/`long_dwell_cv`** 로 변경.
  - 레포 규약 확인 후 적용: cvEvent 는 **한글·영문 양쪽에 `_cv` 접미사**(24개 중 21개가 그 형태
    — `["배회_cv","loitering_cv"]` 식). 그래서 요청받은 `장기체류`/`long_dwell` 에 `_cv` 를 붙였다.
  - ★ **백엔드와 동시 반영 필요(배포 게이트)**: 이 목록에 없는 name 은 `AddStreamModel` 이
    `Unknown category name` 으로 **스트림 등록 자체를 거절**한다. 실측 확인:
    `장기체류_cv`·`long_dwell_cv` 통과 / `재실체류_cv`·`presence_dwell_cv`·`long_dwell`(접미사 없음)
    ·`장기체류`(접미사 없음) 전부 거절. 구 명칭으로 등록된 스트림이 있으면 재등록해야 한다.
  - 이 값이 알람 MQ 봉투의 `name` 으로 그대로 나간다(실측: START/END 둘 다 `"장기체류_cv"`,
    uuid 짝 일치). 한글로 등록하면 한글이, 영문으로 등록하면 영문이 나간다.
  - 곁들여 `category_name` 을 피어 규약대로 정렬: event=`long_dwell_cv`(등록 카테고리명),
    service=`long_dwell`(단축형). 예전 값 `vanguard_mmwave`(모듈명)는 규약 이탈이었다.
    기능 영향 없음 — 알람 키는 `_resolve` 가 user_param 에서 찾은 실제 카테고리명으로 만든다.
  - 모듈 디렉터리/서비스 클래스(`vanguard_mmwave`/`VanguardMmwaveService`)와 상수명
    `VANGUARD_MMWAVE_CV_CATEGORY` 는 그대로 뒀다(고객사 접두사 형태는
    `KUMHO_PROXIMITY_CV_CATEGORY` 선례가 있고, 바꾸면 stream_params.py 까지 번진다).
- **2026-07-30 (제품 2차 리뷰 — mm ROI 오형 결함 + 벤더링 사문 정리, 실기동 알람 검증)**
  - ★★ **최대 결함: 방 좌표 mm ROI 좌표가 홀수 개면 전체 알람이 조용히 멈춘다 (수정)**
    · `polygonCoordinates: List[int]` 라 `AddStreamModel` 검증이 **개수를 막지 못한다**(실측:
      홀수 7개가 그대로 통과). 런타임 `get_pair_list()` 가 `ValueError: Input list length must
      be even.` 을 던지고, 이 호출은 `_detect()` 맨 앞이라 `try_except_only_in_prod_mode` 가
      삼켜 **그 스냅샷 전체가 폐기**된다. ROI 는 매 스냅샷 재등록되므로 매번 같은 곳에서 터진다.
    · **실측 파급**: 방 2개(room_1 정상 / room_2 홀수)를 같은 페이로드로 돌리면 **정상인
      room_1 까지 알람 0건**, `_detect` 15프레임에서 멈춤. 흔적은 태그도 stream_id 도 없는
      bare `print('Input list length must be even.')` 14줄뿐 → 로그로 원인 추적 불가.
    · ★ **이 레포와 직결되는 이유**: 다른 모듈의 ROI 는 화면 작도 도구가 (x,y) 쌍을
      보장하지만, 이 모듈의 ROI 는 **사람이 직접 적는 방 좌표 mm** 다(README §4 예시).
      즉 PoC 에서 캘리브레이션한 mm 좌표를 손으로 옮겨 적는 우리 절차가 곧 이 결함의
      트리거다. ROI 를 쓰기 시작하면 **좌표 개수를 반드시 짝수로 확인할 것.**
    · ★★ **단, ROI 는 현 PoC 범위에서 쓰지 않는다(확장성 대비 기능).** 화장실은 "방 하나 =
      감지 구역 하나" 라 `polygonCoordinates` 를 비운 채 운영하며, 비우면 `polygon=None` 로
      전 트랙을 통과시켜 이 검증 경로를 한 번도 타지 않는다(실측: `roi` 키 없음/`{}`/`[]`/
      `None` 네 형태 모두 등록 통과·방 전체 통과·`invalid=False` → **수정 전과 동작 동일**).
      그래서 이번 수정은 PoC 운영에 영향이 없고, 지금 단계의 튜닝·검토 대상도 아니다.
      이 사실을 `roi_manager.py` 상단 / `param.py` / README §4 세 곳에 명시해 뒀다.
    · 수정 2겹: ① `param.py` 에 `validate_metric_polygon` — 짝수·3점 이상이 아니면
      **등록 거절**(rooms 스키마·센서 id 중복과 같은 방침). ② `roi_manager.add_roi` 안전망 —
      API 밖 경로의 오형은 `invalid` 로 표시하고 `is_inside`→False 로 **그 방만 격리**한다.
      '방 전체' 폴백은 하지 않는다(지정하려던 구역 밖 사람까지 세면 엉뚱한 알람).
      실측 후: 등록 거절 동작 + 오형 방만 억제되고 정상 방 알람 발화, stdout 누출 0줄.
  - **벤더링 사문·거짓 주석 정리 (수정)**: AST 대조(주석·포맷 제거)로 검사했더니
    · `mmwave_core/fusion.py` 가 PoC 의 CLI 헬퍼 `resolve_fusion_opts(cfg, **overrides)` 를
      남겨 뒀는데, 제품에는 **같은 이름의 다른 함수**(`fusion_params.resolve_fusion_opts`)가
      있었다. 원본 쪽은 `cfg["fusion"]` 만 보고 코드 기본값·`best_params.yaml`·env 를 전혀
      반영하지 않아, 잘못 임포트하면 융합값이 조용히 전부 기본값으로 돌아가 60초 알람이
      발화하지 않는다 → 이름 충돌 자체를 제거(헤더의 "CLI 헬퍼 제거" 방침과 일치).
    · `FUSION_KWARGS` 는 **제품에만 추가된 사문**이고 `config.py` 는 "FUSION_KWARGS 로 검증"
      이라 적어 뒀지만 실제 검증은 `fusion_params._KINDS` 가 한다 → 심볼 제거 + 주석 정정.
    · 결과: 벤더링 드리프트에 **제품 전용 추가가 0개**가 되어 남은 차이는 전부 문서화된
      제거·변경뿐이다. `mmwave_parser.py` 는 정의 100% 동일, `epl_config.py` 의 차이는
      `[vanguard_mmwave]` 로그 접두사와 `port` 필드뿐(문서와 일치).
  - ★ **PoC 도 같이 볼 것**: 위 `resolve_fusion_opts` 는 PoC `fusion.py` 에 그대로 있고
    PoC 의 GUI/CLI 가 실제로 쓴다. PoC 에서 `best_params.yaml` 을 적용하려면 이 함수만으로는
    부족하다(파일을 읽지 않는다) — `run_gui.sh` 계열이 파일을 어떻게 먹이는지 확인 필요.
  - **실기동 알람 검증(전용 venv 구축)**: `pytest` **67 passed**(기존 64 + 신규 3).
    나아가 실제 소비 스레드·`send_alarm`·MQ 봉투까지 관측:
    · 방 2개 동시 → 방마다 START/END 각 1건, START↔END **uuid 짝 일치**, `thumbnail` 전부 None
    · ROI 밖 8초 체류 → 0건 / ROI 안 8초 체류 → START 1건
    · 합성 센서 3대 실제 e2e → 센서 3대 connected, 융합 트랙 2명, 체류 5.3초, 재실 2명, START 1건
    · 실행 레시피: `PYTHONPATH=packages TEAM=ai` + `PYTHON_RABBITMQ_MAX_RECONNECT_ATTEMPTS=1`
      `PYTHON_RABBITMQ_SOCKET_TIMEOUT=1` `PYTHON_RABBITMQ_RECONNECT_DELAY=0`
      (브로커가 없으면 `utils/init.py` 의 RabbitMQ 접속 재시도에서 import 가 멈춘다 —
       이 3개를 안 주면 테스트가 아니라 **임포트 단계에서 행**이 걸린다)
    · 필요 패키지: torch·**torchvision**(`pia.vision.preprocessing.resize` 가 요구)·redis·pika·
      boto3·concurrent-log-handler. 모듈 `requirements.txt` 는 "베이스 이미지의 torch 만 쓴다"
      고 적고 있어 맞지만, torchvision 도 베이스 전제임을 알아둘 것.
  - lint: black·flake8(7.1.1 로컬 포함) **모두 clean** — f-string 안 dict 컴프리헨션을 지역
    변수로 빼 구버전 pycodestyle E201/E202 오탐도 제거.
- **2026-07-30 (제품 컨벤션 대조 리뷰 — 고유 결함 수정)**
  - 왜: 제품 `vanguard_mmwave` 를 피어 23개 모듈과 대조해, 발견한 결함 중 **레포 공통(컨벤션이
    같은 것)** 과 **이 모듈 고유** 를 갈랐다. 고유 결함만 고쳤다.
  - **레포 공통(모듈 결함 아님 — 플랫폼 과제로 분리)**: 스트림 해제 시 ① 열린 알람이 END 되지
    않고 ② per-stream 상태가 영구 누적. 근거: `DeleteStreamModel`(`/api/v1/stream/remove` DTO)이
    **레포 전체에서 참조 0건**, per-stream 정리 메서드를 가진 event.py 는 `vanguard_mmwave`
    하나뿐(`drop_stream`, 다만 호출자 없음), 피어 4개 모두 `defaultdict` 를 정리 없이 누적.
    → 이 모듈은 컨벤션을 어긴 게 아니라 오히려 유일하게 해제 API 를 갖고 있다.
    · 마찬가지로 "기동 시 파라미터 조합 경고가 배포 기본값에서 안 울리는 것" 도 선례가 있다
      (`ftpe_int8_2stage/service.py`: 게이트가 env 기본 False + 조건도 이미 만족).
  - **고유 결함 ① 재등록 실패 시 멀쩡한 방이 사라짐 (수정)**: `add_stream` 이 기존 방을 먼저
    `stop`+`pop` 하고 나서 새 방을 만들었다 → 설정 파일 오타 후 같은 `stream_id` 재등록이라는
    흔한 절차에서 **잘 돌던 방이 등록에서 사라지고**(`rooms={}` → 생산 페이로드 `None`) 그 방의
    열린 체류 알람도 END 를 못 냈다. **검증(새 방 생성)을 `self.rooms` 손대기 전으로 이동**,
    충돌 검사에 `sid != stream_id` 추가(자기 자신은 교체 대상), 교체는 예약 후 `existing.stop()`
    (순서를 뒤집으면 같은 센서에 리더가 겹쳐 붙어 한 사람이 두 번 세어진다).
    `AddStreamModel.sensors` 의 id 중복도 `start()` 가 아니라 **생성자**에서 걸러 같은 창을 닫음.
    → 재현·회귀 17항목 자체 검증 PASS + pytest `test_failed_reregistration_keeps_the_working_room`.
    ★ 이 함정은 **이 레포에 대응물이 없다**(PoC 는 스트림 등록 개념이 없음) — 참고만.
  - **고유 결함 ② 문서 숫자 드리프트 (수정)**: 주석이 적은 융합값이 배포 튜닝 파일에 덮여
    실제와 달랐다. `queue_k/size` **5-of-10 → 코드 기본 3-of-5 / 튜닝 파일 7-of-7**(3곳),
    `WINDOW` 20 → 15, `END 지연 3.0+5.0` 은 **측정 당시 max_miss=3.0** 이었음을 명기(현재 5.0),
    grace 권장 하한 예시 `4.0-3.0=1.0` → 현재 `max(0, 2.5-5.0)=0` **⇒ 그 기동 경고는 지금
    구조적으로 발화하지 않음**(coast 가 ReID 창보다 길어 하한이 실제로 없음 = 위험 없음).
    ★ **이 레포에도 같은 함정이 있다**: `debug_logs/best_params.yaml` 을 갈면 문서의 숫자가
    조용히 낡는다. 이제 제품 쪽은 "유효값은 기동 시 `resolve_fusion_opts` info 로그로 확인" 을
    단일 지침으로 적어 뒀다 — PoC 문서도 값을 되풀어 적기보다 그렇게 가리키는 편이 낫다.
  - 남긴 것(Low, 미수정): 센서 0개 방의 stale 경고가 원인을 네트워크로 오지목 /
    `LIMITED_NUM_OF_CAMERA` 가 주석의 상한을 실제로 걸지 않음 / README §4·§5.1 이
    `room_candidates` 도입 전 서술 / f-string 필수 공백이 구버전 pycodestyle 에서 E201·E202.
- **2026-07-30 (제품 vanguard_mmwave 를 방 단위로 정합 — 후속 반영)**
  - 왜: `rooms` 가 생겼는데 제품은 방 이름을 백엔드 값(`room`/`roomId`/`cameraUrl`/
    `stream_id`)에서만 뽑고 있었다. 플랫폼이 방을 안 내려주면 `stream_id`(`1_pia`) 같은
    **임의 값**이 방 이름이 되어 `rooms` 에 없으니 **센서 0개 = 재실·체류 알람이 아예 안
    나가는** 상태가 된다. 설정 파일이 방 이름의 단일 출처가 되도록 고쳤다(§5.2).
  - `source.py`: `resolve_effective_room()`·`is_room_explicit()`·`room_candidates()` 신설.
    명시된 방은 절대 바꾸지 않고, 명시가 없을 때만 설정의 방과 대조한다.
    `add_stream` 은 **한 방을 두 스트림이 잡는 것을 거절**(검사·등록을 같은 락 구간에서 —
    나눠 하면 동시 등록이 둘 다 통과한다). 0센서 error 로그에 **설정된 방 목록**을 함께 남긴다.
  - `mmwave_core/epl_config.py` 재벤더링(`[VENDORED]` 변경 6): `_sensor_keys`·`_rooms_of`·
    `get_room_ids`·`assert_one_room_per_sensor`. 쓰기 함수(`set_sensor_room` 등)는 제품이
    설정을 읽기만 하므로 의도적으로 미벤더링.
  - 트래킹은 원래부터 방 단위였다(방마다 SensorHub 하나 + 그 방 센서만) — 구조적으로
    섞일 경로가 없음을 테스트로 고정했다. 알람 키는 레포 표준 `{stream_id}__{category}` 유지.
  - 적대적 리뷰(5관점 병렬 + 반박 검증, 38건 중 6건 반박)에서 잡아 함께 고친 것:
    · 한 센서를 두 방에 넣은 설정 → `assert_one_room_per_sensor` 로 거절(양쪽 레포)
    · 멤버가 빈 방(`"room_2": []`)이 방 개수에 세어져 **단일 방 폴백이 조용히 꺼지던** 문제
    · `get_sensors_for_room(None, ...)` 이 센서만 파일에서 읽고 `rooms` 는 무시하던 문제
    · 문법 깨진 `epl_config.json` 위에 provision → 기존 센서·캘리브레이션·rooms **전부 소실**
    · 센서별 `room` 태그만 쓰던 파일에 `rooms` 가 처음 생기면 태그 배정이 전부 죽던 문제
      (`set_sensor_room` 이 태그를 먼저 `rooms` 로 마이그레이션)
    · `--room ''` 로 방 2곳 이상을 한 좌표계에 맞춰 저장하던 문제 → 거절
    · 방 센서 일부만 로그에 있으면 남은 센서가 옛 좌표계로 남던 것 → 명시 경고
    · provision 의 '이 방 센서 N개'가 실제 매칭 수와 달랐던 것, 1대일 때 반드시 실패하는
      캘리브레이션 안내, 쉘 `ROOM=` 인용 누락, 제품 0센서 오류 문구 오진
  - 검증: 제품 **64 passed**(52 → +12), black 25.1.0·flake8 7.2.0 clean, PoC 방 로직 100체크
    PASS(제품 헬퍼와 출력 대조 포함), v1/v2 selftest PASS,
    실제 로그 `--room room_1 --dry-run` 회귀 0.
  - 남은 알려진 한계(문서화만): 유니코드 정규화 다른 한글 방 이름은 별개 방으로 취급된다
    (ASCII 권장). `rooms` 한 항목이 두 센서에 매칭될 수 있다 — A 센서의 `node_name` 이
    B 센서의 `id` 와 같은 경우(id/node_name/host 교차 매칭 때문). 둘 다 실현 조건이 좁고
    양쪽 레포가 동일하게 동작한다.
- **2026-07-30 (센서 id 충돌 결함 수정 — 이 레포 + 제품 동시)**
  - `epl_config.short_id()` 가 `host.split(".")[0]` 로 시작해 **IP 만 적힌 센서의 id 가 첫
    옥텟('10')으로 전부 같아지던 결함**을 고쳤다. IP 는 점을 하이픈으로 바꿔 전체를 쓴다
    (`10.201.31.120` → `10-201-31-120`). `.local` 등 DNS 접미사만 잘라내는
    `strip_dns_suffix()` 신설. **node_name 경로(→`98bd80`)와 현재 `epl_config.json` 의
    id 는 그대로** — 회귀 없음(실측 확인).
  - 같은 이유로 `mmwave_wifi_reader._spec_key()` 의 첫 점 절단도 제거했다. 이걸 안 고치면
    설정∪탐색 병합에서 **같은 대역 센서 3대가 한 키로 겹쳐 1대만 남는다**.
  - `assert_unique_sensor_ids()` 신설 — 한 id 가 서로 다른 host 를 가리키면 **ValueError 로
    실패**시킨다(`get_sensors()` + `build_sources()` 의 `hub.add_sensor` 직전 2곳).
    · 자동 개명(`a`→`a-1`)을 하지 않는 이유: 명시된 `a-1` 과 다시 충돌하고, 사용자가 적은
      id 를 코드가 바꾸면 화면·로그의 센서 식별자가 실동작과 달라진다.
    · 같은 id + **같은 host** 중복 기입은 모호함이 없어 허용(중복 제거는 `_spec_key` 담당).
    · mDNS 탐색이 설정에 이미 있는 기기를 다시 얹지 않도록 id 기준 스킵도 추가.
  - **왜 실패시키는가**: 센서 id 는 융합 입력의 `sid` 다. 겹치면 `(sid, tid)` 가 충돌해
    **같은 tid → 3m 떨어진 두 사람이 중간 지점 한 명으로**(median 병합), **다른 tid →
    한 사람이 두 명으로**(센서 겹침 판정에 병합 차단) 세어진다. 둘 다 재현으로 확증했다.
  - 제품(Product-AI-mono) 쪽은 위 수정을 재벤더링하고 `[VENDORED]` 헤더에 변경 3~5로 기록.
    추가로 제품 전용 결함 **H2** 를 고쳤다: `MmwaveRoomSource.start()` 가 방↔센서 매핑
    결과 `[]` 를 `specs or None` 으로 뭉개 **매핑에 없는 방이 파일의 전 센서에 붙던** 문제
    (`build_sources` 의 `specs` 계약을 `None`=미지정 / `[]`=0개로 분리). 테스트 7개 추가 →
    50 passed, black/flake8 0건.
- **2026-07-29 (확장 후보로 전체 탐색 재실행 + 제품 반영 + 제품 모듈 적대적 리뷰)**
  - `run_optimization.sh`(ITERS=3000, INTERVAL_SECOND=0.1) 재실행 → **score 19816.7 → 16188.2**
    (sw_in 27→**2**, 단 교차병합 ep 2→**4** 로 악화). 산출물을 PoC `debug_logs/best_params.yaml` +
    제품 `Product-AI-mono/assets/config/best_params.yaml` **양쪽 갱신**(§10 TODO 해소).
    파일에 `_interval_second: 0.1` 동봉됨. 제품 테스트 43개 전부 통과(반영 전/후 동일).
  - **경계 재대조(이전 항목의 "다음" 과제)**: 탐색 22개 중 **12개가 여전히 경계값** —
    ↑ NOISE_RADIUS(3000) · REID_DIST(2500) · DIR_PEN(600) · FUSE_MIN_FRAMES(8) · MAX_MISS_TENT(1.2)
    / ↓ MOVE_MIN(50) · ANG_GATE(20) · ALPHA(0.3) · BETA(0.1) · COAST_DECAY(0) · PRED_DT_CAP(0.1)
    · RECENT_FRAMES(1). 이 중 COAST_DECAY·PRED_DT_CAP·RECENT_FRAMES 는 물리/논리 하한,
    NOISE_RADIUS·REID_DIST 는 확장 금지(위 항목 경고) → **추가 확장 여지는 FUSE_MIN_FRAMES↑
    DIR_PEN↑ MAX_MISS_TENT↑ MOVE_MIN↓ ANG_GATE↓ ALPHA↓ BETA↓ 뿐**.
  - ⚠ 이번 최적값은 "ID 를 안 갈리게" 쪽으로 더 갔다(점수 유리). 점수가 못 보는 부작용:
    `MAX_MISS` 3.0→5.0 = 사람 나간 뒤 체류시간·END 지연이 각각 +2초(END ≈ 10.2초),
    `NOISE_RADIUS` 3000 = 2인 흡수 위험, `ALPHA/BETA` 하한 = 위치 지연↑ → `compare.mp4` 육안 확인 필수.
  - ★ **`short_id()` / `_spec_key()` 결함(이 레포 원본 + 제품 벤더링 공통)**: 둘 다
    `host.split(".")[0]` 로 시작해서 **node_name 없이 host 에 IP 만 적으면** 센서 id 가 첫 옥텟
    (`10.201.31.120/.153/.76` → 전부 `"10"`)으로 같아진다. 융합 입력 키가 `(sid, tid)` 라
    서로 다른 센서의 target 1 이 median 으로 합쳐진다 → **3m 떨어진 두 사람이 중간 지점 한 명으로**
    (재현 확인). R2(고정 IP) 절차상 IP 만 적기 쉬운 자리라 위험하다 →
    **config 에는 항상 `id` 와 `node_name` 을 함께 적을 것**(현재 `epl_config.json` 은 충족).
    근본 수정은 IP 형태 감지 후 인덱스 폴백.
  - 제품 모듈 리뷰 결과 상세: `~/Downloads/vanguard_mmwave_코드리뷰_20260729.md`
    (멀티룸 `rooms` 미매핑 시 전 센서 폴백 / 튜닝 파일 부재 시 무음 폴백 등)
- **2026-07-29 (탐색 후보 범위 확장 — 경계에 붙은 파라미터 해소)**
  - 0.1초 재샘플 튜닝 결과(score 19816.7)를 후보 목록과 대조하니 **탐색 22개 중 10개가 정확히
    최댓값**, 4개가 최솟값에 붙어 있었다 = "더 가고 싶은데 목록이 끝난" 상태 → `optimize_fusion.py`
    의 `SPACE` 에서 **경계에 걸린 14개만** 그 방향으로 넓혔다(경계가 아닌 항목은 그대로 — 후보를
    늘리면 좌표하강 1라운드 비용이 후보 수에 비례해 늘어난다. 총 115 → 143개, +24%).
    · ↑ WINDOW(→30) · NOISE_RADIUS(→3000) · RECENT_FRAMES(→12) · JUMP_FACTOR(→5) · DIR_PEN(→600)
      · QUEUE_SIZE(→20) · QUEUE_K(→10, SIZE 확장에 맞춘 비율 유지용) · REID_DIST(→2500)
      · REID_MAX_GAP(→7.5) · MAX_MISS(→7.0) · ALPHA(→1.0)
    · ↓ MOVE_MIN(→50) · ANG_GATE(→20) · COAST_DECAY(→0.0) · PRED_DT_CAP(→0.1)
  - **넓히지 않은 상한은 물리/논리 제약**: `ALPHA ≤ 1.0`(측정 100% 추종) · `COAST_DECAY ≥ 0`
    · `PRED_DT_CAP ≥ 0.1`(= INTERVAL_SECOND. 더 작으면 매 스텝이 클램프) · `REID_MAX_GAP < 8.0`
    (run_optimization.sh 의 `DWELL_GAP` 미만이어야 함 — 더 늘리려면 DWELL_GAP 도 같이 올릴 것).
  - 검증: 이전 최적값 24개가 전부 새 후보에 남아 있고(회귀 없음) 옛 경계 14개가 모두 목록 내부로
    들어왔다. 새 후보값 143개 전수 `FusionTracker` 생성 OK, 극단 조합을 현장 로그로 replay 확인
    (최댓값 조합 → ID 2개로 과도하게 안정 / 최솟값 조합 → ID 12개로 스위칭 다발 = 방향 정상).
  - ⚠️ **점수가 못 보는 위험** — 채점은 스위칭/중복/교차병합/커버리지/체류결손만 본다. 아래는
    "점수는 좋아지는데 현장에서 나빠지는" 방향으로 끌리므로 최적 결과가 상단을 고르면 반드시
    `compare.mp4` 로 눈으로 확인할 것(`SPACE` 주석에도 같은 경고를 남겼다):
    · `WINDOW` — 지연이 채점에 없다(0.1초 기준 30 = **3.0초** 스무딩 창).
    · `NOISE_RADIUS` — 확정점 반경 내 '처음 보는' 점을 흡수 → 크면 **두 번째 사람**을 유령으로
      삼켜 재실 카운트가 누락된다(3000mm ≈ 방 전체). ★ **이 값은 더 늘리지 말 것.** REID_DIST 가
      크면 GT 3인은 ReID 로 ID 를 지켜 교차병합 벌점이 안 걸려 점수가 흡수를 벌하지 못한다.
    · `REID_DIST` — 센서 병합 한계(~0.7m) 초과분은 문 근처에서 남의 ID 를 가로챌 위험.
    · `MAX_MISS` — 제품에선 체류시간이 사람이 나간 뒤 이 값만큼 더 늘어난다(알람 임계에 직접 영향).
  - 다음: 전체 탐색(ITERS=3000) 재실행 후 **다시 경계 대조**. 짧은 150회 시험탐색에서는
    NOISE_RADIUS·QUEUE_SIZE 가 새 상한에도 붙었지만, 같은 탐색에서 RECENT_FRAMES 가 정반대
    끝(1)을 골라 신뢰할 수 없다 → 판단은 전체 탐색 결과로만 할 것.
- **2026-07-29 (샘플링 간격 INTERVAL_SECOND — 제품과 동일 조건 튜닝)**
  - **문제**: 튜닝은 기록 로그의 프레임 격자(현장 기록 ≈12.7Hz)로 replay 했는데, 제품
    (`vanguard_mmwave`)은 `OD_TIME_INTERVAL_SECOND`(기본 **0.1초**)마다 `hub.snapshot()` 을 떠
    융합 1스텝을 돈다. `WINDOW/STRIDE/QUEUE_SIZE/RECENT_FRAMES/FUSE_MIN_FRAMES` 는 **'프레임 수'**
    라 스텝 주기가 곧 시간 단위 → 12.7Hz 에서 찾은 `WINDOW=20`(1.57초)이 제품(10Hz)에선 2.0초가
    되어 **튜닝값의 의미가 달라진다**.
  - **해결**: `run_optimization.sh` `INTERVAL_SECOND=0.1` 신설 → `optimize_fusion.resample_frames()`
    가 로그를 그 간격 격자로 **ZOH 재샘플**(각 tick 의 '최신' 프레임, t 는 tick 시각으로 교체)한 뒤
    채점. 제품의 폴링 = '그 시점 최신 센서값'과 같은 입력·같은 dt 가 된다. `0` 이면 옛 동작.
    · `merge_gt.build_gt(interval_sec=)` = R7. 재샘플은 **로그 겹치기 '전'** 에 로그별로 — 그래야
      R5(프레임 인덱스 정렬)의 같은 인덱스가 로그마다 같은 경과시간을 뜻한다.
    · `replay_video.py` 도 같은 `--interval-sec` 를 받아 채점과 동일 스트림을 그린다(fps=`step_hz`).
  - `run_gui.sh`: `FUSE_HZ=15` → **`INTERVAL_SECOND=0.1`**(`--interval-sec`, `fusion.apply_fusion_opts`
    에서 초가 Hz 보다 우선). 두 스크립트의 `INTERVAL_SECOND` 는 **같은 값이어야** 한다.
  - **의도적으로 안 바꾼 것**: `run_debug_gui.sh`(기록) · `run_auto_positioning_v2.sh`(캘리브레이션)
    는 촘촘할수록 좋다 → 센서에서 측정되는 대로 유지. 기록을 15Hz 로 촘촘히 남기고 튜닝에서만
    0.1초로 내려 뽑는 구조라 서로 맞물린다. `run.sh` 도 `FUSE_HZ` 유지(회귀 없음).
  - **튜닝 조건을 결과 파일에 동봉**: `save_params` 가 `_interval_second: <값>` 을 함께 적는다.
    · `run_gui.sh` 의 PARAMS_FILE 로더가 이 키를 읽어 `INTERVAL_SECOND` 를 **자동 동기화** →
      "튜닝 간격과 실행 간격 불일치"가 구조적으로 안 생긴다(실측: 파일 0.2 → `--interval-sec 0.2`).
    · **키 이름의 `_` 접두는 의도적**이다. 같은 파일을 먹는 제품 `fusion_params` 는 `_` 접두 키를
      주석용으로 보고 조용히 건너뛴다 → 제품 코드를 안 고쳐도 "알 수 없는 융합 파라미터" 경고가
      안 남는다. (대문자 키로 적으면 매 기동마다 경고)
  - **제품 파리티 감사(코드 무수정, 읽기 전용 검증)** — 같은 HP 로 같은 성능이 나오는지 확인:
    · **알고리즘 동일**: 벤더링된 `mmwave_core/fusion.py`·`mmwave_reader.py` 를 PoC 원본과 AST
      비교 → 로직 차이 **0건**(fusion 29유닛/reader 34유닛 전부 동일). 제품에서 빠진 건
      `add_fusion_args`/`apply_fusion_opts`/`selftest`/`autodetect_port` 뿐.
    · **파라미터 전달 무손실**: `best_params.yaml` 27개 대문자 키 → `NAME2KW` → `FusionTracker`
      까지 1:1 대조 전부 일치, 기본값으로 떨어지는 항목 0개, `FusionTracker(**opts)` 생성 OK.
    · **실제 융합 스텝 = `max(스냅샷 주기, 1/FUSION_HZ)` = max(0.1, 0.0667) = 0.1초** → 튜닝
      간격 0.1 과 일치. (`_maybe_fuse` 게이트는 상한일 뿐이고 호출부가 `snapshot()` 하나라
      느린 쪽이 구동 주기 — 제품 `config.py` §FUSION_HZ 에도 같은 설명이 있다)
    · ★ `dir`(이동방향)은 폴링 주기와 무관하다 — `trail` 은 `apply_updates`(센서 push, ~383ms)
      에서만 쌓이므로 GUI 60fps·제품 10Hz 어디서도 같은 시간 폭이다. MOVE_MIN/DIR_PEN/ANG_GATE
      가 주기 변경에 흔들리지 않는 근거.
  - ⚠️ **`best_params.yaml` 은 재튜닝 필요**(PoC `debug_logs/` + 제품 `assets/config/` 둘 다) —
    현재 파일은 재샘플 없이(≈12.7Hz) 찾은 값이라 0.1초로 도는 실행에서는 시간 폭이 어긋난다
    (`WINDOW=20`: 1.57초 → 2.0초). 재튜닝하면 `_interval_second` 가 함께 박혀 이후 추적 가능.
  - 미구현(제품 쪽, 요청에 따라 코드 무수정): `fusion_params` 가 `_interval_second` 를 읽어 실제
    스냅샷 주기와 **대조 후 경고**하는 가드. 지금은 값만 파일에 남는다 → §10 TODO.
- **2026-07-29 (MACS 이식 완료)**
  - §12 범위대로 **Product-AI-mono 에 신규 모듈 `vanguard_mmwave` 신설**(브랜치 `aiprod-288-mmwave-재실감지-기능-생성`). 카테고리 `재실체류_cv`/`presence_dwell_cv`(cvEvent), 서비스 `VanguardMmwaveService`.
  - **벤더링**(서브모듈 금지 규약): `fusion.py` / `mmwave_reader.py` / `mmwave_wifi_reader.py` / `mmwave_parser.py` / `epl_config.py` → `modules/vanguard_mmwave/mmwave_core/`. **알고리즘 본체 무변경**(현장 튜닝값 의미 유지). 제거: argparse CLI·selftest·USB 포트탐지·config 쓰기 함수.
  - ⚠️ **PoC 코드의 위험한 기본값 2개를 제품에서 바꿨다** — 이 레포도 같은 방향으로 볼 것:
    · `build_sources()` 의 "센서 없으면 데모(합성 인원) 폴백" → 제거(허위 알람 위험). 제품은 error 로그 + 데이터 없음.
    · `discovery` 기본값 `True` → `False`. R1(서버실=다른 서브넷, mDNS 불가)에서 자동탐색에 의존하면 안 됨.
  - **R1~R4 반영**: 접속은 설정파일 `host`(고정 IP) 직결, 포트 env(`VANGUARD_MMWAVE_API_PORT`, 기본 6053), compose `network_mode: host`. 프로비저닝/캘리브레이션은 런타임 범위 밖(설치 도구).
  - **멀티룸 확장**: 한 `epl_config.json` 에 여러 방을 담도록 `rooms` 매핑 / 센서별 `room` 필드 / `port` 필드 추가(`get_sensors_for_room()`). 매핑 정보가 없으면 전 센서 = 현재 단일 화장실 설정 하위호환. 방(stream) 식별자는 `cameraUrl="mmwave://<room_id>"` 로 전달.
  - **알람 규약**: 방 단위 상태 유지형(START~END 구간 유지, 펄스 아님). 체류 임계=`incidentThresholdSecond`, 이탈 유예=`incidentTimeoutSecond`. ROI 는 픽셀이 아니라 **방 좌표 mm 폴리곤**(point-in-polygon).
  - ★ **체류시간은 '마지막 관측' 기준**(`last_ts - first_ts`)으로 재야 한다. `now` 기준으로 재면 이미 나간 사람이 이탈 유예 동안 임계를 넘어 **"떠난 뒤 START"** 하는 오발화가 생긴다(임계 3s/유예 2s에 2s만 머문 사람이 3s를 넘김). 이식 중 테스트로 잡은 실제 버그 — PoC 쪽에서 체류시간을 쓰는 코드도 같은 함정 주의.
  - 검증: 라이브 센서·GPU 없이 24개 테스트 통과(합성 스냅샷 START/END·가림·ROI·재실카운트, `DemoSensorThread` 3대 e2e, 융합 리플레이 결정성, `room_transform` 역변환).
- **2026-07-29**
  - §12 **MACS 개발 범위** 기재: `run_gui.sh`의 런타임 기능(센서연결·트래킹·재실카운팅·알람)만 MACS에 개발. 프로비저닝/캘리브레이션/튜닝/디버그 스크립트는 제외.
- **2026-07-28 (현장 시나리오)**
  - §★ **핵심 설계 요구사항** + §11 **현장 시나리오(3-phase runbook)** 기록: **크로스 서브넷 내부망 지원**을 최종 기준으로 확립.
  - 원칙: 프로비저닝(병실·같은 서브넷)에서 **센서 IP 캡처** → 서버실(다른 서브넷)에서 **IP 직결 TCP**. 센서 IP는 예약/Static으로 고정. 접속은 mDNS 비의존.
- **2026-07-28**
  - `skill.md` 신설(레포 내 지식 로그).
  - `epl_config.json`: `host`→고정 IP, `discovery:false`로 전환. 3대 라이브 검증(엔티티 78, ~75상태/3s, login=True).
  - 센서 3대 MAC 확정·기록(ARP+API 이중검증). IT 요청서(docx 간소화/전체 + 도식 PNG/PDF) 확정. Monitoring/Inference server IP 안내 문구 추가.
  - IP 자동 resolve 설계 검토(같은 서브넷 자동 / 다른 서브넷 예약IP·스캔).
- **(이전)**
  - Wi-Fi 프로비저닝 성공, mDNS host 공백 이슈 수정. piaspace(같은 /24)에서 무선 E2E 성공. seoulaihub1 단말격리 이슈 진단.
