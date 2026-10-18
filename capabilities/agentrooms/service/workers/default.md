---
enabled: true
description: Answers every sender the room's wake grant admits through a turn on agentrooms-claude; a project replaces it whole by saving a worker named default.
profile: agentrooms-claude
history: 30
accepts_from:
  - "*"
invites_from:
  - "*"
wakes_on:
  - mention
  - direct
  - marked
limits:
  debounce_seconds: 0
  turn_timeout: 900
  window_seconds: 3600
  turns_per_agent: 120
  turns_per_sender: 30
  turns_per_room: 30
  quiet_seconds: 900
---
