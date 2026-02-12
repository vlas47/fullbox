# Fullbox Agent (Windows)

MVP local agent: Windows service + tray app.

## Requirements
- Windows 11
- .NET 8 SDK (for build)

## Build
```powershell
.\build.ps1
```

## Install service (example)
```powershell
sc.exe create "FullboxAgent" binPath= "C:\path\to\fullbox_agent\out\service\Fullbox.Agent.Service.exe"
sc.exe start "FullboxAgent"
```

## Tray app
```powershell
C:\path\to\fullbox_agent\out\tray\Fullbox.Agent.Tray.exe
```

## Config
Config file is stored at:
```
C:\ProgramData\FullboxAgent\config.json
```

Minimal fields:
```json
{
  "agentId": "pc-001",
  "name": "Warehouse-PC-01",
  "host": "WMS-PC01",
  "baseUrl": "https://kondelyabr.ru",
  "token": "CHANGE_ME",
  "printToken": "PRINT_AGENT_TOKEN",
  "printAgentName": "FullboxAgent",
  "pingIntervalSec": 10,
  "pollIntervalSec": 5,
  "printPollIntervalSec": 2,
  "com": {
    "enabled": true,
    "portName": "COM3",
    "baudRate": 9600,
    "eol": "CrLf",
    "idleMs": 200
  }
}
```

## Notes
- Uses HTTP polling (`/agent/ping/`, `/agent/commands/`, `/agent/events/`).
- WebSocket can be added later without breaking the command model.
