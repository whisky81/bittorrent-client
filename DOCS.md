# Kết quả của swarmManager.getAllStats() 
- dùng để tham khảo
```json
[
  {
    "sessionId": 0, 
    "infoHash": "731b5115366af8c1d56c87effb3889f5891402db",
    "mode": "rarest-first",
    "totalPieces": 700,
    "downloadedPieces": 700,
    "remainingPieces": 0,
    "progress": "100.0",
    "dlSpeedBps": 29148748,
    "ulSpeedBps": 0,
    "dlSpeedKBps": "28465.6",
    "ulSpeedKBps": "0.0",
    "downloadedMB": "1398.99",
    "uploadedMB": "0.00",
    "connectedPeers": 1,
    "peers": [
      {
        "peerId": "c768a374",
        "ip": "127.0.0.1",
        "port": 6881,
        "downloaded": 1466949500,
        "uploaded": 0,
        "dlSpeed": 29148748, // bytes per second
        "ulSpeed": 0,
        "peerChoking": false,
        "amChoking": false,
        "pending": null
      }
    ],
    "workerPool": [
      {
        "id": 0,
        "busy": false,
        "queueLength": 0
      },
      {
        "id": 1,
        "busy": false,
        "queueLength": 0
      },
      {
        "id": 2,
        "busy": false,
        "queueLength": 0
      },
      {
        "id": 3,
        "busy": false,
        "queueLength": 0
      }
    ]
  }
]
```