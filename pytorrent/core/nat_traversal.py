"""
pytorrent/core/nat_traversal.py
────────────────────────────────
NAT traversal để client nhận được kết nối từ bên ngoài khi seed.

Chiến lược (ưu tiên):
  1. UPnP IGD  — dùng miniupnpc (blocking → executor)
  2. PCP       — Port Control Protocol, RFC 6887 (kế nhiệm NAT-PMP)
                  Ưu điểm so với NAT-PMP (RFC 6886):
                    • Version 2, dùng 96-bit nonce → an toàn hơn
                    • External IP trả về ngay trong MAP response
                    • Hỗ trợ cả IPv4 lẫn IPv6
                    • Được hỗ trợ rộng rãi trên router hiện đại (2012+)
  3. Fallback  — không map được; hoạt động nếu đã có port forward thủ công

Sau khi map: renewal_loop() gia hạn mỗi RENEW_INTERVAL giây.
Khi tắt: cleanup() xóa mapping.
"""

import asyncio
import logging
import os
import socket
import struct
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

RENEW_INTERVAL          = 2700   # 45 phút — trước khi router expire (thường 60 phút)
PCP_LIFETIME            = 7200   # 2 giờ
PCP_PORT                = 5351   # Cổng PCP/NAT-PMP (giống nhau)
PCP_VERSION             = 2
PCP_OPCODE_MAP          = 1
PCP_PROTOCOL_TCP        = 6
UPNP_DISCOVER_DELAY_MS  = 500


