"""
pytorrent/core/nat_traversal.py
────────────────────────────────
Xử lý NAT traversal để client có thể nhận kết nối từ bên ngoài khi đang seed.

Chiến lược (ưu tiên theo thứ tự):
  1. UPnP IGD   — phổ biến nhất, dùng miniupnpc
  2. NAT-PMP    — giao thức Apple/IETF RFC 6886, UDP tới cổng 5351 của gateway
  3. Fallback   — không map được, dùng port nội bộ (hoạt động nếu không có NAT)

Sau khi map thành công, gọi renew() mỗi RENEW_INTERVAL giây để gia hạn.
Gọi cleanup() khi tắt để xóa mapping khỏi router.
"""

import asyncio
import logging
import socket
import struct
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# Bao lâu thì gia hạn mapping (giây). Hầu hết router hết hạn sau 3600s.
RENEW_INTERVAL = 2700   # Gia hạn sau 45 phút
NATPMP_LIFETIME = 7200  # Yêu cầu lifetime 2 giờ với NAT-PMP
NATPMP_PORT = 5351
UPNP_DISCOVER_DELAY_MS = 500


class NATTraversal:
    """
    Quản lý việc mở cổng trên router bằng UPnP hoặc NAT-PMP.

    Sử dụng:
        nat = NATTraversal(internal_port=6881)
        external_ip, external_port = await nat.setup()
        # Lưu external_ip/port vào torrent_info để announce đúng với tracker
        ...
        # Chạy renewal loop:
        asyncio.create_task(nat.renewal_loop())
        ...
        # Khi tắt:
        nat.cleanup()
    """

    def __init__(self, internal_port: int):
        self.internal_port = internal_port
        self.external_port = internal_port
        self.external_ip: Optional[str] = None
        self.method: Optional[str] = None   # 'upnp' | 'natpmp' | None
        self._upnp = None
        self._gateway: Optional[str] = None
        self._active = False

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def setup(self) -> tuple[Optional[str], int]:
        """
        Thử UPnP rồi NAT-PMP.
        Trả về (external_ip, external_port).
        external_ip = None nếu cả hai đều thất bại.
        """
        # 1. UPnP
        result = await asyncio.get_event_loop().run_in_executor(None, self._try_upnp)
        if result:
            self.external_ip, self.external_port = result
            self.method = "upnp"
            self._active = True
            logger.info(
                f"NAT: UPnP OK — external {self.external_ip}:{self.external_port} "
                f"→ local :{self.internal_port}"
            )
            return self.external_ip, self.external_port

        # 2. NAT-PMP
        result = await self._try_natpmp()
        if result:
            self.external_ip, self.external_port = result
            self.method = "natpmp"
            self._active = True
            logger.info(
                f"NAT: NAT-PMP OK — external {self.external_ip}:{self.external_port} "
                f"→ local :{self.internal_port}"
            )
            return self.external_ip, self.external_port

        logger.warning(
            "NAT: UPnP và NAT-PMP đều thất bại. "
            "Client sẽ không nhận được kết nối vào nếu đứng sau NAT chặt."
        )
        return None, self.internal_port

    async def renew(self):
        """Gia hạn mapping hiện tại."""
        if not self._active:
            return

        if self.method == "upnp":
            ok = await asyncio.get_event_loop().run_in_executor(None, self._renew_upnp)
            if not ok:
                # UPnP không còn hoạt động, thử lại từ đầu
                self._active = False
                await self.setup()

        elif self.method == "natpmp":
            result = await self._try_natpmp()
            if result:
                self.external_ip, self.external_port = result
                logger.debug(f"NAT: NAT-PMP renewed port {self.external_port}")
            else:
                logger.warning("NAT: NAT-PMP renewal failed")
                self._active = False

    async def renewal_loop(self):
        """
        Chạy nền, gia hạn port mapping định kỳ.
        asyncio.create_task(nat.renewal_loop())
        """
        while True:
            await asyncio.sleep(RENEW_INTERVAL)
            try:
                await self.renew()
            except Exception as e:
                logger.debug(f"NAT: renewal error: {e}")

    def cleanup(self):
        """Xóa mapping khỏi router. Gọi khi tắt client."""
        if not self._active:
            return

        if self.method == "upnp" and self._upnp:
            try:
                self._upnp.deleteportmapping(self.external_port, "TCP")
                logger.info(f"NAT: Removed UPnP mapping port {self.external_port}")
            except Exception as e:
                logger.debug(f"NAT: Failed to remove UPnP mapping: {e}")

        elif self.method == "natpmp" and self._gateway:
            # Gửi request NAT-PMP với lifetime=0 để xóa mapping
            asyncio.get_event_loop().run_until_complete(
                self._natpmp_map_port(self._gateway, self.internal_port, lifetime=0)
            ) if not asyncio.get_event_loop().is_closed() else None

        self._active = False

    @property
    def status(self) -> dict:
        return {
            "method": self.method,
            "active": self._active,
            "internal_port": self.internal_port,
            "external_port": self.external_port,
            "external_ip": self.external_ip,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # UPnP (miniupnpc)
    # ─────────────────────────────────────────────────────────────────────────

    def _try_upnp(self) -> Optional[tuple[str, int]]:
        """Blocking; chạy trong executor."""
        try:
            import miniupnpc  # type: ignore
        except ImportError:
            logger.debug("NAT: miniupnpc chưa được cài (pip install miniupnpc). Bỏ qua UPnP.")
            return None

        try:
            upnp = miniupnpc.UPnP()
            upnp.discoverdelay = UPNP_DISCOVER_DELAY_MS
            n = upnp.discover()
            if n == 0:
                logger.debug("NAT: UPnP — không tìm thấy thiết bị IGD nào")
                return None

            upnp.selectigd()
            external_ip = upnp.externalipaddress()
            if not external_ip:
                return None

            # Thử lần lượt external_port = internal_port ... internal_port+9
            for try_ext in range(self.internal_port, self.internal_port + 10):
                # Kiểm tra port đã được dùng chưa
                existing = upnp.getspecificportmapping(try_ext, "TCP")
                if existing and existing[1] != upnp.lanaddr:
                    # Port đã dùng bởi thiết bị khác
                    continue

                ok = upnp.addportmapping(
                    try_ext,             # external port
                    "TCP",
                    upnp.lanaddr,        # internal IP (LAN)
                    self.internal_port,  # internal port
                    "pytorrent",         # description
                    "",                  # remote host (any)
                )
                if ok:
                    self._upnp = upnp
                    self.external_port = try_ext
                    return external_ip, try_ext

            logger.warning("NAT: UPnP — không thể map port nào trong dải thử")
            return None

        except Exception as e:
            logger.debug(f"NAT: UPnP lỗi: {e}")
            return None

    def _renew_upnp(self) -> bool:
        if self._upnp is None:
            return False
        try:
            ok = self._upnp.addportmapping(
                self.external_port, "TCP",
                self._upnp.lanaddr, self.internal_port,
                "pytorrent", ""
            )
            logger.debug(f"NAT: UPnP renewed port {self.external_port}: {'OK' if ok else 'FAIL'}")
            return bool(ok)
        except Exception as e:
            logger.warning(f"NAT: UPnP renewal error: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # NAT-PMP (RFC 6886)
    # ─────────────────────────────────────────────────────────────────────────

    async def _try_natpmp(self) -> Optional[tuple[str, int]]:
        """Thực hiện NAT-PMP hoàn toàn thủ công qua UDP."""
        gateway = await asyncio.get_event_loop().run_in_executor(
            None, self._get_default_gateway
        )
        if not gateway:
            logger.debug("NAT: NAT-PMP — không xác định được gateway")
            return None

        self._gateway = gateway

        # Bước 1: Lấy external IP
        ext_ip = await self._natpmp_get_external_ip(gateway)
        if not ext_ip:
            logger.debug("NAT: NAT-PMP — không lấy được external IP từ gateway")
            return None

        # Bước 2: Map port TCP
        ext_port = await self._natpmp_map_port(gateway, self.internal_port)
        if not ext_port:
            logger.debug("NAT: NAT-PMP — không map được port")
            return None

        return ext_ip, ext_port

    async def _natpmp_get_external_ip(self, gateway: str) -> Optional[str]:
        """
        NAT-PMP op=0: lấy external IP của router.
        Request:  version(1B)=0 + op(1B)=0
        Response: version(1B) + op(1B)=128 + result(2B) + epoch(4B) + ip(4B)
        """
        request = struct.pack("!BB", 0, 0)
        data = await _udp_send_recv(gateway, NATPMP_PORT, request, timeout=3, retries=3)
        if data is None or len(data) < 12:
            return None
        try:
            version, op, result_code, epoch = struct.unpack("!BBHI", data[:8])
            if result_code != 0 or op != 128:
                return None
            ip = socket.inet_ntoa(data[8:12])
            return ip
        except Exception:
            return None

    async def _natpmp_map_port(
        self, gateway: str, internal_port: int, lifetime: int = NATPMP_LIFETIME
    ) -> Optional[int]:
        """
        NAT-PMP op=2: map TCP port.
        Request:
          version(1B)=0 + op(1B)=2 + reserved(2B)=0
          + internal_port(2B) + suggested_external_port(2B)
          + lifetime(4B)
        Response (16 bytes):
          version(1B) + op(1B)=130 + result(2B)
          + epoch(4B) + internal_port(2B) + external_port(2B) + lifetime(4B)
        """
        request = struct.pack(
            "!BBHHHI",
            0,             # version
            2,             # op: map TCP
            0,             # reserved
            internal_port, # internal port
            internal_port, # suggested external port (same)
            lifetime,      # lifetime seconds (0 = delete)
        )
        data = await _udp_send_recv(gateway, NATPMP_PORT, request, timeout=3, retries=3)
        if data is None or len(data) < 16:
            return None
        try:
            version, op, result_code, epoch, int_port, ext_port, got_lifetime = \
                struct.unpack("!BBHIHHI", data[:16])
            if result_code != 0 or op != 130:
                return None
            return ext_port
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_default_gateway() -> Optional[str]:
        """
        Xác định IP của default gateway. Hỗ trợ Linux, macOS, Windows.
        Fallback: giả định gateway là .1 trên cùng subnet.
        """
        # Thử đọc từ OS routing table
        try:
            system = sys.platform
            if system.startswith("linux"):
                # Đọc /proc/net/route
                with open("/proc/net/route") as f:
                    for line in f.readlines()[1:]:
                        parts = line.split()
                        if parts[1] == "00000000":  # destination = 0.0.0.0 (default)
                            gw_hex = parts[2]
                            gw_int = int(gw_hex, 16)
                            # Little-endian
                            gw_ip = socket.inet_ntoa(
                                struct.pack("<I", gw_int)
                            )
                            if gw_ip != "0.0.0.0":
                                return gw_ip

            elif system == "darwin":
                result = subprocess.run(
                    ["route", "-n", "get", "default"],
                    capture_output=True, text=True, timeout=3
                )
                for line in result.stdout.splitlines():
                    if "gateway:" in line:
                        return line.split("gateway:")[1].strip()

            elif system == "win32":
                result = subprocess.run(
                    ["ipconfig"],
                    capture_output=True, text=True, timeout=3
                )
                for line in result.stdout.splitlines():
                    if "Default Gateway" in line or "Gateway" in line:
                        parts = line.split(":")
                        if len(parts) > 1:
                            gw = parts[-1].strip()
                            if gw and gw != "":
                                return gw

        except Exception as e:
            logger.debug(f"NAT: gateway detection via OS failed: {e}")

        # Fallback heuristic: .1 trên cùng /24 subnet
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


# ─────────────────────────────────────────────────────────────────────────────
# UDP helper
# ─────────────────────────────────────────────────────────────────────────────

async def _udp_send_recv(
    host: str, port: int, data: bytes,
    timeout: float = 3.0, retries: int = 3
) -> Optional[bytes]:
    """
    Gửi UDP datagram và chờ response.
    NAT-PMP spec yêu cầu retry với timeout tăng dần: 250ms, 500ms, 1000ms, ...
    """
    loop = asyncio.get_event_loop()

    for attempt in range(retries):
        wait = timeout * (2 ** attempt) / (2 ** (retries - 1))  # bắt đầu nhỏ, tăng dần
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
            return await asyncio.wait_for(asyncio.shield(future), timeout=wait)

        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.debug(f"NAT: UDP send_recv error (attempt {attempt+1}): {e}")
            return None
        finally:
            if transport:
                transport.close()

    return None
