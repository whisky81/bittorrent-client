import asyncio
from random import randint
from typing import Any
from socket import gaierror
from ipaddress import IPv4Address
from struct import pack, unpack
from urllib.parse import urlparse, urlencode, quote_from_bytes
from http.client import HTTPConnection, HTTPSConnection
from .constants import ENCODING, UDP_PROTOCOL_ID
from . import bencode_wrapper
import logging

logger = logging.getLogger(__name__)

DEFAULT_PORT = 6887


class Tracker:
    def __init__(self, tracker_addr, torrent_info) -> None:
        tracker_addr = urlparse(tracker_addr)
        self.scheme = tracker_addr.scheme
        self.hostname = tracker_addr.hostname
        self.port = tracker_addr.port
        self.key = randint(10000, 99999)
        self.active = False
        self.torrent_info = torrent_info
        self.peers = []
        self.connect_response = {}
        self.announce_response = {}

    @staticmethod
    def serialize_connect(format, connect_params: dict):
        if format == "bytes":
            return pack(
                ">QII",
                connect_params["connection_id"],
                connect_params["action"],
                connect_params["transaction_id"],
            )
        return urlencode(connect_params)

    def gen_udp_connect_req(self):
        connection_id = UDP_PROTOCOL_ID
        action = 0
        transaction_id = randint(1, 2**16 - 1)
        return {
            "connection_id": connection_id,
            "action": action,
            "transaction_id": transaction_id,
        }

    def gen_udp_announce_req(self, connection_id: int = 0, transaction_id: int = 0) -> dict:
        event_map = {"none": 0, "completed": 1, "started": 2, "stopped": 3}
        event_val = event_map.get(self.torrent_info.get("event", "none"), 0)
        # Ưu tiên external_port (sau khi NAT mapping thành công) để tracker
        # quảng bá đúng địa chỉ mà peers bên ngoài có thể kết nối được.
        listen_port = self.torrent_info.get("external_port") \
                      or self.torrent_info.get("port", DEFAULT_PORT)
        announce_params = {
            "connection_id": connection_id,
            "action": 1,
            "transaction_id": transaction_id,
            "info_hash": self.torrent_info["info_hash"],
            "peer_id": self.torrent_info["peer_id"],
            "downloaded": self.torrent_info["downloaded"],
            "left": self.torrent_info["size"] - self.torrent_info["downloaded"],
            "uploaded": self.torrent_info["uploaded"],
            "event": event_val,
            "ip_address": 0,
            "key": self.key,
            "num_want": 200,
            "port": listen_port,
        }
        return announce_params

    def gen_http_announce_req(self):
        event_str = self.torrent_info.get("event", "started")
        # Ưu tiên external_port nếu NAT traversal thành công
        listen_port = self.torrent_info.get("external_port") \
                      or self.torrent_info.get("port", DEFAULT_PORT)
        req = {
            "info_hash": quote_from_bytes(self.torrent_info["info_hash"]),
            "peer_id": quote_from_bytes(self.torrent_info["peer_id"]),
            "port": listen_port,
            "uploaded": self.torrent_info["uploaded"],
            "downloaded": self.torrent_info["downloaded"],
            "left": self.torrent_info["size"] - self.torrent_info["downloaded"],
            "compact": 1,
            "num_want": 200,
        }
        if event_str and event_str != "none":
            req["event"] = event_str
        return req

    def serialize_announce_req(self, format, announce_params):
        if format == "bytes":
            return pack(
                ">QII20s20sQQQIIIiH",
                announce_params["connection_id"],
                announce_params["action"],
                announce_params["transaction_id"],
                announce_params["info_hash"],
                announce_params["peer_id"],
                announce_params["downloaded"],
                announce_params["left"],
                announce_params["uploaded"],
                announce_params["event"],
                announce_params["ip_address"],
                announce_params["key"],
                announce_params["num_want"],
                announce_params["port"],
            )
        return urlencode(announce_params)

    def parse_udp_connect_res(self, response: bytes):
        action, transaction_id, connection_id = unpack(">IIQ", response)
        return {
            "action": action,
            "transaction_id": transaction_id,
            "connection_id": connection_id,
        }

    def parse_udp_announce_res(self, response):
        if len(response) < 20:
            # BUG FIX: Không raise — hàm được gọi từ asyncio datagram_received callback.
            # Exception ở đây sẽ không được catch và sẽ crash vào asyncio event loop
            # tạo ra "Exception in callback _SelectorDatagramTransport._read_ready()" trong log.
            logger.warning(
                f"UDP tracker: announce response quá ngắn ({len(response)}B) "
                "— có thể là error packet, bỏ qua."
            )
            return {}
        response, raw_peers = response[:20], response[20:]
        action, transaction_id, interval, leechers, seeders = unpack(">IIIII", response)
        peers = []
        for i in range(0, len(raw_peers), 6):
            ip, port = unpack(">IH", raw_peers[i: i + 6])
            ip = IPv4Address(ip).compressed
            peers.append((ip, port))
        self.peers = peers
        return {
            "action": action,
            "transaction_id": transaction_id,
            "interval": interval,
            "leechers": leechers,
            "seeders": seeders,
            "ip_addresses": peers,
        }


