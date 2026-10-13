"""Posts one unsigned and one signed event to the webhook route of the gateway (spike Z2-01).

Runs inside a throwaway container on the instance network; prints the two HTTP statuses.
"""

import hashlib
import hmac
import json
import urllib.error
import urllib.request

body = json.dumps({"text": "do the thing", "event_type": "task"}).encode()
signature = "sha256=" + hmac.new(b"spike-webhook-secret", body, hashlib.sha256).hexdigest()
statuses = []
for headers in ({}, {"X-Hub-Signature-256": signature}):
    request = urllib.request.Request(
        "http://zs01-gw:8644/webhooks/task",
        data=body,
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        response = urllib.request.urlopen(request, timeout=10)  # noqa: S310 - fixed http URL
        statuses.append(str(response.status))
    except urllib.error.HTTPError as error:
        statuses.append(str(error.code))
print(" ".join(statuses))
