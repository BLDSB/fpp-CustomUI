#!/bin/bash
# Deactivate the "All" pixel overlay model so lights return to FPP control.
# Used as a "Run Script" item in FPP playlists (e.g. "All Off").
curl -s -X PUT http://localhost/api/overlays/model/All/state \
  -H "Content-Type: application/json" \
  -d '{"State": 0}'
