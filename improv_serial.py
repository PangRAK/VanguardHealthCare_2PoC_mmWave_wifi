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

참고 흐름:
    open → GET_CURRENT_STATE(상태확인) → [선택] GET_WIFI_NETWORKS(주변 AP 스캔)
         → WIFI_SETTINGS(ssid/pw 전송) → PROVISIONED + URL 수신(성공) 또는 ERROR_STATE(실패)
"""
from __future__ import annotations

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
def iter_frames(buf: bytearray):
    """버퍼에서 유효한 improv 프레임을 (type, data) 로 꺼내며 소비한다.

    프레임이 아닌 바이트(=ESPHome 로그)는 한 바이트씩 건너뛴다.
    완성되지 않은 프레임이 남으면 멈추고 다음 read 를 기다린다."""
    while True:
        i = buf.find(MAGIC)
        if i < 0:
            # 매직이 없으면 거의 다 비우되, 매직이 잘려 들어올 경우 대비해 꼬리 5바이트는 남김
            del buf[: max(0, len(buf) - (len(MAGIC) - 1))]
            return
        if i:
            del buf[:i]                      # 매직 앞의 로그 텍스트 버림
        if len(buf) < 9:
            return                           # 헤더(타입/길이)까지 못 받음
        if buf[6] != VERSION:
            del buf[:1]                       # 잘못된 버전 → 1바이트 전진 재동기화
            continue
        dlen = buf[8]
        need = 9 + dlen + 1                   # checksum 까지 필요한 길이
        if len(buf) < need:
            return                           # 프레임 미완성 → 더 받기
        if (sum(buf[: 9 + dlen]) & 0xFF) != buf[9 + dlen]:
            del buf[:1]                       # checksum 불일치 → 재동기화
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


# ---------------------------------------------------------------- 세션
class ImprovError(Exception):
    pass


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

    def _collect(self, seconds: float):
        """seconds 동안 들어오는 improv 프레임을 순서대로 yield."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            chunk = self.ser.read(2048)
            if chunk:
                self.buf += chunk
                for ptype, data in iter_frames(self.buf):
                    yield ptype, data

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
