#!/usr/bin/env python3
"""Explicit local entry point for the independent Intake worker."""

import argparse
import logging
import signal
import sys
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from herdr.intake.schema import initialize
from herdr.intake.store import EventStore, Scope
from herdr.intake.worker import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Instance tenant from trusted local configuration")
    parser.add_argument("--connection", required=True, help="Registered Intake connection")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    scope = Scope(args.tenant, args.connection)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(EventStore(initialize(), scope), stop)


if __name__ == "__main__":
    main()
