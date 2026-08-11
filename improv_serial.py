"""
Improv Wi-Fi Serial 프로토콜 (호스트 측 구현)

USB로 연결된 ESP32(ESPHome improv_serial)에 Wi-Fi 자격증명을 보내 무선 연결시키는
프로비저닝 프로토콜이다. 바이트 포맷은 improv-wifi 공식 SDK 및 ESPHome 구현과 1:1로
대조하여 검증했다.

프레임 구조(송/수신 공통):
    "IMPROV"(6) | version=0x01 | type(1) | length(1) | data[length] | checksum(1) | 0x0A
    checksum = ('I' 부터 data 마지막 바이트까지 전부 합) & 0xFF   (checksum/개행 제외)
    0x0A 개행은 ESPHome 로그 텍스트와 바이너리 패킷을 구분하는 구분자

같은 USB 시리얼에 ESPHome 로그(ASCII)와 improv 패킷(binary)이 섞여 나오므로,
수신 시 스트림에서 "IMPROV" 매직을 찾아 checksum 검증된 프레임만 골라내고 나머지는 로그로 본다.

그 '나머지 로그' 도 버리지 않고 모아둔다 — 센서의 실제 IP 를 알아낼 유일한 경로라서다
(find_ip_in_logs / ImprovSerial.wait_for_ip 주석 참조).

참고 흐름:
    open → GET_CURRENT_STATE(상태확인) → [선택] GET_WIFI_NETWORKS(주변 AP 스캔)
         → WIFI_SETTINGS(ssid/pw 전송) → PROVISIONED + URL 수신(성공) 또는 ERROR_STATE(실패)
         → wait_for_ip(로그에서 실제 IPv4 확보)
"""
from __future__ import annotations

import re
import time
from typing import List, Optional, Tuple

import serial  # pyserial

MAGIC = b"IMPROV"
VERSION = 0x01

# packet type (byte 7)
T_CURRENT_STATE = 0x01   # device -> host
T_ERROR_STATE = 0x02     # device -> host
T_RPC = 0x03             # host -> device (command)
T_RPC_RESPONSE = 0x04    # device -> host (result)

# device state (CURRENT_STATE data byte)
STATE = {
    0x00: "STOPPED", 0x01: "AWAITING_AUTHORIZATION", 0x02: "AUTHORIZED",
    0x03: "PROVISIONING", 0x04: "PROVISIONED",
}
# error codes (ERROR_STATE data byte)
ERROR = {
    0x00: "NONE", 0x01: "INVALID_RPC", 0x02: "UNKNOWN_RPC",
    0x03: "UNABLE_TO_CONNECT", 0x04: "NOT_AUTHORIZED", 0x05: "BAD_HOSTNAME",
    0xFF: "UNKNOWN",
}
# RPC command id (first byte of an RPC data section)
CMD_WIFI_SETTINGS = 0x01
CMD_GET_CURRENT_STATE = 0x02
CMD_GET_DEVICE_INFO = 0x03
CMD_GET_WIFI_NETWORKS = 0x04


# ---------------------------------------------------------------- 빌드(송신)
def build_packet(ptype: int, data: bytes = b"") -> bytes:
    f = bytearray(MAGIC)
    f += bytes([VERSION, ptype, len(data)])
    f += data
    f.append(sum(f) & 0xFF)   # checksum
    f.append(0x0A)            # newline
    return bytes(f)


def _rpc(cmd: int, body: bytes = b"") -> bytes:
    # RPC data 섹션 = [command_id][rpc_body_len][rpc_body...]
    return build_packet(T_RPC, bytes([cmd, len(body)]) + body)


def pkt_get_state() -> bytes:
    return _rpc(CMD_GET_CURRENT_STATE)


def pkt_get_device_info() -> bytes:
    return _rpc(CMD_GET_DEVICE_INFO)


def pkt_get_networks() -> bytes:
    return _rpc(CMD_GET_WIFI_NETWORKS)


def pkt_set_wifi(ssid: str, password: str) -> bytes:
    s = ssid.encode("utf-8")
    p = password.encode("utf-8")
    body = bytes([len(s)]) + s + bytes([len(p)]) + p
    return _rpc(CMD_WIFI_SETTINGS, body)


