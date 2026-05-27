"""Test migration chain on SQLite and offline SQL mode."""
import sys
import tempfile
import os

sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

PASS = 0
FAIL = 0


def run_test(name: str, cmd: str, expect_code: int = 0):
    global PASS, FAIL
    print(f"\n=== Test: {name} ===")
    print(f"  $ {cmd}")
    r = os.system(cmd + " 2>&1")
    ok = os.WEXITSTATUS(r) if os.WIFEXITED(r) else -1
    if ok == expect_code:
        print(f"  >> PASS (exit={ok})")
        PASS += 1
    else:
        print(f"  >> FAIL (exit={ok}, expected={expect_code})")
        FAIL += 1


# Test A: Fresh SQLite DB
tmp = tempfile.mktemp(suffix=".db")
run_test(
    "A1: Fresh SQLite - alembic upgrade head",
    f"alembic upgrade head 2>&1",
)
os.unlink(tmp)

# Test B: Fresh SQLite with explicit URL
tmp2 = tempfile.mktemp(suffix=".db")
run_test(
    "B1: Fresh SQLite - explicit URL upgrade head",
    f"alembic -x db=sqlite:///{tmp2} upgrade head",
)

# Test C: Re-run idempotency
run_test(
    "C1: SQLite idempotency - re-run upgrade head",
    f"alembic -x db=sqlite:///{tmp2} upgrade head",
)
os.unlink(tmp2)

# Test D: Offline SQL generation
run_test(
    "D1: Offline SQL generation (mysql target)",
    "alembic upgrade head --sql 2>&1",
)

print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)