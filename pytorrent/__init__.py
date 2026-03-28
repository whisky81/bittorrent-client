import logging

logging.basicConfig(
    filename="pytorrent.log",
    filemode="w",
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logging.getLogger("werkzeug").setLevel(logging.ERROR)