# ---------------------------------------------------------------- 파싱(수신)
def iter_frames(buf: bytearray, log_out: Optional[bytearray] = None):
    """버퍼에서 유효한 improv 프레임을 (type, data) 로 꺼내며 소비한다.

    프레임이 아닌 바이트(=ESPHome 로그)는 한 바이트씩 건너뛴다.
    완성되지 않은 프레임이 남으면 멈추고 다음 read 를 기다린다.

    log_out 을 주면 '프레임이 아니라서 버리는' 바이트를 그쪽에 모아둔다. 그 텍스트가
    센서의 **실제 IP 를 알아낼 유일한 경로**이기 때문이다(find_ip_in_logs 주석 참조)."""
    def _drop(n: int) -> None:
        """프레임이 아닌 앞쪽 n 바이트를 소비한다(log_out 이 있으면 그쪽에 넘기고)."""
        if n <= 0:
            return
        if log_out is not None:
            log_out.extend(buf[:n])
        del buf[:n]

    while True:
        i = buf.find(MAGIC)
        if i < 0:
            # 매직이 없으면 거의 다 비우되, 매직이 잘려 들어올 경우 대비해 꼬리 5바이트는 남김
            _drop(max(0, len(buf) - (len(MAGIC) - 1)))
            return
        _drop(i)                             # 매직 앞의 로그 텍스트 버림
        if len(buf) < 9:
            return                           # 헤더(타입/길이)까지 못 받음
        if buf[6] != VERSION:
            _drop(1)                          # 잘못된 버전 → 1바이트 전진 재동기화
            continue
        dlen = buf[8]
        need = 9 + dlen + 1                   # checksum 까지 필요한 길이
        if len(buf) < need:
            return                           # 프레임 미완성 → 더 받기
        if (sum(buf[: 9 + dlen]) & 0xFF) != buf[9 + dlen]:
            _drop(1)                          # checksum 불일치 → 재동기화
            continue
        ptype = buf[7]
        data = bytes(buf[9: 9 + dlen])
        consume = need
        if len(buf) > need and buf[need] == 0x0A:
            consume = need + 1               # 뒤따르는 개행도 소비
        del buf[:consume]
        yield ptype, data


def parse_rpc_response(data: bytes) -> Tuple[Optional[int], List[str]]:
    """RPC_RESPONSE data = [command_id][result_len][ {len, str_bytes} * ] 를 해석."""
    if len(data) < 2:
        return None, []
    cmd_id = data[0]
    rlen = data[1]
    body = data[2: 2 + rlen]
    out: List[str] = []
    i = 0
    while i < len(body):
        n = body[i]
        out.append(body[i + 1: i + 1 + n].decode("utf-8", "replace"))
        i += 1 + n
    return cmd_id, out


# ------------------------------------------------------------- 로그에서 IP 뽑기
# ESPHome 은 Wi-Fi 에 붙은 직후 접속 정보를 한 줄씩 찍는다: "  IP Address: 192.168.0.42"
# (포맷 문자열 'IP Address: %s' 는 EPL 펌웨어 바이너리에서 직접 확인했다.)
#
# ★ 왜 improv 응답 URL 대신 로그를 보는가:
#   improv 의 WIFI_SETTINGS 응답에 담기는 URL 은 펌웨어에 web_server 나 improv 의
#   next_url 이 있을 때만 채워진다. EPL 기본 빌드에는 둘 다 없어서 응답이 **빈 목록**이고,
#   그래서 지금까지 mDNS 이름(<node>.local)밖에 기록할 수 없었다. 로그는 그 설정과
#   무관하게 항상 찍히고, **노트북이 그 Wi-Fi 에 붙어 있지 않아도** USB 로 읽을 수 있다.
_IP_LOG_RE = re.compile(rb"IP\s+Address:\s*(\d{1,3}(?:\.\d{1,3}){3})")


