# Official Vapi CLI

`vapic` is a harness around the official Vapi CLI. Use `vapic help` for the
capability contract and `vapi --help` for the native command surface.

Official homes:

- CLI docs: https://docs.vapi.ai/cli
- CLI site: https://vapi.ai/cli
- CLI source: https://github.com/VapiAI/cli

Install or check the official binary:

```sh
vapic bootstrap
vapic bootstrap --yes
```

Native commands stay native:

```sh
vapic --connection staging assistant list
vapic --connection staging call list
vapic --connection production logs
```

For native account login and account switching, run `vapi` directly. vapic uses
capability connections and injects `VAPI_API_KEY` for each invocation.
