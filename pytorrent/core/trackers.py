import asyncio
from random import randint
from typing import Any
from socket import gaierror
from ipaddress import IPv4Address
from struct import pack, unpack
from urllib.parse import urlparse, urlencode, quote_from_bytes
from http.client import HTTPConnection, HTTPSConnection
from pytorrent.core.constants import ENCODING, PEER_ID_PREFIX,UDP_PROTOCOL_ID
from pytorrent.core import bencode_wrapper

PEER_ID = PEER_ID_PREFIX + "1qazx2ws3e4r".encode()

class Tracker:
    def __init__(self, tracker_addr, torrent_info) -> None:
        tracker_addr = urlparse(tracker_addr)
        # ParseResult(
        # scheme='udp',
        # netloc='tracker.opentrackr.org:1337',
        # path='/announce',
        # params='',
        # query='',
        # fragment=''
        # )
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

    def gen_udp_announce_req(
        self, connection_id: int = 0, transaction_id: int = 0
    ) -> dict:

        announce_params = {
            "connection_id": connection_id,  # 64 bit integer
            "action": 1,  # 32 bit integer; announce
            "transaction_id": transaction_id,  # 32 bit integer
            "info_hash": self.torrent_info["info_hash"],  # 20 byte string
            "peer_id": PEER_ID,  # 20 byte string; Should be the same and only change if the client restarts
            "downloaded": 0,  # 64 bit integer
            "left": self.torrent_info["size"],  # 64 bit integer
            "uploaded": 0,  # 64 bit integer
            "event": 2,  # 32 bit integer; started
            "ip_address": 0,  # 32 bit integer; 0 is default
            "key": self.key,  # 32 bit integer
            "num_want": 200,  # 32 bit integer; -1 is default
            "port": 6887,  # 16 bit integer; should be between 6881 & 6889
        }
        return announce_params

    def gen_http_announce_req(self):

        return {
            "info_hash": quote_from_bytes(self.torrent_info["info_hash"]),
            "peer_id": quote_from_bytes(PEER_ID),
            "port": 6887,
            "uploaded": 0,
            "downloaded": 0,
            "left": self.torrent_info["size"],
            "compact": 1,
            "event": "started",
            "num_want": 200,
        }

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
        connect_response = {
            "action": action,
            "transaction_id": transaction_id,
            "connection_id": connection_id,
        }
        return connect_response

    def parse_udp_announce_res(self, response):
        """
        Byte 0-3: action = 1 (announce)
        Byte 4-7: transaction_id
        Byte 8-11: interval (giây, chờ trước khi announce lại)
        Byte 12-15: leechers (số người đang tải)
        Byte 16-19: seeders (số người đã có đủ)
        Byte 20...: Danh sách peer (mỗi peer 6 byte: IP 4 byte + port 2 byte)
        """
        if len(response) < 20:
            raise ValueError("fail: parse udp announce response (< 20 bytes)")
        response, raw_peers = response[:20], response[20:]
        action, transaction_id, interval, leechers, seeders = unpack(">IIIII", response)
        peers = []
        for i in range(0, len(raw_peers), 6):
            ip, port = unpack(">IH", raw_peers[i : i + 6])
            ip = IPv4Address(ip).compressed
            peers.append((ip, port))

        self.peers = peers
        announce_response = {
            "action": action,
            "transaction_id": transaction_id,
            "interval": interval,
            "leechers": leechers,
            "seeders": seeders,
            "ip_addresses": peers,
        }
        return announce_response


