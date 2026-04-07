from urllib.parse import urlparse
from .core.trackers import UDPTracker, HTTPTracker

class TrackerFactory:
    def __new__(cls, tracker_addr, torrent_info):
        tracker_types = {
            'udp': UDPTracker,
            'http': HTTPTracker,
            'https': HTTPTracker
        }
        scheme = urlparse(tracker_addr).scheme
        if scheme in tracker_types:
            return tracker_types[scheme](tracker_addr, torrent_info)
        return None