class UDPTracker(Tracker):
    def __init__(self, tracker_addr, torrent_info) -> None:
        super().__init__(tracker_addr, torrent_info)
        self.transport = None

    def __repr__(self) -> str:
        return f"UDPTracker({self.hostname}:{self.port})"

    class UDPProtocolFactory(asyncio.DatagramProtocol):
        def __init__(self, parent_obj) -> None:
            super().__init__()
            self.parent_obj = parent_obj

        def connection_made(self, transport: asyncio.DatagramTransport) -> None:
            self.parent_obj.transport = transport

        def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
            action = unpack(">I", data[:4])[0]
            if action == 0:
                self.parent_obj.connect_response = self.parent_obj.parse_udp_connect_res(data)
                self.parent_obj.active = True
                if hasattr(self.parent_obj, "connect_future") and not self.parent_obj.connect_future.done():
                    self.parent_obj.connect_future.set_result(True)
            elif action == 1:
                parsed = self.parent_obj.parse_udp_announce_res(data)
                # parse_udp_announce_res trả về {} khi response lỗi — không set future
                if parsed:
                    self.parent_obj.announce_response = parsed
                    if hasattr(self.parent_obj, "announce_future") and not self.parent_obj.announce_future.done():
                        self.parent_obj.announce_future.set_result(True)
            else:
                logger.warning(f"Unsupported action {action} received")

    async def get_peers(self, timeout: int = 3):
        self.peers = []
        try:
            loop = asyncio.get_running_loop()
            self.connect_future = loop.create_future()
            self.announce_future = loop.create_future()

            await loop.create_datagram_endpoint(
                lambda: self.UDPProtocolFactory(self),
                remote_addr=(self.hostname, self.port),
            )

            connected = False
            for k in range(9):
                connect_req_params = self.gen_udp_connect_req()
                connect_req = UDPTracker.serialize_connect("bytes", connect_req_params)
                if self.transport:
                    self.transport.sendto(connect_req)
                try:
                    await asyncio.wait_for(asyncio.shield(self.connect_future), timeout=15 * (2 ** k))
                    connected = True
                    break
                except asyncio.TimeoutError:
                    continue

            if not connected:
                self.active = False
                return []

            announced = False
            for k in range(9):
                connection_id = self.connect_response["connection_id"]
                transaction_id = self.connect_response["transaction_id"]
                announce_req_params = self.gen_udp_announce_req(connection_id, transaction_id)
                announce_req = self.serialize_announce_req("bytes", announce_req_params)
                if self.transport:
                    self.transport.sendto(announce_req)
                try:
                    await asyncio.wait_for(asyncio.shield(self.announce_future), timeout=15 * (2 ** k))
                    announced = True
                    break
                except asyncio.TimeoutError:
                    continue

            if not announced:
                self.active = False
                return []

        except gaierror as e:
            logger.error(f"Failed to get addr info from {self}: {e}")
            self.active = False
        except Exception as e:
            self.peers = []
            self.active = False
            logger.error(f"Error getting peers from {self}: {e}")

        return self.peers


class HTTPTracker(Tracker):
    def __init__(self, tracker_addr: str, torrent_info):
        super().__init__(tracker_addr, torrent_info)
        self.path = urlparse(tracker_addr).path

    def __repr__(self):
        return f"HTTPTracker({self.hostname}:{self.port})"

    async def get_peers(self):
        self.peers = []
        announce_req_raw = self.gen_http_announce_req()
        announce_req = HTTPTracker.serialize_connect("url", announce_req_raw)

        def connect_to_tracker(payload: str):
            http_conn_factory = HTTPSConnection if self.scheme == "https" else HTTPConnection
            connection = http_conn_factory(host=self.hostname, port=self.port)
            final_query = f"{self.path}?{payload}"
            connection.request("GET", final_query)
            response = connection.getresponse()
            if response.status == 200:
                self.active = True
                self.announce_response = bencode_wrapper.bdecode(response.read())
                peer_list = self.announce_response["peers"]
                if isinstance(peer_list, (str, bytes)):
                    if isinstance(peer_list, str):
                        peer_list_bytes = peer_list.encode(ENCODING)
                    else:
                        peer_list_bytes = peer_list
                    for i in range(0, len(peer_list_bytes), 6):
                        ip, port = unpack(">IH", peer_list_bytes[i: i + 6])
                        ip = IPv4Address(ip).compressed
                        self.peers.append((ip, port))
                elif isinstance(peer_list, list):
                    for peer in peer_list:
                        self.peers.append((peer["ip"], peer["port"]))
                else:
                    raise RuntimeError(f"Unknown peers data: {peer_list}")
            else:
                logger.error(f"Error fetching peers from {self}: Error Code: {response.status} - {response.reason}")
                self.active = False
                return []

        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, connect_to_tracker, announce_req), timeout=3
            )
            return self.peers
        except Exception as e:
            logger.error(f"Error occured while connecting to {self}: {e}")
            return []