class UDPTracker(Tracker):
    def __init__(self, tracker_addr, torrent_info) -> None:
        super().__init__(tracker_addr, torrent_info)

    def __repr__(self) -> str:
        return f"UDPTracker({self.hostname}:{self.port})"

    class UDPProtocolFactory(asyncio.DatagramProtocol):
        def __init__(self, parent_obj) -> None:
            super().__init__()
            self.transport = None
            self.address = (parent_obj.hostname, parent_obj.port)
            self.parent_obj = parent_obj

        def connection_made(self, transport: asyncio.DatagramTransport) -> None:
            self.transport = transport
            connect_req_params = self.parent_obj.gen_udp_connect_req()
            connect_req = UDPTracker.serialize_connect("bytes", connect_req_params)
            print(f"{self.parent_obj} sending connect request to {self.address}")
            self.transport.sendto(connect_req)  # type: ignore

        def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
            action = unpack(">I", data[:4])[0]
            if action == 0:
                print(f"{self.parent_obj} Received connect response from {addr}")
                print(f"{self.parent_obj} Connect response: {data[:16]}")
                self.parent_obj.connect_response = (
                    self.parent_obj.parse_udp_connect_res(data)
                )
                self.parent_obj.active = True

                connection_id = self.parent_obj.connect_response["connection_id"]
                transaction_id = self.parent_obj.connect_response["transaction_id"]
                announce_req_params = self.parent_obj.gen_udp_announce_req(
                    connection_id, transaction_id
                )
                announce_req = self.parent_obj.serialize_announce_req(
                    "bytes", announce_req_params
                )

                print(f"{self.parent_obj} Sending announce request to {self.address}")
                self.transport.sendto(announce_req)  # type: ignore
            elif action == 1:
                print(f"{self.parent_obj} Received announce response from {addr}")
                print(f"{self.parent_obj} Announce response: {data[:32]}")
                self.parent_obj.announce_response = (
                    self.parent_obj.parse_udp_announce_res(data)
                )
            else:
                raise ValueError("got unsupport action")

    async def get_peers(self, timeout: int = 3):
        self.peers = []
        try:
            loop = asyncio.get_running_loop()
            await loop.create_datagram_endpoint(
                lambda: self.UDPProtocolFactory(self),
                remote_addr=(self.hostname, self.port),  # type: ignore
            )
            await asyncio.sleep(timeout)
        except gaierror as e:
            print(f"failed to get addr info from {self}")
            print(e)
            self.active = False 
        except Exception as e:
            self.peers = []
            self.active = False
            print(e)

        return self.peers


class HTTPTracker(Tracker):
    def __init__(self, tracker_addr: str, torrent_info):
        # self.active = False
        super().__init__(tracker_addr, torrent_info)

        # Set http path parameter manually because the BaseTracker doesn't
        self.path = urlparse(tracker_addr).path

    def __repr__(self):
        return f"HTTPTracker({self.hostname}:{self.port})"

    async def get_peers(self):
        self.peers = []

        announce_req_raw = self.gen_http_announce_req()
        announce_req = HTTPTracker.serialize_connect("url", announce_req_raw)
        # print("HTTP ANNOUNCE REQUEST")
        # print("\t", announce_req)
        # print("\n"*3)
        def connect_to_tracker(payload: str):
            http_conn_factory = (
                HTTPSConnection if self.scheme == "https" else HTTPConnection
            )
            connection = http_conn_factory(host=self.hostname, port=self.port)  # type: ignore
            final_query = f"{self.path}?{payload}"

            connection.request("GET", final_query)
            response = connection.getresponse()

            if response.status == 200:
                self.active = True
                self.announce_response = bencode_wrapper.bdecode(response.read())
                peer_list = self.announce_response["peers"]  # type: ignore
                print("\nHERE\n")
                print(peer_list)
                print("\nHERE\n")
                if isinstance(peer_list, str):
                    for i in range(0, len(peer_list), 6):
                        ip, port = unpack(">IH", peer_list[i : i + 6].encode(ENCODING))
                        ip = IPv4Address(ip).compressed
                        self.peers.append((ip, port))
                elif isinstance(peer_list, list):
                    for peer in peer_list:
                        self.peers.append((peer["ip"], peer["port"]))
                else:
                    raise RuntimeError(f"Unknown peers data: {peer_list}")
            else:
                print(
                    f"Error fetching peers from {self}: Error Code: {response.status} - {response.reason}: {response.read()}"
                )
                self.active = False 
                return []

        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, connect_to_tracker, announce_req), timeout=3 # type: ignore
            ) 
            return self.peers

        except Exception as e:
            print(
                f"Error occured while connecting to {self}: {e} [resp:{self.announce_response}]"
            )
            return []
