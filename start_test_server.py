#!/usr/bin/env python3
"""Start the A2A Registry server for testing."""
import os
import sys

os.environ["ADMIN_PASSWORD"] = "admin123"  # nosec — test-only, never production

# Change to project dir
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from simple_a2a_registry.cli import main
sys.argv = [
    "a2a-registry",
    "--port", "8321",
    "--data-dir", "data",
    "--auth-enabled",
    "--bootstrap-secret", "test123",  # nosec — test-only secret
]
main()