def is_usable_ipv4(text: str) -> bool:
    """접속 주소로 **쓸 수 있는** IPv4 인지 판정한다.

    0.0.0.0(DHCP 완료 전 과도 상태)과 169.254.x.x(DHCP 실패 → 링크로컬)는 제외한다.
    둘 다 연결 과정에서 실제로 로그에 찍히는데, 그대로 기록하면 나중에 접속이
    조용히 실패한다(주소가 있으니 도구는 정상으로 보고 붙지 못한다)."""
    parts = text.split(".")
    if len(parts) != 4 or not all(p.isdigit() and int(p) <= 255 for p in parts):
        return False
    return text != "0.0.0.0" and not text.startswith("169.254.")


def find_ip_in_logs(log: bytes) -> str:
    """ESPHome 로그 텍스트에서 **마지막으로** 보고된 쓸 만한 IPv4 를 뽑는다. 없으면 "".

    마지막 것을 쓰는 이유: 한 번의 연결 과정에서 0.0.0.0 → 실주소 순으로 여러 번
    찍히고, 재연결까지 겹치면 옛 주소가 앞에 남는다. 최신 값이 지금 붙은 주소다."""
    found = ""
    for m in _IP_LOG_RE.finditer(log):
        cand = m.group(1).decode("ascii", "ignore")
        if is_usable_ipv4(cand):
            found = cand
    return found


# ---------------------------------------------------------------- 세션
class ImprovError(Exception):
    pass


# 로그 버퍼 상한(바이트). 한 번의 프로비저닝 실행은 수십 KB 수준이라 넉넉하다 —
# 상한이 필요한 이유는 ESPHome 이 DEBUG 로그를 계속 흘려보내기 때문이다.
LOG_KEEP = 262_144


