import logging

logging.basicConfig(
    filename="pytorrent.log",
    filemode="w",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
