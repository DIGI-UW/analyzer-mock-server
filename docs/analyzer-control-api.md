# Dynamic analyzer control API

## Delete an analyzer instance

`DELETE /analyzers/{name}` accepts removal of an existing mock network identity:

```http
HTTP/1.0 202 Accepted
Content-Type: application/json

{"removalScheduled": true, "name": "example-analyzer"}
```

The response is sent before asynchronous teardown starts because deleting the
network can disconnect the HTTP caller. `removalScheduled` acknowledges the
request, not successful completion. No `removed` field is returned on this
path. Before relying on removal, check `GET /analyzers` until the instance is
absent. Use a bounded wait and report a timeout; removal can fail and its
failure is logged by the mock.

An unknown instance returns HTTP 404 with
`{"removed": false, "error": "Analyzer '<name>' not found"}`. Missing names return
HTTP 400; an unavailable Docker manager returns HTTP 500.

This is test-infrastructure control only. It neither deletes a durable Bridge
connection nor modifies OpenELIS analyzer records.

`test_analyzers_api.py` exercises accepted removal, query-string handling,
response-before-teardown ordering, and the unknown-instance response.