class NATTraversal:
    """
    Quản lý port mapping trên router bằng UPnP hoặc PCP.

        nat = NATTraversal(6881)
        ext_ip, ext_port = await nat.setup()
        asyncio.create_task(nat.renewal_loop())
        ...
        nat.cleanup()
    """

    def __init__(self, internal_port: int):
        self.internal_port  = internal_port
        self.external_port  = internal_port
        self.external_ip: Optional[str] = None
        self.method: Optional[str] = None      # 'upnp' | 'pcp' | None
        self._upnp          = None
        self._gateway: Optional[str] = None
        self._pcp_nonce: Optional[bytes] = None
        self._active        = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def setup(self) -> tuple[Optional[str], int]:
        """Thử UPnP → PCP. Trả về (external_ip, external_port)."""
        result = await asyncio.get_event_loop().run_in_executor(None, self._try_upnp)
        if result:
            self.external_ip, self.external_port = result
            self.method  = "upnp"
            self._active = True
            logger.info(
                f"NAT: UPnP OK — {self.external_ip}:{self.external_port} "
                f"→ :{self.internal_port}"
            )
            return self.external_ip, self.external_port

        result = await self._try_pcp()
        if result:
            self.external_ip, self.external_port = result
            self.method  = "pcp"
            self._active = True
            logger.info(
                f"NAT: PCP OK — {self.external_ip}:{self.external_port} "
                f"→ :{self.internal_port}"
            )
            return self.external_ip, self.external_port

        logger.warning(
            "NAT: UPnP và PCP đều thất bại. "
            "Kết nối vào chỉ hoạt động nếu đã forward port thủ công."
        )
        return None, self.internal_port

    async def renew(self):
        if not self._active:
            return
        if self.method == "upnp":
            ok = await asyncio.get_event_loop().run_in_executor(None, self._renew_upnp)
            if not ok:
                self._active = False
                await self.setup()
        elif self.method == "pcp":
            result = await self._try_pcp()
            if result:
                self.external_ip, self.external_port = result
                logger.debug(f"NAT: PCP renewed → {self.external_ip}:{self.external_port}")
            else:
                logger.warning("NAT: PCP renewal failed")
                self._active = False

    async def renewal_loop(self):
        """Gia hạn định kỳ. Chạy bằng asyncio.create_task()."""
        while True:
            await asyncio.sleep(RENEW_INTERVAL)
            try:
                await self.renew()
            except Exception as e:
                logger.debug(f"NAT: renewal error: {e}")

    def cleanup(self):
        if not self._active:
            return
        if self.method == "upnp" and self._upnp:
            try:
                self._upnp.deleteportmapping(self.external_port, "TCP")
                logger.info(f"NAT: Removed UPnP mapping port {self.external_port}")
            except Exception as e:
                logger.debug(f"NAT: UPnP delete error: {e}")
        elif self.method == "pcp" and self._gateway and self._pcp_nonce:
            # PCP MAP với lifetime=0 = xóa mapping (RFC 6887 §11.2)
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    req = self._build_pcp_map_request(
                        self.internal_port, self._pcp_nonce, lifetime=0
                    )
                    loop.run_until_complete(
                        _pcp_send_recv(self._gateway, PCP_PORT, req, retries=1)
                    )
                    logger.info(f"NAT: Removed PCP mapping port {self.external_port}")
            except Exception as e:
                logger.debug(f"NAT: PCP delete error: {e}")
        self._active = False

    @property
    def status(self) -> dict:
        return {
            "method":        self.method,
            "active":        self._active,
            "internal_port": self.internal_port,
            "external_port": self.external_port,
            "external_ip":   self.external_ip,
        }

    # ── UPnP ─────────────────────────────────────────────────────────────────

    def _try_upnp(self) -> Optional[tuple[str, int]]:
        try:
            import miniupnpc  # type: ignore
        except ImportError:
            logger.debug("NAT: miniupnpc không có — bỏ qua UPnP (pip install miniupnpc)")
            return None
        try:
            upnp = miniupnpc.UPnP()
            upnp.discoverdelay = UPNP_DISCOVER_DELAY_MS
            if upnp.discover() == 0:
                return None
            upnp.selectigd()
            ext_ip = upnp.externalipaddress()
            if not ext_ip:
                return None
            for try_ext in range(self.internal_port, self.internal_port + 10):
                existing = upnp.getspecificportmapping(try_ext, "TCP")
                if existing and existing[1] != upnp.lanaddr:
                    continue
                if upnp.addportmapping(
                    try_ext, "TCP", upnp.lanaddr,
                    self.internal_port, "pytorrent", ""
                ):
                    self._upnp = upnp
                    return ext_ip, try_ext
            return None
        except Exception as e:
            logger.debug(f"NAT: UPnP error: {e}")
            return None

    def _renew_upnp(self) -> bool:
        if self._upnp is None:
            return False
        try:
            return bool(self._upnp.addportmapping(
                self.external_port, "TCP",
                self._upnp.lanaddr, self.internal_port, "pytorrent", ""
            ))
        except Exception as e:
            logger.warning(f"NAT: UPnP renewal error: {e}")
            return False

    # ── PCP — Port Control Protocol (RFC 6887) ────────────────────────────────
    #
    # Request layout (60 bytes):
    #   Header (24B):  version(1) | opcode(1) | reserved(2) | lifetime(4) | client_ip(16)
    #   MAP body (36B): nonce(12) | proto(1) | pad(3) | int_port(2) | sug_ext_port(2) | sug_ext_ip(16)
    #
    # Response layout (60 bytes):
    #   Header (24B):  version(1) | opcode+R(1) | reserved(1) | result(1)
    #                  | lifetime(4) | epoch(4) | reserved(12)
    #   MAP body (36B): nonce(12) | proto(1) | pad(3) | int_port(2) | ext_port(2) | ext_ip(16)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _local_ip() -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]

    @staticmethod
    def _ipv4_to_mapped(ipv4_str: str) -> bytes:
        """IPv4 → IPv4-mapped IPv6 (16 bytes): ::ffff:a.b.c.d"""
        return b'\x00' * 10 + b'\xff\xff' + socket.inet_aton(ipv4_str)

    @staticmethod
    def _mapped_to_ipv4(data16: bytes) -> Optional[str]:
        if data16[:12] == b'\x00' * 10 + b'\xff\xff':
            return socket.inet_ntoa(data16[12:16])
        try:
            return socket.inet_ntop(socket.AF_INET6, data16)
        except Exception:
            return None

    def _build_pcp_map_request(
        self, internal_port: int, nonce: bytes,
        lifetime: int = PCP_LIFETIME,
        client_ip: Optional[str] = None,
    ) -> bytes:
        if client_ip is None:
            try:
                client_ip = self._local_ip()
            except Exception:
                client_ip = "0.0.0.0"

        client_ip_b = self._ipv4_to_mapped(client_ip)

        # Header: version(1B) | opcode(1B) | reserved(2B) | lifetime(4B) | client_ip(16B)
        header = struct.pack("!BBH I", PCP_VERSION, PCP_OPCODE_MAP, 0, lifetime) + client_ip_b

        # MAP body: nonce(12B) | proto(1B) | pad(3B) | int_port(2B) | ext_port(2B) | ext_ip(16B)
        body = (
            nonce
            + struct.pack("!B 3x HH", PCP_PROTOCOL_TCP, internal_port, internal_port)
            + b'\x00' * 16   # suggested external IP = any
        )

        return header + body  # 24 + 36 = 60 bytes

    @staticmethod
    def _parse_pcp_map_response(
        data: bytes, expected_nonce: bytes
    ) -> Optional[tuple[str, int, int]]:
        """Returns (external_ip, external_port, lifetime) or None."""
        if len(data) < 60:
            return None

        version     = data[0]
        r_opcode    = data[1]   # must be 0x81 (R=1, opcode=MAP=1)
        result_code = data[3]

        if version != PCP_VERSION or r_opcode != (PCP_OPCODE_MAP | 0x80):
            return None
        if result_code != 0:
            PCP_ERRORS = {
                1: "UNSUPP_VERSION", 2: "NOT_AUTHORIZED", 3: "MALFORMED_REQUEST",
                4: "UNSUPP_OPCODE",  7: "NO_RESOURCES",   8: "UNSUPP_PROTOCOL",
               11: "CANNOT_PROVIDE_EXTERNAL", 12: "ADDRESS_MISMATCH",
            }
            logger.debug(
                f"NAT: PCP MAP error: {PCP_ERRORS.get(result_code, f'code={result_code}')}"
            )
            return None

        lifetime          = struct.unpack("!I", data[4:8])[0]
        # epoch           = struct.unpack("!I", data[8:12])[0]
        # reserved          data[12:24]

        resp_nonce        = data[24:36]
        if resp_nonce != expected_nonce:
            logger.debug("NAT: PCP nonce mismatch — ignoring stale response")
            return None

        # protocol        = data[36]
        # pad               data[37:40]
        ext_port          = struct.unpack("!H", data[42:44])[0]
        ext_ip_bytes      = data[44:60]

        ext_ip = NATTraversal._mapped_to_ipv4(ext_ip_bytes)
        if not ext_ip:
            return None

        return ext_ip, ext_port, lifetime

    async def _try_pcp(self) -> Optional[tuple[str, int]]:
        gateway = await asyncio.get_event_loop().run_in_executor(
            None, self._get_default_gateway
        )
        if not gateway:
            logger.debug("NAT: PCP — gateway không xác định được")
            return None

        self._gateway = gateway

        # Dùng lại nonce cũ khi renewal để router nhận ra mapping cần gia hạn
        if self._pcp_nonce is None:
            self._pcp_nonce = os.urandom(12)

        request = self._build_pcp_map_request(
            self.internal_port, self._pcp_nonce, lifetime=PCP_LIFETIME
        )
        data = await _pcp_send_recv(gateway, PCP_PORT, request, retries=4)
        if not data:
            logger.debug("NAT: PCP — không nhận response từ gateway")
            return None

        result = self._parse_pcp_map_response(data, self._pcp_nonce)
        if not result:
            return None

        ext_ip, ext_port, _lifetime = result
        return ext_ip, ext_port

    # ── Gateway detection ─────────────────────────────────────────────────────

    @staticmethod
    def _get_default_gateway() -> Optional[str]:
        try:
            system = sys.platform
            if system.startswith("linux"):
                with open("/proc/net/route") as f:
                    for line in f.readlines()[1:]:
                        parts = line.split()
                        if len(parts) > 2 and parts[1] == "00000000":
                            gw = socket.inet_ntoa(struct.pack("<I", int(parts[2], 16)))
                            if gw != "0.0.0.0":
                                return gw

            elif system == "darwin":
                r = subprocess.run(
                    ["route", "-n", "get", "default"],
                    capture_output=True, text=True, timeout=3
                )
                for line in r.stdout.splitlines():
                    if "gateway:" in line:
                        return line.split("gateway:")[1].strip()

            elif system == "win32":
                r = subprocess.run(
                    ["ipconfig"], capture_output=True, text=True, timeout=3
                )
                for line in r.stdout.splitlines():
                    if "Default Gateway" in line:
                        parts = line.split(":")
                        if len(parts) > 1:
                            gw = parts[-1].strip()
                            if gw:
                                return gw
        except Exception as e:
            logger.debug(f"NAT: OS gateway detection error: {e}")

        # Heuristic: .1 trên cùng /24 subnet
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
            parts = local_ip.split(".")
            parts[3] = "1"
            gw = ".".join(parts)
            logger.debug(f"NAT: gateway heuristic → {gw}")
            return gw
        except Exception:
            return None


# ── UDP helper ────────────────────────────────────────────────────────────────

async def _pcp_send_recv(
    host: str, port: int, data: bytes,
    retries: int = 4,
) -> Optional[bytes]:
    """
    Gửi UDP và chờ response với exponential backoff theo RFC 6887 §8.1.1.
    Timeout: 250ms → 500ms → 1000ms → 2000ms
    """
    loop = asyncio.get_event_loop()
    timeout_ms = 250

    for attempt in range(retries):
        transport = None
        try:
            future: asyncio.Future = loop.create_future()

            class _Proto(asyncio.DatagramProtocol):
                def connection_made(self, t):
                    t.sendto(data)

                def datagram_received(self, d, _addr):
                    if not future.done():
                        future.set_result(d)

                def error_received(self, exc):
                    if not future.done():
                        future.set_exception(exc)

            transport, _ = await loop.create_datagram_endpoint(
                _Proto, remote_addr=(host, port)
            )
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=timeout_ms / 1000.0
            )

        except asyncio.TimeoutError:
            timeout_ms *= 2
            continue
        except Exception as e:
            logger.debug(f"NAT: UDP error attempt {attempt + 1}: {e}")
            return None
        finally:
            if transport:
                transport.close()

    return None
