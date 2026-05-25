"""Performance benchmarks for the orchestration engine.

Each benchmark outputs numeric performance metrics (tasks/sec, avg latency, etc.)
and can be run independently:

    python3 -m pytest tests/benchmarks/ -v --tb=short --benchmark-quiet
"""

