"""送指令給 sixdDevHarness（檔案式）。用法：
  python client.py state
  python client.py profiles
  python client.py activate '{"flags": ["FORSESSION"]}'
  python client.py dots '{"dots": 56, "space": true}'
"""
import json
import os
import sys
import time
import uuid

sys.stdout.reconfigure(encoding="utf-8")

d = os.path.join(os.environ["TEMP"], "sixdDevHarness")
os.makedirs(d, exist_ok=True)
resp = os.path.join(d, "resp.json")
if os.path.exists(resp):
	os.remove(resp)
rid = uuid.uuid4().hex
req = {"id": rid, "cmd": sys.argv[1], "args": json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}}
tmp = os.path.join(d, "req.tmp")
with open(tmp, "w", encoding="utf-8") as f:
	json.dump(req, f)
os.replace(tmp, os.path.join(d, "req.json"))
for _ in range(100):
	time.sleep(0.1)
	if os.path.exists(resp):
		try:
			with open(resp, encoding="utf-8") as f:
				r = json.load(f)
		except (PermissionError, json.JSONDecodeError):
			continue   # harness 還在寫

		if r.get("id") == rid:
			print(json.dumps(r, ensure_ascii=False, indent=1))
			sys.exit(0)
print("timeout: harness 沒有回應")
sys.exit(1)
