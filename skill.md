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
- [ ] **`best_params.yaml` 0.1초 기준 재튜닝** → PoC `debug_logs/` + 제품 `assets/config/` 동시 갱신
- [ ] (제품) `fusion_params` 가 `_interval_second` 를 읽어 실제 스냅샷 주기와 다르면 **경고**
      (지금은 PoC 가 값만 동봉. 조용히 어긋나면 같은 HP 가 다른 시간 폭을 뜻해 성능이 안 재현됨)
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
