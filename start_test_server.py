#!/usr/bin/env python3
"""Start the A2A Registry server for testing."""
import os
import sys

os.environ["ADMIN_PASSWORD"] = "admin123"  # nosec — test-only, never production

# Change to project dir
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from simple_a2a_registry.config import load_config
from simple_a2a_registry.server import run_server

# Load config (auto-reads ~/.simple-a2a-registry/config.yaml)
cfg = load_config()

run_server(
    host="0.0.0.0",
    port=8321,
    data_dir="data",
    config=cfg,
    auth_enabled=True,
    bootstrap_secret="test123",  # nosec — test-only secret
)