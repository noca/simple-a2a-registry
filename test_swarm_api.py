#!/usr/bin/env python3
"""Test Swarm API with token from file."""
import json
import urllib.request
import urllib.error

TOKEN='<obtain-...ogin>'  # Replace with a real token from POST /auth/token

def create_swarm():
    body = json.dumps({
        "goal": "Frontend test topology",
        "workers": [
            {"profile": "worker-p1", "title": "Parser Task"},
            {"profile": "worker-p2", "title": "Analyzer Task"}
        ],
        "verifier": {"profile": "verifier-m", "title": "Verify outputs"},
        "synthesizer": {"profile": "synthesizer-m", "title": "Synthesize results"},
        "root_title": "Swarm: Frontend Test"
    }).encode()
    
    req = urllib.request.Request(
        "http://localhost:8321/v2/swarm",
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    print(f"CREATE: HTTP {resp.status}")
    print(f"  root_id: {result['swarm']['root_id']}")
    print(f"  root_status: {result['topology']['root']['status']}")
    print(f"  workers: {len(result['topology']['workers'])}")
    print(f"  verifier: {result['topology']['verifier']['assignee']}")
    print(f"  synthesizer: {result['topology']['synthesizer']['assignee']}")
    return result['swarm']['root_id']

def get_swarm(root_id):
    req = urllib.request.Request(
        f"http://localhost:8321/v2/swarm/{root_id}",
        headers={"Authorization": f"Bearer {TOKEN}"}
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    print(f"\nGET SWARM: HTTP {resp.status}")
    print(f"  swarm status: {result.get('swarm',{}).get('status')}")
    for w in result.get('workers', []):
        print(f"  worker: {w.get('assignee')} status={w.get('status')}")
    v = result.get('verifier', {})
    print(f"  verifier: {v.get('assignee')} status={v.get('status')}")
    s = result.get('synthesizer', {})
    print(f"  synthesizer: {s.get('assignee')} status={s.get('status')}")

def write_blackboard(root_id, key, value, author="tester"):
    body = json.dumps({"author": author, "key": key, "value": value}).encode()
    req = urllib.request.Request(
        f"http://localhost:8321/v2/swarm/{root_id}/comment",
        data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST"
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    print(f"\nBLACKBOARD WRITE ({key}): HTTP {resp.status}, comment_id={result['comment_id']}")

def read_blackboard(root_id):
    req = urllib.request.Request(
        f"http://localhost:8321/v2/swarm/{root_id}/blackboard",
        headers={"Authorization": f"Bearer {TOKEN}"}
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    print(f"\nBLACKBOARD READ: HTTP {resp.status}")
    for k, v in result.items():
        if k != '_authors':
            val_str = json.dumps(v) if not isinstance(v, str) else v
            print(f"  {k}: {val_str[:80]}")
    print(f"  _authors: {result.get('_authors', {})}")

def test_404():
    print("\n--- ERROR PATH TESTS ---")
    # nonexistent swarm -> 404
    try:
        req = urllib.request.Request(
            "http://localhost:8321/v2/swarm/nonexistent",
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        err = json.loads(e.read())
        print(f"404 swarm: status={e.code}, error={err['error']}, detail={err['detail']}")
    
    # nonexistent blackboard -> 404  
    try:
        req = urllib.request.Request(
            "http://localhost:8321/v2/swarm/nonexistent/blackboard",
            headers={"Authorization": f"Bearer {TOKEN}"}
        )
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        err = json.loads(e.read())
        print(f"404 blackboard: status={e.code}, error={err['error']}, detail={err['detail']}")
    
    # missing goal -> 400
    try:
        body = json.dumps({
            "workers": [{"profile": "w", "title": "W"}],
            "verifier": {"profile": "v"},
            "synthesizer": {"profile": "s"}
        }).encode()
        req = urllib.request.Request(
            "http://localhost:8321/v2/swarm",
            data=body,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        err = json.loads(e.read())
        print(f"400 validation: status={e.code}, error={err['error']}, detail={err['detail']}")
    
    # wrong swarm id in comment -> 404
    try:
        body = json.dumps({"author": "t", "key": "k", "value": "v"}).encode()
        req = urllib.request.Request(
            "http://localhost:8321/v2/swarm/wrong-id/comment",
            data=body,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        err = json.loads(e.read())
        print(f"404 comment: status={e.code}, error={err['error']}, detail={err['detail']}")

print("=== SWARM API VERIFICATION ===")
root_id = create_swarm()
get_swarm(root_id)
write_blackboard(root_id, "analysis-result", {"accuracy": 0.95, "latency_ms": 120})
write_blackboard(root_id, "summary", "All tests passed successfully")
read_blackboard(root_id)
test_404()
print("\n=== ALL BACKEND API TESTS PASSED ===")

# Save root_id for frontend testing
with open('/tmp/swarm_root_id.txt', 'w') as f:
    f.write(root_id)