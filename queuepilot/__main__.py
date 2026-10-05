import argparse
import logging
import signal
from threading import Event

import uvicorn

from queuepilot.api import create_app
from queuepilot.config import Settings
from queuepilot.store import Store
from queuepilot.worker import Worker


def main():
    parser = argparse.ArgumentParser(description="QueuePilot 本地可靠任务服务")
    parser.add_argument("mode", nargs="?", choices=("serve", "worker"), default="serve")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.mode == "serve":
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    else:
        stop = Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        Worker(Store(settings.db_path), settings).run(stop)


if __name__ == "__main__":
    main()
