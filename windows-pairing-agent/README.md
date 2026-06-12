# Windows Pairing Agent

Windows system tray app that keeps this PC registered with the relay's IP
pairing endpoint. It is the Windows counterpart of the home server's
`relay_pairing_worker`: every interval it POSTs the machine's hostname, local
IPs, and advertised ports to `POST /paired-devices/{device_id}` using the
relay's `RELAY_IP_PAIRING_TOKEN`.

## Tray Icon States

| Color | Meaning |
| --- | --- |
| Green | Running, last pairing update succeeded |
| Red | Last pairing update failed (hover for the error) |
| Gray | Updates paused |
| Yellow | Not configured (relay URL or pairing token missing) |

Right-click the icon for: status line, **Send update now**, **Pause updates**,
**Settings...**, **Start with Windows**, and **Exit**. Double-click opens
Settings.

## Build And Run

Requires the .NET 9 SDK with Windows desktop workload.

```sh
cd windows-pairing-agent
dotnet build
dotnet run
```

For a standalone single-file exe:

```sh
dotnet publish -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true
```

The exe lands in `bin/Release/net9.0-windows/win-x64/publish/`. Copy it
anywhere; **Start with Windows** registers that exe path under
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

## Configuration

Stored at `%APPDATA%\RelayPairingAgent\config.json` and editable from the
Settings dialog:

```json
{
  "relay_url": "https://relay.dracon.au",
  "pairing_token": "replace-with-relay-ip-pairing-token",
  "device_id": "my-windows-pc",
  "name": "My Windows PC",
  "type": "windows-pc",
  "ports": "rdp:3389",
  "notes": "",
  "local_ips": "",
  "interval_seconds": 300
}
```

- `pairing_token` must match the relay's `RELAY_IP_PAIRING_TOKEN`.
- `local_ips` is normally left empty; the agent discovers non-loopback,
  non-link-local addresses itself. Set it (comma separated) to override.
- `interval_seconds` is clamped to a minimum of 60, matching the home server.
- The relay infers `external_ip` from the HTTPS request source address.

On first run with no token configured, the icon shows yellow and a balloon tip
points at the Settings dialog.
