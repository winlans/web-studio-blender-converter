#!/usr/bin/env python3
"""Runpod Queue worker process entrypoint."""

import runpod

from handler import handler


runpod.serverless.start({"handler": handler})
