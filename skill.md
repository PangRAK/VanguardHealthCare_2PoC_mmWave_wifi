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
| id | 이름 | **MAC (고정 앵커)** | node_name | 색 | 현재 로컬 IP(가변) |
|----|------|----------------------|-----------|----|--------------------|
| 98bd80 | Sensor 1 | `A4:F0:0F:98:BD:80` | everything-presence-lite-98bd80 | #27e0c8 | 10.201.31.120 |
| bc0e00 | Sensor 2 | `20:E7:C8:BC:0E:00` | everything-presence-lite-bc0e00 | #ffb454 | 10.201.31.153 |
| b9a07c | Sensor 3 | `00:70:07:B9:A0:7C` | everything-presence-lite-b9a07c | #ff5d8f | 10.201.31.76 |
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
- 리더: `mmwave_wifi_reader.py` `WifiApiReader` → `APIClient(host,6053)` + `ReconnectLogic(login=True)` + `subscribe_states`.
  - 접속엔 **`host`만 사용**(IP/이름 무관). `mac`은 참조·resolve 앵커용(리더가 직접 안 씀).
  - `discovery` 플래그는 **자동탐색(browse)만** 제어 — host의 `.local` 조회와는 별개.
- 파서/융합/시각화 로직은 배포 이관 시 **무변경**(값만 교체).

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
- [ ] `server.py`/`gui` 실제 실행 렌더 확인(고정 IP 전환 후 잔여 검증)
- [ ] `run_provision.sh`: 프로비저닝 직후 **센서 IP 자동 캡처 → config `host` 기록** (R3)
- [ ] 병실 공유기 **DHCP 예약**(또는 firmware static IP)로 센서 IP 고정 (R2)
- [ ] 서버실 ↔ 병실 **서브넷 라우팅 + TCP 6053 방화벽** 확인 (R4, IT)

---

## 11. 현장 시나리오 (PoC On-site Runbook)
> 전제: **서버실 LAN 과 병실 Wi-Fi 는 내부망으로 라우팅 연결**(서로 다른 서브넷). 각 Phase의 서브넷·mDNS 가용성에 주목. (설계 기준 §★ 참조)

### Phase A — 병실에서 프로비저닝  *(Mac·센서 = 같은 서브넷, mDNS 가능)*
1. MacBook 들고 **병실로 이동**.
2. Mac을 **병실 Wi-Fi에 연결**.
3. **센서를 Mac에 USB 연결**.
4. `./run_provision.sh --ssid <병실SSID> --password <PW>` → 센서를 병실 Wi-Fi에 등록.
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

## [변경 로그]
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