class ImprovSerial:
    """USB 시리얼로 improv 세션을 수행한다."""

    def __init__(self, port: str, baud: int = 115200, boot_wait: float = 2.0,
                 verbose: bool = False):
        self.port = port
        self.baud = baud
        self.boot_wait = boot_wait
        self.verbose = verbose
        self.ser: Optional[serial.Serial] = None
        self.buf = bytearray()
        # 같은 UART 에 섞여 오는 ESPHome 로그 텍스트. improv 프레임이 아니라 버려지는
        # 바이트를 여기 모아 실제 IP 를 뽑는다(wait_for_ip).
        self.log = bytearray()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=0.2)
        # 포트를 열면 ESP32(CH340 자동리셋)가 재부팅할 수 있으므로 잠시 대기
        time.sleep(self.boot_wait)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.buf.clear()
        self.log.clear()

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _send(self, pkt: bytes):
        assert self.ser is not None
        self.ser.write(pkt)
        self.ser.flush()

    def _trim_log(self):
        if len(self.log) > LOG_KEEP:
            del self.log[: len(self.log) - LOG_KEEP]

    def _collect(self, seconds: float):
        """seconds 동안 들어오는 improv 프레임을 순서대로 yield."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            chunk = self.ser.read(2048)
            if chunk:
                self.buf += chunk
                for ptype, data in iter_frames(self.buf, self.log):
                    yield ptype, data
                self._trim_log()

    # ---- 동작들 ----
    def request_state(self, timeout: float = 8.0, retries: int = 4) -> Optional[str]:
        """현재 상태를 조회. 상태 문자열(AUTHORIZED 등) 반환."""
        for _ in range(retries):
            self._send(pkt_get_state())
            for ptype, data in self._collect(timeout / retries):
                if ptype == T_CURRENT_STATE and data:
                    return STATE.get(data[0], f"0x{data[0]:02x}")
                if ptype == T_ERROR_STATE and data:
                    raise ImprovError(ERROR.get(data[0], f"0x{data[0]:02x}"))
        return None

    def device_info(self, timeout: float = 8.0, retries: int = 3) -> List[str]:
        """장치 정보 [firmware, version, variant, node_name] 반환."""
        for _ in range(retries):
            self._send(pkt_get_device_info())
            for ptype, data in self._collect(timeout / retries):
                if ptype == T_RPC_RESPONSE:
                    cmd, strings = parse_rpc_response(data)
                    if cmd == CMD_GET_DEVICE_INFO:
                        return strings
        return []

    def scan_networks(self, timeout: float = 20.0) -> List[Tuple[str, str, bool]]:
        """주변 Wi-Fi 스캔. (ssid, rssi, secured) 리스트. 빈 응답이 끝 신호."""
        self._send(pkt_get_networks())
        nets: List[Tuple[str, str, bool]] = []
        for ptype, data in self._collect(timeout):
            if ptype != T_RPC_RESPONSE:
                continue
            cmd, strings = parse_rpc_response(data)
            if cmd != CMD_GET_WIFI_NETWORKS:
                continue
            if not strings:
                break                         # 빈 응답 = 목록 끝
            ssid = strings[0]
            rssi = strings[1] if len(strings) > 1 else ""
            secured = (len(strings) > 2 and strings[2].upper() == "YES")
            if ssid:
                nets.append((ssid, rssi, secured))
        return nets

    def wait_for_ip(self, timeout: float = 20.0) -> str:
        """방금 연결된 센서의 **실제 IPv4** 를 ESPHome 로그에서 얻는다. 못 얻으면 "".

        provision() 직후에 부르는 것을 전제로 한다 — 연결 성공 로그가 그 시점에 찍히고,
        provision() 이 수집해둔 self.log 에 이미 들어있는 경우가 많아 먼저 그쪽을 본다
        (대개 즉시 반환된다). 왜 improv 응답 URL 이 아니라 로그인지는 위 _IP_LOG_RE
        주석 참조."""
        ip = find_ip_in_logs(self.log)
        if ip:
            return ip
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            chunk = self.ser.read(2048)
            if not chunk:
                continue
            self.buf += chunk
            for _ptype, _data in iter_frames(self.buf, self.log):
                pass                          # 남은 프레임은 버리고 로그만 모은다
            self._trim_log()
            ip = find_ip_in_logs(self.log)
            if ip:
                return ip
        return ""

    def reset_and_wait_for_ip(self, timeout: float = 45.0) -> str:
        """센서를 **재부팅시켜** 부팅 로그의 'IP Address: …' 를 확보한다. 못 얻으면 "".

        ★ 왜 필요한가 — wait_for_ip 만으로는 부족한 실측 사례:
          이미 그 Wi-Fi 에 붙어 있는 센서는 improv 가 **재접속 없이** 기존 연결을 근거로
          성공을 돌려준다. 그러면 ESPHome 이 접속 정보를 다시 찍을 일이 없어(그 줄은
          (재)접속 시점과 dump_config 에서만 나온다) 로그에 IP 가 영원히 안 나타난다
          — 이미 연결된 센서에서 25초/56KB 를 받아도 'IP Address' 0회였다.
          재부팅은 그 줄을 **반드시** 다시 찍게 만드는 확실한 방법이다.
        ※ ESPHome safe_mode 의 '실패 부팅' 카운터가 1 올라가지만 60초 정상 구동으로
          해제되고 임계는 10 이므로, 1회 재부팅은 안전하다."""
        if self.ser is None:
            return ""
        self.buf.clear()
        self.log.clear()
        try:                                  # CH340 자동리셋 회로(RTS→EN)
            self.ser.setDTR(False)
            self.ser.setRTS(True)
            time.sleep(0.15)
            self.ser.setRTS(False)
        except Exception:                     # noqa: BLE001 — 어댑터가 제어선을 안 물린 경우
            return ""
        return self.wait_for_ip(timeout=timeout)

    def provision(self, ssid: str, password: str,
                  timeout: float = 30.0) -> List[str]:
        """Wi-Fi 자격증명 전송 후 PROVISIONED 까지 대기. 성공 시 URL 리스트 반환.

        실패 시 ImprovError(에러명) 발생."""
        self._send(pkt_set_wifi(ssid, password))
        last_state = None
        for ptype, data in self._collect(timeout):
            if ptype == T_CURRENT_STATE and data:
                last_state = STATE.get(data[0], f"0x{data[0]:02x}")
                if self.verbose:
                    print(f"  [state] {last_state}")
            elif ptype == T_ERROR_STATE and data:
                raise ImprovError(ERROR.get(data[0], f"0x{data[0]:02x}"))
            elif ptype == T_RPC_RESPONSE:
                cmd, strings = parse_rpc_response(data)
                if cmd == CMD_WIFI_SETTINGS:
                    return strings            # 성공 (URL 리스트; 비어있을 수도)
        raise ImprovError(f"timeout (마지막 상태: {last_state})")
