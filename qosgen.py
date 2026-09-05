#!/usr/bin/env python3
"""qosgen — traffic generators for exercising router QoS policies.

This file is only the front door. Each traffic pipeline lives in its own
module and owns all of its own code — constants, socket setup, worker
threads, and CLI options:

    qos.py     — marked voice / signaling / noise UDP streams (EF, AF31/CS3, BE)
    stream.py  — one unmarked UDP or TCP stream from an explicit source socket

Adding a pipeline means writing one module and adding one line below.
"""

import click

from qos import qos_pipeline
from stream import stream_pipeline


@click.group()
def cli():
    """Generate traffic to exercise router QoS policies."""


cli.add_command(qos_pipeline)
cli.add_command(stream_pipeline)


if __name__ == "__main__":
    cli()
