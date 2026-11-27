# Twilio number to LiveKit

This guide connects a phone number bought from Twilio to an agent's lkagent line: the number reaches LiveKit through a Twilio Elastic SIP trunk, LiveKit's inbound trunk and dispatch rule hand each call to the line's agent name, an outbound trunk carries the line's own calls out, and the line's settings name the number and its rights. Every value below is a placeholder; put your own in its place. lkagent reads trunks and dispatch rules and never creates or changes them.

Twilio's and LiveKit's own pages are the reference for every screen and command named here: [LiveKit: Twilio as a SIP provider](https://docs.livekit.io/telephony/start/providers/twilio/) and [Twilio Elastic SIP Trunking](https://www.twilio.com/docs/sip-trunking).

## 1. Buy the number

In the Twilio console, buy a number with voice capability (Phone Numbers, Buy a number). Note it in E.164 form, `+<country code><number>`; this guide calls it `+1XXXXXXXXXX`.

## 2. Find the project's SIP URI

LiveKit Cloud shows the project's SIP URI on its **Project settings** page, in the form `sip:<subdomain>.sip.livekit.cloud`. LiveKit's server API does not return it, so neither `lkagent doctor` nor `lkagent inventory` can show it: copy it from the project settings. A self-hosted server's SIP URI is the address its own SIP service listens on.

## 3. The Twilio Elastic SIP trunk

Create an Elastic SIP trunk in the Twilio console (Elastic SIP Trunking, Trunks).

- **Origination**: add an origination URI of the SIP URI from step 2 with `;transport=tcp` appended, `sip:<subdomain>.sip.livekit.cloud;transport=tcp`, weight 1, priority 1. This is how a call to the number reaches LiveKit.
- **Termination**: give the trunk a termination SIP URI, `<trunk-name>.pstn.twilio.com`, and under Authentication attach a credential list (Voice, Credential lists) holding a username and password of your choosing. This is how the line's own calls leave through Twilio.
- **Numbers**: attach `+1XXXXXXXXXX` to the trunk.

## 4. LiveKit's inbound trunk and dispatch rule

In LiveKit (the Cloud dashboard's Telephony section, or the `lk sip` commands), create an inbound trunk that accepts calls to the number:

```json
{"trunk": {"name": "<trunk name>", "numbers": ["+1XXXXXXXXXX"]}}
```

Then a dispatch rule that sends each call into a room of its own and dispatches the line's agent to it. The agent name is the one `lkagent doctor` reports under `checks.connection.agent_name`: the `agent_name` setting, else `lkagent-<connection id>`.

```json
{"dispatch_rule": {
  "name": "<rule name>",
  "trunk_ids": ["ST_<inbound trunk id>"],
  "rule": {"dispatchRuleIndividual": {"roomPrefix": "call-"}},
  "room_config": {"agents": [{"agent_name": "lkagent-<connection id>"}]}}}
```

A rule routes to the line only when its `room_config.agents` names the line's agent name; a rule without it dispatches no agent of this line.

## 5. An outbound trunk for the line's own calls

Create an outbound trunk that dials through the Twilio termination URI with the credential list's username and password, and note its id, `ST_<outbound trunk id>`:

```json
{"trunk": {"name": "<trunk name>", "address": "<trunk-name>.pstn.twilio.com",
           "numbers": ["+1XXXXXXXXXX"],
           "auth_username": "<username>", "auth_password": "<password>"}}
```

## 6. The line's settings

Name the number, what the backend is told on its calls and what it may do, and the outbound trunk:

```sh
lkagent settings set numbers '{"+1XXXXXXXXXX": {"instructions": "<what calls on this number are for>", "allowed_capabilities": ["<capability>"], "read_only": false}}'
lkagent settings set outbound_trunk ST_<outbound trunk id>
```

Set `agent_name` too where the dispatch rule names another name than the default. `numbers` is keyed by the number in E.164. A call to a number with no entry in `numbers` is hung up at once and recorded as failed, and `lkagent call` dials only from a number listed there (`--from`). Settings are read when the worker starts, so restart `lkagent service run` after changing them. On a node, `capabilities node supervise lkagent` keeps the worker running where the node's gate places it; `lkagent service status` and `lkagent service logs` read it.

## 7. Check it

`lkagent doctor` lists under `checks.sip` the dispatch rules that route to the line and the numbers they route, and warns when none does. `lkagent inventory --network` lists the same rules and numbers beside the configured ones, marking a routed number the line has no entry for as `not configured`. Then call `+1XXXXXXXXXX` from a phone, and place a call with `lkagent call +1YYYYYYYYYY --from +1XXXXXXXXXX`.
